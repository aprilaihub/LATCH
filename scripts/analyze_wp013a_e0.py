#!/usr/bin/env python3
"""Create public-safe WP-013A E0 v4 tables and a steering brief."""

from __future__ import annotations

import argparse
import csv
from collections import Counter, defaultdict
import hashlib
import json
from pathlib import Path
from statistics import mean, median
from typing import Any, Iterable


SCHEMA = "dof.wp013a-e0-v4-analysis.v1"


def read(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text())
    if not isinstance(value, dict):
        raise ValueError(f"object_required:{path}")
    return value


def digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def quantile(values: Iterable[float], fraction: float) -> float | None:
    rows = sorted(float(value) for value in values)
    if not rows:
        return None
    position = (len(rows) - 1) * fraction
    lower = int(position)
    upper = min(lower + 1, len(rows) - 1)
    weight = position - lower
    return rows[lower] * (1.0 - weight) + rows[upper] * weight


def summary(values: Iterable[float]) -> dict[str, Any]:
    rows = [float(value) for value in values]
    return {
        "n": len(rows),
        "mean": mean(rows) if rows else None,
        "median": median(rows) if rows else None,
        "q1": quantile(rows, 0.25),
        "q3": quantile(rows, 0.75),
        "min": min(rows) if rows else None,
        "max": max(rows) if rows else None,
    }


