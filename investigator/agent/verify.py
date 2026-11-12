"""Deterministic verification of the agent's findings.

The verify node does not ask another LLM whether the report "looks right".
It checks, in code, that every claim is grounded in what the tools actually
returned:

1. Every claim cites at least one evidence id, and every cited id exists.
2. Every number, version and clock time in a claim appears in the evidence it
   cites (with unit conversion and rounding tolerance: "2.4s" matches 2412.3 ms,
   "28%" matches 28.43, "x15" matches 2684/171 as a ratio). The tolerance is
   relative, so a small invented number cannot hide next to a different one.
3. The root-cause component is named in the evidence cited for the root cause.

Then confidence is calibrated: the model's own confidence can only go down.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from typing import Any

from investigator.agent.schemas import (
    ClaimCheck,
    Confidence,
    Evidence,
    Findings,
    Verification,
)

_DATE = re.compile(r"\b\d{4}-\d{2}-\d{2}(?:T)?")
_TIME = re.compile(r"(?<![\d:.])([01]?\d|2[0-3]):([0-5]\d)(?::[0-5]\d)?(?![\d])")
_VERSION = re.compile(r"(?<![\w.])v?(\d+\.\d+\.\d+)(?![\w.]*\d)")
# A number not glued to a word on the left (skips p95, E3, H2, gpt-4) with an optional unit.
_NUMBER = re.compile(
    r"(?<![A-Za-z_\d.\-])(\d+(?:,\d{3})*(?:\.\d+)?)\s*(ms|s\b|sec|seconds|%|x\b|×|times)?"
)
_RATIO_PREFIX = re.compile(r"(?:x|×)\s*(\d+(?:\.\d+)?)")

_LEVELS: list[Confidence] = ["low", "medium", "high"]


def _cap(level: Confidence, ceiling: Confidence) -> Confidence:
    return _LEVELS[min(_LEVELS.index(level), _LEVELS.index(ceiling))]


def _walk(value: Any) -> Iterable[Any]:
    if isinstance(value, dict):
        for v in value.values():
            yield from _walk(v)
    elif isinstance(value, list):
        for v in value:
            yield from _walk(v)
    else:
        yield value


class EvidenceFacts:
    """Numbers, clock times and text extracted from a set of evidence items."""

    def __init__(self, items: list[Evidence]):
        self.text = " ".join(json.dumps(e.result, default=str) for e in items).lower()
        numbers: set[float] = set()
        minutes: set[int] = set()
        for e in items:
            for leaf in _walk(e.result):
                if isinstance(leaf, bool):
                    continue
                if isinstance(leaf, int | float):
                    numbers.add(float(leaf))
                elif isinstance(leaf, str):
                    for m in _TIME.finditer(leaf):
                        minutes.add(int(m[1]) * 60 + int(m[2]))
                    stripped = _TIME.sub(" ", _DATE.sub(" ", leaf))
                    for m in _NUMBER.finditer(stripped):
                        numbers.add(float(m[1].replace(",", "")))
        self.numbers = numbers
        self.minutes = minutes

    def has_number(self, value: float, unit: str | None, step: float = 1.0) -> bool:
        """``step`` is the claim's rounding granularity (0.1 for "2.4"), in the claim's unit.

        Tools report durations in ms and rates as percentages, so the only
        conversion needed is seconds -> ms.
        """
        scale = 1000.0 if unit in ("s", "sec", "seconds") else 1.0
        if any(_close(value * scale, n, step * scale) for n in self.numbers):
            return True
        if unit in ("x", "×", "times"):
            return self.has_ratio(value, step)
        return False

    def has_ratio(self, value: float, step: float = 1.0) -> bool:
        nums = [n for n in self.numbers if n > 0]
        return any(_close(value, a / b, step) for a in nums for b in nums if a > b)

    def has_time(self, minute_of_day: int) -> bool:
        return any(abs(minute_of_day - m) <= 2 for m in self.minutes)


def _close(claimed: float, actual: float, step: float) -> bool:
    """Equal up to the claim's own rounding, or within 6% ("2.4s" ~ 2412.3ms, "30%" ~ 28.43)."""
    return abs(claimed - actual) <= max(step / 2, 0.06 * abs(actual))


def _step(number: str) -> float:
    """Rounding granularity of a written number: "2.4" -> 0.1, "30" -> 1."""
    return 10.0 ** -len(number.split(".")[1]) if "." in number else 1.0


