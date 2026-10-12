"""Expansion strategies over the same DCP-state frontier model."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional


@dataclass(frozen=True)
class FixedSequenceExpansion:
    """Run an explicit sequence of optimizer flows from the current node."""

    flows: List[str]
    source: str = "fixed_sequence"
    stop_on_reject: Optional[bool] = None


@dataclass(frozen=True)
class SinglePathFrontierExpansion:
    """Expand one frontier path by ``depth`` transitions."""

    depth: int
    policy: str = "deterministic"
    source: str = "single_path_frontier"


@dataclass(frozen=True)
class FrontierExpansion:
    """Expand a frontier of DCP-state nodes.

    This is the expansion primitive.  Single-path behavior is represented by
    using frontier size 1, branching factor 1, batch size 1, and parallelism 1.
    """

    max_transitions: Optional[int] = None
    policy: str = "deterministic"
    parallelism: Optional[int] = None
    heuristic: bool = True
    scheduler_mode: Optional[str] = None
    run_dir_name: Optional[str] = None
    source: str = "frontier_expansion"
