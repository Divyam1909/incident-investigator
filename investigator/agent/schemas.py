"""Pydantic models for everything the LLM produces and the agent passes around.

Structured output (not free text) at every LLM step means each node's output
can be validated, stored and tested, and the eval can score the root cause
exactly.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

RootCauseCategory = Literal[
    "bad_deploy",
    "db_pool_exhaustion",
    "dependency_timeout",
    "cache_outage",
    "traffic_surge",
    "unknown",
]
Confidence = Literal["high", "medium", "low"]


class Triage(BaseModel):
    """Scope of the incident, extracted from the alert."""

    service: str = Field(description="Affected service, exactly as in the known services list")
    endpoint: str | None = Field(
        default=None, description="Affected endpoint as 'METHOD /route', e.g. 'POST /checkout'"
    )
    symptom: Literal["latency", "errors", "latency_and_errors", "other"]
    severity: Literal["sev1", "sev2", "sev3"]
    summary: str = Field(description="One sentence restating the symptom, without guessing a cause")


class Hypothesis(BaseModel):
    id: str = Field(description="H1, H2, ...")
    category: RootCauseCategory
    statement: str = Field(description="A specific, testable explanation of the symptom")
    test_plan: str = Field(description="Which tools and what result would confirm or rule it out")


class HypothesisSet(BaseModel):
    """2 to 4 competing hypotheses, most likely first."""

    hypotheses: list[Hypothesis]


class Claim(BaseModel):
    text: str = Field(
        description="One factual statement. Quote numbers exactly as the tools returned them."
    )
    evidence_ids: list[str] = Field(
        description="Ids (E1, E2, ...) of the tool results that support this statement"
    )


class HypothesisVerdict(BaseModel):
    hypothesis_id: str
    verdict: Literal["confirmed", "ruled_out", "inconclusive"]
    reason: str
    evidence_ids: list[str] = Field(default_factory=list)


class Findings(BaseModel):
    """The agent's conclusion. Submitted once the evidence is sufficient."""

    root_cause_category: RootCauseCategory
    root_cause_component: str = Field(
        description="The failing component: a service (with version for a deploy), or a "
        "dependency name such as 'postgres', 'redis', 'stripe', 'gemini'"
    )
    root_cause: str = Field(description="One or two sentences: what failed and why")
    root_cause_evidence_ids: list[str] = Field(
        description="Evidence ids that directly show the root-cause component failing"
    )
    claims: list[Claim] = Field(description="3-6 supporting facts, each citing evidence")
    hypothesis_verdicts: list[HypothesisVerdict] = Field(
        description="A verdict for every hypothesis"
    )
    confidence: Confidence
    remediation: list[str] = Field(description="1-3 concrete next actions")


class Evidence(BaseModel):
    """One tool call and its result. Claims cite these by id."""

    id: str
    tool: str
    args: dict[str, Any]
    result: dict[str, Any]
    summary: str
    latency_ms: float


class ClaimCheck(BaseModel):
    text: str
    evidence_ids: list[str]
    supported: bool
    problems: list[str] = Field(default_factory=list)


class Verification(BaseModel):
    passed: bool
    root_cause_supported: bool
    root_cause_problems: list[str]
    checks: list[ClaimCheck]
    confidence: Confidence  # after calibration
    notes: list[str] = Field(default_factory=list)


class Report(BaseModel):
    incident_id: int | None
    title: str
    alert_text: str
    root_cause_category: RootCauseCategory
    root_cause_component: str
    root_cause: str
    confidence: Confidence
    model_confidence: Confidence
    supported_claims: list[ClaimCheck]
    removed_claims: list[ClaimCheck]
    hypothesis_verdicts: list[HypothesisVerdict]
    hypotheses: list[Hypothesis]
    remediation: list[str]
    evidence: list[dict[str, Any]]
    steps_used: int
    max_steps: int
    budget_exhausted: bool
    revisions: int
    verification_notes: list[str]
    markdown: str = ""
