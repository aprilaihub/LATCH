"""Stable research diagnostics schema for FPGA implementation optimisation.

The production Vivado/RapidWright reports available to this repository vary by
flow and benchmark.  The research frontier-expansion path therefore normalises whatever
is available into a stable v2 JSON object.  Missing report-derived values are
kept as ``None`` rather than changing the prompt schema across nodes.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from .models import DcpStateNode, to_jsonable


DEFAULT_RESEARCH_FLOWS = [
    "phys_opt",
    "auto_pblock",
    "lut_cone",
    "retime",
    "congestion_spread",
    "memory_path_opt",
    "hardblock_relocate",
    "net_swap",
]


def node_artifact_dict(node: DcpStateNode) -> Dict[str, Any]:
    """Build the legacy-shaped in-memory history row without persisting it."""
    data = node.to_dict()
    pure_qor_score = data.pop("score", None)
    data["pure_qor_score"] = pure_qor_score
    data["trajectory_objective_profile"] = "pure_qor_v1"
    data["trajectory_objective_score"] = pure_qor_score
    return data


def first_present(source: Dict[str, Any], *keys: Any) -> Any:
    """Return the first non-None value from flat or nested keys."""
    for key in keys:
        if isinstance(key, tuple):
            value: Any = source
            for part in key:
                if not isinstance(value, dict) or part not in value:
                    value = None
                    break
                value = value[part]
            if value is not None:
                return value
        elif isinstance(source, dict) and key in source and source[key] is not None:
            return source[key]
    return None


def as_float(value: Any) -> Optional[float]:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def as_int(value: Any) -> Optional[int]:
    try:
        if value is None or value == "":
            return None
        return int(float(value))
    except (TypeError, ValueError):
        return None


def calculate_fmax_mhz(wns_ns: Any, clock_period_ns: Any) -> Optional[float]:
    wns = as_float(wns_ns)
    period = as_float(clock_period_ns)
    if wns is None or period is None or period <= 0:
        return None
    achievable_period = period - wns
    if achievable_period <= 0:
        return None
    return 1000.0 / achievable_period


def extract_metrics_from_context(context: Dict[str, Any]) -> Dict[str, Any]:
    """Extract common timing metrics from a v1/v2 analysis context or run summary."""
    if not isinstance(context, dict):
        return {}
    timing = context.get("timing") if isinstance(context.get("timing"), dict) else {}
    runtime = context.get("runtime") if isinstance(context.get("runtime"), dict) else {}
    metrics = {
        "clock_period_ns": first_present(context, "clock_period_ns", ("timing", "clock_period_ns")),
        "target_clock": first_present(context, "target_clock", ("timing", "target_clock"), ("timing", "clock_name")),
        "initial_wns_ns": first_present(context, "initial_wns_ns", "wns_ns", ("timing", "initial_wns_ns"), ("timing", "wns_ns")),
        "initial_tns_ns": first_present(context, "initial_tns_ns", "tns_ns", ("timing", "initial_tns_ns"), ("timing", "tns_ns")),
        "initial_fmax_mhz": first_present(context, "initial_fmax_mhz", "fmax_mhz", ("timing", "initial_fmax_mhz"), ("timing", "achievable_fmax_mhz")),
        "final_wns_ns": first_present(context, "final_wns_ns", ("timing", "final_wns_ns")),
        "final_tns_ns": first_present(context, "final_tns_ns", ("timing", "final_tns_ns")),
        "final_fmax_mhz": first_present(context, "final_fmax_mhz", ("timing", "final_fmax_mhz")),
        "failing_endpoints": first_present(context, "failing_endpoints", ("timing", "failing_endpoints")),
        "duration_seconds": first_present(context, "total_runtime_seconds", "duration_seconds", ("runtime", "duration_seconds")),
        "duration_minutes": first_present(context, "duration_minutes", ("runtime", "duration_minutes")),
    }
    if metrics["initial_fmax_mhz"] is None:
        metrics["initial_fmax_mhz"] = calculate_fmax_mhz(metrics["initial_wns_ns"], metrics["clock_period_ns"])
    if metrics["final_fmax_mhz"] is None:
        metrics["final_fmax_mhz"] = calculate_fmax_mhz(metrics["final_wns_ns"], metrics["clock_period_ns"])
    if metrics["duration_minutes"] is None and metrics["duration_seconds"] is not None:
        seconds = as_float(metrics["duration_seconds"])
        metrics["duration_minutes"] = None if seconds is None else seconds / 60.0
    return metrics


def build_analysis_context_v2(
    input_dcp: Path,
    provider_context: Optional[Dict[str, Any]],
    history: Iterable[DcpStateNode],
    config: Any,
    provider_error: Optional[str] = None,
    node_id: Optional[str] = None,
    parent_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Build a stable schema v2 selector/search context."""
    path = Path(input_dcp)
    raw = provider_context if isinstance(provider_context, dict) else {}
    history_items = [node_artifact_dict(node) for node in history]
    completed_history = [node for node in history if getattr(node, "metrics", None)]
    previous_metrics = completed_history[-1].metrics if completed_history else {}
    metrics = extract_metrics_from_context(raw)

    util = raw.get("utilization_summary") or raw.get("utilization") or {}
    if not isinstance(util, dict):
        util = {}

    flow_scores = _normalise_flow_heuristic_scores(raw.get("flow_scores") if isinstance(raw.get("flow_scores"), list) else [])
    raw_for_artifact = dict(raw)
    if isinstance(raw_for_artifact.get("flow_scores"), list):
        raw_for_artifact["flow_scores"] = flow_scores
    selected_top3 = raw.get("selected_top3") if isinstance(raw.get("selected_top3"), list) else []
    allowed_flows = [flow for flow in config.allowed_flows if flow]

    route_delay = first_present(raw, "route_delay_ns", ("critical_path_features", "avg_route_delay_ns"), ("timing", "route_delay_ns"))
    logic_delay = first_present(raw, "logic_delay_ns", ("critical_path_features", "avg_logic_delay_ns"), ("timing", "logic_delay_ns"))
    route_ratio = first_present(raw, "route_delay_ratio", ("critical_path_features", "route_delay_ratio"))
    r = as_float(route_delay)
    l = as_float(logic_delay)
    if route_ratio is None and r is not None and l is not None and (r + l) > 0:
        route_ratio = r / (r + l)

    dominant_path_type = raw.get("dominant_path_type") or first_present(raw, ("critical_path_features", "dominant_path_type"))
    if not dominant_path_type:
        ratio = as_float(route_ratio)
        if ratio is None:
            dominant_path_type = "unknown"
        elif ratio >= 0.65:
            dominant_path_type = "routing_dominated"
        elif ratio <= 0.35:
            dominant_path_type = "logic_dominated"
        else:
            dominant_path_type = "mixed"

    runtime = raw.get("runtime") if isinstance(raw.get("runtime"), dict) else {}

    context: Dict[str, Any] = {
        "schema_version": 2,
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "node_id": node_id,
        "parent_id": parent_id,
        "benchmark_name": raw.get("benchmark_name") or path.stem,
        "design": {
            "benchmark_name": raw.get("benchmark_name") or path.stem,
            "input_dcp": str(path),
            "input_dcp_name": path.name,
            "input_dcp_exists": path.exists(),
            "input_dcp_size_bytes": path.stat().st_size if path.exists() else None,
            "device": raw.get("device"),
        },
        "timing": {
            "target_clock": metrics.get("target_clock"),
            "clock_period_ns": as_float(metrics.get("clock_period_ns")),
            "initial_wns_ns": as_float(metrics.get("initial_wns_ns")),
            "initial_tns_ns": as_float(metrics.get("initial_tns_ns")),
            "initial_fmax_mhz": as_float(metrics.get("initial_fmax_mhz")),
            "final_wns_ns": as_float(metrics.get("final_wns_ns")),
            "final_tns_ns": as_float(metrics.get("final_tns_ns")),
            "final_fmax_mhz": as_float(metrics.get("final_fmax_mhz")),
            "failing_endpoints": as_int(metrics.get("failing_endpoints")),
            "multi_clock": raw.get("multi_clock"),
            "clock_count": raw.get("clock_count"),
        },
        "critical_path_features": {
            "num_paths_analyzed": first_present(raw, "num_paths_analyzed", ("critical_path_features", "num_paths_analyzed")),
            "avg_logic_levels": first_present(raw, "avg_logic_levels", ("critical_path_features", "avg_logic_levels")),
            "max_logic_levels": first_present(raw, "max_logic_levels", ("critical_path_features", "max_logic_levels")),
            "avg_route_delay_ns": as_float(route_delay),
            "avg_logic_delay_ns": as_float(logic_delay),
            "route_delay_ratio": as_float(route_ratio),
            "dominant_path_type": dominant_path_type,
            "hardblock_on_path": first_present(raw, "hardblock_on_path", ("critical_path_features", "hardblock_on_path")),
        },
        "congestion": {
            "report_available": bool(raw.get("congestion") or raw.get("congestion_report_available")),
            "has_overflow": first_present(raw, "has_overflow", ("congestion", "has_overflow")),
            "max_overflow": first_present(raw, "max_overflow", ("congestion", "max_overflow")),
            "hotspot_count": first_present(raw, "hotspot_count", ("congestion", "hotspot_count")),
        },
        "fanout": {
            "num_high_fanout_nets": first_present(raw, "num_high_fanout_nets", ("fanout", "num_high_fanout_nets")),
            "max_fanout": first_present(raw, "max_fanout", ("fanout", "max_fanout")),
            "top_nets": raw.get("top_high_fanout_nets") or first_present(raw, ("fanout", "top_nets")) or [],
        },
        "utilization_summary": {
            "luts": as_int(util.get("luts")),
            "ffs": as_int(util.get("ffs")),
            "dsps": as_int(util.get("dsps")),
            "brams": as_int(util.get("brams")),
            "urams": as_int(util.get("urams")),
        },
        "rapidwright": {
            "enabled_in_action_space": False,
            "critical_path_spread": {
                "avg_max_distance": first_present(raw, "avg_max_distance", ("rapidwright", "critical_path_spread", "avg_max_distance")),
                "max_distance": first_present(raw, "max_distance", ("rapidwright", "critical_path_spread", "max_distance")),
            },
            "net_detour": {
                "max_detour_ratio": first_present(raw, "max_detour_ratio", ("rapidwright", "net_detour", "max_detour_ratio")),
            },
        },
        "runtime": {
            "duration_seconds": as_float(runtime.get("duration_seconds") or metrics.get("duration_seconds")),
            "duration_minutes": as_float(runtime.get("duration_minutes") or metrics.get("duration_minutes")),
        },
        "candidate_flows": {
            "allowed_flows": allowed_flows,
            "deterministic_scores": flow_scores,
            "selected_top3": [flow for flow in selected_top3 if flow in allowed_flows] or [
                item.get("flow") for item in flow_scores if isinstance(item, dict) and item.get("flow") in allowed_flows
            ][: min(3, len(allowed_flows))],
            "fallback_flow": config.fallback_flow,
        },
        "expansion": {
            "policy_type": getattr(config, "policy_type", "deterministic"),
            "max_depth": config.max_depth,
            "beam_width": config.beam_width,
            "max_parallel_jobs": config.max_parallel_jobs,
            "flow_history": [flow for node in history for flow in getattr(node, "flow_sequence", []) if flow],
        },
        "previous_node_metrics": to_jsonable(previous_metrics),
        "history": history_items,
        "raw_provider_context": raw_for_artifact,
        "analysis_provider_error": provider_error,
    }

    # Keep v1 flat keys for existing selector/tests/backwards compatibility.
    context.update({
        "target_clock": context["timing"]["target_clock"],
        "clock_period_ns": context["timing"]["clock_period_ns"],
        "initial_wns_ns": context["timing"]["initial_wns_ns"],
        "initial_tns_ns": context["timing"]["initial_tns_ns"],
        "failing_endpoints": context["timing"]["failing_endpoints"],
        "initial_fmax_mhz": context["timing"]["initial_fmax_mhz"],
        "flow_scores": context["candidate_flows"]["deterministic_scores"],
        "selected_top3": context["candidate_flows"]["selected_top3"],
        "allowed_flows": context["candidate_flows"]["allowed_flows"],
        "fallback_flow": context["candidate_flows"]["fallback_flow"],
    })
    return context


def _normalise_flow_heuristic_scores(items: Iterable[Any]) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict) or not item.get("flow"):
            continue
        value = item.get("flow_heuristic_score")
        row = dict(item)
        row.pop("score", None)
        row["flow_heuristic_score"] = as_float(value) if value is not None else 0.0
        rows.append(row)
    return rows
