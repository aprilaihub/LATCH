"""Safe intervention execution facade over the current run adapter."""

from __future__ import annotations

from typing import Any

from .controller import ContextInterventionExecutor, _fmax_from_metrics
from .strategy import InterventionSnapshot, InterventionStrategy


class InterventionCapability:
    """Apply a trigger strategy, then execute only guarded intervention paths."""

    def __init__(self, ctx: Any) -> None:
        self.ctx = ctx
        self._executor = ContextInterventionExecutor(ctx)

    def snapshot(self) -> InterventionSnapshot:
        best_id = getattr(self.ctx.state, "best_node_id", None)
        best = self.ctx.nodes.get(best_id) if best_id else self.ctx.current_node
        root_fmax = _fmax_from_metrics(dict(self.ctx.root_metrics or {}))
        best_fmax = _fmax_from_metrics(dict(getattr(best, "metrics", {}) or {}))
        delta = None if root_fmax is None or best_fmax is None else best_fmax - root_fmax
        return InterventionSnapshot(
            root_fmax_mhz=root_fmax,
            best_fmax_mhz=best_fmax,
            best_delta_fmax_mhz=delta,
            remaining_eda_runs=self.ctx.budget.remaining_eda_runs,
            finalized=bool(self.ctx.finalized),
        )

    async def run(self, strategy: InterventionStrategy, **options: Any):
        if not isinstance(strategy, InterventionStrategy):
            raise TypeError("intervention execution requires an InterventionStrategy")
        decision = strategy.decide(self.snapshot())
        return await self._executor.run(
            gate_decision=(decision.execute, decision.reason),
            **options,
        )
