#!/usr/bin/env python3
"""Track-separated descriptive analysis for WP-014C repeat-1 evidence."""

from __future__ import annotations

import argparse
import collections
import csv
import hashlib
import json
from pathlib import Path
import statistics
from typing import Any, Iterable, Mapping


TRACK_BY_PHASE = {
    "N_K": "N_NEUTRAL",
    "S_N": "S_NEUTRAL_REDUCED",
    "C_S": "C_CONDITIONED",
}
CONDITION_LEVEL = {"M_ONLY": "L2", "P_BOUNDARY": "L3", "G_BOUNDARY": "L4"}


def canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def digest(value: Any) -> str:
    return hashlib.sha256(value if isinstance(value, bytes) else canonical(value)).hexdigest()


def read(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text())
    if not isinstance(value, dict):
        raise ValueError("json_object_required:" + str(path))
    return value


def verify(value: dict[str, Any], hash_key: str) -> None:
    claimed = value.pop(hash_key)
    if digest(value) != claimed:
        raise ValueError("hash_invalid:" + hash_key)
    value[hash_key] = claimed


def final_compiled(row: Mapping[str, Any]) -> Mapping[str, Any] | None:
    for turn in reversed(list(row.get("turns", []))):
        result = turn.get("planner_result", {})
        if result.get("status") == "COMMITTED" and isinstance(result.get("compiled"), Mapping):
            return result["compiled"]
    return None


def candidate_labels(candidate: Mapping[str, Any] | None) -> tuple[list[str], list[str]]:
    actions: set[str] = set()
    operations: set[str] = set()

    def visit(value: Any) -> None:
        if isinstance(value, Mapping):
            action = value.get("action_key")
            if isinstance(action, str):
                actions.add(action)
            for key in ("op", "node"):
                operation = value.get(key)
                if isinstance(operation, str):
                    operations.add(operation)
            for item in value.values():
                visit(item)
        elif isinstance(value, list):
            for item in value:
                visit(item)

    visit(candidate)
    return sorted(actions), sorted(operations)


def compiled_mechanisms(row: Mapping[str, Any]) -> list[str]:
    compiled = final_compiled(row)
    if compiled is None:
        return []
    return sorted({
        str(step["operation"])
        for step in compiled.get("host_steps", [])
        if isinstance(step, Mapping) and isinstance(step.get("operation"), str)
    })


def fmax(period_ns: float, wns_ns: float) -> float | None:
    denominator = period_ns - wns_ns
    return 1000.0 / denominator if denominator > 0 else None


def outcome(row: Mapping[str, Any], period_ns: float) -> tuple[float | None, float | None, float | None]:
    if row.get("canonical_terminal") != "VALID_COMPLETION":
        return None, None, None
    execution = row.get("execution", {})
    initial = execution.get("initial_fmax_mhz")
    final = execution.get("final_fmax_mhz")
    if not isinstance(initial, (int, float)):
        wns = execution.get("pre_timing", {}).get("wns")
        initial = fmax(period_ns, float(wns)) if isinstance(wns, (int, float)) else None
    if not isinstance(final, (int, float)):
        wns = execution.get("post_timing", {}).get("wns")
        final = fmax(period_ns, float(wns)) if isinstance(wns, (int, float)) else None
    value = execution.get("fmax_improvement_mhz")
    improvement = (
        float(value) if isinstance(value, (int, float))
        else float(final) - float(initial)
        if isinstance(initial, (int, float)) and isinstance(final, (int, float))
        else None
    )
    return (
        float(initial) if isinstance(initial, (int, float)) else None,
        float(final) if isinstance(final, (int, float)) else None,
        improvement,
    )


def mean(values: Iterable[float]) -> float | None:
    rows = list(values)
    return statistics.mean(rows) if rows else None


def median(values: Iterable[float]) -> float | None:
    rows = list(values)
    return statistics.median(rows) if rows else None


def candidate_map(run_dirs: Iterable[Path]) -> dict[str, Mapping[str, Any]]:
    result: dict[str, Mapping[str, Any]] = {}
    for run_dir in run_dirs:
        planning = run_dir / "planning"
        if not planning.exists():
            continue
        for path in planning.glob("*/private/committed-source.json"):
            value = read(path)
            result[path.parents[1].name] = value
    return result


