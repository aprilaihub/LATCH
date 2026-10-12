"""Pipeline profile dispatch for the contest-compatible entrypoint."""

from __future__ import annotations

import importlib
from typing import Any, Awaitable, Callable, List

from optimizer_pipeline.apps.fpl_contest.analysis_profiles import resolved_analysis_profile


async def run_profile(ctx: Any, args: Any) -> Any:
    """Run the selected Analysis/Expansion profile."""

    pipeline = str(getattr(args, "pipeline", "contest_default") or "contest_default")
    if pipeline == "contest_default":
        return await profile_contest_default(ctx, args)
    if pipeline == "fixed":
        return await profile_fixed(ctx, args)
    if pipeline == "linear":
        return await profile_linear(ctx, args)
    if pipeline == "frontier":
        return await profile_frontier(ctx, args)
    if pipeline == "analysis_single_path":
        await maybe_run_analysis(ctx, args)
        return await profile_linear(ctx, args)
    if pipeline == "analysis_frontier":
        await maybe_run_analysis(ctx, args)
        return await profile_frontier(ctx, args)
    if pipeline == "analysis_single_path_frontier":
        await maybe_run_analysis(ctx, args)
        await profile_linear(ctx, args)
        return await profile_frontier(ctx, args, use_remaining_budget=True)
    if pipeline == "custom":
        return await profile_custom(ctx, args)
    raise ValueError(f"Unsupported pipeline profile: {pipeline}")


async def maybe_run_analysis(ctx: Any, args: Any) -> Any:
    profile = resolved_analysis_profile(args)
    if not profile.enabled:
        return None
    return await ctx.analysis.run_once(mode=profile.loop_mode, trigger_reason=profile.trigger_reason)


async def profile_contest_default(ctx: Any, args: Any) -> Any:
    depth = min(max(0, int(getattr(args, "single_path_depth", 3) or 0)), ctx.budget.remaining_eda_runs)
    if depth > 0:
        await ctx.expand.linear(depth=depth, policy="deterministic", source="contest_default_single_path_frontier")
    return await ctx.expand.frontier_if_budget(
        policy="deterministic",
        max_transitions=ctx.budget.remaining_eda_runs,
        parallelism=max(1, int(getattr(args, "parallelism", 1) or 1)),
        heuristic=True,
        source="contest_default_frontier",
    )


async def profile_fixed(ctx: Any, args: Any) -> Any:
    flows = parse_plan(getattr(args, "plan", None))
    if not flows:
        raise ValueError("--pipeline fixed requires --plan flow_a,flow_b")
    return await ctx.expand.fixed(flows, source="fixed_profile")


async def profile_linear(ctx: Any, args: Any) -> Any:
    depth = min(max(0, int(getattr(args, "single_path_depth", 3) or 0)), ctx.budget.remaining_eda_runs)
    return await ctx.expand.linear(depth=depth, policy=getattr(args, "policy", "deterministic"), source="single_path_frontier_profile")


async def profile_frontier(ctx: Any, args: Any, *, use_remaining_budget: bool = False) -> Any:
    max_transitions = ctx.budget.remaining_eda_runs if use_remaining_budget else int(getattr(args, "budget_runs", 0) or 0)
    return await ctx.expand.frontier_if_budget(
        policy=getattr(args, "policy", "deterministic"),
        max_transitions=max_transitions,
        parallelism=max(1, int(getattr(args, "parallelism", 1) or 1)),
        heuristic=True,
        source="frontier_profile",
    )


async def profile_custom(ctx: Any, args: Any) -> Any:
    target = getattr(args, "custom_pipeline", None)
    if not target:
        raise ValueError("--pipeline custom requires --custom-pipeline module:function")
    fn = load_custom_pipeline(str(target))
    result = fn(ctx)
    if hasattr(result, "__await__"):
        return await result
    return result


def parse_plan(raw: Any) -> List[str]:
    if raw is None:
        return []
    if isinstance(raw, (list, tuple)):
        values: List[str] = []
        for item in raw:
            values.extend(parse_plan(item))
        return values
    return [part.strip() for part in str(raw).replace(" ", ",").split(",") if part.strip()]


def load_custom_pipeline(spec: str) -> Callable[[Any], Awaitable[Any]]:
    if ":" not in spec:
        raise ValueError("Custom pipeline must use module:function syntax")
    module_name, function_name = spec.split(":", 1)
    module = importlib.import_module(module_name)
    fn = getattr(module, function_name)
    if not callable(fn):
        raise TypeError(f"Custom pipeline target is not callable: {spec}")
    return fn
