"""Runs an investigation and records it in the database.

The graph itself knows nothing about the ``investigations`` tables. This
module streams the graph's per-node updates and writes each one as an
``investigation_steps`` row as it happens, which is what the API's SSE
endpoint replays. Because progress lives in the database, any process
(CLI, API worker, another replica) can watch a run.
"""

from __future__ import annotations

import time
import traceback
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from investigator.agent import build_graph, initial_state
from investigator.agent.llm import LLMInfo
from investigator.agent.schemas import Report
from investigator.agent.state import IncidentContext
from investigator.config import Settings
from investigator.db import Incident, Investigation, InvestigationStep, Service, session_scope
from investigator.db.models import utcnow
from investigator.tools import Toolbox


@dataclass
class StepEvent:
    node: str
    summary: str
    tool_name: str | None = None
    evidence_id: str | None = None
    args: dict[str, Any] | None = None
    latency_ms: float | None = None


def incident_context(session: Session, incident: Incident) -> IncidentContext:
    return {
        "incident_id": incident.id,
        "title": incident.title,
        "alert_text": incident.alert_text,
        "service": incident.service.name if incident.service else None,
        "endpoint": incident.endpoint,
        "detected_at": incident.detected_at.isoformat(),
        "known_services": sorted(session.scalars(select(Service.name))),
    }


def describe(node: str, delta: dict[str, Any]) -> list[StepEvent]:
    """Turn one node's state update into human-readable step events."""
    if node == "triage":
        t = delta["triage"]
        return [
            StepEvent(
                node,
                f"service={t.service} endpoint={t.endpoint} "
                f"symptom={t.symptom} severity={t.severity}: {t.summary}",
            )
        ]
    if node == "hypothesize":
        return [
            StepEvent(
                node,
                " | ".join(f"{h.id} [{h.category}] {h.statement}" for h in delta["hypotheses"])
                or "no hypotheses",
            )
        ]
    if node == "investigate":
        if delta.get("findings") is not None:
            f = delta["findings"]
            how = "forced (step budget used up)" if delta.get("budget_exhausted") else "submitted"
            return [
                StepEvent(
                    node,
                    f"findings {how}: {f.root_cause_category} in "
                    f"{f.root_cause_component} (model confidence {f.confidence})",
                )
            ]
        ai = next(
            (m for m in reversed(delta.get("messages", [])) if isinstance(m, AIMessage)), None
        )
        if ai is not None and ai.tool_calls:
            calls = ", ".join(f"{c['name']}({_short_args(c['args'])})" for c in ai.tool_calls)
            return [StepEvent(node, f"calling {calls}")]
        return [StepEvent(node, "model replied without a tool call")]
    if node == "tools":
        return [
            StepEvent(node, e.summary, e.tool, e.id, e.args, e.latency_ms)
            for e in delta.get("evidence", [])
        ]
    if node == "verify":
        v = delta["verification"]
        failed = sum(not c.supported for c in v.checks)
        verdict = (
            "passed"
            if v.passed
            else (
                f"{failed} unsupported claim(s), root cause "
                f"{'supported' if v.root_cause_supported else 'NOT supported'}"
            )
        )
        retry = "; sent back to investigate" if delta.get("findings", "keep") is None else ""
        return [StepEvent(node, f"{verdict}; confidence {v.confidence}{retry}")]
    if node == "report":
        r = delta["report"]
        return [
            StepEvent(
                node,
                f"{r.root_cause_category} / {r.root_cause_component} (confidence {r.confidence})",
            )
        ]
    return [StepEvent(node, "done")]


def _short_args(args: dict[str, Any]) -> str:
    return ", ".join(f"{k}={v!r}" for k, v in args.items() if k != "claims")[:160]


def create_investigation(
    factory: sessionmaker[Session], incident_id: int, info: LLMInfo, max_steps: int
) -> int:
    with session_scope(factory) as session:
        if session.get(Incident, incident_id) is None:
            raise LookupError(f"incident {incident_id} not found")
        inv = Investigation(
            incident_id=incident_id,
            status="queued",
            llm_provider=info.provider,
            llm_model=info.model,
            max_steps=max_steps,
        )
        session.add(inv)
        session.flush()
        return inv.id


def run_investigation(
    factory: sessionmaker[Session],
    investigation_id: int,
    llm: BaseChatModel,
    info: LLMInfo,
    settings: Settings,
    on_event: Callable[[int, StepEvent], None] | None = None,
) -> Investigation:
    with session_scope(factory) as session:
        inv = session.get(Investigation, investigation_id)
        assert inv is not None
        inv.status, inv.started_at = "running", utcnow()
        context = incident_context(session, inv.incident)
        detected_at, max_steps = inv.incident.detected_at, inv.max_steps

    graph = build_graph(
        llm,
        Toolbox(factory, detected_at),
        max_steps=max_steps,
        max_revisions=settings.max_revisions,
    )
    started = time.perf_counter()
    seq, steps, tokens_in, tokens_out = 0, 0, 0, 0
    report: Report | None = None
    try:
        for update in graph.stream(
            initial_state(context),
            stream_mode="updates",
            config={"recursion_limit": 4 * max_steps + 20},
        ):
            for node, delta in update.items():
                if not delta:
                    continue
                usage = delta.get("usage") or {}
                tokens_in += usage.get("input_tokens", 0)
                tokens_out += usage.get("output_tokens", 0)
                steps = delta.get("steps", steps)
                report = delta.get("report", report)
                with session_scope(factory) as session:
                    for event in describe(node, delta):
                        seq += 1
                        session.add(
                            InvestigationStep(
                                investigation_id=investigation_id,
                                seq=seq,
                                node=event.node,
                                tool_name=event.tool_name,
                                evidence_id=event.evidence_id,
                                args=event.args,
                                result_summary=event.summary,
                                latency_ms=event.latency_ms,
                            )
                        )
                        if on_event:
                            on_event(seq, event)
                    inv = session.get(Investigation, investigation_id)
                    assert inv is not None
                    inv.steps, inv.input_tokens, inv.output_tokens = steps, tokens_in, tokens_out
        error = None
    except Exception as exc:  # recorded on the row; the API must not crash the worker
        error = f"{type(exc).__name__}: {exc}\n{traceback.format_exc(limit=5)}"

    with session_scope(factory) as session:
        inv = session.get(Investigation, investigation_id)
        assert inv is not None
        inv.latency_ms = round((time.perf_counter() - started) * 1000, 1)
        inv.cost_usd = info.cost(tokens_in, tokens_out)
        inv.finished_at = utcnow()
        if report is not None and error is None:
            inv.status = "completed"
            inv.root_cause_category = report.root_cause_category
            inv.root_cause_component = report.root_cause_component
            inv.confidence = report.confidence
            inv.report = report.model_dump(mode="json", exclude={"markdown"})
            inv.report_markdown = report.markdown
        else:
            inv.status = "failed"
            inv.error = error or "graph finished without a report"
        session.flush()
        session.expunge(inv)
        return inv