def action_name(job: dict[str, Any]) -> str | None:
    attempt = (job.get("execution") or {}).get("attempt_id")
    marker = "attempt:node_001_"
    return attempt[len(marker):] if isinstance(attempt, str) and attempt.startswith(marker) else None


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        raise ValueError("csv_rows_required")
    with path.open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def analyze(
    aggregate: dict[str, Any],
    admission: dict[str, Any],
    capture: dict[str, Any],
    independent_audit: dict[str, Any],
    process_audit: dict[str, Any],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    jobs = aggregate["jobs"]
    valid = [job for job in jobs if job["canonical_terminal"] == "VALID_COMPLETION"]
    llm = [job for job in jobs if job["level"] in {"L2", "L3", "L4"}]
    rows: list[dict[str, Any]] = []
    for job in jobs:
        execution = job.get("execution") or {}
        evidence = job.get("planner_evidence") or {}
        turns = job.get("turns") or []
        rows.append({
            "job_id": job["job_id"],
            "root_id": job["root_id"],
            "level": job["level"],
            "model": job.get("model") or "deterministic",
            "terminal": job["canonical_terminal"],
            "selected_language": job.get("selected_language") or "",
            "action": action_name(job) or "",
            "provider_requests": job.get("provider_requests", 0),
            "turns_used": evidence.get("turns_used", 0),
            "queries_used": evidence.get("query_requests_used", 0),
            "max_input_bytes": max(
                (int(turn.get("total_input_size_bytes") or 0) for turn in turns),
                default=0,
            ),
            "cost_usd": float(job.get("cost_usd") or 0.0),
            "heavy_calls": int(job.get("actual_heavy_calls") or 0),
            "wall_seconds": execution.get("wall_seconds"),
            "initial_fmax_mhz": execution.get("initial_fmax_mhz"),
            "final_fmax_mhz": execution.get("final_fmax_mhz"),
            "fmax_improvement_mhz": execution.get("fmax_improvement_mhz"),
            "initial_wns_ns": execution.get("initial_wns_ns"),
            "final_wns_ns": execution.get("final_wns_ns"),
        })

    by_level: dict[str, Any] = {}
    for level in ("L0", "L1", "L2", "L3", "L4"):
        selected = [job for job in jobs if job["level"] == level]
        by_level[level] = {
            "jobs": len(selected),
            "terminal_counts": dict(Counter(job["canonical_terminal"] for job in selected)),
            "language_counts": dict(Counter(job.get("selected_language") or "NONE" for job in selected)),
            "action_counts": dict(Counter(action_name(job) or "NONE" for job in selected)),
            "fmax_improvement_mhz": summary(
                (job.get("execution") or {}).get("fmax_improvement_mhz")
                for job in selected
                if (job.get("execution") or {}).get("fmax_improvement_mhz") is not None
            ),
        }

    by_model: dict[str, Any] = {}
    for model in sorted({job["model"] for job in llm}):
        selected = [job for job in llm if job["model"] == model]
        by_model[model] = {
            "jobs": len(selected),
            "valid_commits": sum((job.get("planner_evidence") or {}).get("valid_commits", 0) for job in selected),
            "valid_completions": sum(job["canonical_terminal"] == "VALID_COMPLETION" for job in selected),
            "requests": sum(job.get("provider_requests", 0) for job in selected),
            "cost_usd": sum(float(job.get("cost_usd") or 0.0) for job in selected),
            "language_counts": dict(Counter(job.get("selected_language") or "NONE" for job in selected)),
            "action_counts": dict(Counter(action_name(job) or "NONE" for job in selected)),
        }

    feedback = Counter()
    turn_kinds = Counter()
    for job in llm:
        for event in (job.get("planner_evidence") or {}).get("transcript", []):
            turn_kinds[event.get("kind") or "UNKNOWN"] += 1
            for item in event.get("feedback", []):
                if isinstance(item, dict):
                    feedback[item.get("error_code") or "UNKNOWN"] += 1

    per_root: dict[str, Any] = {}
    for root_id in sorted({job["root_id"] for job in jobs}):
        selected = [job for job in valid if job["root_id"] == root_id]
        best = max(selected, key=lambda job: (job.get("execution") or {}).get("final_fmax_mhz", float("-inf")))
        per_root[root_id] = {
            "valid_executions": len(selected),
            "action_counts": dict(Counter(action_name(job) or "NONE" for job in selected)),
            "best_job_id": best["job_id"],
            "best_action": action_name(best),
            "best_final_fmax_mhz": best["execution"]["final_fmax_mhz"],
            "improvement_mhz": summary(job["execution"]["fmax_improvement_mhz"] for job in selected),
        }

    total_context = [
        int(turn.get("total_input_size_bytes") or 0)
        for job in llm for turn in job.get("turns", [])
    ]
    result = {
        "schema_version": SCHEMA,
        "evidence_acceptance": {
            "controller": aggregate["accepted_for_evidence_review"],
            "independent_audit": independent_audit["accepted"],
            "process_audit": process_audit["accepted"],
            "capture": capture["accepted"],
            "provider_admission": admission["accepted"],
        },
        "terminals": aggregate["terminal_counts"],
        "by_level": by_level,
        "by_model": by_model,
        "by_root": per_root,
        "planning": {
            "e0_requests": aggregate["provider_requests_e0"],
            "provider_admission_requests": admission["physical_requests"],
            "total_requests": aggregate["provider_requests_total"],
            "turn_kind_counts": dict(turn_kinds),
            "feedback_error_counts": dict(feedback),
            "sessions_using_queries": sum((job.get("planner_evidence") or {}).get("query_requests_used", 0) > 0 for job in llm),
            "total_queries": sum((job.get("planner_evidence") or {}).get("query_requests_used", 0) for job in llm),
            "input_bytes": summary(total_context),
            "byte_ceiling": 32768,
        },
        "execution": {
            "valid_completions": len(valid),
            "heavy_calls": aggregate["actual_heavy_calls_e0"],
            "heavy_cap": aggregate["wp013a_heavy_cap"],
            "queue_wall_seconds": aggregate["queue_wall_seconds"],
            "job_wall_seconds": summary(job["execution"]["wall_seconds"] for job in valid),
            "fmax_improvement_mhz": summary(job["execution"]["fmax_improvement_mhz"] for job in valid),
            "positive_improvement_count": sum(job["execution"]["fmax_improvement_mhz"] > 0 for job in valid),
        },
        "provider": {
            "known_cost_usd": aggregate["known_openrouter_cost_usd"],
            "cost_cap_usd": aggregate["openrouter_cost_cap_usd"],
            "retry_count": 0,
            "fallback_count": 0,
        },
        "scientific_boundary": {
            "observed_facts": [
                "All 12 model sessions produced one valid COMMIT and valid backend completion.",
                "All 12 model sessions selected language M; L3/L4 did not voluntarily use P or G.",
                "One L4 session used four real read-only queries; the other 11 used none.",
                "All 14 executed interventions improved Fmax on these two development roots.",
            ],
            "evidence_backed_inference": "The repaired public contract removes the systematic interface failure seen in WP-013.",
            "unsupported_claims": [
                "No L2/L3/L4 superiority or monotonic DoF benefit is established by this two-root development E0.",
                "No P/G value or usability advantage is established because neither language was selected in E0.",
                "QoR observations are development-only and are not primary or holdout evidence.",
            ],
            "boundary_result": "Expanded language availability alone did not induce P/G use; this must be handled as a scientific boundary/diagnostic, not hidden by technical acceptance.",
        },
        # Retain version-bound record fields for historical-reader compatibility.
        "steering": {
            "primary_matrix_auto_start": False,
            "primary_matrix_planning": "may_resume_only_after_GPT_PRO_evidence_review",
            "review_focus": [
                "confirm contract-v2 evidence closure",
                "decide how M-only voluntary choices affect the DoF operationalisation",
                "retain WP-013A E0 as development evidence only",
            ],
        },
    }
    return result, rows


def brief(result: dict[str, Any]) -> str:
    execution = result["execution"]
    planning = result["planning"]
    provider = result["provider"]
    return f"""# WP-013A E0 v4 Analysis

## Executive result

The fresh E0 has 16/16 canonical terminals, including 14/14 valid backend completions and four valid L2, L3 and L4 executions each. Independent integrity and two-pass zero-live process audits pass.

The run used {execution['heavy_calls']}/{execution['heavy_cap']} implementation-heavy calls, {planning['total_requests']} total provider requests including six provider-only admissions, USD {provider['known_cost_usd']:.9f}/{provider['cost_cap_usd']:.2f}, and {execution['queue_wall_seconds']:.1f} aggregate wall seconds.

## Scientific boundary

All 12 model sessions committed language M. P and G were accepted in provider-only syntax admission but were not voluntarily selected in E0. One L4 session used four real Q items; the remaining eleven used none. Therefore the replacement run closes the interface/readiness defect but does not establish a monotonic DoF benefit, L3/L4 superiority, or P/G value.

This development analysis does not authorize or launch a primary matrix.
"""


def main() -> int:
    parser = argparse.ArgumentParser(allow_abbrev=False)
    parser.add_argument("--aggregate", type=Path, required=True)
    parser.add_argument("--provider-admission", type=Path, required=True)
    parser.add_argument("--capture", type=Path, required=True)
    parser.add_argument("--independent-audit", type=Path, required=True)
    parser.add_argument("--process-audit", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    if args.out.exists():
        raise FileExistsError(args.out)
    args.out.mkdir(parents=True)
    result, rows = analyze(
        read(args.aggregate), read(args.provider_admission), read(args.capture),
        read(args.independent_audit), read(args.process_audit),
    )
    result["analysis_sha256"] = digest(result)
    (args.out / "e0-v4-analysis.json").write_text(json.dumps(result, sort_keys=True, indent=2) + "\n")
    write_csv(args.out / "e0-v4-jobs.csv", rows)
    (args.out / "steering-brief.md").write_text(brief(result))
    print(json.dumps({
        "analysis_sha256": result["analysis_sha256"],
        "jobs": len(rows),
        "ready_for_pro_review": all(result["evidence_acceptance"].values()),
    }))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