def flatten(row: Mapping[str, Any], candidate: Mapping[str, Any] | None, period_ns: float) -> dict[str, Any]:
    actions, operations = candidate_labels(candidate)
    execution = row.get("execution", {})
    turns = row.get("turns", [])
    initial_fmax, final_fmax, d = outcome(row, period_ns)
    return {
        "job_id": row["job_id"], "phase": row["phase"], "study_track": row["study_track"],
        "root_id": row["root_id"], "model": row["model"], "level": row["level"],
        "agent_level": row["agent_level"], "coverage_regime": row["coverage_regime"],
        "language_condition": row["language_condition"], "selected_language": row.get("selected_language"),
        "selected_action_keys": actions, "selected_program_operations": operations,
        "compiled_host_mechanisms": compiled_mechanisms(row),
        "canonical_terminal": row["canonical_terminal"], "execution_started": bool(row.get("execution_started")),
        "valid_child": row["canonical_terminal"] == "VALID_COMPLETION",
        "beneficial_strict_gt_zero": d is not None and d > 0.0,
        "initial_fmax_mhz": initial_fmax,
        "final_fmax_mhz": final_fmax, "delta_fmax_mhz": d,
        "actual_heavy_calls": int(row.get("actual_heavy_calls") or 0),
        "actual_light_mutations": row.get("actual_light_mutations"),
        "backend_wall_seconds": execution.get("wall_seconds", execution.get("outer_duration_seconds", execution.get("duration_seconds"))),
        "planning_wall_seconds": row.get("planning_wall_seconds"),
        "provider_requests": int(row.get("provider_requests") or 0),
        "input_tokens": sum(int(turn.get("input_tokens") or 0) for turn in turns),
        "output_tokens": sum(int(turn.get("output_tokens") or 0) for turn in turns),
        "cost_usd": float(row.get("cost_usd") or 0.0),
        "query_items_used": int(row.get("planner_evidence", {}).get("query_requests_used") or 0),
        "turns_used": int(row.get("planner_evidence", {}).get("turns_used") or 0),
        "feedback_rounds": int(row.get("planner_evidence", {}).get("feedback_rounds") or 0),
        "error_type": execution.get("error_type", execution.get("error")),
        "evidence_reused": bool(row.get("evidence_reused")),
        "observation_sha256": row.get("observation_sha256"),
        "committed_sha256": row.get("committed_sha256"),
    }


def group_summary(rows: list[Mapping[str, Any]], keys: tuple[str, ...]) -> list[dict[str, Any]]:
    groups: dict[tuple[Any, ...], list[Mapping[str, Any]]] = collections.defaultdict(list)
    for row in rows:
        groups[tuple(row.get(key) for key in keys)].append(row)
    result = []
    for identity, members in sorted(groups.items(), key=lambda item: tuple(str(v) for v in item[0])):
        valid_deltas = [float(row["delta_fmax_mhz"]) for row in members if row.get("delta_fmax_mhz") is not None]
        result.append(dict(zip(keys, identity, strict=True)) | {
            "available_n": len(members),
            "selected_n": sum(row.get("selected_language") is not None for row in members),
            "valid_commit_n": sum(row.get("committed_sha256") is not None for row in members),
            "compiled_n": sum(bool(row.get("compiled_host_mechanisms")) for row in members),
            "execution_started_n": sum(bool(row.get("execution_started")) for row in members),
            "valid_child_n": sum(bool(row.get("valid_child")) for row in members),
            "beneficial_n": sum(bool(row.get("beneficial_strict_gt_zero")) for row in members),
            "mean_delta_fmax_mhz_valid_only": mean(valid_deltas),
            "median_delta_fmax_mhz_valid_only": median(valid_deltas),
            "mean_cost_usd": mean(float(row["cost_usd"]) for row in members),
            "mean_heavy_calls": mean(float(row["actual_heavy_calls"]) for row in members),
            "mean_backend_wall_seconds_started": mean(
                float(row["backend_wall_seconds"]) for row in members
                if isinstance(row.get("backend_wall_seconds"), (int, float))
            ),
            "mean_input_tokens": mean(float(row["input_tokens"]) for row in members),
            "mean_output_tokens": mean(float(row["output_tokens"]) for row in members),
        })
    return result


