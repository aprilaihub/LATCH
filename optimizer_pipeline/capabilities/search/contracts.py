"""Runtime boundary consumed by search strategies."""

from __future__ import annotations

from typing import Any, Dict, Optional, Protocol, runtime_checkable

from optimizer_pipeline.domain.transition import DcpTransitionRecord
from optimizer_pipeline.models import PipelineResult


@runtime_checkable
class SearchRuntime(Protocol):
    """Minimal state/execution surface required by fixed and frontier search."""

    @property
    def remaining_eda_runs(self) -> int: ...

    @property
    def stopped(self) -> bool: ...

    async def execute_transition(
        self,
        optimizer_name: str,
        *,
        source: str,
        reason: str,
        stop_on_reject: Optional[bool] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> DcpTransitionRecord: ...

    async def execute_frontier(
        self,
        *,
        policy: Any = "deterministic",
        max_transitions: Optional[int] = None,
        parallelism: Optional[int] = None,
        heuristic: bool = True,
        scheduler_mode: Optional[str] = None,
        run_dir_name: Optional[str] = None,
        source: str = "frontier_expansion",
        branching_factor: Optional[int] = None,
        beam_width: Optional[int] = None,
        batch_size: Optional[int] = None,
        max_depth: Optional[int] = None,
    ) -> Optional[PipelineResult]: ...
