"""Offline contest-entry fixtures for composition and failure handling."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from optimizer_pipeline.models import ExecutionResult


async def analysis_then_single_path_then_frontier(ctx: Any) -> Any:
    """Run optional analysis, a short single-path frontier warmup, then frontier expansion."""

    await ctx.analysis.run_once(mode="mock_llm_closed_loop", trigger_reason="custom_pipeline_analysis")
    await ctx.expand.linear(depth=2, policy="deterministic", source="custom_single_path_frontier_warmup")
    await ctx.expand.frontier_if_budget(policy="deterministic", parallelism=1, source="custom_frontier_tail")
    return None


async def pblock_retime_then_frontier_demo(ctx: Any) -> Any:
    """Demonstrate a composed fixed sequence followed by budgeted frontier expansion."""

    await ctx.analysis.run_once(mode="static_plan", trigger_reason="custom_pipeline_static_analysis")
    await ctx.expand.fixed(["pblock", "retime"], source="custom_pblock_retime_demo")
    await ctx.expand.frontier_if_budget(policy="deterministic", parallelism=1, source="custom_frontier_tail")
    return None


async def raise_before_transition(ctx: Any) -> Any:
    """Exercise early-stop handling before any transition is scheduled."""

    raise RuntimeError("intentional early stop before transition")


async def crash_after_scheduled(ctx: Any) -> Any:
    """Exercise orphaned scheduled-record handling after journal append."""

    async def _crash_after_schedule(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("intentional crash after scheduled transition")

    ctx.transition_executor.run = _crash_after_schedule
    await ctx.expand.transition("phys_opt", source="fault_injection", reason="crash after scheduled append")


async def invalid_transition(ctx: Any) -> Any:
    """Exercise rejected transition handling without publishing a bad output."""

    class InvalidExecutor:
        async def execute(self, input_dcp: Path, output_dcp: Path, run_dir: Path, flow: str) -> ExecutionResult:
            run_dir.mkdir(parents=True, exist_ok=True)
            return ExecutionResult(
                success=False,
                flow=flow,
                input_dcp=str(input_dcp),
                output_dcp=str(output_dcp),
                run_dir=str(run_dir),
                summary_path=None,
                metrics={"total_runtime_seconds": 0.01},
                exit_code=1,
                error="intentional invalid transition",
            )

    ctx.transition_executor.executor = InvalidExecutor()
    await ctx.expand.transition("phys_opt", source="fault_injection", reason="invalid transition fixture", stop_on_reject=False)
