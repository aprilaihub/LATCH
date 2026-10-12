#!/usr/bin/env python3
"""Descriptive, track-separated WP-014 repeat-1 analysis."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
from pathlib import Path
import statistics
from typing import Any, Iterable, Mapping


def canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def digest(value: Any) -> str:
    raw = value if isinstance(value, bytes) else canonical(value)
    return hashlib.sha256(raw).hexdigest()


def read(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text())
    if not isinstance(value, dict):
        raise ValueError("json_object_required")
    return value


def verify(value: dict[str, Any], hash_key: str) -> None:
    claimed = value.pop(hash_key)
    if digest(value) != claimed:
        raise ValueError("hash_invalid:" + hash_key)
    value[hash_key] = claimed


def require_track(rows: Iterable[Mapping[str, Any]], expected: str) -> list[Mapping[str, Any]]:
    result = list(rows)
    if any(row.get("study_track") != expected for row in result):
        raise ValueError("track_pooling_forbidden")
    return result


def _delta(row: Mapping[str, Any]) -> float | None:
    if row.get("canonical_terminal") == "VALID_COMPLETION":
        value = row.get("execution", {}).get("fmax_improvement_mhz")
        return float(value) if isinstance(value, (int, float)) else None
    if row.get("canonical_terminal") in {"PROVIDER_FAILURE", "MODEL_OR_ROUTE_DRIFT", "CONTROLLER_FAILURE"}:
        return None
    return 0.0


def _mean(values: list[float]) -> float | None:
    return statistics.mean(values) if values else None


def _median(values: list[float]) -> float | None:
    return statistics.median(values) if values else None


def _group(rows: list[Mapping[str, Any]], keys: tuple[str, ...]) -> list[dict[str, Any]]:
    groups: dict[tuple[Any, ...], list[Mapping[str, Any]]] = {}
    for row in rows:
        groups.setdefault(tuple(row.get(key) for key in keys), []).append(row)
    result = []
    for identity, members in sorted(groups.items(), key=lambda item: tuple(str(v) for v in item[0])):
        deltas = [_delta(row) for row in members]
        observed = [value for value in deltas if value is not None]
        result.append(
            dict(zip(keys, identity, strict=True))
            | {
                "n": len(members), "observed_n": len(observed),
                "missing_n": len(members) - len(observed),
                "valid_completion_n": sum(row.get("canonical_terminal") == "VALID_COMPLETION" for row in members),
                "mean_root_fallback_delta_fmax_mhz": _mean(observed),
                "median_root_fallback_delta_fmax_mhz": _median(observed),
                "mean_cost_usd": _mean([float(row.get("cost_usd", 0.0)) for row in members]),
                "mean_heavy_calls": _mean([float(row.get("actual_heavy_calls", 0.0) or 0.0) for row in members]),
            }
        )
    return result


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        path.write_text("")
        return
    fields = sorted({key for row in rows for key in row})
    with path.open("w", newline="") as stream:
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
    verify(bundle, "bundle_sha256")
    verify(references, "audit_sha256")
    aggregates = {
        "CALIBRATION": read(args.calibration / "phase-aggregate.json"),
        "N_K": read(args.nk / "phase-aggregate.json"),
        "S_N": read(args.sn / "phase-aggregate.json"),
        "C_S": read(args.cs / "phase-aggregate.json"),
    }
    for name, value in aggregates.items():
        verify(value, "aggregate_sha256")
        if value.get("phase") != name or not value.get("accepted_for_evidence_review"):
            raise ValueError("phase_not_accepted:" + name)
    neutral = require_track(
        aggregates["N_K"]["jobs"] + aggregates["S_N"]["jobs"], "N_NEUTRAL"
    )
    conditioned = require_track(aggregates["C_S"]["jobs"], "C_CONDITIONED")
    if len(neutral) != 126 or len(conditioned) != 36:
        raise ValueError("repeat1_matrix_incomplete")

    canonical_rows = [dict(row) for row in neutral + conditioned]
    _write_csv(args.output / "canonical-jobs.csv", canonical_rows)
    (args.output / "canonical-jobs.json").write_text(json.dumps(canonical_rows, sort_keys=True, indent=2) + "\n")
    neutral_summary = _group(neutral, ("phase", "coverage_regime", "level", "model"))
    conditioned_summary = _group(conditioned, ("language_condition", "model"))
    utilisation = []
    for summary in neutral_summary:
        members = [row for row in neutral if all(row.get(key) == summary[key] for key in ("phase", "coverage_regime", "level", "model"))]
        utilisation.append(
            summary | {language: sum(row.get("selected_language") == language for row in members) for language in ("M", "P", "G")}
        )
    funnel = []
    for summary in conditioned_summary:
        members = [row for row in conditioned if all(row.get(key) == summary[key] for key in ("language_condition", "model"))]
        funnel.append(
            summary | {
                "valid_commit_n": sum(row.get("planner_evidence", {}).get("valid_commits", 0) == 1 for row in members),
                "compile_n": sum(row.get("committed_sha256") is not None for row in members),
                "execution_started_n": sum(bool(row.get("execution_started")) for row in members),
                "valid_child_n": sum(row.get("canonical_terminal") == "VALID_COMPLETION" for row in members),
                "scored_n": sum(isinstance(row.get("execution", {}).get("final_fmax_mhz"), (int, float)) for row in members),
                "conditional_mean_delta_fmax_mhz": _mean([
                    float(row["execution"]["fmax_improvement_mhz"]) for row in members
                    if row.get("canonical_terminal") == "VALID_COMPLETION"
                    and isinstance(row.get("execution", {}).get("fmax_improvement_mhz"), (int, float))
                ]),
            }
        )
    gaps = []
    for neutral_row in [row for row in neutral if row["phase"] == "S_N" and row["level"] in {"L3", "L4"}]:
        candidates = [
            row for row in conditioned
            if row["root_id"] == neutral_row["root_id"] and row["model"] == neutral_row["model"]
            and row["language_condition"] in {"P_EXCLUSIVE", "G_EXCLUSIVE"}
            and _delta(row) is not None
        ]
        neutral_delta = _delta(neutral_row)
        best = max((_delta(row) for row in candidates), default=None)
        gaps.append(
            {"root_id": neutral_row["root_id"], "model": neutral_row["model"],
             "neutral_level": neutral_row["level"], "neutral_delta_fmax_mhz": neutral_delta,
             "best_conditioned_pg_delta_fmax_mhz": best,
             "selection_gap_mhz": None if best is None or neutral_delta is None else best - neutral_delta,
             "interpretation": "descriptive_only_not_causal_or_statistical"}
        )
    failures = []
    for row in canonical_rows:
        if row["canonical_terminal"] != "VALID_COMPLETION":
            failures.append({key: row.get(key) for key in (
                "job_id", "phase", "root_id", "model", "study_track",
                "coverage_regime", "language_condition", "level", "canonical_terminal",
            )})
    _write_csv(args.output / "neutral-utilisation.csv", utilisation)
    _write_csv(args.output / "conditioned-funnel.csv", funnel)
    _write_csv(args.output / "selection-gap-descriptive.csv", gaps)
    _write_csv(args.output / "failures-missingness.csv", failures)
    core = {
        "schema_version": "dof.wp014-repeat1-analysis.v1",
        "protocol_epoch": "dof.dual-track.v1", "descriptive_repeat": 1,
        "jobs": {"neutral": len(neutral), "conditioned": len(conditioned), "total": len(canonical_rows)},
        "neutral_utilisation": utilisation, "conditioned_funnel": funnel,
        "selection_gaps": gaps, "failures_missingness": failures,
        "resource": {
            "cumulative_cost_usd": aggregates["C_S"]["cumulative_cost_usd"],
            "cumulative_heavy_calls": aggregates["C_S"]["cumulative_heavy_calls"],
            "cumulative_queue_wall_seconds": aggregates["C_S"]["cumulative_queue_wall_seconds"],
        },
        "track_pooling_performed": False, "conditioned_results_are_deployment_means": False,
        "sealed_holdouts_accessed": False,
        "claim_boundary": "one characterised-development repeat; descriptive only; no final significance, superiority or generalisation claim",
    }
    value = core | {"analysis_sha256": digest(core)}
    (args.output / "repeat1-analysis.json").write_text(json.dumps(value, sort_keys=True, indent=2) + "\n")
    print(json.dumps({"jobs": value["jobs"], "resource": value["resource"], "analysis_sha256": value["analysis_sha256"]}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
