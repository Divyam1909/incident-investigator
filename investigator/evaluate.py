"""Runs every labelled scenario and scores the agent against ground truth.

A run is correct when the root-cause category matches exactly AND the named
component matches one of the scenario's accepted aliases. Both are needed:
"dependency_timeout" is useless if it blames stripe instead of gemini.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path

from investigator.agent.llm import build_llm
from investigator.bootstrap import open_db, scenario_incident_id
from investigator.config import Settings, get_settings
from investigator.runner import create_investigation, run_investigation
from investigator.scenarios import SCENARIOS, Scenario


@dataclass
class EvalRow:
    scenario: str
    expected: str
    predicted: str
    correct: bool
    confidence: str | None
    tool_calls: int
    input_tokens: int
    output_tokens: int
    latency_s: float
    cost_usd: float
    removed_claims: int
    investigation_id: int
    status: str


def is_correct(s: Scenario, category: str | None, component: str | None) -> bool:
    component = (component or "").lower()
    return category == s.root_cause_category and any(a in component for a in s.component_aliases)


def to_markdown(rows: list[EvalRow], provider: str, model: str) -> str:
    correct = sum(r.correct for r in rows)
    lines = [
        f"**Provider:** {provider} / `{model}`. **Accuracy: {correct}/{len(rows)}**",
        "",
        "| Scenario | Ground truth | Agent's root cause | Correct | Confidence | Tool calls "
        "| Tokens in / out | Latency | Cost (USD) | Claims removed by verify |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for r in rows:
        lines.append(
            f"| {r.scenario} | {r.expected} | {r.predicted} | {'yes' if r.correct else '**no**'}"
            f" | {r.confidence or '-'} | {r.tool_calls} | {r.input_tokens:,} / "
            f"{r.output_tokens:,} | {r.latency_s:.1f}s | {r.cost_usd:.4f} | {r.removed_claims} |"
        )
    return "\n".join(lines)


def main(
    provider: str | None,
    out_dir: Path,
    max_steps: int | None = None,
    settings: Settings | None = None,
) -> list[EvalRow]:
    settings = settings or get_settings()
    factory = open_db(settings)
    llm, info = build_llm(settings, provider)
    rows: list[EvalRow] = []
    for key, s in SCENARIOS.items():
        inv_id = create_investigation(
            factory, scenario_incident_id(factory, key), info, max_steps or settings.max_steps
        )
        print(f"Running {key} (investigation #{inv_id}) ...", flush=True)
        inv = run_investigation(factory, inv_id, llm, info, settings)
        report = inv.report or {}
        rows.append(
            EvalRow(
                scenario=key,
                expected=f"{s.root_cause_category} / {s.root_cause_component}",
                predicted=f"{inv.root_cause_category} / {inv.root_cause_component}"
                if inv.status == "completed"
                else f"failed: {(inv.error or '')[:80]}",
                correct=is_correct(s, inv.root_cause_category, inv.root_cause_component),
                confidence=inv.confidence,
                tool_calls=inv.steps,
                input_tokens=inv.input_tokens,
                output_tokens=inv.output_tokens,
                latency_s=round((inv.latency_ms or 0) / 1000, 2),
                cost_usd=inv.cost_usd,
                removed_claims=len(report.get("removed_claims", [])),
                investigation_id=inv.id,
                status=inv.status,
            )
        )

    md = to_markdown(rows, info.provider, info.model)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / f"results-{info.provider}.md").write_text(md + "\n", encoding="utf-8")
    (out_dir / f"results-{info.provider}.json").write_text(
        json.dumps([asdict(r) for r in rows], indent=2) + "\n", encoding="utf-8"
    )
    print()
    print(md)
    return rows