def utilisation(rows: list[Mapping[str, Any]], keys: tuple[str, ...]) -> list[dict[str, Any]]:
    result = group_summary(rows, keys)
    for summary in result:
        members = [row for row in rows if all(row.get(key) == summary[key] for key in keys)]
        summary.update({f"selected_{language}_n": sum(row.get("selected_language") == language for row in members) for language in ("M", "P", "G")})
    return result


def paired_rows(neutral: list[Mapping[str, Any]], conditioned: list[Mapping[str, Any]]) -> list[dict[str, Any]]:
    lookup = {(row["root_id"], row["model"], row["level"]): row for row in neutral}
    result = []
    for row in conditioned:
        level = CONDITION_LEVEL.get(str(row["language_condition"]), str(row["level"]))
        base = lookup[(row["root_id"], row["model"], level)]
        a, b = base.get("delta_fmax_mhz"), row.get("delta_fmax_mhz")
        result.append({
            "root_id": row["root_id"], "model": row["model"], "language_condition": row["language_condition"],
            "neutral_level": level, "neutral_terminal": base["canonical_terminal"],
            "conditioned_terminal": row["canonical_terminal"], "neutral_selected_language": base.get("selected_language"),
            "conditioned_selected_language": row.get("selected_language"), "neutral_delta_fmax_mhz": a,
            "conditioned_delta_fmax_mhz": b,
            "conditioned_minus_neutral_delta_fmax_mhz": None if a is None or b is None else float(b) - float(a),
            "interpretation": "descriptive_paired_development_only",
        })
    return result


def write_csv(path: Path, rows: list[Mapping[str, Any]]) -> None:
    fields = sorted({key for row in rows for key in row})
    with path.open("w", newline="") as stream:
        if not fields:
            return
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow({key: json.dumps(value, sort_keys=True) if isinstance(value, (dict, list)) else value for key, value in row.items()})


