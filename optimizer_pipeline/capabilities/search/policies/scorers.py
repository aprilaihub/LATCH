"""Candidate priority adjustment components for expansion policies."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional


@dataclass
class CandidatePriorityAdjustment:
    priority_delta: float = 0.0
    risk_delta: float = 0.0
    confidence: float = 0.0
    reason: str = ""


class AnalysisPriorScorer:
    """Reads analysis priors from a policy request."""

    def priority_adjustment(self, *, node_id: str, flow: str, request: Dict[str, Any]) -> CandidatePriorityAdjustment:
        priors = request.get("analysis_priors") or []
        best: Optional[Dict[str, Any]] = None
        for item in priors:
            if not isinstance(item, dict):
                continue
            if item.get("node_id") == node_id and item.get("flow") == flow:
                if best is None or float(item.get("confidence") or 0.0) > float(best.get("confidence") or 0.0):
                    best = item
        if not best:
            return CandidatePriorityAdjustment()
        return CandidatePriorityAdjustment(
            priority_delta=_float(best.get("priority_delta"), 0.0),
            risk_delta=_float(best.get("risk_delta"), 0.0),
            confidence=_float(best.get("confidence"), 0.0),
            reason=str(best.get("reason") or "analysis_prior"),
        )


def _float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default
