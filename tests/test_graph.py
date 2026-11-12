"""Graph routing: the happy path, the max-steps guard and verify's revise loop."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest
from langchain_core.messages import AIMessage, BaseMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from sqlalchemy.orm import Session, sessionmaker

from investigator.agent import build_graph, initial_state
from investigator.agent.llm import MockSREModel
from investigator.agent.state import IncidentContext
from investigator.evaluate import is_correct
from investigator.scenarios import SCENARIOS
from tests.conftest import toolbox_for

Turn = Callable[[list[BaseMessage], int], AIMessage]


class ScriptedModel(MockSREModel):
    """Mock triage and hypotheses, but each investigate turn comes from a test script."""

    turn: Turn
    calls: int = 0

    def _generate(
        self, messages: list[BaseMessage], stop: Any = None, run_manager: Any = None, **kwargs: Any
    ) -> ChatResult:
        names = [t["function"]["name"] for t in kwargs.get("tools", [])]
        if "submit_findings" not in names:
            return super()._generate(messages, stop, run_manager, **kwargs)
        self.calls += 1
        return ChatResult(generations=[ChatGeneration(message=self.turn(messages, self.calls))])


def context(key: str) -> IncidentContext:
    s = SCENARIOS[key]
    return {
        "incident_id": None,
        "title": s.title,
        "alert_text": s.alert_text,
        "service": s.service,
        "endpoint": s.endpoint,
        "detected_at": s.detected_at.isoformat(),
        "known_services": [
            "catalog-service",
            "checkout-api",
            "payments-service",
            "support-assistant",
        ],
    }


def call(name: str, args: dict[str, Any], i: int) -> dict[str, Any]:
    return {"name": name, "args": args, "id": f"c{i}", "type": "tool_call"}


def tool_turn(name: str, args: dict[str, Any]) -> Turn:
    return lambda msgs, i: AIMessage("", tool_calls=[call(name, args, i)])


def run(
    factory: sessionmaker[Session], model: MockSREModel, key: str = "db_pool", **kw: Any
) -> dict[str, Any]:
    graph = build_graph(model, toolbox_for(factory, key), **kw)
    return graph.invoke(initial_state(context(key)), {"recursion_limit": 100})


GOOD_FINDINGS: dict[str, Any] = {
    "root_cause_category": "db_pool_exhaustion",
    "root_cause_component": "postgres",
    "root_cause": "checkout-api waits for postgres connections.",
    "root_cause_evidence_ids": ["E1"],
    "claims": [
        {"text": "postgres pool wait is high", "evidence_ids": ["E1"]},
        {"text": "QueuePool timeouts appear", "evidence_ids": ["E1"]},
    ],
    "hypothesis_verdicts": [],
    "confidence": "high",
    "remediation": ["Stop the export job."],
}


@pytest.mark.parametrize("key", list(SCENARIOS))
def test_mock_agent_solves_every_scenario(factory: sessionmaker[Session], key: str) -> None:
    out = run(factory, MockSREModel(), key)
    report = out["report"]
    assert is_correct(SCENARIOS[key], report.root_cause_category, report.root_cause_component)
    assert out["verification"].passed
    assert report.removed_claims == []
    # triage + hypothesize + one turn per tool call + the submit_findings turn
    assert out["usage"]["llm_calls"] == 2 + out["steps"] + 1


def test_max_steps_guard_forces_a_conclusion(factory: sessionmaker[Session]) -> None:
    """A model that never stops calling tools still terminates at the budget."""
    model = ScriptedModel(turn=tool_turn("query_error_spans", {"service": "checkout-api"}))
    out = run(factory, model, max_steps=3)
    assert out["steps"] == 3
    assert model.calls == 3  # the 4th turn is the forced structured conclusion instead
    assert out["budget_exhausted"] is True
    assert out["report"].confidence in ("medium", "low")
    assert "Step budget" in out["report"].markdown


def test_guard_caps_parallel_tool_calls(factory: sessionmaker[Session]) -> None:
    """Five calls in one turn with a budget of two: two run, three are skipped."""

    def turn(msgs: list[BaseMessage], i: int) -> AIMessage:
        calls = [call("recent_deploys", {"service": "checkout-api"}, 10 * i + n) for n in range(5)]
        return AIMessage("", tool_calls=calls)

    out = run(factory, ScriptedModel(turn=turn), max_steps=2)
    skipped = [
        m
        for m in out["messages"]
        if isinstance(m, ToolMessage) and "budget exhausted" in str(m.content)
    ]
    assert out["steps"] == 2
    assert len(out["evidence"]) == 2
    assert len(skipped) == 3


def _submit_after_one_tool(extra_claim: dict[str, Any], bad_turns: set[int]) -> Turn:
    def turn(msgs: list[BaseMessage], i: int) -> AIMessage:
        if i == 1:
            return AIMessage(
                "", tool_calls=[call("dependency_health", {"service": "checkout-api"}, i)]
            )
        findings = dict(GOOD_FINDINGS)
        if i in bad_turns:
            findings["claims"] = [*GOOD_FINDINGS["claims"], extra_claim]
        return AIMessage("", tool_calls=[call("submit_findings", findings, i)])

    return turn


def test_verify_sends_back_unsupported_claims_once(factory: sessionmaker[Session]) -> None:
    invented = {"text": "pool wait p95 is 98765ms", "evidence_ids": ["E1"]}
    model = ScriptedModel(turn=_submit_after_one_tool(invented, bad_turns={2}))
    out = run(factory, model, max_revisions=1)
    assert out["revisions"] == 1
    assert out["verification"].passed  # the second submission is clean
    feedback = [
        m
        for m in out["messages"]
        if isinstance(m, ToolMessage) and "Verification failed" in str(m.content)
    ]
    assert len(feedback) == 1
    assert "98765ms" in str(feedback[0].content)


def test_verify_gives_up_after_max_revisions(factory: sessionmaker[Session]) -> None:
    invented = {"text": "errors hit 77.7%", "evidence_ids": ["E1"]}
    model = ScriptedModel(turn=_submit_after_one_tool(invented, bad_turns={2, 3, 4}))
    out = run(factory, model, max_revisions=1)
    report = out["report"]
    assert out["revisions"] == 1
    assert [c.text for c in report.removed_claims] == ["errors hit 77.7%"]
    assert report.confidence == "medium"
    # The invented claim only appears in the Verification section, as removed.
    assert "77.7%" not in report.markdown.split("## Verification")[0]


def test_plain_text_answer_is_forced_into_findings(factory: sessionmaker[Session]) -> None:
    model = ScriptedModel(turn=lambda msgs, i: AIMessage("I think it is the database."))
    out = run(factory, model)
    assert out["findings"] is not None
    assert out["steps"] == 0
    assert out["report"].confidence == "low"  # nothing was gathered, so nothing verifies


def test_invalid_findings_cost_a_step(factory: sessionmaker[Session]) -> None:
    model = ScriptedModel(turn=tool_turn("submit_findings", {"root_cause": "db"}))
    out = run(factory, model, max_steps=2)
    assert out["steps"] == 2
    assert out["budget_exhausted"] is True
    assert model.calls == 2


def test_only_provider_overload_is_retried() -> None:
    from investigator.agent.graph import is_transient

    assert is_transient(RuntimeError("429 RESOURCE_EXHAUSTED quota"))
    assert is_transient(RuntimeError("503 UNAVAILABLE high demand"))
    assert not is_transient(ValueError("invalid argument"))
