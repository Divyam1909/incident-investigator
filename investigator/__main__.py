"""Command line interface.

python -m investigator seed [--reset]
python -m investigator run --scenario db_pool [--provider gemini]
python -m investigator eval [--provider mock|gemini]
python -m investigator serve
python -m investigator graph
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from investigator.config import get_settings
from investigator.scenarios import SCENARIOS

ROOT = Path(__file__).resolve().parent.parent


def cmd_seed(args: argparse.Namespace) -> None:
    from investigator.bootstrap import open_db
    from investigator.seed import reset, seed

    factory = open_db(get_settings(), auto_seed=False)
    if args.reset:
        reset(factory)
    ids = seed(factory, scale=args.scale)
    print(f"Seeded scenarios (scenario -> incident id): {ids}")


def cmd_run(args: argparse.Namespace) -> None:
    from investigator.agent.llm import build_llm
    from investigator.bootstrap import open_db, scenario_incident_id
    from investigator.runner import StepEvent, create_investigation, run_investigation

    settings = get_settings()
    factory = open_db(settings)
    llm, info = build_llm(settings, args.provider)
    incident_id = args.incident or scenario_incident_id(factory, args.scenario)
    max_steps = args.max_steps or settings.max_steps
    inv_id = create_investigation(factory, incident_id, info, max_steps)
    print(
        f"Investigation #{inv_id} of incident #{incident_id} with {info.provider}/{info.model}"
        f" (max {max_steps} tool calls)\n"
    )

    def show(seq: int, e: StepEvent) -> None:
        tag = f"{e.evidence_id} {e.tool_name}" if e.tool_name else e.node
        timing = f" ({e.latency_ms:.0f}ms)" if e.latency_ms is not None else ""
        print(f"[{seq:02d}] {tag:<26} {e.summary}{timing}")

    inv = run_investigation(factory, inv_id, llm, info, settings, on_event=show)
    print()
    if inv.status != "completed":
        print(f"Investigation failed: {inv.error}")
        sys.exit(1)
    print(inv.report_markdown)
    print(
        f"\nTokens: {inv.input_tokens} in / {inv.output_tokens} out, cost ${inv.cost_usd:.4f}, "
        f"latency {inv.latency_ms / 1000:.1f}s"
    )


def cmd_eval(args: argparse.Namespace) -> None:
    from investigator.evaluate import main as eval_main

    eval_main(
        provider=args.provider, out_dir=ROOT / "eval", max_steps=args.max_steps, pause_s=args.pause
    )


def cmd_serve(args: argparse.Namespace) -> None:
    import uvicorn

    uvicorn.run("investigator.api:app", host=args.host, port=args.port)


def cmd_graph(args: argparse.Namespace) -> None:
    from investigator.agent.graph import mermaid

    text = mermaid()
    out = ROOT / "docs" / "graph.mmd"
    out.write_text(text + "\n", encoding="utf-8")
    print(text)
    print(f"\nWritten to {out.relative_to(ROOT)}")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="investigator",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("seed", help="migrate the database and seed the four scenarios")
    p.add_argument("--reset", action="store_true", help="delete all data first")
    p.add_argument("--scale", type=float, default=1.0, help="traffic multiplier")
    p.set_defaults(func=cmd_seed)

    p = sub.add_parser("run", help="investigate one incident and print the report")
    target = p.add_mutually_exclusive_group(required=True)
    target.add_argument("--scenario", choices=sorted(SCENARIOS))
    target.add_argument("--incident", type=int, help="incident id")
    p.add_argument("--provider", choices=["mock", "gemini"], help="overrides LLM_PROVIDER")
    p.add_argument("--max-steps", type=int, help="overrides MAX_STEPS")
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("eval", help="run all scenarios and score root-cause accuracy")
    p.add_argument("--provider", choices=["mock", "gemini"], help="overrides LLM_PROVIDER")
    p.add_argument("--max-steps", type=int)
    p.add_argument(
        "--pause",
        type=float,
        default=0.0,
        help="seconds to wait between scenarios (free-tier rate limits)",
    )
    p.set_defaults(func=cmd_eval)

    p = sub.add_parser("serve", help="start the HTTP API")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8000)
    p.set_defaults(func=cmd_serve)

    p = sub.add_parser("graph", help="print the graph as Mermaid and save docs/graph.mmd")
    p.set_defaults(func=cmd_graph)

    args = parser.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
