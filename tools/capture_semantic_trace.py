#!/usr/bin/env python3
"""Normalize a legacy run directory into a semantic migration trace."""

from __future__ import annotations

import argparse
from collections import OrderedDict
import hashlib
import json
from pathlib import Path
import sys
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from optimizer_pipeline.core.store import SQLiteRunStore


def read_json(path: Path, default: Any = None) -> Any:
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return default


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return rows
    for line in lines:
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def sha256(path: Path | None) -> str | None:
    if path is None or not path.is_file():
        return None
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def latest_attempt_records(run_dir: Path, summary: dict[str, Any]) -> list[dict[str, Any]]:
    journal = read_jsonl(run_dir / "transition_journal.jsonl")
    if not journal:
        return [row for row in summary.get("transition_records", []) if isinstance(row, dict)]
    latest: OrderedDict[str, dict[str, Any]] = OrderedDict()
    for row in journal:
        intent = row.get("intent") if isinstance(row.get("intent"), dict) else {}
        transition_id = str(intent.get("transition_id") or f"attempt_{len(latest):03d}")
        latest[transition_id] = row
    return list(latest.values())


def root_state_id(summary: dict[str, Any]) -> str:
    state_graph = summary.get("state_graph") if isinstance(summary.get("state_graph"), list) else []
    for state in state_graph:
        if isinstance(state, dict) and state.get("parent_id") is None and state.get("node_id"):
            return str(state["node_id"])
    return "node_000_root"


def selected_trace_row(run_dir: Path, published: dict[str, Any]) -> dict[str, Any]:
    rows = read_jsonl(run_dir / "official_contest_trace.jsonl")
    selected_transition = published.get("transition_id")
    selected_node = published.get("node_id")
    for row in reversed(rows):
        if selected_transition and row.get("transition_id") == selected_transition:
            return row
        if selected_node and row.get("node_id") == selected_node:
            return row
    return rows[0] if rows else {}


