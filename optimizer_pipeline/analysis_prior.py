"""Analysis-prior utilities shared by policies and strategy scorers."""

from __future__ import annotations

from typing import Any, Dict, Tuple


def find_analysis_prior(request: Dict[str, Any], node_id: str, flow: str) -> Dict[str, Any]:
    priors = request.get("analysis_candidate_priors") or request.get("analysis_priors") or []
    best: Dict[str, Any] = {}
    best_conf = -1.0
    for prior in priors if isinstance(priors, list) else []:
        if not isinstance(prior, dict):
            continue
        p_node = str(prior.get("target_node_id") or prior.get("node_id") or "")
        p_flow = str(prior.get("flow") or prior.get("candidate_flow") or "")
        if p_node != str(node_id) or p_flow != str(flow):
            continue
        conf = _confidence_value(prior.get("confidence"))
        if conf >= best_conf:
            best = prior
            best_conf = conf
    return best


def apply_analysis_prior(base_priority: float, request: Dict[str, Any], node_id: str, flow: str) -> Tuple[float, Dict[str, Any]]:
    prior = find_analysis_prior(request, node_id, flow)
    if not prior:
        return float(base_priority), {}
    try:
        priority_delta = float(prior.get("priority_delta") or 0.0)
    except (TypeError, ValueError):
        priority_delta = 0.0
    try:
        risk_delta = float(prior.get("risk_delta") or 0.0)
    except (TypeError, ValueError):
        risk_delta = 0.0
    adjusted = max(0.0, min(1.0, float(base_priority) + priority_delta - risk_delta))
    return adjusted, {
        "source": "analysis_prior",
        "priority_delta": priority_delta,
        "risk_delta": risk_delta,
        "confidence": prior.get("confidence"),
        "reason": prior.get("reason"),
        "analysis_session_id": prior.get("analysis_session_id") or prior.get("source_analysis_session_id"),
        "base_priority": base_priority,
        "adjusted_priority": adjusted,
    }


def _confidence_value(value: Any) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value or "").lower()
    return {"low": 0.3, "medium": 0.6, "high": 0.9}.get(text, 0.0)
