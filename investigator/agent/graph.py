"""The investigation graph.

    triage -> hypothesize -> investigate <-> tools -> verify -> report
                                  ^                      |
                                  +---- (revise once) ---+

* investigate/tools is the ReAct-style loop. ``steps`` counts executed tool
  calls; the max-steps guard lives in two places on purpose: the tools node
  never runs more calls than the budget allows (a single model turn can ask for
  several), and investigate forces a conclusion once the budget is spent.
* The model ends the loop by calling ``submit_findings`` (structured output as
  a tool call), or the guard forces a structured conclusion.
* verify is deterministic code (see verify.py). If claims fail and budget
  remains, it sends the agent back once with the exact problems.
* report renders only verified claims. The LLM decides; code writes the report.
"""

from __future__ import annotations

import json
import time
from typing import Any, Literal

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)
from langchain_core.tools import StructuredTool
from langchain_core.utils.function_calling import convert_to_openai_tool
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from pydantic import ValidationError

from investigator.agent import prompts
from investigator.agent.report import build_report
from investigator.agent.schemas import Evidence, Findings, HypothesisSet, Triage
from investigator.agent.state import InvestigationState, Usage
from investigator.agent.verify import feedback_message, verify_findings
from investigator.tools import Toolbox, summarize

SUBMIT = "submit_findings"


def _usage(message: Any) -> Usage:
    meta = getattr(message, "usage_metadata", None) or {}
    return {
        "llm_calls": 1,
        "input_tokens": int(meta.get("input_tokens", 0)),
        "output_tokens": int(meta.get("output_tokens", 0)),
    }


def _structured(llm: BaseChatModel, schema: type, messages: list[BaseMessage]) -> tuple[Any, Usage]:
    """Structured output plus token usage (include_raw keeps the raw AIMessage)."""
    out = llm.with_structured_output(schema, include_raw=True).invoke(messages)
    return out["parsed"], _usage(out["raw"])


def tool_specs(toolbox: Toolbox) -> list[Any]:
    """LangChain tool definitions: the Toolbox methods plus the submit_findings 'tool'."""
    specs: list[Any] = [
        StructuredTool.from_function(func=fn, name=name, description=fn.__doc__ or name)
        for name, fn in toolbox.registry().items()
    ]
    submit = convert_to_openai_tool(Findings)
    submit["function"]["name"] = SUBMIT
    submit["function"]["description"] = prompts.SUBMIT_FINDINGS_DESCRIPTION
    specs.append(submit)
    return specs