def canonical_trace(run_dir: Path, *, case_id: str) -> dict[str, Any]:
    if (run_dir / "run.db").is_file():
        return canonical_db_trace(run_dir, case_id=case_id)
    summary = read_json(run_dir / "pipeline_summary.json", {})
    if not isinstance(summary, dict) or not summary:
        raise RuntimeError(f"pipeline_summary.json is missing or invalid in {run_dir}")
    config = read_json(run_dir / "config.json", {})
    config = config if isinstance(config, dict) else {}
    published = read_json(run_dir / "published_best.json", summary.get("published_best") or {})
    published = published if isinstance(published, dict) else {}
    records = latest_attempt_records(run_dir, summary)

    raw_root_id = root_state_id(summary)
    state_names: dict[str, str] = {raw_root_id: "state_000_root"}

    def canonical_state(raw_id: Any) -> str | None:
        if raw_id is None:
            return None
        key = str(raw_id)
        if key not in state_names:
            state_names[key] = f"state_{len(state_names):03d}"
        return state_names[key]

    attempts: list[dict[str, Any]] = []
    action_sequence: list[str] = []
    total_runtime = 0.0
    for index, record in enumerate(records):
        intent = record.get("intent") if isinstance(record.get("intent"), dict) else {}
        outcome = record.get("outcome") if isinstance(record.get("outcome"), dict) else None
        handling = record.get("handling") if isinstance(record.get("handling"), dict) else {}
        metadata = intent.get("metadata") if isinstance(intent.get("metadata"), dict) else {}
        action = str(intent.get("optimizer_name") or "unknown")
        action_sequence.append(action)
        parent = canonical_state(intent.get("parent_node_id") or raw_root_id)
        child_raw = outcome.get("child_node_id") if outcome else None
        original_child_raw = metadata.get("original_transition_id")
        child = None
        if child_raw is not None or original_child_raw is not None:
            known = next(
                (state_names[str(raw)] for raw in (child_raw, original_child_raw) if raw is not None and str(raw) in state_names),
                None,
            )
            child = known or f"state_{len(set(state_names.values())):03d}"
            for raw in (child_raw, original_child_raw):
                if raw is not None:
                    state_names[str(raw)] = child
        validation = str(outcome.get("validation_status") or "NOT_RECORDED") if outcome else "NOT_RECORDED"
        accepted = bool(outcome and outcome.get("accepted_by_transition_executor"))
        if outcome is None:
            lifecycle = "orphaned_scheduled"
        elif accepted:
            lifecycle = "accepted"
        else:
            lifecycle = "rejected"
        metrics = outcome.get("metrics") if outcome and isinstance(outcome.get("metrics"), dict) else {}
        publish_eligible = metrics.get("publish_eligible")
        selection_eligible = bool(accepted if publish_eligible is None else accepted and publish_eligible)
        parent_eligible = bool(accepted and validation.upper() not in {"INVALID", "MISSING", "FAILED"})
        runtime_seconds = float(outcome.get("runtime_seconds") or 0.0) if outcome else 0.0
        total_runtime += runtime_seconds
        attempts.append(
            {
                "attempt_index": index,
                "action": action,
                "source": intent.get("source"),
                "parent_state": parent,
                "output_state": child,
                "lifecycle": lifecycle,
                "validation_class": validation,
                "accepted": accepted,
                "parent_eligible": parent_eligible,
                "selection_eligible": selection_eligible,
                "updated_incumbent": bool(handling.get("updated_incumbent")),
                "runtime_seconds": runtime_seconds,
            }
        )

    selected_raw = published.get("node_id") or raw_root_id
    selected_state = canonical_state(selected_raw)
    selected_row = selected_trace_row(run_dir, published)
    output_path_value = published.get("output_path") or summary.get("contest_output_dcp")
    output_path = Path(output_path_value) if output_path_value else None
    input_value = summary.get("input_dcp")
    input_path = Path(input_value) if input_value else None
    selected_valid = selected_row.get("valid_for_scoring")
    if selected_valid is None:
        selected_valid = published.get("transition_id") == "baseline" or any(
            item["output_state"] == selected_state and item["selection_eligible"] for item in attempts
        )

    budget = summary.get("budget") if isinstance(summary.get("budget"), dict) else {}
    validation_classes = [item["validation_class"] for item in attempts]
    selected_fmax = published.get("published_best_fmax_mhz")
    if selected_fmax is None:
        selected_fmax = selected_row.get("fmax_output_mhz")
    root_metrics = summary.get("root_metrics") if isinstance(summary.get("root_metrics"), dict) else {}
    resolved_config_keys = (
        "policy_type",
        "max_total_runs",
        "max_parallel_jobs",
        "allowed_flows",
        "fallback_flow",
        "rescue_mode",
        "rescue_trigger",
        "rescue_max_runs",
    )
    return {
        "schema_version": "semantic_trace.v1",
        "case_id": case_id,
        "run": {
            "success": bool(summary.get("success")),
            "mode": summary.get("mode"),
            "pipeline": summary.get("pipeline"),
        },
        "resolved_config": {key: config.get(key) for key in resolved_config_keys if key in config},
        "input_sha256": sha256(input_path),
        "action_sequence": action_sequence,
        "attempt_count": len(attempts),
        "attempts": attempts,
        "parent_lineage": [
            {
                "attempt_index": item["attempt_index"],
                "parent_state": item["parent_state"],
                "output_state": item["output_state"],
            }
            for item in attempts
        ],
        "budget_events": [
            {
                "attempt_index": item["attempt_index"],
                "reserved": True,
                "completion": item["lifecycle"],
            }
            for item in attempts
        ],
        "budget_summary": {
            key: budget.get(key)
            for key in ("max_eda_runs", "scheduled_eda_runs", "completed_eda_runs", "failed_eda_runs", "remaining_eda_runs")
        },
        "validation_classes": validation_classes,
        "selection": {
            "state": selected_state,
            "baseline_fallback": published.get("transition_id") == "baseline",
            "valid_for_publication": bool(selected_valid),
            "reason": published.get("reason"),
        },
        "output_exists": bool(output_path and output_path.is_file()),
        "selected_output_sha256": sha256(output_path),
        "baseline_fmax_mhz": root_metrics.get("initial_fmax_mhz"),
        "final_fmax_mhz": selected_fmax,
        "baseline_wns_ns": root_metrics.get("initial_wns_ns"),
        "final_wns_ns": selected_row.get("wns_ns"),
        "runtime": {
            "recorded_attempt_seconds": total_runtime,
            "per_attempt_seconds": [item["runtime_seconds"] for item in attempts],
        },
        "certification_result": selected_row.get("functional_equivalence_status", "NOT_RUN"),
    }


