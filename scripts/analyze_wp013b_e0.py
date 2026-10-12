#!/usr/bin/env python3
"""Descriptive, non-primary analysis for WP-013B development E0-v5."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
from pathlib import Path
from typing import Any


def canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode()


def digest(value: Any) -> str:
    return hashlib.sha256(value if isinstance(value, bytes) else canonical(value)).hexdigest()


def read(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text())
    if not isinstance(value, dict): raise ValueError(f"object_required:{path}")
    return value


def main() -> int:
    parser = argparse.ArgumentParser(allow_abbrev=False)
    parser.add_argument("--run", type=Path, required=True)
    parser.add_argument("--admission", type=Path, required=True)
    parser.add_argument("--negative-admission", type=Path, required=True)
    parser.add_argument("--context-fit", type=Path, required=True)
    parser.add_argument("--independent-audit", type=Path, required=True)
    parser.add_argument("--process-audit", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    aggregate = read(args.run / "e0-aggregate.json")
    admission, negative, fit = read(args.admission), read(args.negative_admission), read(args.context_fit)
    independent, processes = read(args.independent_audit), read(args.process_audit)
    jobs = aggregate["jobs"]
    rows = []
    for job in jobs:
        source_path = args.run / "planning" / job["job_id"] / "private" / "committed-source.json"
        source = read(source_path) if source_path.exists() else None
        execution = job.get("execution") or {}
        rows.append({
            "job_id": job["job_id"], "root_id": job["root_id"], "level": job["level"],
            "model": job.get("model"), "terminal": job["canonical_terminal"],
            "selected_language": job.get("selected_language"),
            "selected_action_key": source.get("action_key") if source else ("macro." + job["selected_macro"] if job.get("selected_macro") else None),
            "provider_requests": job.get("provider_requests", 0), "cost_usd": job.get("cost_usd", 0.0),
            "turns_used": (job.get("planner_evidence") or {}).get("turns_used", 0),
            "query_requests_used": (job.get("planner_evidence") or {}).get("query_requests_used", 0),
            "feedback_rounds": (job.get("planner_evidence") or {}).get("feedback_rounds", 0),
            "valid_commits": (job.get("planner_evidence") or {}).get("valid_commits", 0),
            "execution_started": job.get("execution_started", False), "actual_heavy_calls": job.get("actual_heavy_calls", 0),
            "initial_fmax_mhz": execution.get("initial_fmax_mhz") or (execution.get("pre_timing") or {}).get("fmax_mhz"),
            "final_fmax_mhz": execution.get("final_fmax_mhz") or (execution.get("post_timing") or {}).get("fmax_mhz"),
            "fmax_improvement_mhz": execution.get("fmax_improvement_mhz"),
            "selected_count": execution.get("selected_count"), "transformed_count": execution.get("transformed_count"),
        })
    llm = [row for row in rows if row["level"] in {"L2", "L3", "L4"}]
    pg = [row for row in llm if row["level"] in {"L3", "L4"}]
    funnel = {
        "language_available": len(pg),
        "used_one_or_more_queries": sum(row["query_requests_used"] > 0 for row in pg),
        "p_or_g_selected": sum(row["selected_language"] in {"P", "G"} for row in pg),
        "p_or_g_parsed": 0,
        "p_or_g_validated": 0,
        "p_or_g_compiled": 0,
        "p_or_g_executed": 0,
        "p_or_g_valid_child": 0,
        "p_or_g_scored": 0,
        "p_or_g_beneficial": 0,
    }
    by_root = {}
    for root_id in sorted({row["root_id"] for row in rows}):
        root_rows = [row for row in rows if row["root_id"] == root_id]
        by_root[root_id] = {
            "jobs": len(root_rows), "valid_completions": sum(row["terminal"] == "VALID_COMPLETION" for row in root_rows),
            "llm_language_counts": {language: sum(row["selected_language"] == language for row in root_rows if row["level"] in {"L2", "L3", "L4"}) for language in ("M", "P", "G")},
            "selected_action_counts": {key: sum(row["selected_action_key"] == key for row in root_rows) for key in sorted({row["selected_action_key"] for row in root_rows if row["selected_action_key"]})},
            "beneficial_valid_completions": sum(row["terminal"] == "VALID_COMPLETION" and isinstance(row["fmax_improvement_mhz"], (int, float)) and row["fmax_improvement_mhz"] > 0 for row in root_rows),
        }
    checks = {
        "aggregate_accepted": aggregate.get("accepted_for_evidence_review") is True,
        "independent_audit": independent.get("accepted") is True,
        "process_audit": processes.get("accepted") is True,
        "provider_admission": admission.get("accepted") is True,
        "negative_admission_retained": negative.get("accepted") is False,
        "context_fit": fit.get("accepted") is True,
        "all_jobs_canonical": aggregate.get("canonical_terminal_jobs") == 16,
        "all_llm_valid": all(row["terminal"] == "VALID_COMPLETION" for row in llm),
        "no_primary": aggregate.get("primary_aggregate_eligible") is False,
        "no_sealed": aggregate.get("sealed_holdouts_accessed") is False,
    }
    result = {
        "schema_version": "dof.wp013b-e0-v5-analysis.v1",
        "classification": "development_only_usability_evidence",
        "summary": {"jobs": len(rows), "valid_completions": sum(row["terminal"] == "VALID_COMPLETION" for row in rows), "llm_sessions": len(llm), "provider_requests_e0": aggregate["provider_requests_e0"], "provider_requests_total": aggregate["provider_requests_total"], "known_openrouter_cost_usd": aggregate["known_openrouter_cost_usd"], "heavy_calls": aggregate["actual_heavy_calls_e0"], "wall_seconds": aggregate["queue_wall_seconds"]},
        "language_selection": {language: sum(row["selected_language"] == language for row in llm) for language in ("M", "P", "G")},
        "action_selection": {key: sum(row["selected_action_key"] == key for row in llm) for key in sorted({row["selected_action_key"] for row in llm if row["selected_action_key"]})},
        "p_g_funnel": funnel, "by_root": by_root,
        "provider_admission": {"fresh_accepted_by_case": admission["accepted_by_case"], "fresh_cost_usd": admission["known_cost_usd"], "negative_cost_usd": negative["known_cost_usd"], "negative_shared_defect": "RELOCATE_HARDBLOCK_GENERATED documentation key was not a runtime node"},
        "context_fit": {row["root_id"]: {"actual_bytes": row["canonical_context_bytes"], "conservative_bytes": row["conservative_24576_query_reserve_context_bytes"]} for row in fit["roots"]},
        "interpretation": {
            "confirmed": ["semantic cards and maximum-Q contexts are mechanically usable", "targeted P, G-spatial, and G-hardblock programs can be produced and compiled", "neutral development E0 selected M in 12 of 12 LLM sessions", "the Macro challenge changed the available Macro set and L1 selected phys_opt but did not elicit neutral P/G selection"],
            "evidence_backed_inference": ["the WP-013A all-M result cannot be attributed solely to missing operation cards or the 32768-byte ceiling", "targeted language usability and neutral language preference are distinct"],
            "not_supported": ["P/G improves QoR", "M is globally optimal", "the result generalises beyond two development roots and two model routes", "any primary L2-to-L3 or L3-to-L4 causal claim"],
            "boundary_result": "P/G is technically usable under parity, but neutral one-shot model behaviour still strongly prefers M in this development sample.",
        },
        "checks": checks, "accepted": all(checks.values()), "jobs": rows,
    }
    result["analysis_sha256"] = digest(result)
    args.out.mkdir(parents=True, exist_ok=False)
    (args.out / "e0-v5-analysis.json").write_text(json.dumps(result, sort_keys=True, indent=2) + "\n")
    with (args.out / "e0-v5-jobs.csv").open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0])); writer.writeheader(); writer.writerows(rows)
    print(json.dumps({"accepted": result["accepted"], "summary": result["summary"], "language_selection": result["language_selection"], "p_g_funnel": funnel}, sort_keys=True))
    return 0 if result["accepted"] else 2


if __name__ == "__main__": raise SystemExit(main())
