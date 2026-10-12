"""Trace-lake extraction for validation-gated FPGA flow search.

The trace lake turns a frontier-expansion experiment directory into two reusable data
sets:

* transitions.jsonl: executed state/action/outcome samples.
* candidates.jsonl: all planner-proposed candidates, including those not run.

These files are intentionally tabular/JSONL so that lightweight world-model and
reliability experiments can be run without opening Vivado artifacts again.
"""

from __future__ import annotations

import csv
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from experiments.llm.runtime.decision_events import llm_metadata_by_event_index, read_llm_decision_events
from optimizer_pipeline.reliability import write_jsonl


def export_trace_lake(
    experiment_roots: Iterable[Path],
    out_dir: Path,
    include_partial: bool = False,
    *,
    canonical_run_dirs: Optional[Iterable[str | Path]] = None,
    debug_run_dirs: Optional[Iterable[str | Path]] = None,
) -> Dict[str, Any]:
    roots = [Path(root) for root in experiment_roots]
    out_dir.mkdir(parents=True, exist_ok=True)
    runs: List[Dict[str, Any]] = []
    nodes: List[Dict[str, Any]] = []
    transitions: List[Dict[str, Any]] = []
    candidates: List[Dict[str, Any]] = []
    llm_decision_events: List[Dict[str, Any]] = []
    included_run_dirs: List[str] = []

    completed_run_dirs = set()
    for root in roots:
        for summary_path in sorted(root.rglob("pipeline_summary.json")):
            run_dir = summary_path.parent
            completed_run_dirs.add(run_dir)
            try:
                summary = json.loads(summary_path.read_text())
            except Exception:
                continue
            run_id = run_dir.name
            included_run_dirs.append(str(run_dir))
            design = _infer_design(run_dir, summary)
            run_record = _run_record(run_dir, summary, design)
            runs.append(run_record)
            run_llm_decision_events = read_llm_decision_events(run_dir)
            llm_decision_events.extend(_trace_llm_decision_event_rows(run_dir, design, run_llm_decision_events))

            node_list = summary.get("nodes") if isinstance(summary.get("nodes"), list) else []
            node_map = {str(node.get("node_id")): node for node in node_list if isinstance(node, dict)}
            for node in node_list:
                if not isinstance(node, dict):
                    continue
                node_row = _node_record(run_dir, summary, design, node)
                nodes.append(node_row)
                if node.get("parent_id"):
                    parent = node_map.get(str(node.get("parent_id")), {})
                    transitions.append(_transition_record(run_dir, summary, design, node, parent))

            candidates.extend(_candidate_records(run_dir, summary, design, node_map, run_llm_decision_events))
        if include_partial:
            for run_dir, summary in _partial_runs(root, completed_run_dirs):
                run_record = _run_record(run_dir, summary, _infer_design(run_dir, summary))
                run_record["artifact_kind"] = "partial_run"
                runs.append(run_record)
                included_run_dirs.append(str(run_dir))
                design = run_record["design"]
                run_llm_decision_events = read_llm_decision_events(run_dir)
                llm_decision_events.extend(_trace_llm_decision_event_rows(run_dir, design, run_llm_decision_events))
                node_list = summary.get("nodes") if isinstance(summary.get("nodes"), list) else []
                node_map = {str(node.get("node_id")): node for node in node_list if isinstance(node, dict)}
                for node in node_list:
                    if not isinstance(node, dict):
                        continue
                    nodes.append(_node_record(run_dir, summary, design, node))
                    if node.get("parent_id"):
                        parent = node_map.get(str(node.get("parent_id")), {})
                        transitions.append(_transition_record(run_dir, summary, design, node, parent))
                candidates.extend(_candidate_records(run_dir, summary, design, node_map, run_llm_decision_events))

    write_jsonl(out_dir / "transitions.jsonl", transitions)
    write_jsonl(out_dir / "candidates.jsonl", candidates)
    write_jsonl(out_dir / "candidates_all.jsonl", candidates)
    write_jsonl(out_dir / "llm_decision_events.jsonl", llm_decision_events)
    _write_csv(out_dir / "runs.csv", runs)
    _write_csv(out_dir / "nodes.csv", nodes)
    (out_dir / "runs.json").write_text(json.dumps(runs, indent=2) + "\n")
    manifest = {
        "experiment_roots": [str(root) for root in roots],
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "included_run_dirs": included_run_dirs,
        "canonical_run_dirs": [str(path) for path in (canonical_run_dirs or [])],
        "debug_run_dirs": [str(path) for path in (debug_run_dirs or [])],
        "num_runs": len(runs),
        "num_nodes": len(nodes),
        "num_transitions": len(transitions),
        "num_candidates": len(candidates),
        "num_llm_decision_events": len(llm_decision_events),
        "include_partial": include_partial,
        "outputs": {
            "runs_csv": str(out_dir / "runs.csv"),
            "nodes_csv": str(out_dir / "nodes.csv"),
            "transitions_jsonl": str(out_dir / "transitions.jsonl"),
            "candidates_jsonl": str(out_dir / "candidates.jsonl"),
            "candidates_all_jsonl": str(out_dir / "candidates_all.jsonl"),
            "llm_decision_events_jsonl": str(out_dir / "llm_decision_events.jsonl"),
        },
    }
    (out_dir / "trace_lake_manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    return manifest


def _partial_runs(root: Path, completed_run_dirs: set[Path]) -> List[Tuple[Path, Dict[str, Any]]]:
    candidates: List[Path] = []
    if (root / "pipeline_state.json").exists() and not (root / "pipeline_summary.json").exists():
        candidates.append(root)
    candidates.extend(
        state_path.parent
        for state_path in sorted(root.rglob("pipeline_state.json"))
        if not (state_path.parent / "pipeline_summary.json").exists()
    )
    out: List[Tuple[Path, Dict[str, Any]]] = []
    seen: set[Path] = set()
    for run_dir in candidates:
        if run_dir in seen or run_dir in completed_run_dirs:
            continue
        seen.add(run_dir)
        summary = _synthesize_partial_summary(run_dir)
        if summary:
            out.append((run_dir, summary))
    return out


def _synthesize_partial_summary(run_dir: Path) -> Dict[str, Any]:
    try:
        state = json.loads((run_dir / "pipeline_state.json").read_text())
    except Exception:
        return {}
    config = _read_json(run_dir / "config.json")
    graph = _read_json(run_dir / "state_graph.json")
    nodes = graph.get("nodes") if isinstance(graph.get("nodes"), list) else []
    return {
        "artifact_kind": "partial_run",
        "policy_type": config.get("policy_type"),
        "policy_model": config.get("model"),
        "scheduler_mode": config.get("scheduler_mode"),
        "max_total_runs": config.get("max_total_runs"),
        "max_depth": config.get("max_depth"),
        "analysis": {"enabled": bool(config.get("online_deep_analysis")) or (run_dir / "analysis").exists()},
        "state": state,
        "run_status": state.get("status"),
        "stop_reason": state.get("stop_reason"),
        "best_node_id": state.get("best_node_id"),
        "nodes": nodes,
        "nodes_total": len(nodes),
        "nodes_completed": len(state.get("completed_node_ids") or []),
        "nodes_failed": len(state.get("failed_node_ids") or []),
        "nodes_pruned": len(state.get("pruned_node_ids") or []),
        "root_dcp": state.get("root_dcp"),
    }


def _read_json(path: Path) -> Dict[str, Any]:
    try:
        data = json.loads(path.read_text())
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _run_record(run_dir: Path, summary: Dict[str, Any], design: str) -> Dict[str, Any]:
    record = {
        "run_id": run_dir.name,
        "run_dir": str(run_dir),
        "design": design,
        "policy_type": summary.get("policy_type") or summary.get("mode"),
        "model": _effective_model(summary),
        "policy_model": summary.get("policy_model"),
        "scheduler_mode": summary.get("scheduler_mode"),
        "max_total_runs": summary.get("max_total_runs"),
        "eda_runs": summary.get("eda_runs"),
        "nodes_total": summary.get("nodes_total"),
        "nodes_completed": summary.get("nodes_completed"),
        "nodes_failed": summary.get("nodes_failed"),
        "nodes_pruned": summary.get("nodes_pruned"),
        "best_node_id": summary.get("best_node_id"),
        "best_pure_qor_score": summary.get("best_pure_qor_score"),
        "best_sequence": " -> ".join(summary.get("best_flow_sequence") or []),
        "best_validation_status": summary.get("best_validation_status"),
        "total_wall_runtime_seconds": summary.get("total_wall_runtime_seconds"),
        "total_llm_calls": summary.get("total_llm_calls"),
        "total_prompt_tokens": summary.get("total_prompt_tokens"),
        "total_completion_tokens": summary.get("total_completion_tokens"),
        "total_tokens": summary.get("total_tokens"),
        "total_cost_usd": summary.get("total_cost_usd"),
        "total_provider_reported_cost_usd": summary.get("total_provider_reported_cost_usd"),
        "total_estimated_cost_usd": summary.get("total_estimated_cost_usd"),
        "cost_source": summary.get("cost_source"),
        "pricing_profile_id": summary.get("pricing_profile_id"),
        "total_latency_sec": summary.get("total_latency_sec"),
        "analysis_llm_calls": summary.get("analysis_llm_calls"),
        "analysis_tokens": summary.get("analysis_tokens"),
        "analysis_model": summary.get("analysis_model"),
        "analysis_provider": summary.get("analysis_provider"),
        "combined_llm_calls": summary.get("combined_llm_calls", summary.get("total_llm_calls")),
        "combined_tokens": summary.get("combined_tokens", summary.get("total_tokens")),
    }
    record.update(_summary_llm_fields(summary))
    return record


def _node_record(run_dir: Path, summary: Dict[str, Any], design: str, node: Dict[str, Any]) -> Dict[str, Any]:
    metrics = node.get("metrics") if isinstance(node.get("metrics"), dict) else {}
    qos = metrics.get("qor_score") if isinstance(metrics.get("qor_score"), dict) else {}
    components = qos.get("components") if isinstance(qos.get("components"), dict) else {}
    return {
        "run_id": run_dir.name,
        "run_dir": str(run_dir),
        "design": design,
        "node_id": node.get("node_id"),
        "parent_id": node.get("parent_id"),
        "depth": node.get("depth"),
        "flow": node.get("selected_flow") or "root",
        "flow_sequence": " -> ".join(node.get("flow_sequence") or []),
        "status": node.get("status"),
        "validation_status": node.get("validation_status"),
        "fallback_used": node.get("fallback_used"),
        "pure_qor_score": _node_pure_qor(node),
        "trajectory_objective_profile": "pure_qor_v1",
        "trajectory_objective_score": _node_pure_qor(node),
        "qor_status": qos.get("status"),
        "final_fmax_mhz": metrics.get("final_fmax_mhz") or metrics.get("fmax_mhz"),
        "final_wns_ns": metrics.get("final_wns_ns"),
        "final_tns_ns": metrics.get("final_tns_ns"),
        "delta_fmax_mhz": components.get("fmax_delta_mhz"),
        "delta_wns_ns": components.get("wns_improvement_ns"),
        "delta_tns_ns": components.get("tns_improvement_ns"),
        "node_runtime_seconds": metrics.get("node_runtime_seconds"),
        "path_runtime_seconds": metrics.get("path_runtime_seconds"),
        "pruned": _node_is_pruned(summary, node),
        "prune_reason": metrics.get("prune_reason") or metrics.get("policy_prune_reason"),
        "artifact_dir": node.get("artifact_dir"),
    }


def _transition_record(run_dir: Path, summary: Dict[str, Any], design: str, node: Dict[str, Any], parent: Dict[str, Any]) -> Dict[str, Any]:
    metrics = node.get("metrics") if isinstance(node.get("metrics"), dict) else {}
    parent_metrics = parent.get("metrics") if isinstance(parent.get("metrics"), dict) else {}
    parent_ctx = _analysis_context_for_node(run_dir, parent)
    node_ctx = _analysis_context_for_node(run_dir, node)
    parent_diag = _diagnostic_features(parent_metrics, parent_ctx)
    node_diag = _diagnostic_features(metrics, node_ctx)
    qos = metrics.get("qor_score") if isinstance(metrics.get("qor_score"), dict) else {}
    components = qos.get("components") if isinstance(qos.get("components"), dict) else {}
    parent_score = _safe_float(_node_pure_qor(parent), None)
    node_pure_qor = _safe_float(_node_pure_qor(node), None)
    flow = str(node.get("selected_flow") or "unknown")
    before = list(parent.get("flow_sequence") or [])
    after = list(node.get("flow_sequence") or [])
    validation_status = str(node.get("validation_status") or metrics.get("validation_status") or "CLEAN")
    invalid = validation_status == "INVALID" or qos.get("status") == "invalid"
    fallback_used = bool(node.get("fallback_used") or metrics.get("validation_fallback_used"))
    warning = validation_status == "WARNING"
    regressed = validation_status == "REGRESSED" or qos.get("status") == "regressed"
    legal_clean = not invalid
    useful_clean = legal_clean and not regressed
    record = {
        "sample_id": f"{run_dir.name}/{parent.get('node_id')}_to_{node.get('node_id')}",
        "design": design,
        "run_id": run_dir.name,
        "run_dir": str(run_dir),
        "policy_type": summary.get("policy_type") or summary.get("mode"),
        "model": _effective_model(summary),
        "policy_model": summary.get("policy_model"),
        "parent_node_id": parent.get("node_id"),
        "child_node_id": node.get("node_id"),
        "candidate_flow": flow,
        "flow_sequence_before": before,
        "flow_sequence_after": after,
        "state_before": {
            "depth": parent.get("depth"),
            "pure_qor_score": _node_pure_qor(parent),
            "fmax_mhz": parent_metrics.get("final_fmax_mhz") or parent_metrics.get("fmax_mhz"),
            "wns_ns": parent_metrics.get("final_wns_ns"),
            "tns_ns": parent_metrics.get("final_tns_ns"),
            "validation_status": parent.get("validation_status"),
            "node_runtime_seconds": parent_metrics.get("node_runtime_seconds"),
            "path_runtime_seconds": parent_metrics.get("path_runtime_seconds"),
            **parent_diag,
        },
        "state_after": {
            "depth": node.get("depth"),
            "pure_qor_score": _node_pure_qor(node),
            "fmax_mhz": metrics.get("final_fmax_mhz") or metrics.get("fmax_mhz"),
            "wns_ns": metrics.get("final_wns_ns"),
            "tns_ns": metrics.get("final_tns_ns"),
            "validation_status": validation_status,
            "node_runtime_seconds": metrics.get("node_runtime_seconds"),
            "path_runtime_seconds": metrics.get("path_runtime_seconds"),
            **node_diag,
        },
        "outcome": {
            "validation_status": validation_status,
            "qor_status": qos.get("status"),
            "delta_pure_qor": None if parent_score is None or node_pure_qor is None else node_pure_qor - parent_score,
            "delta_pure_qor_from_root": components.get("fmax_improvement_ratio"),
            "delta_fmax_mhz": components.get("fmax_delta_mhz"),
            "delta_wns_ns": components.get("wns_improvement_ns"),
            "delta_tns_ns": components.get("tns_improvement_ns"),
            "runtime_seconds": metrics.get("node_runtime_seconds"),
            "path_runtime_seconds": metrics.get("path_runtime_seconds"),
            "strict_clean": validation_status == "CLEAN" and not invalid,
            "legal_clean": legal_clean,
            "useful_clean": useful_clean,
            "invalid": invalid,
            "fallback_used": fallback_used,
            "warning": warning,
            "regressed": regressed,
            "pruned": _node_is_pruned(summary, node),
        },
        "logs": {
            "error_tags": metrics.get("validation_errors") or node.get("validation_errors") or [],
            "warning_tags": metrics.get("validation_warnings") or node.get("validation_warnings") or [],
            "log_paths": metrics.get("validation_log_paths") or [],
        },
        "policy_context": {
            "candidate": metrics.get("candidate"),
            "planner_priority": (metrics.get("candidate") or {}).get("priority") if isinstance(metrics.get("candidate"), dict) else None,
        },
    }
    record.update(_summary_llm_fields(summary))
    return record


def _read_jsonl(path: Path) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    try:
        with open(path, "r") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                obj = json.loads(line)
                if isinstance(obj, dict):
                    rows.append(obj)
    except Exception:
        return []
    return rows


def _candidate_records(
    run_dir: Path,
    summary: Dict[str, Any],
    design: str,
    node_map: Dict[str, Dict[str, Any]],
    llm_decision_events: Optional[List[Dict[str, Any]]] = None,
) -> List[Dict[str, Any]]:
    # : prefer the raw policy candidates when available because it carries
    # analysis-prior, rank-before/after, schedule-stage, and unexecuted-candidate
    # lifecycle fields that cannot be reconstructed from policy_events alone.
    raw_pool = run_dir / "policy_candidates.jsonl"
    if raw_pool.exists():
        raw_rows = _read_jsonl(raw_pool)
        rows = []
        existing = _executed_candidate_index(node_map)
        for idx, row in enumerate(raw_rows):
            if not isinstance(row, dict):
                continue
            target_id = row.get("target_node_id")
            flow = row.get("candidate_flow")
            child = existing.get((str(target_id), str(flow)))
            enriched = dict(row)
            enriched.setdefault("candidate_id", f"{run_dir.name}/policy_candidates_{idx}")
            enriched.setdefault("run_id", run_dir.name)
            enriched["run_dir"] = str(run_dir)
            enriched.setdefault("design", design)
            enriched.setdefault("policy_type", summary.get("policy_type") or summary.get("mode"))
            enriched.setdefault("model", _effective_model(summary))
            enriched.setdefault("policy_model", summary.get("policy_model"))
            _merge_llm_fields(enriched, _summary_llm_fields(summary))
            if child:
                enriched.setdefault("outcome_node_id", child.get("node_id"))
                enriched.setdefault("outcome_validation_status", child.get("validation_status"))
                enriched.setdefault("outcome_pure_qor_score", _node_pure_qor(child))
                enriched.setdefault("trajectory_objective_profile", "pure_qor_v1")
                enriched.setdefault("trajectory_objective_score", _node_pure_qor(child))
                enriched.setdefault("outcome_useful_clean", _candidate_useful_clean(child))
                enriched.setdefault("outcome_invalid", _candidate_invalid(child))
            rows.append(enriched)
        return rows

    rows: List[Dict[str, Any]] = []
    llm_metadata_by_event = llm_metadata_by_event_index(llm_decision_events or [])
    policy_path = run_dir / "policy_events.json"
    if not policy_path.exists():
        return rows
    try:
        events = json.loads(policy_path.read_text())
    except Exception:
        return rows
    existing = _executed_candidate_index(node_map)
    for event in events if isinstance(events, list) else []:
        decision = event.get("decision") if isinstance(event.get("decision"), dict) else {}
        validation = event.get("validation") if isinstance(event.get("validation"), dict) else {}
        candidates = decision.get("candidates") if isinstance(decision.get("candidates"), list) else []
        for idx, cand in enumerate(candidates):
            if not isinstance(cand, dict):
                continue
            target_id = cand.get("target_node_id") or decision.get("target_node_id")
            flow = cand.get("flow")
            key = (str(target_id), str(flow))
            child = existing.get(key)
            rows.append({
                "candidate_id": f"{run_dir.name}/event_{event.get('event_index')}_{idx}",
                "run_id": run_dir.name,
                "run_dir": str(run_dir),
                "design": design,
                "policy_type": summary.get("policy_type") or summary.get("mode"),
                "model": _effective_model(summary),
                "policy_model": summary.get("policy_model"),
                "event_index": event.get("event_index"),
                "target_node_id": target_id,
                "candidate_flow": flow,
                "priority": cand.get("priority"),
                "risk": cand.get("risk"),
                "confidence": cand.get("confidence"),
                "expected_effect": cand.get("expected_effect"),
                "decision_valid": bool(validation.get("valid", True)) if isinstance(validation, dict) else True,
                "validation_errors": validation.get("errors") if isinstance(validation, dict) else [],
                "candidate_rank": idx,
                "schedule_stage": "executed" if child is not None else "not_executed",
                "executed": child is not None,
                "outcome_node_id": child.get("node_id") if child else None,
                "outcome_validation_status": child.get("validation_status") if child else None,
                "outcome_pure_qor_score": _node_pure_qor(child) if child else None,
                "trajectory_objective_profile": "pure_qor_v1",
                "trajectory_objective_score": _node_pure_qor(child) if child else None,
                "outcome_useful_clean": _candidate_useful_clean(child) if child else None,
                "outcome_invalid": _candidate_invalid(child) if child else None,
                "reason_not_executed": None if child else _reason_not_executed(validation, target_id, flow),
                "decision_reason": decision.get("reason"),
                **_candidate_llm_fields(event, summary, llm_metadata_by_event),
            })
    return rows


def _executed_candidate_index(node_map: Dict[str, Dict[str, Any]]) -> Dict[Tuple[str, str], Dict[str, Any]]:
    index: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for node in node_map.values():
        parent_id = node.get("parent_id")
        flow = node.get("selected_flow")
        if parent_id and flow:
            index.setdefault((str(parent_id), str(flow)), node)
    return index


def _reason_not_executed(validation: Dict[str, Any], target_id: Any, flow: Any) -> str:
    errors = validation.get("errors") if isinstance(validation.get("errors"), list) else []
    if errors:
        joined = "; ".join(str(e) for e in errors)
        if str(flow) in joined or str(target_id) in joined:
            return joined[:500]
        return "candidate_not_scheduled_after_validation"
    return "not_selected_or_budget_exhausted"


def _infer_design(run_dir: Path, summary: Dict[str, Any]) -> str:
    root = summary.get("root_metrics") if isinstance(summary.get("root_metrics"), dict) else {}
    if root.get("benchmark_name"):
        return str(root.get("benchmark_name"))
    nodes = summary.get("nodes") if isinstance(summary.get("nodes"), list) else []
    for node in nodes:
        metrics = node.get("metrics") if isinstance(node, dict) and isinstance(node.get("metrics"), dict) else {}
        if metrics.get("benchmark_name"):
            return str(metrics.get("benchmark_name"))
    return run_dir.name.split("__")[0]


def _node_is_pruned(summary: Dict[str, Any], node: Dict[str, Any]) -> bool:
    state = summary.get("state") if isinstance(summary.get("state"), dict) else {}
    pruned_ids = set(state.get("pruned_node_ids") or [])
    metrics = node.get("metrics") if isinstance(node.get("metrics"), dict) else {}
    return str(node.get("node_id")) in pruned_ids or bool(metrics.get("pruned_by_qor_gate") or metrics.get("pruned_by_policy"))


def _effective_model(summary: Dict[str, Any]) -> Optional[str]:
    calls = summary.get("total_llm_calls")
    try:
        if calls is not None and int(calls) <= 0:
            return None
    except Exception:
        pass
    policy = str(summary.get("policy_type") or summary.get("mode") or "")
    if policy in {"deterministic", "reliability", "retrieval"}:
        return None
    return summary.get("model") or summary.get("llm_model")


def _summary_llm_fields(summary: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "llm_provider": summary.get("llm_provider"),
        "llm_model": summary.get("llm_model") or _effective_model(summary),
        "llm_prompt_id": summary.get("llm_prompt_id"),
        "llm_prompt_hash": summary.get("llm_prompt_hash"),
        "llm_response_schema_hash": summary.get("llm_response_schema_hash"),
        "llm_temperature": summary.get("llm_temperature"),
        "llm_max_tokens": summary.get("llm_max_tokens") if summary.get("llm_max_tokens") is not None else ((summary.get("config") or {}).get("max_tokens") if isinstance(summary.get("config"), dict) else None),
        "llm_response_mode": summary.get("llm_response_mode"),
        "llm_cost_source": summary.get("cost_source"),
        "llm_pricing_profile_id": summary.get("pricing_profile_id"),
    }


def _candidate_llm_fields(event: Dict[str, Any], summary: Dict[str, Any], llm_metadata_by_event: Dict[Any, Dict[str, Any]]) -> Dict[str, Any]:
    fields = _summary_llm_fields(summary)
    metadata = llm_metadata_by_event.get(event.get("event_index"), {})
    if not metadata:
        return fields
    fields.update({
        "llm_provider": metadata.get("provider", fields.get("llm_provider")),
        "llm_model": metadata.get("model", fields.get("llm_model")),
        "llm_prompt_id": metadata.get("prompt_id", fields.get("llm_prompt_id")),
        "llm_prompt_hash": metadata.get("prompt_hash", fields.get("llm_prompt_hash")),
        "llm_response_schema_hash": metadata.get("response_schema_hash", fields.get("llm_response_schema_hash")),
        "llm_temperature": metadata.get("temperature", fields.get("llm_temperature")),
        "llm_max_tokens": metadata.get("max_tokens", fields.get("llm_max_tokens")),
        "llm_response_mode": metadata.get("response_mode", fields.get("llm_response_mode")),
        "llm_prompt_tokens": metadata.get("prompt_tokens"),
        "llm_completion_tokens": metadata.get("completion_tokens"),
        "llm_total_tokens": metadata.get("total_tokens"),
        "llm_cost_usd": metadata.get("cost_usd"),
        "llm_provider_reported_cost_usd": metadata.get("provider_reported_cost_usd"),
        "llm_estimated_cost_usd": metadata.get("estimated_cost_usd"),
        "llm_cost_source": metadata.get("cost_source"),
        "llm_pricing_profile_id": metadata.get("pricing_profile_id"),
        "llm_latency_sec": metadata.get("latency_sec"),
        "llm_error": metadata.get("error"),
    })
    return fields


def _trace_llm_decision_event_rows(run_dir: Path, design: str, rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    output: List[Dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        enriched = dict(row)
        enriched.setdefault("run_id", run_dir.name)
        enriched["run_dir"] = str(run_dir)
        enriched.setdefault("design", design)
        output.append(enriched)
    return output


def _merge_llm_fields(row: Dict[str, Any], fields: Dict[str, Any]) -> None:
    for key, value in fields.items():
        row.setdefault(key, value)


def _analysis_context_for_node(run_dir: Path, node: Dict[str, Any]) -> Dict[str, Any]:
    metrics = node.get("metrics") if isinstance(node.get("metrics"), dict) else {}
    path = metrics.get("analysis_context_path")
    candidates = []
    if path:
        candidates.append(Path(path))
    artifact_dir = node.get("artifact_dir")
    if artifact_dir:
        candidates.append(Path(artifact_dir) / "analysis_context.json")
    node_id = node.get("node_id")
    if node_id:
        candidates.extend(run_dir.glob(f"**/{node_id}/analysis_context.json"))
    for candidate in candidates:
        try:
            if candidate.exists():
                data = json.loads(candidate.read_text())
                if isinstance(data, dict):
                    return data
        except Exception:
            continue
    return {}


def _diagnostic_features(metrics: Dict[str, Any], ctx: Dict[str, Any]) -> Dict[str, Any]:
    timing = ctx.get("timing") if isinstance(ctx.get("timing"), dict) else {}
    critical = ctx.get("critical_path_features") if isinstance(ctx.get("critical_path_features"), dict) else {}
    congestion = ctx.get("congestion") if isinstance(ctx.get("congestion"), dict) else {}
    fanout = ctx.get("fanout") if isinstance(ctx.get("fanout"), dict) else {}
    util = ctx.get("utilization_summary") if isinstance(ctx.get("utilization_summary"), dict) else {}
    return {
        "failing_endpoints": metrics.get("failing_endpoints") or timing.get("failing_endpoints"),
        "route_delay_ratio": metrics.get("route_delay_ratio") or critical.get("route_delay_ratio"),
        "avg_logic_levels": metrics.get("avg_logic_levels") or critical.get("avg_logic_levels"),
        "max_logic_levels": metrics.get("max_logic_levels") or critical.get("max_logic_levels"),
        "num_paths_analyzed": metrics.get("num_paths_analyzed") or critical.get("num_paths_analyzed"),
        "dominant_path_type": metrics.get("dominant_path_type") or critical.get("dominant_path_type"),
        "hardblock_on_path": metrics.get("hardblock_on_path") or critical.get("hardblock_on_path"),
        "congestion_report_available": congestion.get("report_available"),
        "congestion_has_overflow": congestion.get("has_overflow"),
        "congestion_max_overflow": congestion.get("max_overflow"),
        "congestion_hotspot_count": congestion.get("hotspot_count"),
        "num_high_fanout_nets": metrics.get("num_high_fanout_nets") or fanout.get("num_high_fanout_nets"),
        "max_fanout": metrics.get("max_fanout") or fanout.get("max_fanout"),
        "util_luts": util.get("luts"),
        "util_ffs": util.get("ffs"),
        "util_dsps": util.get("dsps"),
        "util_brams": util.get("brams"),
    }


def _candidate_invalid(child: Optional[Dict[str, Any]]) -> Optional[bool]:
    if not child:
        return None
    metrics = child.get("metrics") if isinstance(child.get("metrics"), dict) else {}
    qos = metrics.get("qor_score") if isinstance(metrics.get("qor_score"), dict) else {}
    status = str(child.get("validation_status") or metrics.get("validation_status") or "")
    return status == "INVALID" or qos.get("status") == "invalid"


def _candidate_useful_clean(child: Optional[Dict[str, Any]]) -> Optional[bool]:
    if not child:
        return None
    metrics = child.get("metrics") if isinstance(child.get("metrics"), dict) else {}
    qos = metrics.get("qor_score") if isinstance(metrics.get("qor_score"), dict) else {}
    status = str(child.get("validation_status") or metrics.get("validation_status") or "")
    invalid = status == "INVALID" or qos.get("status") == "invalid"
    regressed = status == "REGRESSED" or qos.get("status") == "regressed"
    return not invalid and not regressed


def _node_pure_qor(node: Dict[str, Any]) -> Any:
    if not isinstance(node, dict):
        return None
    value = node.get("pure_qor_score")
    if value is None:
        value = node.get("trajectory_objective_score")
    return value


def _safe_float(value: Any, default: Optional[float] = 0.0) -> Optional[float]:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _write_csv(path: Path, rows: List[Dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("")
        return
    fieldnames: List[str] = list(rows[0].keys())
    for row in rows[1:]:
        for key in row.keys():
            if key not in fieldnames:
                fieldnames.append(key)
    with open(path, "w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