def check_text(text: str, facts: EvidenceFacts) -> list[str]:
    """Problems with the specifics in one statement, judged against its evidence."""
    problems: list[str] = []
    for m in _VERSION.finditer(text):
        if m[1] not in facts.text:
            problems.append(f"version {m[1]} not in cited evidence")
    without_versions = _VERSION.sub(" ", text)
    for m in _TIME.finditer(without_versions):
        if not facts.has_time(int(m[1]) * 60 + int(m[2])):
            problems.append(f"time {m[0]} not in cited evidence")
    rest = _TIME.sub(" ", _DATE.sub(" ", without_versions))
    for m in _RATIO_PREFIX.finditer(rest):
        if not facts.has_number(float(m[1]), "x", _step(m[1])):
            problems.append(f"ratio x{m[1]} not in cited evidence")
    rest = _RATIO_PREFIX.sub(" ", rest)
    for m in _NUMBER.finditer(rest):
        value, unit = float(m[1].replace(",", "")), (m[2] or None)
        if unit is None and value < 10:
            continue  # small counts ("3 services", "2 minutes") are not checked
        if not facts.has_number(value, unit, _step(m[1].replace(",", ""))):
            problems.append(f"number {m[0].strip()} not in cited evidence")
    return problems


def _resolve(ids: list[str], by_id: dict[str, Evidence]) -> tuple[list[Evidence], list[str]]:
    problems = [f"cites unknown evidence id {i}" for i in ids if i not in by_id]
    if not ids:
        problems.append("cites no evidence")
    return [by_id[i] for i in ids if i in by_id], problems


def _component_terms(component: str) -> list[str]:
    words = re.findall(r"[a-z0-9][a-z0-9.\-]{2,}", component.lower())
    stop = {"the", "and", "connection", "service", "api", "pool", "provider", "outage", "cache"}
    terms = [w for w in words if w not in stop]
    return terms or words


def verify_findings(
    findings: Findings, evidence: list[Evidence], *, budget_exhausted: bool = False
) -> Verification:
    by_id = {e.id: e for e in evidence}

    checks: list[ClaimCheck] = []
    for claim in findings.claims:
        items, problems = _resolve(claim.evidence_ids, by_id)
        if items:
            problems += check_text(claim.text, EvidenceFacts(items))
        checks.append(
            ClaimCheck(
                text=claim.text,
                evidence_ids=claim.evidence_ids,
                supported=not problems,
                problems=problems,
            )
        )

    rc_items, rc_problems = _resolve(findings.root_cause_evidence_ids, by_id)
    if rc_items:
        facts = EvidenceFacts(rc_items)
        terms = _component_terms(findings.root_cause_component)
        if not any(t in facts.text for t in terms):
            rc_problems.append(
                f"component {findings.root_cause_component!r} does not appear in the cited evidence"
            )
        rc_problems += check_text(findings.root_cause, facts)
    root_ok = not rc_problems

    supported = [c for c in checks if c.supported]
    notes: list[str] = []
    confidence: Confidence = findings.confidence
    if not root_ok:
        confidence = "low"
        notes.append("Root cause is not backed by the cited evidence; confidence set to low.")
    if len(supported) < len(checks):
        confidence = _cap(confidence, "medium")
        notes.append(
            f"{len(checks) - len(supported)} claim(s) failed verification and were "
            "removed from the report."
        )
    if len(supported) < 2:
        confidence = _cap(confidence, "medium")
        notes.append("Fewer than two verified supporting claims.")
    if budget_exhausted:
        confidence = _cap(confidence, "medium")
        notes.append("Step budget ran out before the agent finished on its own.")
    if findings.root_cause_category == "unknown":
        confidence = "low"

    return Verification(
        passed=root_ok and len(supported) == len(checks) and bool(checks),
        root_cause_supported=root_ok,
        root_cause_problems=rc_problems,
        checks=checks,
        confidence=confidence,
        notes=notes,
    )


def feedback_message(v: Verification) -> str:
    """What the agent is told when verify sends it back to fix its findings."""
    lines = ["Verification failed. Fix these problems and call submit_findings again:"]
    lines += [f"- root cause: {p}" for p in v.root_cause_problems]
    for c in v.checks:
        if not c.supported:
            lines.append(f"- claim {c.text!r}: {'; '.join(c.problems)}")
    lines.append(
        "Quote numbers exactly as the tools returned them and cite the evidence ids "
        "that contain them. You may call more tools if you need more evidence."
    )
    return "\n".join(lines)