def canonical_db_trace(run_dir: Path, *, case_id: str) -> dict[str, Any]:
    store = SQLiteRunStore(run_dir / "run.db")
    manifest = read_json(run_dir / "result_manifest.json", {}) or {}
    run = store.get_run()
    states = list(store.states())
    artifacts = {item.artifact_id: item for item in store.artifacts()}
    actions = {item.action_id: item for item in store.actions()}
    outcomes = {item.attempt_id: item for item in store.outcomes()}
    validations = {item.subject_id: item for item in store.validations()}
    eligibility = {item.state_id: item for item in store.eligibility()}
    events = store.attempt_events()
    events_by_attempt: dict[str, list[Any]] = {}
    for event in events:
        events_by_attempt.setdefault(event.attempt_id, []).append(event)
    canonical_names = {"node_000_root": "state_000_root"}

    def canonical(raw: str | None) -> str | None:
        if raw is None:
            return None
        if raw not in canonical_names:
            canonical_names[raw] = f"state_{len(canonical_names):03d}"
        return canonical_names[raw]

    state_by_attempt = {state.produced_by_attempt_id: state for state in states if state.produced_by_attempt_id}
    attempts = []
    total_runtime = 0.0
    for index, attempt in enumerate(store.attempts()):
        action = actions[attempt.action_id]
        outcome = outcomes.get(attempt.attempt_id)
        state = state_by_attempt.get(attempt.attempt_id)
        validation = validations.get(state.state_id) if state else None
        eligible = eligibility.get(state.state_id) if state else None
        kinds = {event.kind.value for event in events_by_attempt.get(attempt.attempt_id, [])}
        lifecycle = "orphaned_scheduled" if outcome is None else "accepted" if outcome.status.value == "SUCCEEDED" and validation and validation.status.value != "INVALID" else "rejected"
        runtime = 0.0
        if outcome:
            metrics = outcome.metrics.to_dict()
            runtime = float(metrics.get("node_runtime_seconds") or metrics.get("total_runtime_seconds") or 0.0)
        total_runtime += runtime
        attempts.append({
            "attempt_index": index,
            "action": action.action_type,
            "source": next((proposal.source for proposal in store.proposals() if proposal.proposal_id == attempt.proposal_id), None),
            "parent_state": canonical(attempt.parent_state_id),
            "output_state": canonical(state.state_id) if state else None,
            "lifecycle": lifecycle,
            "validation_class": validation.status.value if outcome is not None and validation else "NOT_RECORDED",
            "accepted": bool(outcome and outcome.status.value == "SUCCEEDED"),
            "parent_eligible": bool(eligible and eligible.parent_eligible),
            "selection_eligible": bool(eligible and eligible.selection_eligible),
            "updated_incumbent": bool(eligible and eligible.selection_eligible),
            "runtime_seconds": runtime,
        })
    selections = list(store.selections())
    selected = selections[-1]
    selected_state = canonical(selected.state_id)
    selected_artifact = artifacts.get(selected.artifact_id)
    root_state = next(state for state in states if state.parent_state_id is None)
    root_artifact = artifacts[root_state.artifact_id]
    root_metrics = root_state.metadata.to_dict().get("metrics", {})
    selected_state_fact = next(state for state in states if state.state_id == selected.state_id)
    selected_metrics = selected_state_fact.metadata.to_dict().get("metrics", {})
    budget = manifest.get("budget") if isinstance(manifest.get("budget"), dict) else {}
    result = {
        "schema_version": "semantic_trace.v1",
        "case_id": case_id,
        "run": {"success": manifest.get("success", bool(run)), "mode": run.mode if run else None, "pipeline": manifest.get("pipeline")},
        "resolved_config": {"max_parallel_jobs": budget.get("max_parallel_jobs")},
        "input_sha256": sha256(Path(root_artifact.uri)),
        "action_sequence": [item["action"] for item in attempts],
        "attempt_count": len(attempts),
        "attempts": attempts,
        "parent_lineage": [{"attempt_index": item["attempt_index"], "parent_state": item["parent_state"], "output_state": item["output_state"]} for item in attempts],
        "budget_events": [{"attempt_index": item["attempt_index"], "reserved": True, "completion": item["lifecycle"]} for item in attempts],
        "budget_summary": {key: budget.get(key) for key in ("max_eda_runs", "scheduled_eda_runs", "completed_eda_runs", "failed_eda_runs", "remaining_eda_runs")},
        "validation_classes": [item["validation_class"] for item in attempts],
        "selection": {
            "state": selected_state,
            "baseline_fallback": selected.kind.value == "BASELINE",
            "valid_for_publication": selected.kind.value == "BASELINE" or bool(eligibility.get(selected.state_id) and eligibility[selected.state_id].selection_eligible),
            "reason": selected.reason,
        },
        "output_exists": bool(selected_artifact and Path(selected_artifact.uri).is_file()),
        "selected_output_sha256": sha256(Path(selected_artifact.uri)) if selected_artifact else None,
        "baseline_fmax_mhz": _first_metric(root_metrics, "initial_fmax_mhz", "final_fmax_mhz", "fmax_mhz"),
        "final_fmax_mhz": _first_metric(selected_metrics, "final_fmax_mhz", "initial_fmax_mhz", "fmax_mhz"),
        "baseline_wns_ns": _first_metric(root_metrics, "initial_wns_ns", "final_wns_ns"),
        "final_wns_ns": _first_metric(selected_metrics, "final_wns_ns", "initial_wns_ns"),
        "runtime": {"recorded_attempt_seconds": total_runtime, "per_attempt_seconds": [item["runtime_seconds"] for item in attempts]},
        "certification_result": next((item.status.value for item in reversed(store.validations(subject_id=selected.state_id)) if item.level.value == "CERTIFICATION"), "NOT_RUN"),
    }
    store.close()
    return result


