"""Read-only tools over frontier-expansion artifacts and in-memory node state."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional

from experiments.ablation.runtime.analysis_schema import strip_heuristic_ranking

from .base import ToolContext


class LoadNodeSummaryTool:
    name = "load_node_summary"
    tool_class = "inspect"
    mutates_design = False

    def run(self, args: Dict[str, Any], context: ToolContext) -> Dict[str, Any]:
        node = context.node
        metrics = getattr(node, "metrics", {}) or {}
        parent = _parent_node(node, context.nodes)
        parent_metrics = (getattr(parent, "metrics", {}) or {}) if parent is not None else {}
        current_qor = _qor_summary(node, context.analysis_contexts)
        parent_qor = _qor_summary(parent, context.analysis_contexts) if parent is not None else {}
        missing = _missing_diagnostic_features(_analysis_context_for_node(node, context.analysis_contexts))
        return {
            "node_id": getattr(node, "node_id", None),
            "parent_id": getattr(node, "parent_id", None),
            "parent_selected_flow": getattr(parent, "selected_flow", None) if parent is not None else None,
            "depth": getattr(node, "depth", None),
            "selected_flow": getattr(node, "selected_flow", None),
            "flow_sequence": list(getattr(node, "flow_sequence", []) or []),
            "status": str(getattr(node, "status", "")),
            "validation_status": getattr(node, "validation_status", None),
            "pure_qor_score": _first_present(metrics.get("pure_qor_score"), getattr(node, "score", None)),
            "trajectory_objective_profile": "pure_qor_v1",
            "trajectory_objective_score": _first_present(metrics.get("pure_qor_score"), getattr(node, "score", None)),
            "path_runtime_seconds": metrics.get("path_runtime_seconds"),
            "node_runtime_seconds": metrics.get("node_runtime_seconds"),
            "final_fmax_mhz": _first_present(metrics.get("final_fmax_mhz"), metrics.get("fmax_mhz")),
            "final_wns_ns": metrics.get("final_wns_ns"),
            "final_tns_ns": metrics.get("final_tns_ns"),
            "parent_pure_qor_score": parent_metrics.get("pure_qor_score") if parent is not None else None,
            "delta_pure_qor_from_parent": _delta(_score(node), _score(parent)) if parent is not None else None,
            "delta_fmax_mhz_from_parent": _delta(current_qor.get("final_fmax_mhz"), parent_qor.get("final_fmax_mhz")) if parent is not None else None,
            "delta_wns_ns_from_parent": _delta(current_qor.get("final_wns_ns"), parent_qor.get("final_wns_ns")) if parent is not None else None,
            "validation_errors": list(getattr(node, "validation_errors", []) or [])[:10],
            "validation_warnings": list(getattr(node, "validation_warnings", []) or [])[:10],
            "missing_diagnostic_features": missing,
        }


class LoadAnalysisContextTool:
    name = "load_analysis_context"
    tool_class = "inspect"
    mutates_design = False

    def run(self, args: Dict[str, Any], context: ToolContext) -> Dict[str, Any]:
        node_id = args.get("node_id") or getattr(context.node, "node_id", None)
        data = context.analysis_contexts.get(str(node_id), {}) if node_id else {}
        return _compact_analysis_context(data)


class LoadTrajectoryOutcomesTool:
    name = "load_trajectory_outcomes"
    tool_class = "inspect"
    mutates_design = False

    def run(self, args: Dict[str, Any], context: ToolContext) -> Dict[str, Any]:
        limit = _int_or_none(args.get("limit") or args.get("k"))
        return build_trajectory_outcomes(
            node=context.node,
            nodes=context.nodes,
            analysis_contexts=context.analysis_contexts,
            limit=limit,
        )


class LoadFrontierOutcomeMemoryTool:
    name = "load_frontier_outcome_memory"
    tool_class = "inspect"
    mutates_design = False

    def run(self, args: Dict[str, Any], context: ToolContext) -> Dict[str, Any]:
        limit = _int_or_default(args.get("limit") or args.get("k"), 10)
        return build_frontier_outcome_memory(
            nodes=context.nodes,
            analysis_contexts=context.analysis_contexts,
            allowed_flows=list(context.allowed_flows or []),
            current_node=context.node,
            limit=limit,
        )


class InspectValidationEventsTool:
    name = "inspect_validation_events"
    tool_class = "inspect"
    mutates_design = False

    def run(self, args: Dict[str, Any], context: ToolContext) -> Dict[str, Any]:
        node = context.node
        return {
            "node_id": getattr(node, "node_id", None),
            "validation_status": getattr(node, "validation_status", None),
            "fallback_used": bool(getattr(node, "fallback_used", False)),
            "warnings": list(getattr(node, "validation_warnings", []) or [])[:20],
            "errors": list(getattr(node, "validation_errors", []) or [])[:20],
        }


class AnalyzeMotifsTool:
    name = "analyze_trajectory_motifs"
    tool_class = "inspect"
    mutates_design = False

    def run(self, args: Dict[str, Any], context: ToolContext) -> Dict[str, Any]:
        motif_counts: Dict[str, Dict[str, Any]] = {}
        for node in context.nodes.values():
            seq = list(getattr(node, "flow_sequence", []) or [])
            if not seq:
                continue
            parent = _parent_node(node, context.nodes)
            delta_qor = _delta(_score(node), _score(parent)) if parent is not None else None
            delta_fmax = _delta(_qor_summary(node, context.analysis_contexts).get("final_fmax_mhz"), _qor_summary(parent, context.analysis_contexts).get("final_fmax_mhz")) if parent is not None else None
            for n in (1, 2, 3):
                if len(seq) >= n:
                    motif = " -> ".join(seq[-n:])
                    rec = motif_counts.setdefault(motif, {"support": 0, "clean": 0, "invalid": 0, "examples": [], "best_delta_pure_qor": None, "best_delta_fmax_mhz": None, "recent_deltas": []})
                    rec["support"] += 1
                    status = getattr(node, "validation_status", None)
                    if status == "INVALID":
                        rec["invalid"] += 1
                    else:
                        rec["clean"] += 1
                    if delta_qor is not None:
                        rec["best_delta_pure_qor"] = _max_optional(rec.get("best_delta_pure_qor"), delta_qor)
                        if len(rec["recent_deltas"]) < 5:
                            rec["recent_deltas"].append({"node_id": getattr(node, "node_id", None), "delta_pure_qor": delta_qor, "delta_fmax_mhz": delta_fmax})
                    if delta_fmax is not None:
                        rec["best_delta_fmax_mhz"] = _max_optional(rec.get("best_delta_fmax_mhz"), delta_fmax)
                    if len(rec["examples"]) < 3:
                        rec["examples"].append(getattr(node, "node_id", None))
        top = sorted(motif_counts.items(), key=lambda item: item[1]["support"], reverse=True)[:10]
        return {"motifs": [{"motif": key, **value} for key, value in top]}


def build_optimization_state_context(
    *,
    node: Any,
    nodes: Dict[str, Any],
    analysis_contexts: Dict[str, Dict[str, Any]],
    allowed_flows: List[str],
) -> Dict[str, Any]:
    """Compact state/action/outcome context for the analysis LLM."""

    node_id = str(getattr(node, "node_id", "node"))
    legal, illegal = _legal_next_flows(node, nodes, allowed_flows)
    current_summary = LoadNodeSummaryTool().run({}, ToolContext(
        run_dir=Path("."),
        node=node,
        nodes=nodes,
        analysis_contexts=analysis_contexts,
        allowed_flows=allowed_flows,
    ))
    return {
        "schema_version": 1,
        "context_mode": "compact_v1",
        "current_node_summary": current_summary,
        "trajectory_outcome_table": build_trajectory_outcomes(node=node, nodes=nodes, analysis_contexts=analysis_contexts, limit=8),
        "frontier_outcome_memory": build_frontier_outcome_memory(nodes=nodes, analysis_contexts=analysis_contexts, allowed_flows=allowed_flows, current_node=node, limit=4),
        "run_budget_context": {
            "current_node_id": node_id,
            "current_depth": getattr(node, "depth", None),
            "allowed_flows": list(allowed_flows),
            "legal_next_flows": legal,
            "illegal_next_flows": illegal,
            "current_child_count": sum(1 for item in nodes.values() if str(getattr(item, "parent_id", "")) == node_id),
            "remaining_runs": None,
            "max_depth": None,
        },
        "observed_action_effects": _observed_action_effects(nodes, analysis_contexts, allowed_flows),
        "invalid_action_effects": _invalid_action_effects(nodes, analysis_contexts, allowed_flows),
        "invalid_sequence_motifs": _sequence_motif_stats(nodes, analysis_contexts, only_invalid=True, limit=8),
        "mixed_evidence_flows": _mixed_evidence_flows(nodes, analysis_contexts, allowed_flows),
    }


def build_trajectory_outcomes(
    *,
    node: Any,
    nodes: Dict[str, Any],
    analysis_contexts: Dict[str, Dict[str, Any]],
    limit: Optional[int] = None,
) -> Dict[str, Any]:
    path = _ancestor_path(node, nodes)
    if limit is not None and limit > 0:
        path = path[-limit:]
    rows = [_outcome_row(item, nodes, analysis_contexts) for item in path]
    best = max((row for row in rows if row.get("trajectory_objective_score") is not None), key=_row_score, default=None)
    return {
        "current_node_id": getattr(node, "node_id", None),
        "steps": rows,
        "best_step": best,
        "plateau_steps": [row for row in rows if _is_plateau_delta(row)],
        "warnings": [row for row in rows if row.get("validation_warnings")],
        "missing_evidence": sorted({feature for row in rows for feature in row.get("missing_diagnostic_features", [])}),
    }


def build_frontier_outcome_memory(
    *,
    nodes: Dict[str, Any],
    analysis_contexts: Dict[str, Dict[str, Any]],
    allowed_flows: List[str],
    current_node: Any = None,
    limit: int = 10,
) -> Dict[str, Any]:
    completed = [
        node for node in nodes.values()
        if getattr(node, "depth", 0) > 0 and str(getattr(getattr(node, "status", None), "value", getattr(node, "status", ""))).upper().endswith("COMPLETED")
    ]
    if not completed:
        completed = [node for node in nodes.values() if getattr(node, "depth", 0) > 0]
    rows = [_outcome_row(node, nodes, analysis_contexts) for node in completed]
    score_rows = [row for row in rows if row.get("trajectory_objective_score") is not None]
    top = sorted(score_rows, key=_row_score, reverse=True)[:limit]
    bottom = sorted(score_rows, key=_row_score)[:limit]
    recent = sorted(rows, key=lambda row: _node_numeric_id(str(row.get("node_id") or "")), reverse=True)[:limit]
    best = top[0] if top else None
    return {
        "current_node_id": getattr(current_node, "node_id", None) if current_node is not None else None,
        "best_node": best,
        "top_completed": top,
        "bottom_completed": bottom,
        "recent_completed": recent,
        "no_op_plateaus": [row for row in rows if _is_plateau_delta(row)][:limit],
        "warning_or_invalid_nodes": [row for row in rows if row.get("validation_status") in {"WARNING", "INVALID", "FALLBACK_USED"} or row.get("validation_warnings") or row.get("validation_errors")][:limit],
        "observed_action_effects": _observed_action_effects(nodes, analysis_contexts, allowed_flows),
        "invalid_action_effects": _invalid_action_effects(nodes, analysis_contexts, allowed_flows),
        "invalid_sequence_motifs": _sequence_motif_stats(nodes, analysis_contexts, only_invalid=True, limit=limit),
        "mixed_evidence_flows": _mixed_evidence_flows(nodes, analysis_contexts, allowed_flows),
        "missing_evidence_summary": _missing_evidence_summary(rows),
    }


def _compact_analysis_context(data: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(data, dict):
        return {}
    keep: Dict[str, Any] = {}
    for key in [
        "benchmark_name",
        "clock_period_ns",
        "initial_fmax_mhz",
        "final_fmax_mhz",
        "initial_wns_ns",
        "final_wns_ns",
        "initial_tns_ns",
        "final_tns_ns",
        "failing_endpoints",
        "target_clock",
        "candidate_flows",
        "critical_path_features",
        "congestion",
        "fanout",
        "utilization_summary",
        "runtime",
        "timing",
        "diagnostic_missing_reasons",
        "route_vs_logic_delay_hint",
        "hardblock_resource_pressure",
    ]:
        if key in data:
            keep[key] = data[key]
    timing = data.get("timing") if isinstance(data.get("timing"), dict) else {}
    runtime = data.get("runtime") if isinstance(data.get("runtime"), dict) else {}
    keep.setdefault("timing", {
        "clock_period_ns": _first_present(data.get("clock_period_ns"), timing.get("clock_period_ns")),
        "target_clock": _first_present(data.get("target_clock"), timing.get("target_clock")),
        "initial_wns_ns": _first_present(data.get("initial_wns_ns"), timing.get("initial_wns_ns")),
        "final_wns_ns": _first_present(data.get("final_wns_ns"), timing.get("final_wns_ns")),
        "initial_tns_ns": _first_present(data.get("initial_tns_ns"), timing.get("initial_tns_ns")),
        "final_tns_ns": _first_present(data.get("final_tns_ns"), timing.get("final_tns_ns")),
        "initial_fmax_mhz": _first_present(data.get("initial_fmax_mhz"), timing.get("initial_fmax_mhz")),
        "final_fmax_mhz": _first_present(data.get("final_fmax_mhz"), timing.get("final_fmax_mhz")),
    })
    keep.setdefault("runtime", {
        "duration_seconds": _first_present(data.get("duration_seconds"), runtime.get("duration_seconds")),
        "duration_minutes": _first_present(data.get("duration_minutes"), runtime.get("duration_minutes")),
    })
    cpf = data.get("critical_path_features") if isinstance(data.get("critical_path_features"), dict) else {}
    keep["critical_path_primitive_mix"] = cpf.get("primitive_mix") or cpf.get("cell_type_mix") or {}
    keep["route_vs_logic_delay_hint"] = cpf.get("route_vs_logic_delay_hint") or cpf.get("dominant_delay_type")
    util = data.get("utilization_summary") if isinstance(data.get("utilization_summary"), dict) else {}
    keep["hardblock_resource_pressure"] = data.get("hardblock_resource_pressure") if isinstance(data.get("hardblock_resource_pressure"), dict) else _hardblock_resource_pressure(util)
    keep["missing_diagnostic_features"] = _missing_diagnostic_features(data)
    keep["diagnostic_missing_reasons"] = data.get("diagnostic_missing_reasons") if isinstance(data.get("diagnostic_missing_reasons"), dict) else {
        key: "not present in analysis context" for key in keep["missing_diagnostic_features"]
    }
    return strip_heuristic_ranking(keep)


def _outcome_row(node: Any, nodes: Dict[str, Any], analysis_contexts: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    parent = _parent_node(node, nodes)
    current_qor = _qor_summary(node, analysis_contexts)
    parent_qor = _qor_summary(parent, analysis_contexts) if parent is not None else {}
    return {
        "node_id": getattr(node, "node_id", None),
        "parent_id": getattr(node, "parent_id", None),
        "depth": getattr(node, "depth", None),
        "action": getattr(node, "selected_flow", None) or "root",
        "flow_sequence": list(getattr(node, "flow_sequence", []) or []),
        "status": str(getattr(node, "status", "")),
        "validation_status": getattr(node, "validation_status", None),
        "trajectory_objective_score": _score(node),
        "parent_trajectory_objective_score": _score(parent) if parent is not None else None,
        "delta_pure_qor_from_parent": _delta(_score(node), _score(parent)) if parent is not None else None,
        "final_fmax_mhz": current_qor.get("final_fmax_mhz"),
        "parent_final_fmax_mhz": parent_qor.get("final_fmax_mhz"),
        "delta_fmax_mhz_from_parent": _delta(current_qor.get("final_fmax_mhz"), parent_qor.get("final_fmax_mhz")) if parent is not None else None,
        "final_wns_ns": current_qor.get("final_wns_ns"),
        "parent_final_wns_ns": parent_qor.get("final_wns_ns"),
        "delta_wns_ns_from_parent": _delta(current_qor.get("final_wns_ns"), parent_qor.get("final_wns_ns")) if parent is not None else None,
        "final_tns_ns": current_qor.get("final_tns_ns"),
        "runtime_seconds": current_qor.get("runtime_seconds"),
        "validation_warnings": list(getattr(node, "validation_warnings", []) or [])[:5],
        "validation_errors": list(getattr(node, "validation_errors", []) or [])[:5],
        "missing_diagnostic_features": _missing_diagnostic_features(_analysis_context_for_node(node, analysis_contexts)),
    }


def _observed_action_effects(nodes: Dict[str, Any], analysis_contexts: Dict[str, Dict[str, Any]], allowed_flows: List[str]) -> Dict[str, Dict[str, Any]]:
    effects: Dict[str, Dict[str, Any]] = {
        str(flow): {"clean": 0, "warning": 0, "invalid": 0, "fallback": 0, "regressed": 0, "plateau": 0, "best_delta_pure_qor": None, "best_delta_fmax_mhz": None, "recent_examples": []}
        for flow in allowed_flows
    }
    for node in nodes.values():
        flow = getattr(node, "selected_flow", None)
        if not flow:
            continue
        rec = effects.setdefault(str(flow), {"clean": 0, "warning": 0, "invalid": 0, "fallback": 0, "regressed": 0, "plateau": 0, "best_delta_pure_qor": None, "best_delta_fmax_mhz": None, "recent_examples": []})
        status = str(getattr(node, "validation_status", "") or "").upper()
        if status == "INVALID":
            rec["invalid"] += 1
        elif status in {"WARNING", "FALLBACK_USED"} or getattr(node, "validation_warnings", None):
            rec["warning"] += 1
        else:
            rec["clean"] += 1
        if bool(getattr(node, "fallback_used", False)):
            rec["fallback"] += 1
        parent = _parent_node(node, nodes)
        delta_qor = _delta(_score(node), _score(parent)) if parent is not None else None
        delta_fmax = _delta(_qor_summary(node, analysis_contexts).get("final_fmax_mhz"), _qor_summary(parent, analysis_contexts).get("final_fmax_mhz")) if parent is not None else None
        if delta_qor is not None:
            if delta_qor < 0:
                rec["regressed"] += 1
            if abs(delta_qor) <= 1e-6:
                rec["plateau"] += 1
            rec["best_delta_pure_qor"] = _max_optional(rec.get("best_delta_pure_qor"), delta_qor)
        if delta_fmax is not None:
            rec["best_delta_fmax_mhz"] = _max_optional(rec.get("best_delta_fmax_mhz"), delta_fmax)
        examples = rec["recent_examples"]
        if len(examples) < 5:
            examples.append({"node_id": getattr(node, "node_id", None), "delta_pure_qor": delta_qor, "delta_fmax_mhz": delta_fmax, "validation_status": getattr(node, "validation_status", None)})
    return effects


def _invalid_action_effects(nodes: Dict[str, Any], analysis_contexts: Dict[str, Dict[str, Any]], allowed_flows: List[str]) -> Dict[str, Dict[str, Any]]:
    effects = _observed_action_effects(nodes, analysis_contexts, allowed_flows)
    return {
        flow: {
            "invalid": rec.get("invalid", 0),
            "regressed": rec.get("regressed", 0),
            "warning": rec.get("warning", 0),
            "recent_invalid_examples": [
                item for item in rec.get("recent_examples", [])
                if str(item.get("validation_status", "")).upper() == "INVALID"
            ][:3],
        }
        for flow, rec in effects.items()
        if rec.get("invalid", 0) or rec.get("regressed", 0) or rec.get("warning", 0)
    }


def _mixed_evidence_flows(nodes: Dict[str, Any], analysis_contexts: Dict[str, Dict[str, Any]], allowed_flows: List[str]) -> Dict[str, Dict[str, Any]]:
    effects = _observed_action_effects(nodes, analysis_contexts, allowed_flows)
    mixed: Dict[str, Dict[str, Any]] = {}
    for flow, rec in effects.items():
        if rec.get("clean", 0) and (rec.get("invalid", 0) or rec.get("regressed", 0) or rec.get("warning", 0)):
            mixed[flow] = {
                "clean": rec.get("clean", 0),
                "invalid": rec.get("invalid", 0),
                "regressed": rec.get("regressed", 0),
                "warning": rec.get("warning", 0),
                "best_delta_pure_qor": rec.get("best_delta_pure_qor"),
                "best_delta_fmax_mhz": rec.get("best_delta_fmax_mhz"),
            }
    return mixed


def _sequence_motif_stats(
    nodes: Dict[str, Any],
    analysis_contexts: Dict[str, Dict[str, Any]],
    *,
    only_invalid: bool = False,
    limit: int = 8,
) -> List[Dict[str, Any]]:
    motifs: Dict[str, Dict[str, Any]] = {}
    for node in nodes.values():
        seq = list(getattr(node, "flow_sequence", []) or [])
        if len(seq) < 2:
            continue
        status = str(getattr(node, "validation_status", "") or "").upper()
        parent = _parent_node(node, nodes)
        delta_qor = _delta(_score(node), _score(parent)) if parent is not None else None
        delta_fmax = _delta(_qor_summary(node, analysis_contexts).get("final_fmax_mhz"), _qor_summary(parent, analysis_contexts).get("final_fmax_mhz")) if parent is not None else None
        for length in (2, 3):
            if len(seq) < length:
                continue
            motif = " -> ".join(seq[-length:])
            rec = motifs.setdefault(motif, {"motif": motif, "support": 0, "clean": 0, "invalid": 0, "regressed": 0, "examples": []})
            rec["support"] += 1
            if status == "INVALID":
                rec["invalid"] += 1
            else:
                rec["clean"] += 1
            if delta_qor is not None and delta_qor < 0:
                rec["regressed"] += 1
            if len(rec["examples"]) < 3:
                rec["examples"].append({
                    "node_id": getattr(node, "node_id", None),
                    "validation_status": getattr(node, "validation_status", None),
                    "delta_pure_qor": delta_qor,
                    "delta_fmax_mhz": delta_fmax,
                    "rejection_reason": getattr(node, "rejection_reason", None),
                })
    rows = list(motifs.values())
    if only_invalid:
        rows = [row for row in rows if row.get("invalid") or row.get("regressed")]
    rows.sort(key=lambda row: (row.get("invalid", 0), row.get("regressed", 0), row.get("support", 0)), reverse=True)
    return rows[:limit]


def _legal_next_flows(node: Any, nodes: Dict[str, Any], allowed_flows: List[str]) -> tuple[List[str], List[Dict[str, Any]]]:
    seq = list(getattr(node, "flow_sequence", []) or [])
    existing = {tuple(getattr(item, "flow_sequence", []) or []) for item in nodes.values()}
    legal: List[str] = []
    illegal: List[Dict[str, Any]] = []
    for flow in allowed_flows:
        flow = str(flow)
        next_seq = tuple(seq + [flow])
        reason = None
        if seq and seq[-1] == flow:
            reason = "consecutive_repeat"
        elif next_seq in existing:
            reason = "duplicate_sequence"
        if reason:
            illegal.append({"flow": flow, "reason": reason})
        else:
            legal.append(flow)
    return legal, illegal


def _ancestor_path(node: Any, nodes: Dict[str, Any]) -> List[Any]:
    path: List[Any] = []
    seen = set()
    cur = node
    while cur is not None:
        node_id = str(getattr(cur, "node_id", ""))
        if node_id in seen:
            break
        seen.add(node_id)
        path.append(cur)
        parent_id = getattr(cur, "parent_id", None)
        cur = nodes.get(str(parent_id)) if parent_id is not None else None
    return list(reversed(path))


def _parent_node(node: Any, nodes: Dict[str, Any]) -> Any:
    if node is None:
        return None
    parent_id = getattr(node, "parent_id", None)
    return nodes.get(str(parent_id)) if parent_id is not None else None


def _score(node: Any) -> Optional[float]:
    if node is None:
        return None
    metrics = getattr(node, "metrics", {}) or {}
    return _float_or_none(metrics.get("pure_qor_score") if metrics.get("pure_qor_score") is not None else getattr(node, "score", None))


def _qor_summary(node: Any, analysis_contexts: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    if node is None:
        return {}
    metrics = getattr(node, "metrics", {}) or {}
    ctx = _analysis_context_for_node(node, analysis_contexts)
    timing = ctx.get("timing") if isinstance(ctx.get("timing"), dict) else {}
    runtime = ctx.get("runtime") if isinstance(ctx.get("runtime"), dict) else {}
    return {
        "final_fmax_mhz": _float_or_none(_first_present(metrics.get("final_fmax_mhz"), metrics.get("fmax_mhz"), ctx.get("final_fmax_mhz"), timing.get("final_fmax_mhz"))),
        "final_wns_ns": _float_or_none(_first_present(metrics.get("final_wns_ns"), ctx.get("final_wns_ns"), timing.get("final_wns_ns"))),
        "final_tns_ns": _float_or_none(_first_present(metrics.get("final_tns_ns"), ctx.get("final_tns_ns"), timing.get("final_tns_ns"))),
        "runtime_seconds": _float_or_none(_first_present(metrics.get("node_runtime_seconds"), metrics.get("path_runtime_seconds"), runtime.get("duration_seconds"), ctx.get("duration_seconds"))),
    }


def _analysis_context_for_node(node: Any, analysis_contexts: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    node_id = getattr(node, "node_id", None)
    data = analysis_contexts.get(str(node_id), {}) if node_id is not None else {}
    return data if isinstance(data, dict) else {}


def _delta(current: Any, parent: Any) -> Optional[float]:
    cur = _float_or_none(current)
    par = _float_or_none(parent)
    if cur is None or par is None:
        return None
    return round(cur - par, 6)


def _is_plateau_delta(row: Dict[str, Any]) -> bool:
    for key in ("delta_pure_qor_from_parent", "delta_fmax_mhz_from_parent"):
        value = _float_or_none(row.get(key))
        if value is not None and abs(value) <= 1e-6:
            return True
    return False


def _missing_evidence_summary(rows: List[Dict[str, Any]]) -> Dict[str, int]:
    out: Dict[str, int] = {}
    for row in rows:
        for feature in row.get("missing_diagnostic_features", []) or []:
            out[str(feature)] = out.get(str(feature), 0) + 1
    return out


def _missing_diagnostic_features(data: Dict[str, Any]) -> List[str]:
    missing: List[str] = []
    for key in ("critical_path_features", "congestion", "fanout"):
        value = data.get(key)
        if not value:
            missing.append(key)
    return missing


def _hardblock_resource_pressure(util: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(util, dict):
        return {}
    out: Dict[str, Any] = {}
    for key, value in util.items():
        lower = str(key).lower()
        if any(token in lower for token in ("bram", "uram", "dsp", "bufg", "mmcm", "pll")):
            out[str(key)] = value
    return out


def _max_optional(a: Any, b: Any) -> Optional[float]:
    fa = _float_or_none(a)
    fb = _float_or_none(b)
    if fa is None:
        return fb
    if fb is None:
        return fa
    return max(fa, fb)


def _float_or_none(value: Any) -> Optional[float]:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _first_present(*values: Any) -> Any:
    for value in values:
        if value is not None and value != "":
            return value
    return None


def _row_score(row: Dict[str, Any]) -> float:
    value = _float_or_none(row.get("trajectory_objective_score"))
    return value if value is not None else float("-inf")


def _int_or_none(value: Any) -> Optional[int]:
    try:
        if value is None or value == "":
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def _int_or_default(value: Any, default: int) -> int:
    parsed = _int_or_none(value)
    return parsed if parsed is not None else default


def _node_numeric_id(node_id: str) -> int:
    try:
        return int(str(node_id).split("_")[1])
    except Exception:
        return 0
