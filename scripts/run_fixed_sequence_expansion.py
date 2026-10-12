#!/usr/bin/env python3
"""Run a fixed sequence expansion through the unified DCP-state graph API.

This is the smallest handwritten-pipeline CLI: every item in ``--plan`` becomes
one graph transition appended to canonical ``run.db``. Resume reconstructs the
last materialized incumbent from database facts.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from optimizer_pipeline import (  # noqa: E402
    DeterministicAnalyzer,
    DeterministicFlowExecutor,
    OutputValidator,
    OptimizationConfig,
)
from optimizer_pipeline.real_analysis_provider import build_real_analysis_provider  # noqa: E402
from optimizer_pipeline.run import bootstrap_run, resume_run  # noqa: E402
from scripts.run_frontier_expansion import MockExecutor, load_dotenv, mock_analysis_provider  # noqa: E402


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Run a fixed optimizer sequence as graph expansion")
    p.add_argument("input_dcp", type=Path)
    p.add_argument("--plan", nargs="+", required=True, help="Optimizer/flow names to run in order")
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--config", type=Path, default=REPO_ROOT / "configs" / "frontier_expansion_default.json")
    p.add_argument("--mock", action="store_true")
    p.add_argument("--no-dotenv", action="store_true")
    p.add_argument("--stop-on-reject", action="store_true", default=True)
    p.add_argument("--resume", action="store_true", help="Resume from canonical run.db")
    p.add_argument("--max-transitions", type=int, default=None, help="Run at most this many new transitions before leaving a resumable partial run")
    return p.parse_args()


async def main_async() -> int:
    args = parse_args()
    if not args.no_dotenv:
        load_dotenv(REPO_ROOT / ".env")
        load_dotenv(Path.cwd() / ".env")
    if args.mock and not args.input_dcp.exists():
        args.input_dcp.parent.mkdir(parents=True, exist_ok=True)
        args.input_dcp.write_text("mock input\n")
    if not args.input_dcp.exists():
        print(f"Input DCP not found: {args.input_dcp}", file=sys.stderr)
        return 2

    config = OptimizationConfig.from_json_file(args.config)
    analyzer = build_analyzer_for_args(args, config)
    if args.mock:
        executor = MockExecutor()
    else:
        try:
            from optimizer_pipeline.backends.xilinx import run_test_mode
        except Exception as exc:
            print(f"Error importing dcp_optimizer dependencies: {type(exc).__name__}: {exc}", file=sys.stderr)
            print("Configure an explicit isolated interpreter and tool environment, or rerun with --mock for local smoke tests.", file=sys.stderr)
            return 3
        executor = DeterministicFlowExecutor(run_test_mode=run_test_mode, debug=config.debug, max_nets=config.max_nets)
    validator = OutputValidator(config.validation_mode)

    context_factory = resume_run if args.resume else bootstrap_run
    ctx = await context_factory(
        input_dcp=args.input_dcp,
        run_dir=args.out,
        config=config,
        analyzer=analyzer,
        executor=executor,
        output_validator=validator,
        run_id=args.out.name,
        mode="fixed_sequence_expansion",
        max_eda_runs=len(args.plan),
        stop_on_reject=args.stop_on_reject,
    )

    rows = []
    new_transitions = 0
    partial_stop = False

    for idx, optimizer in enumerate(args.plan):
        expected_transition_id = f"node_{idx + 1:03d}_{ctx.safe_label(optimizer)}"
        if idx < ctx.next_transition_index:
            continue
        else:
            if args.max_transitions is not None and new_transitions >= max(0, int(args.max_transitions)):
                partial_stop = True
                break
            if ctx.stopped or ctx.budget.remaining_eda_runs <= 0:
                break
            record = await ctx.expand.transition(
                optimizer,
                source="fixed_sequence_expansion",
                reason="fixed sequence expansion",
            )
            new_transitions += 1
        outcome = record.outcome
        handling = record.handling
        row = {
            "transition_index": idx,
            "transition_id": record.intent.transition_id,
            "optimizer_name": optimizer,
            "skipped_existing": False,
            "accepted": bool(outcome.accepted_by_transition_executor) if outcome else False,
            "success": bool(outcome.success) if outcome else False,
            "validation_status": outcome.validation_status if outcome else None,
            "pure_qor_score": outcome.pure_qor_score if outcome else None,
            "input_dcp": record.intent.parent_dcp,
            "output_dcp": outcome.output_dcp if outcome else None,
            "rejection_reason": outcome.rejection_reason if outcome else None,
            "final_status": handling.final_status if handling else None,
            "schedule_stage": handling.schedule_stage if handling else None,
        }
        rows.append(row)
        print(json.dumps(row, sort_keys=True))
        if row["final_status"] != "accepted" and args.stop_on_reject:
            break

    if partial_stop or ctx.next_transition_index < len(args.plan):
        ctx.persist_state(strict_orphans=True)
        ctx.transition_sink.refresh_policy_exports()
        ctx.artifact_store.write_json(
            ctx.run_dir / "fixed_sequence_partial_summary.json",
            {
                "schema_version": "fixed_sequence_expansion_partial.v1",
                "input_dcp": str(args.input_dcp),
                "plan": args.plan,
                "transitions": rows,
                "final_dcp": str(ctx.current_dcp),
                "next_transition_index": ctx.next_transition_index,
                "remaining_plan": args.plan[ctx.next_transition_index:],
                "resumable": True,
            },
        )
        return 0

    ctx.finalize(
        summary_name="fixed_sequence_summary.json",
        extra={
            "input_dcp": str(args.input_dcp),
            "plan": args.plan,
            "transitions": rows,
            "final_dcp": str(ctx.current_dcp),
        },
    )
    return 0


def build_analyzer_for_args(args: argparse.Namespace, config: OptimizationConfig) -> DeterministicAnalyzer:
    if args.mock:
        return DeterministicAnalyzer(analysis_provider=mock_analysis_provider)
    return DeterministicAnalyzer(analysis_provider=build_real_analysis_provider(run_dir=args.out, config=config))


def main() -> None:
    raise SystemExit(asyncio.run(main_async()))


if __name__ == "__main__":
    main()