def main() -> int:
    parser = argparse.ArgumentParser(allow_abbrev=False)
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument("--references", type=Path, required=True)
    parser.add_argument("--calibration", type=Path, required=True)
    parser.add_argument("--nk", type=Path, required=True)
    parser.add_argument("--sn", type=Path, required=True)
    parser.add_argument("--cs", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        raise FileExistsError(args.output)
    args.output.mkdir(parents=True)
    bundle, references = read(args.bundle), read(args.references)
    verify(bundle, "bundle_sha256"); verify(references, "audit_sha256")
    run_dirs = {"CALIBRATION": args.calibration, "N_K": args.nk, "S_N": args.sn, "C_S": args.cs}
    aggregates = {name: read(path / "phase-aggregate.json") for name, path in run_dirs.items()}
    for name, value in aggregates.items():
        verify(value, "aggregate_sha256")
        audit = read(run_dirs[name] / "independent-audit.json")
        verify(audit, "audit_sha256")
        if value.get("phase") != name or not value.get("accepted_for_evidence_review") or not audit.get("accepted"):
            raise ValueError("phase_or_audit_not_accepted:" + name)
        if audit.get("aggregate_sha256") != value["aggregate_sha256"]:
            raise ValueError("audit_aggregate_binding_mismatch:" + name)
    candidates = candidate_map(run_dirs.values())
    phase_rows: dict[str, list[dict[str, Any]]] = {}
    for phase in ("N_K", "S_N", "C_S"):
        raw = aggregates[phase]["jobs"]
        if any(row.get("study_track") != TRACK_BY_PHASE[phase] for row in raw):
            raise ValueError("track_pooling_forbidden:" + phase)
        phase_rows[phase] = [
            flatten(
                row, candidates.get(row["job_id"]),
                float(bundle["roots"][row["root_id"]]["target_period_ns"]),
            )
            for row in raw
        ]
    if {key: len(value) for key, value in phase_rows.items()} != {"N_K": 90, "S_N": 36, "C_S": 36}:
        raise ValueError("repeat1_matrix_incomplete")
    rows = phase_rows["N_K"] + phase_rows["S_N"] + phase_rows["C_S"]
    neutral_full = utilisation(phase_rows["N_K"], ("level", "model"))
    neutral_reduced = utilisation(phase_rows["S_N"], ("level", "model"))
    conditioned = utilisation(phase_rows["C_S"], ("language_condition", "model"))
    pair_ns = paired_rows(phase_rows["N_K"], phase_rows["S_N"])
    pair_sc = paired_rows(phase_rows["S_N"], phase_rows["C_S"])
    selection_counter: collections.Counter[tuple[str, str, str]] = collections.Counter()
    mechanism_counter: collections.Counter[tuple[str, str, str]] = collections.Counter()
    for row in rows:
        for action in row["selected_action_keys"] or ["NONE"]:
            selection_counter[(row["phase"], row.get("selected_language") or "NONE", action)] += 1
        for mechanism in row["compiled_host_mechanisms"] or ["NONE"]:
            mechanism_counter[(row["phase"], row.get("selected_language") or "NONE", mechanism)] += 1
    selections = [{"phase": p, "language": l, "action_key": a, "n": n} for (p, l, a), n in sorted(selection_counter.items())]
    mechanisms = [{"phase": p, "language": l, "mechanism": m, "n": n} for (p, l, m), n in sorted(mechanism_counter.items())]
    failures = [{key: row.get(key) for key in (
        "job_id", "phase", "root_id", "model", "level", "language_condition", "selected_language",
        "canonical_terminal", "error_type", "compiled_host_mechanisms",
    )} for row in rows if row["canonical_terminal"] != "VALID_COMPLETION"]
    outputs = {
        "canonical-jobs": rows, "neutral-full-utilisation": neutral_full,
        "neutral-reduced-utilisation": neutral_reduced, "conditioned-frontier": conditioned,
        "neutral-full-vs-reduced-pairs": pair_ns, "reduced-neutral-vs-conditioned-pairs": pair_sc,
        "selection-counts": selections, "mechanism-counts": mechanisms,
        "failures-missingness": failures,
    }
    for name, table in outputs.items():
        write_csv(args.output / f"{name}.csv", table)
        (args.output / f"{name}.json").write_text(json.dumps(table, sort_keys=True, indent=2) + "\n")
    core = {
        "schema_version": "dof.wp014c-repeat1-analysis.v1", "protocol_epoch": bundle["protocol_epoch"],
        "descriptive_repeat": 1, "jobs": {key: len(value) for key, value in phase_rows.items()} | {"total": len(rows)},
        "terminal_counts": dict(sorted(collections.Counter(row["canonical_terminal"] for row in rows).items())),
        "language_counts": dict(sorted(collections.Counter(row.get("selected_language") or "NONE" for row in rows).items())),
        "beneficial_valid_jobs": sum(row["beneficial_strict_gt_zero"] for row in rows),
        "neutral_full_utilisation": neutral_full, "neutral_reduced_utilisation": neutral_reduced,
        "conditioned_frontier": conditioned, "resource": {
            "cumulative_cost_usd": aggregates["C_S"]["cumulative_cost_usd"],
            "cumulative_heavy_calls": aggregates["C_S"]["cumulative_heavy_calls"],
            "cumulative_queue_wall_seconds": aggregates["C_S"]["cumulative_queue_wall_seconds"],
        },
        "phase_aggregate_sha256": {name: aggregates[name]["aggregate_sha256"] for name in aggregates},
        "track_pooling_performed": False, "conditioned_results_are_deployment_means": False,
        "sealed_holdouts_accessed": False,
        "claim_boundary": "one characterised-development repeat; descriptive only; no significance, superiority or generalisation claim",
    }
    value = core | {"analysis_sha256": digest(core)}
    (args.output / "repeat1-analysis.json").write_text(json.dumps(value, sort_keys=True, indent=2) + "\n")
    print(json.dumps({"jobs": value["jobs"], "terminal_counts": value["terminal_counts"], "resource": value["resource"], "analysis_sha256": value["analysis_sha256"]}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
