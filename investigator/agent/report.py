"""Builds the final report from verified findings. No LLM call: the text is assembled
from claims that passed verification, so nothing unverified reaches the reader."""

from __future__ import annotations

from typing import Any

from investigator.agent.schemas import Report
from investigator.agent.state import InvestigationState


def build_report(state: InvestigationState, *, max_steps: int) -> Report:
    findings, verification = state["findings"], state["verification"]
    assert findings is not None and verification is not None
    inc = state["incident"]
    evidence = state.get("evidence", [])
    report = Report(
        incident_id=inc["incident_id"],
        title=inc["title"],
        alert_text=inc["alert_text"],
        root_cause_category=findings.root_cause_category,
        root_cause_component=findings.root_cause_component,
        root_cause=findings.root_cause,
        confidence=verification.confidence,
        model_confidence=findings.confidence,
        supported_claims=[c for c in verification.checks if c.supported],
        removed_claims=[c for c in verification.checks if not c.supported],
        hypothesis_verdicts=findings.hypothesis_verdicts,
        hypotheses=state.get("hypotheses", []),
        remediation=findings.remediation,
        evidence=[
            {"id": e.id, "tool": e.tool, "args": e.args, "summary": e.summary} for e in evidence
        ],
        steps_used=state["steps"],
        max_steps=max_steps,
        budget_exhausted=state.get("budget_exhausted", False),
        revisions=state.get("revisions", 0),
        verification_notes=verification.notes
        + ([f"Root cause check: {p}" for p in verification.root_cause_problems]),
    )
    report.markdown = render_markdown(report)
    return report


def _cite(ids: list[str]) -> str:
    return f" [{', '.join(ids)}]" if ids else ""


def render_markdown(r: Report) -> str:
    statements = {h.id: h.statement for h in r.hypotheses}
    lines: list[str] = [
        f"# Incident report: {r.title}",
        "",
        f"**Alert:** {r.alert_text}",
        "",
        f"**Root cause ({r.root_cause_category}, confidence: {r.confidence}):** {r.root_cause}",
        f"**Component:** `{r.root_cause_component}`",
        "",
        "## Evidence",
    ]
    lines += [f"- {c.text}{_cite(c.evidence_ids)}" for c in r.supported_claims] or ["- none"]
    if r.hypothesis_verdicts:
        lines += ["", "## Hypotheses"]
        for v in r.hypothesis_verdicts:
            title = statements.get(v.hypothesis_id, "")
            lines.append(
                f"- **{v.hypothesis_id} {v.verdict.replace('_', ' ')}**"
                f"{': ' + title if title else ''} {v.reason}{_cite(v.evidence_ids)}"
            )
    if r.remediation:
        lines += ["", "## Next actions"] + [f"{i}. {a}" for i, a in enumerate(r.remediation, 1)]
    if r.removed_claims or r.verification_notes:
        lines += ["", "## Verification"]
        lines += [
            f'- Removed unsupported claim: "{c.text}" ({"; ".join(c.problems)})'
            for c in r.removed_claims
        ]
        lines += [f"- {n}" for n in r.verification_notes]
    lines += ["", "## Evidence index"]
    lines += [
        f"- **{e['id']}** `{e['tool']}({_args(e['args'])})`: {e['summary']}" for e in r.evidence
    ]
    lines += [
        "",
        f"_Steps used: {r.steps_used}/{r.max_steps}; revisions: {r.revisions}; "
        f"model confidence: {r.model_confidence}, after verification: {r.confidence}._",
    ]
    return "\n".join(lines)


def _args(args: dict[str, Any]) -> str:
    return ", ".join(f"{k}={v!r}" for k, v in args.items())
