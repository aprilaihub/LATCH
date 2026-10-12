"""Separated score and cost data models for research frontier expansion.

 deliberately keeps QoR, planner priority, execution efficiency and
analysis cost as separate concepts.  QoR is a property of the implemented DCP;
planner priority is an internal scheduling hint; runtime/analysis cost are
reported as efficiency/evidence metrics and must not silently change the QoR
leaderboard.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Optional

from ..models import to_jsonable


@dataclass
class PlannerPriority:
    """Internal scheduling score used by policies and scorers.

    This is intentionally not a QoR metric.  It may include reliability risk,
    retrieval evidence, expected runtime, uncertainty or online-analysis priors.
    """

    total_priority: float
    source: str = "unknown"
    components: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return to_jsonable(self)


@dataclass
class EfficiencyMetrics:
    """Execution-efficiency metrics reported separately from QoR."""

    node_runtime_seconds: Optional[float] = None
    path_runtime_seconds: Optional[float] = None
    wall_clock_seconds: Optional[float] = None
    run_index: Optional[int] = None
    runs_to_first_clean_improvement: Optional[int] = None
    runs_to_best_pure_qor: Optional[int] = None
    aubc_over_runs: Optional[float] = None
    aubc_over_wall_clock: Optional[float] = None

    def to_dict(self) -> Dict[str, Any]:
        return to_jsonable(self)


@dataclass
class AnalysisMetrics:
    """Online deep-analysis cost reported separately from QoR."""

    sessions: int = 0
    llm_calls: int = 0
    tool_calls: int = 0
    analysis_wall_time_seconds: float = 0.0
    report_probe_seconds: float = 0.0
    act_probe_count: int = 0
    candidate_prior_count: int = 0
    analysis_changed_ranking_count: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return to_jsonable(self)
