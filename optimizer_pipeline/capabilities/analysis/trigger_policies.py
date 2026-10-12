"""Pluggable trigger policies for online deep analysis."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Protocol

from .schemas import AnalysisTriggerDecision


class AnalysisTriggerPolicy(Protocol):
    def should_analyze(self, *, node: Any, state: Any, runner_view: Dict[str, Any]) -> AnalysisTriggerDecision:
        ...


@dataclass
class NaiveOnlineAnalysisTriggerPolicy:
    """Root-first sparse trigger policy.

    Early search is permissive; later search requires warning/invalid/fallback,
    no-improvement, or disagreement signals supplied by the runner.
    """

    root_always: bool = True
    early_node_budget: int = 2
    late_node_budget: int = 2
    max_nodes_per_search: int = 5
    no_improvement_window: int = 5
    frontier_gap_threshold: float = 0.05

    def should_analyze(self, *, node: Any, state: Any, runner_view: Dict[str, Any]) -> AnalysisTriggerDecision:
        node_id = str(getattr(node, "node_id", ""))
        sessions_done = int(runner_view.get("analysis_sessions_done") or 0)
        if sessions_done >= self.max_nodes_per_search:
            return AnalysisTriggerDecision(False, node_id, "max_analysis_sessions_reached")
        already = set(runner_view.get("analyzed_node_ids") or [])
        if node_id in already:
            return AnalysisTriggerDecision(False, node_id, "node_already_analyzed")
        if getattr(node, "depth", None) == 0 and self.root_always:
            return AnalysisTriggerDecision(True, node_id, "root_online_analysis", priority=1.0, budget_profile="root")

        completed_runs = int(runner_view.get("run_count") or 0)
        max_runs = max(1, int(runner_view.get("max_total_runs") or 1))
        progress = completed_runs / max_runs
        validation_status = str(getattr(node, "validation_status", "") or "")

        if progress < 0.30 and getattr(node, "depth", 99) <= 1 and sessions_done < 1 + self.early_node_budget:
            return AnalysisTriggerDecision(True, node_id, "early_depth1_key_node", priority=0.65, budget_profile="node")
        if validation_status in {"WARNING", "FALLBACK_USED", "INVALID"}:
            return AnalysisTriggerDecision(True, node_id, "validation_boundary_node", priority=0.80, budget_profile="node")
        if runner_view.get("no_improvement_window_hit"):
            return AnalysisTriggerDecision(True, node_id, "no_improvement_window", priority=0.75, budget_profile="node")
        if runner_view.get("frontier_disagreement_high"):
            return AnalysisTriggerDecision(True, node_id, "frontier_or_prior_disagreement", priority=0.70, budget_profile="node")
        return AnalysisTriggerDecision(False, node_id, "naive_policy_no_trigger")
