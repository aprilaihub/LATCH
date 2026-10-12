"""Stop policies for budgeted online deep analysis."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .schemas import AnalysisStopDecision


@dataclass
class AnalysisSessionState:
    elapsed_sec: float
    budget_sec: float
    tool_calls: int
    report_probe_calls: int
    last_information_gain: str = "medium"
    ranking_stable: bool = False
    unresolved_disagreement: bool = False


class AnalysisStopPolicy:
    name = "base"

    def should_continue(self, session: AnalysisSessionState) -> AnalysisStopDecision:
        raise NotImplementedError


class TimePressureStopPolicy(AnalysisStopPolicy):
    """Local continuation pressure; it never enters QoR or global scoring."""

    name = "time_pressure_v1"

    def should_continue(self, session: AnalysisSessionState) -> AnalysisStopDecision:
        if session.budget_sec <= 0:
            return AnalysisStopDecision(False, "invalid_budget", confidence=1.0)
        frac = session.elapsed_sec / session.budget_sec
        if frac >= 1.0:
            return AnalysisStopDecision(False, "hard_budget_stop", confidence=1.0)
        if session.tool_calls <= 0:
            return AnalysisStopDecision(True, "initial_evidence_needed", confidence=0.1)
        if frac >= 0.80:
            if session.unresolved_disagreement and not session.ranking_stable:
                return AnalysisStopDecision(True, "strong_time_pressure_but_decision_may_change", confidence=0.55)
            return AnalysisStopDecision(False, "strong_time_pressure_ranking_stable", confidence=0.85)
        if frac >= 0.55:
            if session.unresolved_disagreement or session.last_information_gain == "high":
                return AnalysisStopDecision(True, "moderate_time_pressure_high_value", confidence=0.55)
            return AnalysisStopDecision(False, "moderate_time_pressure_low_marginal_gain", confidence=0.75)
        if session.ranking_stable and session.last_information_gain == "low":
            return AnalysisStopDecision(False, "early_stop_candidate_ranking_stable", confidence=0.70)
        return AnalysisStopDecision(True, "budget_available", confidence=0.35)
