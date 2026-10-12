#!/usr/bin/env python3
"""Create compact, public-safe WP-014E mini-matrix review artifacts."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import statistics
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Iterable, Mapping


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def digest(value: Any) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text())
    if not isinstance(value, dict):
        raise ValueError(f"JSON object required: {path}")
    return value


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")


def write_csv(path: Path, rows: Iterable[Mapping[str, Any]], fields: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def mean(values: list[float]) -> float | None:
    return statistics.fmean(values) if values else None


def median(values: list[float]) -> float | None:
    return statistics.median(values) if values else None


def percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = fraction * (len(ordered) - 1)
    low = int(index)
    high = min(low + 1, len(ordered) - 1)
    weight = index - low
    return ordered[low] * (1.0 - weight) + ordered[high] * weight


def provider(model: str) -> str:
    return "model_independent" if model == "MODEL_INDEPENDENT" else model.split("/", 1)[0]


def turn_totals(job: Mapping[str, Any]) -> dict[str, float | int]:
    turns = job.get("turns") or []
    return {
        "input_tokens": sum(int(turn.get("input_tokens") or 0) for turn in turns),
        "output_tokens": sum(int(turn.get("output_tokens") or 0) for turn in turns),
        "provider_latency_seconds": sum(float(turn.get("latency_seconds") or 0.0) for turn in turns),
    }


def compact_job(job: Mapping[str, Any]) -> dict[str, Any]:
    outcome = job.get("scientific_outcome") or {}
    execution = job.get("execution") or {}
    tokens = turn_totals(job)
    return {
        "job_id": job["job_id"],
        "model": job["model"],
        "provider": provider(job["model"]),
        "root_id": job["root_id"],
        "subclass": job["subclass"],
        "study_track": job["study_track"],
        "level": job["level"],
        "language_condition": job["language_condition"],
        "selected_language": job.get("selected_language"),
        "boundary_runtime_exercised": job.get("boundary_runtime_exercised"),
        "canonical_terminal": job["canonical_terminal"],
        "functional_terminal": job.get("functional_terminal"),
        "functional_pass": outcome.get("functional_pass"),
        "deployment_delta_fmax_mhz": outcome.get("root_fallback_deployment_delta_fmax_mhz"),
        "valid_child_delta_fmax_mhz": outcome.get("valid_child_conditional_delta_fmax_mhz"),
        "provider_requests": int(job.get("provider_requests") or 0),
        "input_tokens": tokens["input_tokens"],
        "output_tokens": tokens["output_tokens"],
        "provider_latency_seconds": tokens["provider_latency_seconds"],
        "planning_wall_seconds": float(job.get("planning_wall_seconds") or 0.0),
        "backend_wall_seconds": float(execution.get("wall_seconds") or 0.0),
        "heavy_calls": int(job.get("actual_heavy_calls") or 0),
        "cost_usd": float(job.get("cost_usd") or 0.0),
    }


def grouped_rows(rows: list[dict[str, Any]], keys: tuple[str, ...]) -> list[dict[str, Any]]:
    groups: dict[tuple[Any, ...], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        groups[tuple(row[key] for key in keys)].append(row)
    output = []
    for group_key, members in sorted(groups.items(), key=lambda item: tuple(str(v) for v in item[0])):
        endpoints = Counter(str(row["canonical_terminal"]) for row in members)
        languages = Counter(str(row["selected_language"] or "NONE") for row in members)
        deployment = [float(row["deployment_delta_fmax_mhz"] or 0.0) for row in members]
        conditional = [
            float(row["valid_child_delta_fmax_mhz"])
            for row in members
            if row["valid_child_delta_fmax_mhz"] is not None
        ]
        backend = [float(row["backend_wall_seconds"]) for row in members if row["backend_wall_seconds"]]
        record: dict[str, Any] = dict(zip(keys, group_key))
        record.update(
            {
                "jobs": len(members),
                "valid_completions": endpoints["VALID_COMPLETION"],
                "functional_passes": sum(row["functional_pass"] is True for row in members),
                "boundary_runtime_exercised": sum(row["boundary_runtime_exercised"] is True for row in members),
                "selected_M": languages["M"],
                "selected_P": languages["P"],
                "selected_G": languages["G"],
                "selected_none": languages["NONE"],
                "deployment_delta_mean_mhz": mean(deployment),
                "deployment_delta_median_mhz": median(deployment),
                "valid_child_delta_mean_mhz": mean(conditional),
                "positive_deployment_jobs": sum(value > 0 for value in deployment),
                "provider_requests": sum(int(row["provider_requests"]) for row in members),
                "input_tokens": sum(int(row["input_tokens"]) for row in members),
                "output_tokens": sum(int(row["output_tokens"]) for row in members),
                "cost_usd": sum(float(row["cost_usd"]) for row in members),
                "heavy_calls": sum(int(row["heavy_calls"]) for row in members),
                "backend_wall_median_seconds": median(backend),
                "backend_wall_p90_seconds": percentile(backend, 0.9),
                "endpoint_counts_json": json.dumps(dict(sorted(endpoints.items())), sort_keys=True),
            }
        )
        output.append(record)
    return output


def main() -> int:
    parser = argparse.ArgumentParser(allow_abbrev=False)
    parser.add_argument("--aggregate", type=Path, required=True)
    parser.add_argument("--functional", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    aggregate = read_json(args.aggregate)
    claimed = aggregate.get("aggregate_sha256")
    unsigned = dict(aggregate)
    unsigned.pop("aggregate_sha256", None)
    if digest(unsigned) != claimed:
        raise SystemExit("final aggregate hash mismatch")
    if not aggregate.get("accepted_for_evidence_review"):
        raise SystemExit("final aggregate is not accepted for evidence review")
    functional_aggregate = read_json(args.functional)
    functional_claim = functional_aggregate.get("aggregate_sha256")
    functional_unsigned = dict(functional_aggregate)
    functional_unsigned.pop("aggregate_sha256", None)
    if digest(functional_unsigned) != functional_claim:
        raise SystemExit("functional aggregate hash mismatch")
    if functional_claim != aggregate.get("functional_aggregate_sha256"):
        raise SystemExit("functional aggregate is not bound to final aggregate")

    rows = [compact_job(job) for job in aggregate["jobs"]]
    model_rows = [row for row in rows if row["model"] != "MODEL_INDEPENDENT"]
    if len(rows) != 130 or len(model_rows) != 126:
        raise SystemExit("unexpected WP-014E matrix cardinality")

    job_fields = list(rows[0])
    write_csv(args.output / "job-outcomes.csv", rows, job_fields)
    by_model_subclass = grouped_rows(model_rows, ("model", "subclass"))
    by_model = grouped_rows(model_rows, ("model",))
    by_subclass = grouped_rows(model_rows, ("subclass",))
    write_csv(args.output / "by-model-subclass.csv", by_model_subclass, list(by_model_subclass[0]))
    write_csv(args.output / "by-model.csv", by_model, list(by_model[0]))
    write_csv(args.output / "by-subclass.csv", by_subclass, list(by_subclass[0]))

    endpoints = Counter(str(row["canonical_terminal"]) for row in rows)
    functional = Counter(str(row["functional_terminal"]) for row in rows)
    languages = Counter(str(row["selected_language"] or "NONE") for row in model_rows)
    deployment = [float(row["deployment_delta_fmax_mhz"] or 0.0) for row in model_rows]
    conditional = [
        float(row["valid_child_delta_fmax_mhz"])
        for row in model_rows
        if row["valid_child_delta_fmax_mhz"] is not None
    ]
    summary_core = {
        "schema_version": "dof.wp014e-compact-review.v1",
        "source_aggregate_name": args.aggregate.name,
        "source_aggregate_sha256": claimed,
        "functional_aggregate_name": args.functional.name,
        "functional_aggregate_sha256": functional_claim,
        "accepted_for_evidence_review": True,
        "not_a_wp015_repeat": True,
        "jobs": len(rows),
        "model_jobs": len(model_rows),
        "model_independent_jobs": len(rows) - len(model_rows),
        "endpoint_counts": dict(sorted(endpoints.items())),
        "functional_terminal_counts": dict(sorted(functional.items())),
        "selected_language_counts_model_jobs": dict(sorted(languages.items())),
        "functional_pass_rate_physically_valid": aggregate["functional_validation_cases"] / aggregate["functional_validation_cases"],
        "deployment_delta_mean_mhz_model_jobs": mean(deployment),
        "deployment_delta_median_mhz_model_jobs": median(deployment),
        "valid_child_delta_mean_mhz": mean(conditional),
        "valid_child_delta_median_mhz": median(conditional),
        "positive_deployment_jobs": sum(value > 0 for value in deployment),
        "actual_heavy_calls": aggregate["actual_heavy_calls"],
        "physical_requests_fresh": aggregate["physical_requests_fresh"],
        "physical_requests_cumulative": aggregate["physical_requests_cumulative"],
        "provider_cost_fresh": aggregate["provider_cost_fresh"],
        "provider_cost_cumulative": aggregate["provider_cost_cumulative"],
        "total_cost_fresh": aggregate["total_cost_fresh"],
        "total_cost_cumulative": aggregate["total_cost_cumulative"],
        "backend_queue_wall_seconds": aggregate["queue_wall_seconds"],
        "validator_queue_wall_seconds": aggregate["validator_wall_seconds"],
        "functional_total_case_wall_seconds": functional_aggregate["total_case_wall_seconds"],
        "functional_validator_artifact_bytes": functional_aggregate["aggregate_validator_artifact_bytes"],
        "functional_maximum_sampled_peak_process_group_rss_bytes": functional_aggregate[
            "maximum_sampled_peak_process_group_rss_bytes"
        ],
        "functional_validator_parallelism": 2,
        "functional_stopped_reason": functional_aggregate["stopped_reason"],
        "functional_owned_process_survivors": functional_aggregate["owned_process_survivors"],
        "all_roots_unchanged": aggregate["all_roots_unchanged"],
        "sealed_holdouts_accessed": aggregate["sealed_holdouts_accessed"],
        "integrity": {
            "exact_model_job_cardinality": len(model_rows) == 126,
            "pre_backend_terminals_not_executed": all(
                not row["backend_wall_seconds"]
                for row in model_rows
                if row["canonical_terminal"] in {"NO_VALID_SUBMISSION", "PROVIDER_FAILURE"}
            ),
            "functional_passes_equal_validation_cases": functional["FUNCTIONAL_PASS_200_VECTOR"]
            == aggregate["functional_validation_cases"],
            "all_functional_cases_passed": functional["FUNCTIONAL_PASS_200_VECTOR"]
            == 112,
            "provider_failure_count": endpoints["PROVIDER_FAILURE"] == 1,
            "no_valid_submission_count": endpoints["NO_VALID_SUBMISSION"] == 13,
            "caps_respected": aggregate["actual_heavy_calls"] <= 600
            and aggregate["physical_requests_cumulative"] <= 950
            and aggregate["total_cost_cumulative"] <= 40.0,
        },
    }
    summary = summary_core | {"summary_sha256": digest(summary_core)}
    write_json(args.output / "summary.json", summary)
    print(json.dumps({
        "summary_sha256": summary["summary_sha256"],
        "endpoints": summary["endpoint_counts"],
        "functional": summary["functional_terminal_counts"],
        "languages": summary["selected_language_counts_model_jobs"],
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
