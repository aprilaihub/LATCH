#!/usr/bin/env python3
"""Create machine-readable WP-013 E0 tables and resource projections."""

from __future__ import annotations

import argparse
import csv
from collections import Counter, defaultdict
import hashlib
import json
import math
from pathlib import Path
from statistics import median
from typing import Any


SCHEMA = "dof.wp013-e0-analysis.v1"


def read(path: Path) -> Any:
    return json.loads(path.read_text())


def digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def write_csv(path: Path, rows: list[dict[str, Any]], fields: list[str]) -> None:
    with path.open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def analyze(aggregate: dict[str, Any], action_summary: list[dict[str, Any]]) -> dict[str, Any]:
    jobs = aggregate["jobs"]
    turns = [
        {"job_id": job["job_id"], "root_id": job["root_id"], "level": job["level"], "model": job["model"], **turn}
        for job in jobs for turn in job.get("turns", [])
    ]
    feedback = Counter(
        reason
        for turn in turns
        for reason in (turn.get("planner_result") or {}).get("feedback", [])
    )
    by_cell: dict[str, dict[str, Any]] = {}
    for job in jobs:
        key = f"{job['level']}|{job.get('model') or 'deterministic'}"
        row = by_cell.setdefault(key, {"jobs": 0, "valid_completion": 0, "no_valid_submission": 0, "commits": 0, "cost_usd": 0.0})
        row["jobs"] += 1
        row["valid_completion"] += job["canonical_terminal"] == "VALID_COMPLETION"
        row["no_valid_submission"] += job["canonical_terminal"] == "NO_VALID_SUBMISSION"
        row["commits"] += job.get("planner_evidence", {}).get("valid_commits", 0)
        row["cost_usd"] += float(job.get("cost_usd") or 0.0)
    runtime_medians = [float(row["median_runtime_seconds"]) for row in action_summary]
    typical_macro_runtime = median(runtime_medians)
    tail_macro_runtime = median(float(row["runtime_iqr_seconds"][1]) for row in action_summary)
    llm_jobs = [job for job in jobs if job["level"] in {"L2", "L3", "L4"}]
    observed_provider_cost_per_root = sum(float(job["cost_usd"]) for job in llm_jobs) / 2.0
    projections = []
    for roots in (1, 10, 20):
        execution_jobs = 7 * roots
        projections.append(
            {
                "roots": roots,
                "total_jobs": 8 * roots,
                "llm_sessions": 6 * roots,
                "provider_requests_upper": 24 * roots,
                "provider_cost_usd_at_e0_rate": observed_provider_cost_per_root * roots,
                "implementation_heavy_upper": 28 * roots,
                "ideal_8way_execution_wall_seconds_at_median": execution_jobs * typical_macro_runtime / 8.0,
                "ideal_8way_execution_wall_seconds_at_median_upper_iqr": execution_jobs * tail_macro_runtime / 8.0,
            }
        )
    return {
        "schema_version": SCHEMA,
        "terminal_counts": aggregate["terminal_counts"],
        "planning_feedback_counts": dict(sorted(feedback.items())),
        "cell_summary": by_cell,
        "provider": {
            "requests": aggregate["provider_requests"],
            "input_tokens": sum(int(turn.get("input_tokens") or 0) for turn in turns),
            "output_tokens": sum(int(turn.get("output_tokens") or 0) for turn in turns),
            "cost_usd": aggregate["known_openrouter_cost_usd"],
            "cost_per_two_root_e0": aggregate["known_openrouter_cost_usd"],
            "cost_per_root_observed": observed_provider_cost_per_root,
        },
        "execution": {
            "valid_completions": sum(job["canonical_terminal"] == "VALID_COMPLETION" for job in jobs),
            "e0_heavy_calls": aggregate["actual_heavy_calls_e0"],
            "wp013_heavy_calls": aggregate["wp013_observed_heavy_total"],
            "queue_wall_seconds": aggregate["queue_wall_seconds"],
            "typical_macro_runtime_seconds": typical_macro_runtime,
            "tail_macro_runtime_seconds": tail_macro_runtime,
        },
        "projections": projections,
        "interpretation": {
            "integrity_acceptance": aggregate["accepted_for_evidence_review"],
            "scientific_acceptance": False,
            "scientific_blocker": "model-visible M exact candidate syntax was omitted; 24 construction failures include 15 language_not_allowed_at_level and 9 macro_candidate_schema_invalid",
            "prompt_or_language_changed_after_e0_start": False,
            "primary_matrix_ready": False,
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(allow_abbrev=False)
    parser.add_argument("--aggregate", type=Path, required=True)
    parser.add_argument("--action-summary", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    aggregate = read(args.aggregate)
    action_summary = read(args.action_summary)
    args.out.mkdir(parents=True, exist_ok=False)
    result = analyze(aggregate, action_summary)
    result["analysis_sha256"] = digest(result)
    (args.out / "e0-analysis.json").write_text(json.dumps(result, sort_keys=True, indent=2) + "\n")
    job_rows = [
        {
            "job_id": job["job_id"], "root_id": job["root_id"], "level": job["level"],
            "model": job.get("model"), "terminal": job["canonical_terminal"],
            "selected_language": job.get("selected_language"), "provider_requests": job.get("provider_requests", 0),
            "cost_usd": job.get("cost_usd", 0.0), "heavy_calls": job.get("actual_heavy_calls", 0),
        }
        for job in aggregate["jobs"]
    ]
    write_csv(args.out / "e0-jobs.csv", job_rows, list(job_rows[0]))
    projection_rows = result["projections"]
    write_csv(args.out / "resource-projections.csv", projection_rows, list(projection_rows[0]))
    print(json.dumps({"analysis_sha256": result["analysis_sha256"], "jobs": len(job_rows), "scientific_acceptance": False}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
