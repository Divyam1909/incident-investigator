"""The graph's typed state.

Fields annotated with a reducer (``add_messages``, ``operator.add``,
``merge_usage``) accumulate across nodes; everything else is overwritten by
whichever node returns it last.
"""

from __future__ import annotations

import operator
from typing import Annotated, Any, TypedDict

from langchain_core.messages import AnyMessage
from langgraph.graph.message import add_messages

from investigator.agent.schemas import (
    Evidence,
    Findings,
    Hypothesis,
    Report,
    Triage,
    Verification,
)


class Usage(TypedDict):
    llm_calls: int
    input_tokens: int
    output_tokens: int


def merge_usage(a: Usage | None, b: Usage | None) -> Usage:
    a = a or {"llm_calls": 0, "input_tokens": 0, "output_tokens": 0}
    b = b or {"llm_calls": 0, "input_tokens": 0, "output_tokens": 0}
    return {k: a[k] + b[k] for k in ("llm_calls", "input_tokens", "output_tokens")}  # type: ignore[return-value]


class IncidentContext(TypedDict):
    incident_id: int | None
    title: str
    alert_text: str
    service: str | None  # hint from the alert source, may be missing
    endpoint: str | None
    detected_at: str  # ISO-8601 UTC; tools treat it as "now"
    known_services: list[str]


class InvestigationState(TypedDict, total=False):
    incident: IncidentContext
    triage: Triage
    hypotheses: list[Hypothesis]
    # The investigate <-> tools conversation (system prompt, tool calls, results).
    messages: Annotated[list[AnyMessage], add_messages]
    evidence: Annotated[list[Evidence], operator.add]
    steps: int  # tool calls executed so far
    findings: Findings | None
    verification: Verification | None
    revisions: int  # times verify sent the agent back
    budget_exhausted: bool
    usage: Annotated[Usage, merge_usage]
    report: Report


def initial_state(incident: IncidentContext) -> dict[str, Any]:
    return {
        "incident": incident,
        "messages": [],
        "evidence": [],
        "steps": 0,
        "findings": None,
        "verification": None,
        "revisions": 0,
        "budget_exhausted": False,
        "usage": {"llm_calls": 0, "input_tokens": 0, "output_tokens": 0},
    }
