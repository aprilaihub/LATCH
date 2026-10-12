"""Intervention trigger strategies, separate from safe execution."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Protocol, runtime_checkable


@dataclass(frozen=True)
class InterventionSnapshot:
    root_fmax_mhz: Optional[float]
    best_fmax_mhz: Optional[float]
    best_delta_fmax_mhz: Optional[float]
    remaining_eda_runs: int
    finalized: bool


@dataclass(frozen=True)
class InterventionDecision:
    execute: bool
    reason: str


@runtime_checkable
class InterventionStrategy(Protocol):
    def decide(self, snapshot: InterventionSnapshot) -> InterventionDecision: ...


@dataclass(frozen=True)
class NoImprovementInterventionStrategy:
    """Trigger always or when the main profile did not clear an Fmax delta."""

    trigger: str = "no_improvement"
    min_main_delta_mhz: float = 0.1

    def decide(self, snapshot: InterventionSnapshot) -> InterventionDecision:
        trigger = str(self.trigger or "no_improvement").strip().lower()
        if trigger == "always":
            return InterventionDecision(True, "always_trigger")
        if trigger != "no_improvement":
            return InterventionDecision(False, "unsupported_trigger")
        if snapshot.best_delta_fmax_mhz is None:
            return InterventionDecision(False, "missing_fmax_for_no_improvement_gate")
        if float(snapshot.best_delta_fmax_mhz) <= max(0.0, float(self.min_main_delta_mhz)):
            return InterventionDecision(True, "no_improvement_threshold_met")
        return InterventionDecision(False, "main_profile_already_improved")
