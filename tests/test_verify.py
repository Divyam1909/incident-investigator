from __future__ import annotations

from typing import Any

from investigator.agent.schemas import Claim, Evidence, Findings
from investigator.agent.verify import EvidenceFacts, check_text, feedback_message, verify_findings

LATENCY = Evidence(
    id="E1",
    tool="latency_percentiles",
    args={},
    summary="",
    latency_ms=1.0,
    result={
        "current": {"p95_ms": 2412.3, "error_rate_pct": 28.43},
        "baseline": {"p95_ms": 171.2},
        "onset_at": "2026-11-03T14:05:00Z",
    },
)
DEPS = Evidence(
    id="E2",
    tool="dependency_health",
    args={},
    summary="",
    latency_ms=1.0,
    result={
        "dependencies": [
            {
                "dependency": "postgres",
                "pool_wait_p95_ms": 1108.0,
                "top_error": "QueuePool limit of size 20 overflow 10 reached",
            }
        ]
    },
)
DEPLOYS = Evidence(
    id="E3",
    tool="recent_deploys",
    args={},
    summary="",
    latency_ms=1.0,
    result={
        "deploys": [
            {"service": "checkout-api", "version": "2.14.0", "deployed_at": "2026-11-03T13:50:00Z"}
        ]
    },
)
EVIDENCE = [LATENCY, DEPS, DEPLOYS]


def findings(**overrides: Any) -> Findings:
    base: dict[str, Any] = {
        "root_cause_category": "db_pool_exhaustion",
        "root_cause_component": "postgres",
        "root_cause": "Requests wait for postgres connections (pool wait p95 1108.0ms).",
        "root_cause_evidence_ids": ["E2"],
        "claims": [
            Claim(text="p95 is 2412.3ms vs 171.2ms baseline", evidence_ids=["E1"]),
            Claim(text="QueuePool limit of size 20 reached", evidence_ids=["E2"]),
        ],
        "hypothesis_verdicts": [],
        "confidence": "high",
        "remediation": ["Stop the export job."],
    }
    return Findings(**(base | overrides))


def test_grounded_findings_pass_with_model_confidence() -> None:
    v = verify_findings(findings(), EVIDENCE)
    assert v.passed and v.root_cause_supported
    assert v.confidence == "high"


def test_units_and_rounding_are_tolerated() -> None:
    f = EvidenceFacts([LATENCY])
    assert check_text("p95 rose to 2.4s", f) == []  # 2.4 s == 2412.3 ms within rounding
    assert check_text("about 28% of requests failed", f) == []
    assert check_text("a x14 jump in p95", f) == []  # 2412.3 / 171.2 = 14.1
    assert check_text("p95 is 14 times the baseline", f) == []
    assert check_text("starting at 14:05 UTC", f) == []
    assert check_text("p95 and p99 on 5xx paths for H2 and E1", f) == []  # labels, not numbers


def test_invented_specifics_are_caught() -> None:
    f = EvidenceFacts([LATENCY])
    assert check_text("p95 rose to 3.9s", f) == ["number 3.9s not in cited evidence"]
    assert check_text("errors began at 13:40", f) == ["time 13:40 not in cited evidence"]
    assert check_text("since v2.15.1 shipped", f) == ["version 2.15.1 not in cited evidence"]


def test_claim_citing_wrong_evidence_is_unsupported() -> None:
    # The number exists, but only in E1; citing E3 does not count.
    bad = Claim(text="p95 is 2412.3ms", evidence_ids=["E3"])
    v = verify_findings(findings(claims=[*findings().claims, bad]), EVIDENCE)
    assert not v.passed
    assert v.checks[-1].problems == ["number 2412.3ms not in cited evidence"]
    assert v.confidence == "medium"  # capped, never raised


def test_unknown_or_missing_evidence_ids() -> None:
    claims = [
        Claim(text="pool is exhausted", evidence_ids=["E9"]),
        Claim(text="pool is exhausted", evidence_ids=[]),
    ]
    v = verify_findings(findings(claims=claims), EVIDENCE)
    assert v.checks[0].problems == ["cites unknown evidence id E9"]
    assert v.checks[1].problems == ["cites no evidence"]


def test_root_cause_component_must_appear_in_its_evidence() -> None:
    v = verify_findings(
        findings(root_cause_component="redis", root_cause="The redis cache failed."), EVIDENCE
    )
    assert not v.root_cause_supported
    assert v.confidence == "low"
    assert "redis" in v.root_cause_problems[0]


def test_budget_exhaustion_caps_confidence() -> None:
    v = verify_findings(findings(), EVIDENCE, budget_exhausted=True)
    assert v.passed and v.confidence == "medium"


def test_unknown_root_cause_is_always_low_confidence() -> None:
    v = verify_findings(findings(root_cause_category="unknown"), EVIDENCE)
    assert v.confidence == "low"


def test_feedback_lists_each_problem() -> None:
    bad = Claim(text="p95 rose to 9.9s", evidence_ids=["E1"])
    text = feedback_message(verify_findings(findings(claims=[bad]), EVIDENCE))
    assert "9.9s" in text and "submit_findings" in text
