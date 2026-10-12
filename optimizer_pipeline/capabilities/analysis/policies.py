"""Pluggable trigger, budget, stop, and synthesis policies for analysis."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Protocol

from .schemas import (
    AnalysisBudget,
    AnalysisStopDecision,
    AnalysisToolCall,
    AnalysisTriggerDecision,
    CandidatePrior,
    NodeDiagnosis,
)


class AnalysisTriggerPolicy(Protocol):
    def should_analyze(self, *, node: Any, state: Any, runner_view: Dict[str, Any]) -> AnalysisTriggerDecision:
        ...


class AnalysisBudgetPolicy(Protocol):
    def budget_for(self, trigger: AnalysisTriggerDecision) -> AnalysisBudget:
        ...


class AnalysisStopPolicy(Protocol):
    def should_continue(self, *, elapsed_sec: float, budget: AnalysisBudget, tool_calls: List[AnalysisToolCall], next_tool: Optional[str]) -> AnalysisStopDecision:
        ...


class DiagnosisSynthesizer(Protocol):
    def synthesize(self, *, node: Any, tool_calls: List[AnalysisToolCall], allowed_flows: List[str], session_id: str) -> NodeDiagnosis:
        ...


@dataclass
class NaiveOnlineAnalysisTriggerPolicy:
    root_always: bool = True
    early_node_budget: int = 2
    late_node_budget: int = 2
    max_nodes_per_search: int = 5
    no_improvement_window: int = 5
    frontier_gap_threshold: float = 0.05

    def should_analyze(self, *, node: Any, state: Any, runner_view: Dict[str, Any]) -> AnalysisTriggerDecision:
        sessions_done = int(runner_view.get("analysis_sessions_done") or 0)
        if sessions_done >= self.max_nodes_per_search:
            return AnalysisTriggerDecision(False, node.node_id, "max_analysis_sessions_reached")
        already = set(runner_view.get("analyzed_node_ids") or [])
        if node.node_id in already:
            return AnalysisTriggerDecision(False, node.node_id, "node_already_analyzed")
        if node.depth == 0 and self.root_always:
            return AnalysisTriggerDecision(True, node.node_id, "root_online_analysis", priority=1.0, budget_profile="root")

        completed_runs = int(runner_view.get("run_count") or 0)
        max_runs = max(1, int(runner_view.get("max_total_runs") or 1))
        progress = completed_runs / max_runs
        validation_status = str(getattr(node, "validation_status", "") or "")

        if progress < 0.30 and node.depth <= 1 and sessions_done < 1 + self.early_node_budget:
            return AnalysisTriggerDecision(True, node.node_id, "early_depth1_key_node", priority=0.65, budget_profile="node")
        if validation_status in {"WARNING", "FALLBACK_USED", "INVALID"}:
            return AnalysisTriggerDecision(True, node.node_id, "validation_boundary_node", priority=0.80, budget_profile="node")
        if runner_view.get("no_improvement_window_hit"):
            return AnalysisTriggerDecision(True, node.node_id, "no_improvement_window", priority=0.75, budget_profile="node")
        if runner_view.get("frontier_disagreement_high"):
            return AnalysisTriggerDecision(True, node.node_id, "frontier_or_prior_disagreement", priority=0.70, budget_profile="node")
        return AnalysisTriggerDecision(False, node.node_id, "naive_policy_no_trigger")


@dataclass
class StaticAnalysisBudgetPolicy:
    root_budget_sec: float = 600.0
    node_budget_sec: float = 120.0
    allow_report_probes: bool = True
    allow_act_probes: bool = False
    root_max_tool_calls: int = 10
    node_max_tool_calls: int = 6
    default_tool_timeout_sec: float = 300.0
    max_act_probe_calls: int = 0
    max_act_probe_time_sec: float = 0.0

    def budget_for(self, trigger: AnalysisTriggerDecision) -> AnalysisBudget:
        if trigger.budget_profile == "root":
            return AnalysisBudget(
                max_wall_time_sec=self.root_budget_sec,
                max_tool_calls=self.root_max_tool_calls,
                max_llm_calls=6,
                max_report_probe_calls=6,
                max_report_probe_time_sec=max(1.0, self.root_budget_sec * 0.90),
                max_act_probe_calls=max(0, int(self.max_act_probe_calls)),
                max_act_probe_time_sec=max(0.0, float(self.max_act_probe_time_sec)),
                allow_report_probes=self.allow_report_probes,
                allow_act_probes=self.allow_act_probes,
                default_tool_timeout_sec=self.default_tool_timeout_sec,
            )
        return AnalysisBudget(
            max_wall_time_sec=self.node_budget_sec,
            max_tool_calls=self.node_max_tool_calls,
            max_llm_calls=4,
            max_report_probe_calls=3,
            max_report_probe_time_sec=max(1.0, self.node_budget_sec * 0.75),
            max_act_probe_calls=max(0, int(self.max_act_probe_calls)),
            max_act_probe_time_sec=max(0.0, float(self.max_act_probe_time_sec)),
            allow_report_probes=self.allow_report_probes,
            allow_act_probes=self.allow_act_probes,
            default_tool_timeout_sec=self.default_tool_timeout_sec,
        )


@dataclass
class TimePressureStopPolicy:
    soft_stop_frac: float = 0.55
    strong_stop_frac: float = 0.80

    def should_continue(self, *, elapsed_sec: float, budget: AnalysisBudget, tool_calls: List[AnalysisToolCall], next_tool: Optional[str]) -> AnalysisStopDecision:
        if elapsed_sec >= budget.max_wall_time_sec:
            return AnalysisStopDecision(False, "hard_wall_time_budget_reached", confidence=1.0)
        if len(tool_calls) >= budget.max_tool_calls:
            return AnalysisStopDecision(False, "tool_call_budget_reached", confidence=1.0)
        frac = elapsed_sec / max(1.0, budget.max_wall_time_sec)
        if frac >= self.strong_stop_frac:
            return AnalysisStopDecision(False, "strong_time_pressure_stop_unless_top3_would_change", confidence=0.85)
        if frac >= self.soft_stop_frac and not next_tool:
            return AnalysisStopDecision(False, "soft_time_pressure_no_specific_next_tool", confidence=0.75)
        return AnalysisStopDecision(True, "continue_within_budget", confidence=0.5, next_tool_name=next_tool)


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
            if cpath:
                route_delay_ratio = _first_float(route_delay_ratio, cpath.get("route_delay_ratio"))
                dominant_path_type = dominant_path_type or cpath.get("dominant_path_type")
            if call.tool_name == "load_analysis_context":
                cands = payload.get("candidate_flows") if isinstance(payload.get("candidate_flows"), dict) else {}
                raw = cands.get("deterministic_scores") if isinstance(cands.get("deterministic_scores"), list) else []
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
        for idx, flow in enumerate(allowed_flows):
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
            recommended_action="inject_candidate_priors_and_continue_search",
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
    for key in ["node_id", "probe_backend", "fresh_vivado_report", "mutates_design", "evidence_provenance", "timing", "critical_path_features", "route_status", "utilization_summary", "available"]:
        if key in output:
            summary[key] = output[key]
    return summary
