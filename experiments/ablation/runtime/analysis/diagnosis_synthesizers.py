"""Diagnosis synthesizers for online deep analysis."""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional, Protocol

from .schemas import AnalysisToolCall, CandidatePrior, CandidatePriority, NodeDiagnosis


class DiagnosisSynthesizer(Protocol):
    def synthesize(self, *, node: Any, tool_calls: List[AnalysisToolCall], allowed_flows: List[str], session_id: str) -> NodeDiagnosis:
        ...


class HeuristicDiagnosisSynthesizer:
    """Small deterministic synthesizer used for smoke tests and no-API runs."""

    def synthesize(self, *, node: Any, tool_calls: List[AnalysisToolCall], allowed_flows: List[str], session_id: str) -> NodeDiagnosis:
        evidence: List[Dict[str, Any]] = []
        risks: List[Dict[str, Any]] = []
        route_delay_ratio = None
        dominant_path_type = None
        deterministic_scores: List[Dict[str, Any]] = []
        retrieval_by_flow: Dict[str, Any] = {}
        for call in tool_calls:
            if not call.success or not isinstance(call.output, dict):
                continue
            evidence.append({"tool": call.tool_name, "summary": _summarize_tool_output(call.output)})
            payload = call.output
            cpath = payload.get("critical_path_features") if isinstance(payload.get("critical_path_features"), dict) else {}
            timing = payload.get("timing") if isinstance(payload.get("timing"), dict) else {}
            if cpath:
                route_delay_ratio = _first_float(route_delay_ratio, cpath.get("route_delay_ratio"))
                dominant_path_type = dominant_path_type or cpath.get("dominant_path_type")
            if call.tool_name == "load_analysis_context":
                cands = payload.get("candidate_flows") if isinstance(payload.get("candidate_flows"), dict) else {}
                raw = cands.get("deterministic_scores") if isinstance(cands.get("deterministic_scores"), list) else []
                if not raw and isinstance(payload.get("flow_scores"), list):
                    raw = payload.get("flow_scores")
                deterministic_scores = [item for item in raw if isinstance(item, dict)]
            if call.tool_name == "query_retrieval_memory" and isinstance(payload.get("flows"), dict):
                retrieval_by_flow = payload.get("flows") or {}

        if dominant_path_type:
            bottleneck = str(dominant_path_type)
        elif route_delay_ratio is not None and route_delay_ratio >= 0.60:
            bottleneck = "routing_dominated"
        elif route_delay_ratio is not None:
            bottleneck = "logic_or_mixed"
        else:
            bottleneck = "unknown"

        ranked = []
        for flow in allowed_flows:
            det = _flow_score(flow, deterministic_scores)
            retrieval = retrieval_by_flow.get(flow) if isinstance(retrieval_by_flow.get(flow), dict) else {}
            success = float(retrieval.get("success_prior") or 0.0) if retrieval else 0.0
            invalid = float(retrieval.get("invalid_risk") or 0.0) if retrieval else 0.0
            priority_delta = max(-0.25, min(0.25, det / 100.0 + success * 0.10 - invalid * 0.20))
            confidence = min(0.85, 0.35 + abs(det) / 100.0 + (0.15 if retrieval else 0.0))
            ranked.append((priority_delta, flow, invalid, confidence))
        ranked.sort(reverse=True)
        priors = [
            CandidatePrior(
                node_id=node.node_id,
                flow=flow,
                priority_delta=round(delta, 6),
                risk_delta=round(invalid, 6),
                confidence=round(conf, 3),
                reason=f"Heuristic deep-analysis prior from bottleneck={bottleneck}, deterministic score, and retrieval evidence.",
                source_analysis_session_id=session_id,
            )
            for delta, flow, invalid, conf in ranked[: min(5, len(ranked))]
        ]
        priorities = [
            CandidatePriority(
                node_id=node.node_id,
                flow=flow,
                priority_score=round(max(0.0, min(1.0, 0.5 + delta - max(0.0, invalid) * 0.05)), 6),
                risk_score=round(max(0.0, invalid), 6),
                confidence=round(conf, 3),
                expected_effect=f"Expected to help bottleneck={bottleneck} based on structured analysis evidence.",
                evidence_refs=[
                    {
                        "basis": "current_observation",
                        "ref": call.tool_name,
                        "node_id": getattr(node, "node_id", None),
                        "flow": flow,
                        "source": "tool_output",
                    }
                    for call in tool_calls
                    if call.success
                ],
                reason=f"Analysis priority from bottleneck={bottleneck}, deterministic score, and retrieval evidence.",
                remaining_uncertainty=[] if bottleneck != "unknown" else ["bottleneck class not available from current reports"],
                source_analysis_session_id=session_id,
            )
            for delta, flow, invalid, conf in ranked[: min(5, len(ranked))]
        ]
        if any(p.risk_delta >= 0.30 for p in priors):
            risks.append({"type": "retrieved_invalid_risk", "flows": [p.flow for p in priors if p.risk_delta >= 0.30]})
        return NodeDiagnosis(
            node_id=node.node_id,
            analysis_type="online_deep_analysis",
            bottleneck_class=bottleneck,
            bottleneck_confidence=0.70 if bottleneck != "unknown" else 0.35,
            evidence=evidence,
            risks=risks,
            candidate_priors=priors,
            candidate_priorities=priorities,
            recommended_action="schedule_from_analysis_priorities",
            remaining_uncertainty=[] if bottleneck != "unknown" else ["bottleneck class not available from current reports"],
        )


def _flow_score(flow: str, deterministic_scores: Iterable[Dict[str, Any]]) -> float:
    for item in deterministic_scores:
        if item.get("flow") == flow:
            try:
                return float(item.get("flow_heuristic_score") or 0.0)
            except (TypeError, ValueError):
                return 0.0
    return 0.0


def _first_float(existing: Optional[float], value: Any) -> Optional[float]:
    if existing is not None:
        return existing
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _summarize_tool_output(output: Dict[str, Any]) -> Dict[str, Any]:
    summary: Dict[str, Any] = {}
    for key in [
        "node_id",
        "probe_backend",
        "mutates_design",
        "mode",
        "timing",
        "critical_path_features",
        "route_status",
        "utilization_summary",
        "available",
    ]:
        if key in output:
            summary[key] = output[key]
    return summary
