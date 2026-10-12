"""Small installed examples; extend by supplying your own module:function."""


async def one_physical_transition(ctx):
    """Require one recipe execution accepted by the shared executor."""
    records = await ctx.expand.fixed(["phys_opt"], source="custom_pipeline_example")
    if len(records) != 1:
        raise RuntimeError("required phys_opt transition was not executed")
    outcome = records[0].outcome
    if outcome is None or not outcome.executed or not outcome.success or not outcome.accepted_by_transition_executor:
        reason = outcome.rejection_reason if outcome is not None else "missing outcome"
        raise RuntimeError(f"required phys_opt transition failed or was rejected: {reason}")


async def analysis_then_fixed(ctx):
    """Compose analysis and execution without a contest or research condition."""
    await ctx.analysis.run_once(mode="static_plan", trigger_reason="custom_pipeline_example")
    await one_physical_transition(ctx)
