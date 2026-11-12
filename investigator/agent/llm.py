"""LLM providers: Gemini for real runs, a deterministic mock for tests and offline demos.

The mock is a real LangChain ``BaseChatModel``, so the graph code is identical
in both modes: it supports ``bind_tools`` and ``with_structured_output`` and
returns proper tool calls. Behind it is a hand-written, rule-based SRE policy
that reads the actual tool results in the conversation. It does not know the
scenario answers; it is roughly what a careful junior SRE's runbook would do.
Its eval score measures the tools and data, not language-model reasoning.
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.runnables import Runnable
from langchain_core.utils.function_calling import convert_to_openai_tool

from investigator.config import Settings

# --------------------------------------------------------------------------
# Factory
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class LLMInfo:
    provider: str
    model: str
    input_price_per_mtok: float
    output_price_per_mtok: float

    def cost(self, input_tokens: int, output_tokens: int) -> float:
        return round(
            input_tokens / 1e6 * self.input_price_per_mtok
            + output_tokens / 1e6 * self.output_price_per_mtok,
            6,
        )


def build_llm(settings: Settings, provider: str | None = None) -> tuple[BaseChatModel, LLMInfo]:
    provider = provider or settings.llm_provider
    if provider == "mock":
        return MockSREModel(), LLMInfo("mock", "rule-based-sre-v1", 0.0, 0.0)
    if provider == "gemini":
        if not settings.google_api_key:
            raise RuntimeError("LLM_PROVIDER=gemini needs GOOGLE_API_KEY (see .env.example)")
        from langchain_google_genai import ChatGoogleGenerativeAI

        llm = ChatGoogleGenerativeAI(
            model=settings.gemini_model,
            google_api_key=settings.google_api_key,
            temperature=settings.llm_temperature,
            max_retries=3,
            timeout=120,
        )
        return llm, LLMInfo(
            "gemini",
            settings.gemini_model,
            settings.llm_input_price_per_mtok,
            settings.llm_output_price_per_mtok,
        )
    raise ValueError(f"unknown LLM provider {provider!r} (use 'mock' or 'gemini')")


# --------------------------------------------------------------------------
# Mock model
# --------------------------------------------------------------------------


def _approx_tokens(text: str) -> int:
    return max(1, len(text) // 4)


class MockSREModel(BaseChatModel):
    """Deterministic chat model that drives the graph with a rule-based SRE policy."""

    @property
    def _llm_type(self) -> str:
        return "mock-sre"

    def bind_tools(self, tools: Sequence[Any], **kwargs: Any) -> Runnable[Any, BaseMessage]:
        return self.bind(tools=[convert_to_openai_tool(t) for t in tools], **kwargs)

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: Any = None,
        **kwargs: Any,
    ) -> ChatResult:
        names = [t["function"]["name"] for t in kwargs.get("tools", [])]
        transcript = "\n".join(str(m.content) for m in messages)
        if names == ["Triage"]:
            call = ("Triage", _mock_triage(transcript))
        elif names == ["HypothesisSet"]:
            call = ("HypothesisSet", _mock_hypotheses(transcript))
        elif names == ["Findings"]:
            call = ("Findings", SREPolicy(messages).conclude())
        elif "submit_findings" in names:
            call = SREPolicy(messages).next_action()
        else:
            raise ValueError(f"mock model got an unexpected tool set: {names}")
        name, args = call
        n_ai = sum(isinstance(m, AIMessage) for m in messages)
        message = AIMessage(
            content="",
            tool_calls=[
                {"name": name, "args": args, "id": f"call_{n_ai + 1}", "type": "tool_call"}
            ],
            usage_metadata={
                "input_tokens": _approx_tokens(transcript),
                "output_tokens": _approx_tokens(json.dumps(args)),
                "total_tokens": _approx_tokens(transcript) + _approx_tokens(json.dumps(args)),
            },
        )
        return ChatResult(generations=[ChatGeneration(message=message)])


def _field(text: str, label: str) -> str | None:
    m = re.search(rf"^{label}:\s*(.+)$", text, re.MULTILINE)
    value = m[1].strip() if m else None
    return None if value in (None, "None", "unknown") else value


def _mock_triage(text: str) -> dict[str, Any]:
    alert = (_field(text, "Alert") or "").lower()
    slow = any(w in alert for w in ("latency", "p95", "p99", "slow"))
    failing = any(w in alert for w in ("5xx", "error", "504", "500", "503"))
    symptom = (
        "latency_and_errors"
        if slow and failing
        else "latency"
        if slow
        else "errors"
        if failing
        else "other"
    )
    return {
        "service": _field(text, "Service hint") or "unknown",
        "endpoint": _field(text, "Endpoint hint"),
        "symptom": symptom,
        "severity": "sev1" if "sev1" in text else "sev2",
        "summary": _field(text, "Alert") or "",
    }


def _mock_hypotheses(text: str) -> dict[str, Any]:
    svc = _field(text, "Service") or "the service"
    return {
        "hypotheses": [
            {
                "id": "H1",
                "category": "bad_deploy",
                "statement": f"A recent deploy of {svc} introduced a regression.",
                "test_plan": "recent_deploys, then error rate by service.version",
            },
            {
                "id": "H2",
                "category": "dependency_timeout",
                "statement": f"A downstream dependency of {svc} is failing or slow.",
                "test_plan": "dependency_health: error rate and p95 per dependency vs baseline",
            },
            {
                "id": "H3",
                "category": "db_pool_exhaustion",
                "statement": "Requests are queueing for database connections.",
                "test_plan": "dependency_health pool wait vs query time; search_logs for 'pool'",
            },
            {
                "id": "H4",
                "category": "cache_outage",
                "statement": "The cache is failing and load falls through to the database.",
                "test_plan": "dependency_health for the cache; blast radius across services",
            },
        ]
    }


REMEDIATION = {
    "bad_deploy": [
        "Roll back {component} to the previous version.",
        "Add a regression test for the failing code path before redeploying.",
    ],
    "db_pool_exhaustion": [
        "Stop or throttle the job holding database connections.",
        "Give batch jobs their own connection pool; alert on pool wait time.",
    ],
    "dependency_timeout": [
        "Fail fast: lower the client timeout and serve a fallback response.",
        "Check the provider's status page; consider a secondary provider.",
    ],
    "cache_outage": [
        "Restore the cache (check the redis node and failover).",
        "Add request coalescing so cache misses do not stampede the database.",
    ],
    "unknown": ["Widen the investigation window and check infrastructure metrics."],
}


class SREPolicy:
    """Rule-based investigation over the tool results already in the conversation."""

    def __init__(self, messages: list[BaseMessage]):
        brief = next((str(m.content) for m in messages if isinstance(m, HumanMessage)), "")
        self.service = _field(brief, "Service") or ""
        self.endpoint = _field(brief, "Endpoint")
        self.evidence: dict[str, tuple[str, dict[str, Any]]] = {}  # tool -> (id, result)
        for m in messages:
            if isinstance(m, ToolMessage):
                try:
                    data = json.loads(str(m.content))
                except json.JSONDecodeError:
                    continue
                if "evidence_id" in data and "error" not in data:
                    self.evidence[data["tool"]] = (data["evidence_id"], data["result"])

    # ---- decisions -------------------------------------------------------

    def next_action(self) -> tuple[str, dict[str, Any]]:
        plan: list[tuple[str, dict[str, Any]]] = []
        if self.endpoint:
            plan.append(
                ("latency_percentiles", {"service": self.service, "endpoint": self.endpoint})
            )
        plan += [
            ("query_error_spans", {"service": self.service}),
            ("recent_deploys", {"service": self.service, "window": "2h"}),
            ("dependency_health", {"service": self.service}),
        ]
        for tool, args in plan:
            if tool not in self.evidence:
                return tool, args
        if "search_logs" not in self.evidence:
            return "search_logs", {"service": self.service, "pattern": self._log_pattern()}
        return "submit_findings", self.conclude()

    def _deps(self) -> list[dict[str, Any]]:
        return self.evidence.get("dependency_health", ("", {"dependencies": []}))[1]["dependencies"]

    def _primary(self) -> tuple[dict[str, Any] | None, str]:
        """The dependency that best explains the symptom, and why."""
        deps = self._deps()
        erroring = [d for d in deps if d["error_rate_pct"] - d["baseline_error_rate_pct"] >= 5]
        if erroring:
            return max(erroring, key=lambda d: d["error_rate_pct"]), "errors"
        for d in deps:
            wait, base = d.get("pool_wait_p95_ms"), d.get("baseline_pool_wait_p95_ms") or 1
            if wait and wait > 100 and wait > 10 * base:
                return d, "pool"
        slow = [
            d
            for d in deps
            if d["p95_ms"] and d["baseline_p95_ms"] and d["p95_ms"] / d["baseline_p95_ms"] >= 5
        ]
        if slow:
            return max(slow, key=lambda d: d["p95_ms"] / d["baseline_p95_ms"]), "latency"
        return None, ""

    def _suspect_deploy(self) -> dict[str, Any] | None:
        deploys = self.evidence.get("recent_deploys", ("", {"deploys": []}))[1]["deploys"]
        recent = [
            d for d in deploys if d["service"] == self.service and d["minutes_before_now"] <= 30
        ]
        return recent[0] if recent else None

    def _log_pattern(self) -> str:
        dep, why = self._primary()
        if dep is not None:
            if why == "pool" or dep["type"] == "database":
                return "pool"
            return dep["dependency"] if dep["type"] == "cache" else "timed out"
        errors = self.evidence.get("query_error_spans", ("", {"top_errors": []}))[1]
        if errors["top_errors"]:
            return errors["top_errors"][0]["message"].split(":")[0]
        return "error"

    # ---- conclusion ------------------------------------------------------

    def conclude(self) -> dict[str, Any]:
        ev = {k: v[0] for k, v in self.evidence.items()}
        res = {k: v[1] for k, v in self.evidence.items()}
        claims: list[dict[str, Any]] = []
        verdicts: dict[str, tuple[str, str, list[str]]] = {}

        lat = res.get("latency_percentiles")
        if lat and lat.get("current"):
            text = (
                f"p95 latency of {lat['endpoint']} is {lat['current']['p95_ms']}ms vs a "
                f"baseline of {lat['baseline']['p95_ms']}ms"
            )
            if lat.get("onset_at"):
                text += f", starting at {lat['onset_at'][11:16]} UTC"
            claims.append({"text": text, "evidence_ids": [ev["latency_percentiles"]]})

        dep, why = self._primary()
        deploy = self._suspect_deploy()
        errors = res.get("query_error_spans")
        category, component, root, root_ids = "unknown", self.service, "", []
        confidence = "low"

        if dep is not None:
            name, kind = dep["dependency"], dep["type"]
            dep_id = ev["dependency_health"]
            if why == "pool":
                category = "db_pool_exhaustion"
                root = (
                    f"{self.service} requests are waiting for {name} connections: pool wait "
                    f"p95 is {dep['pool_wait_p95_ms']}ms vs {dep['baseline_pool_wait_p95_ms']}"
                    f"ms at baseline."
                )
            elif kind == "cache":
                category = "cache_outage"
                root = (
                    f"The {name} cache is failing ({dep['error_rate_pct']}% of calls error), "
                    f"so reads fall through to the database."
                )
            elif kind in ("llm", "http"):
                category = "dependency_timeout"
                root = (
                    f"Calls from {self.service} to {name} are timing out: "
                    f"{dep['error_rate_pct']}% error rate, p95 {dep['p95_ms']}ms vs "
                    f"{dep['baseline_p95_ms']}ms."
                )
            else:
                root = f"{name} is degraded ({dep['error_rate_pct']}% errors)."
            component, root_ids, confidence = name, [dep_id], "high"
            claims.append(
                {
                    "text": (
                        f"{name} error rate is {dep['error_rate_pct']}% (baseline "
                        f"{dep['baseline_error_rate_pct']}%) and p95 is {dep['p95_ms']}ms "
                        f"(baseline {dep['baseline_p95_ms']}ms)"
                    ),
                    "evidence_ids": [dep_id],
                }
            )
            if dep.get("top_error"):
                claims.append(
                    {
                        "text": f"Most common {name} error: {dep['top_error']}",
                        "evidence_ids": [dep_id],
                    }
                )
            spread = [c for c in dep.get("other_callers", []) if c["error_rate_pct"] >= 5]
            if spread:
                c = spread[0]
                claims.append(
                    {
                        "text": (
                            f"{c['service']} also sees {c['error_rate_pct']}% errors "
                            f"calling {name}, so the failure is not local to "
                            f"{self.service}"
                        ),
                        "evidence_ids": [dep_id],
                    }
                )
            for h, cat in (
                ("H2", "dependency_timeout"),
                ("H3", "db_pool_exhaustion"),
                ("H4", "cache_outage"),
            ):
                verdicts[h] = (
                    ("confirmed", root, root_ids)
                    if cat == category
                    else ("ruled_out", f"Evidence points to {category} instead.", [dep_id])
                )
            if deploy:
                reason = (
                    f"{deploy['service']} {deploy['version']} deployed at "
                    f"{deploy['deployed_at'][11:16]}, but the failing component is {name}"
                )
                if spread:
                    reason += f", which also fails for {spread[0]['service']}"
                claims.append({"text": reason, "evidence_ids": [ev["recent_deploys"], dep_id]})
                verdicts["H1"] = ("ruled_out", reason, [ev["recent_deploys"], dep_id])
            else:
                verdicts["H1"] = (
                    "ruled_out",
                    f"No deploy of {self.service} in the last 30 minutes.",
                    [ev["recent_deploys"]],
                )
        elif deploy and errors:
            new = errors["error_rate_by_version"].get(deploy["version"])
            old = errors["error_rate_by_version"].get(deploy["previous_version"] or "")
            if new and new["error_rate_pct"] >= 5 and (not old or old["error_rate_pct"] < 1):
                category, confidence = "bad_deploy", "high"
                component = f"{self.service}@{deploy['version']}"
                root_ids = [ev["recent_deploys"], ev["query_error_spans"]]
                root = (
                    f"{self.service} {deploy['version']}, deployed at "
                    f"{deploy['deployed_at'][11:16]} UTC, fails {new['error_rate_pct']}% of "
                    f"requests while {deploy['previous_version']} fails "
                    f"{old['error_rate_pct'] if old else 0.0}%."
                )
                claims.append(
                    {
                        "text": (
                            f"Errors are concentrated on {deploy['version']}: "
                            f"{new['error_rate_pct']}% vs "
                            f"{old['error_rate_pct'] if old else 0.0}% on "
                            f"{deploy['previous_version']}"
                        ),
                        "evidence_ids": [ev["query_error_spans"]],
                    }
                )
                if errors["top_errors"]:
                    claims.append(
                        {
                            "text": f"Top error: {errors['top_errors'][0]['message']}",
                            "evidence_ids": [ev["query_error_spans"]],
                        }
                    )
                verdicts["H1"] = ("confirmed", root, root_ids)
                for h in ("H2", "H3", "H4"):
                    verdicts[h] = (
                        "ruled_out",
                        "All dependencies are at baseline.",
                        [ev["dependency_health"]],
                    )

        logs = res.get("search_logs")
        if logs and logs["matches"] and logs["top_messages"]:
            claims.append(
                {
                    "text": (
                        f"{logs['matches']} log lines match '{logs['pattern']}', "
                        f'e.g. "{logs["top_messages"][0]["message"]}"'
                    ),
                    "evidence_ids": [ev["search_logs"]],
                }
            )
        if not root:
            root = "No single component explains the symptom with the evidence gathered."
            root_ids = [next(iter(ev.values()))] if ev else []
        return {
            "root_cause_category": category,
            "root_cause_component": component,
            "root_cause": root,
            "root_cause_evidence_ids": root_ids,
            "claims": claims,
            "hypothesis_verdicts": [
                {"hypothesis_id": h, "verdict": v, "reason": r, "evidence_ids": ids}
                for h, (v, r, ids) in sorted(verdicts.items())
            ],
            "confidence": confidence,
            "remediation": [
                s.format(component=component)
                for s in REMEDIATION.get(category, REMEDIATION["unknown"])
            ],
        }


__all__ = ["LLMInfo", "MockSREModel", "SREPolicy", "build_llm"]