def build_graph(
    llm: BaseChatModel, toolbox: Toolbox, *, max_steps: int = 12, max_revisions: int = 1
) -> CompiledStateGraph:
    llm_with_tools = llm.bind_tools(tool_specs(toolbox))

    # ---- nodes -----------------------------------------------------------

    def triage(state: InvestigationState) -> dict[str, Any]:
        inc = state["incident"]
        messages = [
            SystemMessage(prompts.TRIAGE_SYSTEM),
            HumanMessage(
                f"Alert: {inc['alert_text']}\nService hint: {inc['service']}\n"
                f"Endpoint hint: {inc['endpoint']}\nKnown services: {inc['known_services']}"
            ),
        ]
        result, usage = _structured(llm, Triage, messages)
        if result is None:  # unparseable output: fall back to the alert's own fields
            result = Triage(
                service=inc["service"] or "unknown",
                endpoint=inc["endpoint"],
                symptom="other",
                severity="sev2",
                summary=inc["alert_text"],
            )
        # The model may not invent a service; trust the alert source if it does.
        if result.service not in inc["known_services"] and inc["service"]:
            result.service = inc["service"]
        if inc["endpoint"] and not result.endpoint:
            result.endpoint = inc["endpoint"]
        return {"triage": result, "usage": usage}

    def hypothesize(state: InvestigationState) -> dict[str, Any]:
        t = state["triage"]
        messages = [
            SystemMessage(prompts.HYPOTHESIZE_SYSTEM),
            HumanMessage(
                f"Alert: {state['incident']['alert_text']}\nService: {t.service}\n"
                f"Endpoint: {t.endpoint}\nSymptom: {t.symptom}\nSummary: {t.summary}"
            ),
        ]
        result, usage = _structured(llm, HypothesisSet, messages)
        hypotheses = result.hypotheses[:4] if result else []
        return {"hypotheses": hypotheses, "usage": usage}

    def investigate(state: InvestigationState) -> dict[str, Any]:
        new: list[BaseMessage] = []
        if not state.get("messages"):
            new = [
                SystemMessage(prompts.INVESTIGATE_SYSTEM.format(max_steps=max_steps)),
                HumanMessage(_brief(state)),
            ]
        history = [*state.get("messages", []), *new]

        if state["steps"] >= max_steps:
            return _force_conclusion(
                history,
                new,
                f"Step budget of {max_steps} tool calls is used up.",
                budget_exhausted=True,
            )

        ai = llm_with_tools.invoke(history)
        usage = _usage(ai)
        new.append(ai)
        submit = next((c for c in ai.tool_calls if c["name"] == SUBMIT), None)
        if submit is not None:
            try:
                findings = Findings.model_validate(submit["args"])
            except ValidationError as exc:
                # Malformed findings cost a step, so a model that keeps failing
                # still hits the guard instead of looping forever.
                replies = _answer_all(
                    ai, f"Invalid findings: {exc.errors()[:3]}. Fix and resubmit."
                )
                return {"messages": [*new, *replies], "steps": state["steps"] + 1, "usage": usage}
            return {"messages": new, "findings": findings, "usage": usage}
        if ai.tool_calls:
            return {"messages": new, "usage": usage}
        # Plain-text answer with no tool call: make it commit to structured findings.
        out = _force_conclusion([*history, ai], new, "You answered without calling a tool.")
        out["usage"] = {k: out["usage"][k] + usage[k] for k in usage}
        return out

    def _force_conclusion(
        history: list[BaseMessage],
        new: list[BaseMessage],
        reason: str,
        budget_exhausted: bool = False,
    ) -> dict[str, Any]:
        ask = HumanMessage(prompts.FORCE_CONCLUDE.format(reason=reason))
        findings, usage = _structured(llm, Findings, [*history, ask])
        if findings is None:
            findings = Findings(
                root_cause_category="unknown",
                root_cause_component="unknown",
                root_cause="The model did not produce valid findings.",
                root_cause_evidence_ids=[],
                claims=[],
                hypothesis_verdicts=[],
                confidence="low",
                remediation=["Escalate to a human on-call engineer."],
            )
        reply = AIMessage(content=findings.model_dump_json())
        out: dict[str, Any] = {"messages": [*new, ask, reply], "findings": findings, "usage": usage}
        if budget_exhausted:
            out["budget_exhausted"] = True
        return out

    def tools(state: InvestigationState) -> dict[str, Any]:
        ai = state["messages"][-1]
        assert isinstance(ai, AIMessage)
        steps, evidence = state["steps"], state.get("evidence", [])
        replies: list[ToolMessage] = []
        new_evidence: list[Evidence] = []
        for call in ai.tool_calls:
            if steps >= max_steps:
                replies.append(
                    ToolMessage("Skipped: step budget exhausted.", tool_call_id=call["id"])
                )
                continue
            started = time.perf_counter()
            result = toolbox.call(call["name"], call["args"])
            latency = round((time.perf_counter() - started) * 1000, 1)
            steps += 1
            eid = f"E{len(evidence) + len(new_evidence) + 1}"
            new_evidence.append(
                Evidence(
                    id=eid,
                    tool=call["name"],
                    args=call["args"],
                    result=result,
                    summary=summarize(call["name"], result),
                    latency_ms=latency,
                )
            )
            payload = {"evidence_id": eid, "tool": call["name"], "result": result}
            replies.append(ToolMessage(json.dumps(payload, default=str), tool_call_id=call["id"]))
        return {
            "messages": replies,
            "evidence": new_evidence,
            "steps": steps,
            "budget_exhausted": steps >= max_steps,
        }

    def verify(state: InvestigationState) -> dict[str, Any]:
        findings = state["findings"]
        assert findings is not None
        result = verify_findings(
            findings,
            state.get("evidence", []),
            budget_exhausted=state.get("budget_exhausted", False),
        )
        can_retry = state["revisions"] < max_revisions and state["steps"] < max_steps
        if result.passed or not can_retry:
            return {"verification": result}
        last = state["messages"][-1]
        feedback = feedback_message(result)
        replies: list[BaseMessage] = (
            _answer_all(last, feedback)
            if isinstance(last, AIMessage) and last.tool_calls
            else [HumanMessage(feedback)]
        )
        return {
            "verification": result,
            "findings": None,
            "messages": replies,
            "revisions": state["revisions"] + 1,
        }

    def report(state: InvestigationState) -> dict[str, Any]:
        return {"report": build_report(state, max_steps=max_steps)}

    # ---- edges -----------------------------------------------------------

    def after_investigate(state: InvestigationState) -> Literal["verify", "tools", "investigate"]:
        if state.get("findings") is not None:
            return "verify"
        last = state["messages"][-1]
        # A rejected submit_findings was already answered; ask the model again.
        return "tools" if isinstance(last, AIMessage) and last.tool_calls else "investigate"

    def after_verify(state: InvestigationState) -> Literal["report", "investigate"]:
        return "report" if state.get("findings") is not None else "investigate"

    graph = StateGraph(InvestigationState)
    graph.add_node("triage", triage)
    graph.add_node("hypothesize", hypothesize)
    graph.add_node("investigate", investigate)
    graph.add_node("tools", tools)
    graph.add_node("verify", verify)
    graph.add_node("report", report)
    graph.add_edge(START, "triage")
    graph.add_edge("triage", "hypothesize")
    graph.add_edge("hypothesize", "investigate")
    graph.add_conditional_edges("investigate", after_investigate)
    graph.add_edge("tools", "investigate")
    graph.add_conditional_edges("verify", after_verify)
    graph.add_edge("report", END)
    return graph.compile()


def _answer_all(ai: AIMessage, text: str) -> list[ToolMessage]:
    """Reply to every tool call in a message (providers reject unanswered calls)."""
    return [
        ToolMessage(
            text if c["name"] == SUBMIT else "Not executed: findings were submitted.",
            tool_call_id=c["id"],
        )
        for c in ai.tool_calls
    ]


def _brief(state: InvestigationState) -> str:
    inc, t = state["incident"], state["triage"]
    hypotheses = (
        "\n".join(
            f"{h.id} [{h.category}] {h.statement} (test: {h.test_plan})"
            for h in state.get("hypotheses", [])
        )
        or "none"
    )
    return prompts.INVESTIGATE_BRIEF.format(
        title=inc["title"],
        alert_text=inc["alert_text"],
        detected_at=inc["detected_at"],
        service=t.service,
        endpoint=t.endpoint,
        symptom=t.symptom,
        severity=t.severity,
        summary=t.summary,
        hypotheses=hypotheses,
        known_services=", ".join(inc["known_services"]),
    )


def mermaid() -> str:
    """Mermaid diagram of the compiled graph (no database or API key needed)."""
    from investigator.agent.llm import MockSREModel

    graph = build_graph(MockSREModel(), Toolbox(None, None))  # type: ignore[arg-type]
    return graph.get_graph().draw_mermaid()