def _first_metric(metrics: Any, *keys: str) -> Any:
    data = metrics if isinstance(metrics, dict) else {}
    timing = data.get("timing") if isinstance(data.get("timing"), dict) else {}
    for key in keys:
        if data.get(key) is not None:
            return data[key]
        if timing.get(key) is not None:
            return timing[key]
    return None


def canonical_db_trace(run_dir: Path, *, case_id: str) -> dict[str, Any]:
    from optimizer_pipeline.core.store import load_run_view

    view = load_run_view(run_dir)
    manifest = read_json(run_dir / "result_manifest.json", {})
    state_names = {"node_000_root": "state_000_root"}

    def state_name(raw: Any) -> str | None:
        if raw is None:
            return None
        key = str(raw)
        if key not in state_names:
            state_names[key] = f"state_{len(state_names):03d}"
        return state_names[key]

    attempts = []
    total_runtime = 0.0
    for index, row in enumerate(view["attempts"]):
        runtime = float(row["metrics"].get("node_runtime_seconds") or 0.0)
        total_runtime += runtime
        lifecycle = "orphaned_scheduled" if row["status"] == "ORPHANED" else (
            "accepted" if row["status"] == "SUCCEEDED" and row["validation_status"] != "INVALID" else "rejected"
        )
        attempts.append({
            "attempt_index": index,
            "action": row["action_type"],
            "source": row["source"],
            "parent_state": state_name(row["parent_state_id"]),
            "output_state": state_name(row["output_state_id"]),
            "lifecycle": lifecycle,
            "validation_class": row["validation_status"],
            "accepted": lifecycle == "accepted",
            "parent_eligible": row["parent_eligible"],
            "selection_eligible": row["selection_eligible"],
            "updated_incumbent": row["selected_latest"],
            "runtime_seconds": runtime,
        })
    selected_state = state_name(view["selected_state_id"])
    output_value = manifest.get("contest_output_dcp") or manifest.get("final_dcp")
    output_path = Path(output_value) if output_value else None
    input_path = Path(manifest["input_dcp"]) if manifest.get("input_dcp") else None
    selected_attempt = next((row for row in reversed(view["attempts"]) if row["selected_latest"]), None)
    selected_metrics = selected_attempt["metrics"] if selected_attempt else view.get("root_metrics", {})
    root_metrics = view.get("root_metrics", {})
    budget = manifest.get("budget") if isinstance(manifest.get("budget"), dict) else {}
    return {
        "schema_version": "semantic_trace.v1",
        "case_id": case_id,
        "run": {"success": view["run_status"] == "FINALIZED", "mode": view["mode"], "pipeline": manifest.get("pipeline")},
        "resolved_config": {"max_parallel_jobs": budget.get("max_parallel_jobs")},
        "input_sha256": sha256(input_path),
        "action_sequence": [row["action_type"] for row in view["attempts"]],
        "attempt_count": len(attempts),
        "attempts": attempts,
        "parent_lineage": [{"attempt_index": row["attempt_index"], "parent_state": row["parent_state"], "output_state": row["output_state"]} for row in attempts],
        "budget_events": [{"attempt_index": row["attempt_index"], "reserved": True, "completion": row["lifecycle"]} for row in attempts],
        "budget_summary": {key: budget.get(key) for key in ("max_eda_runs", "scheduled_eda_runs", "completed_eda_runs", "failed_eda_runs", "remaining_eda_runs")},
        "validation_classes": [row["validation_class"] for row in attempts],
        "selection": {
            "state": selected_state,
            "baseline_fallback": view["selected_state_id"] == "node_000_root",
            "valid_for_publication": view["selected_state_id"] == "node_000_root" or bool(selected_attempt and selected_attempt["selection_eligible"]),
            "reason": "canonical best-valid selection",
        },
        "output_exists": bool(output_path and output_path.is_file()),
        "selected_output_sha256": sha256(output_path),
        "baseline_fmax_mhz": root_metrics.get("initial_fmax_mhz"),
        "final_fmax_mhz": selected_metrics.get("final_fmax_mhz") or root_metrics.get("initial_fmax_mhz"),
        "baseline_wns_ns": root_metrics.get("initial_wns_ns"),
        "final_wns_ns": selected_metrics.get("final_wns_ns") or root_metrics.get("initial_wns_ns"),
        "runtime": {"recorded_attempt_seconds": total_runtime, "per_attempt_seconds": [row["runtime_seconds"] for row in attempts]},
        "certification_result": (view.get("validation", {}).get("CERTIFICATION", {}).get("status") or "NOT_RUN"),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_dir", type=Path)
    parser.add_argument("--case-id", required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    try:
        trace = canonical_trace(args.run_dir.resolve(), case_id=args.case_id)
    except RuntimeError as exc:
        parser.error(str(exc))
    rendered = json.dumps(trace, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.write_text(rendered)
    else:
        print(rendered, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
