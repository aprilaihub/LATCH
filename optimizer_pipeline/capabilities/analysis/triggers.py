"""OOP trigger policies for online deep analysis."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, Optional

from .schemas import AnalysisAgentConfig, AnalysisTriggerDecision


@dataclass
class SearchStateView:
    run_count: int
    scheduled_run_count: int
    max_total_runs: int
    nodes: Dict[str, Any]
    frontier: Iterable[Any] = field(default_factory=list)
    last_best_node_id: Optional[str] = None


class AnalysisTriggerPolicy:
    name = "base"

    def should_analyze(self, state: SearchStateView, *, memory: Any) -> AnalysisTriggerDecision:
        raise NotImplementedError


class NaiveOnlineAnalysisTriggerPolicy(AnalysisTriggerPolicy):
    """First online trigger: root first, then sparse key-node triggers.

    It is intentionally parameterized, not hardcoded into the runner. Later we
    can replace it with uncertainty/retrieval-disagreement/learned triggers.
    """

    name = "naive_v1"

    def __init__(self, config: AnalysisAgentConfig):
        self.config = config

    def should_analyze(self, state: SearchStateView, *, memory: Any) -> AnalysisTriggerDecision:
        if not self.config.enabled or self.config.max_nodes_per_search <= 0:
            return AnalysisTriggerDecision(False, None, "analysis_disabled")
        if len(getattr(memory, "sessions", [])) >= self.config.max_nodes_per_search:
            return AnalysisTriggerDecision(False, None, "analysis_run_budget_exhausted")

        root = state.nodes.get("node_000_root")
        if root is not None and not _was_analyzed(memory, getattr(root, "node_id", "")):
            return AnalysisTriggerDecision(True, getattr(root, "node_id"), "root_online_analysis", priority=1.0, budget_profile="root")

        progress = state.run_count / max(1, state.max_total_runs)
        if progress < 0.35:
            candidate = _first_unanalyzed_best_so_far(state, memory)
            if candidate is not None:
                return AnalysisTriggerDecision(True, getattr(candidate, "node_id"), "early_best_so_far", priority=0.7, budget_profile="node")

        # Later stages are stricter: analyze mainly after failed/warning nodes or plateau.
        failed = _first_unanalyzed_validation_event(state, memory)
        if failed is not None and progress < 0.85:
            return AnalysisTriggerDecision(True, getattr(failed, "node_id"), "validation_event_diagnosis", priority=0.8, budget_profile="node")

        if state.run_count >= self.config.no_improvement_window and progress >= 0.35:
            candidate = _first_unanalyzed_best_so_far(state, memory)
            if candidate is not None:
                return AnalysisTriggerDecision(True, getattr(candidate, "node_id"), "no_improvement_or_plateau_check", priority=0.6, budget_profile="node")

        return AnalysisTriggerDecision(False, None, "no_trigger")


def _was_analyzed(memory: Any, node_id: str) -> bool:
    return any(getattr(session, "node_id", None) == node_id for session in getattr(memory, "sessions", []))


def _first_unanalyzed_best_so_far(state: SearchStateView, memory: Any) -> Optional[Any]:
    completed = [
        n for n in state.nodes.values()
        if getattr(getattr(n, "status", None), "value", getattr(n, "status", None)) == "COMPLETED" and getattr(n, "depth", 0) > 0
    ]
    if not completed:
        return None
    completed.sort(key=lambda n: getattr(n, "score", None) if getattr(n, "score", None) is not None else float("-inf"), reverse=True)
    for node in completed:
        if not _was_analyzed(memory, getattr(node, "node_id", "")):
            return node
    return None


def _first_unanalyzed_validation_event(state: SearchStateView, memory: Any) -> Optional[Any]:
    interesting = []
    for node in state.nodes.values():
        status = getattr(node, "validation_status", None)
        if status in {"INVALID", "WARNING", "FALLBACK_USED", "REGRESSED"} and getattr(node, "depth", 0) > 0:
            interesting.append(node)
    interesting.sort(key=lambda n: getattr(n, "depth", 0))
    for node in interesting:
        if not _was_analyzed(memory, getattr(node, "node_id", "")):
            return node
    return None
