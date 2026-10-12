"""Backend-neutral strategy orchestration over the search runtime contract."""

from __future__ import annotations

from typing import Any, Optional

from optimizer_pipeline.models import PipelineResult

from .contracts import SearchRuntime
from .strategies import FixedSequenceExpansion, FrontierExpansion, SinglePathFrontierExpansion


class ExpansionController:
    """Compose fixed, single-path, and frontier search without context access."""

    def __init__(self, runtime: SearchRuntime) -> None:
        if not isinstance(runtime, SearchRuntime):
            raise TypeError("ExpansionController requires a SearchRuntime")
        self.runtime = runtime

    async def run(self, strategy: Any, *, policy: Optional[Any] = None) -> Any:
        if isinstance(strategy, FixedSequenceExpansion):
            return await self.fixed(
                strategy.flows,
                source=strategy.source,
                stop_on_reject=strategy.stop_on_reject,
            )
        if isinstance(strategy, SinglePathFrontierExpansion):
            return await self.linear(
                strategy.depth,
                policy=policy or strategy.policy,
                source=strategy.source,
            )
        if isinstance(strategy, FrontierExpansion):
            return await self.frontier(
                policy=policy or strategy.policy,
                max_transitions=strategy.max_transitions,
                parallelism=strategy.parallelism,
                heuristic=strategy.heuristic,
                scheduler_mode=strategy.scheduler_mode,
                run_dir_name=strategy.run_dir_name,
                source=strategy.source,
            )
        raise TypeError(f"Unsupported expansion strategy: {type(strategy).__name__}")

    async def fixed(
        self,
        flows,
        *,
        source: str = "fixed_sequence",
        stop_on_reject: Optional[bool] = None,
    ):
        records = []
        for flow in list(flows):
            if self.runtime.stopped or self.runtime.remaining_eda_runs <= 0:
                break
            records.append(
                await self.transition(
                    flow,
                    source=source,
                    reason="fixed sequence expansion",
                    stop_on_reject=stop_on_reject,
                )
            )
        return records

    async def linear(
        self,
        depth: int,
        *,
        policy: Any = "deterministic",
        source: str = "single_path_frontier",
    ) -> Optional[PipelineResult]:
        requested = min(max(0, int(depth)), self.runtime.remaining_eda_runs)
        if requested <= 0 or self.runtime.stopped:
            return None
        return await self.frontier(
            policy=policy,
            max_transitions=requested,
            parallelism=1,
            heuristic=True,
            scheduler_mode="async",
            source=source,
            branching_factor=1,
            beam_width=1,
            batch_size=1,
            max_depth=requested,
        )

    async def transition(
        self,
        optimizer_name: str,
        *,
        source: str = "manual_transition",
        reason: str = "explicit graph expansion transition",
        stop_on_reject: Optional[bool] = None,
        metadata=None,
    ):
        return await self.runtime.execute_transition(
            optimizer_name,
            source=source,
            reason=reason,
            stop_on_reject=stop_on_reject,
            metadata=metadata,
        )

    async def frontier(self, **kwargs: Any) -> Optional[PipelineResult]:
        return await self.runtime.execute_frontier(**kwargs)

    async def frontier_if_budget(self, **kwargs: Any) -> Optional[PipelineResult]:
        if self.runtime.remaining_eda_runs <= 0:
            return None
        return await self.frontier(**kwargs)
