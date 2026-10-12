#!/usr/bin/env python3
"""Freeze a failure-type-driven WP-014B supplement without using QoR."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys
from typing import Any

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from scripts.freeze_wp014b_admission import digest, job  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(allow_abbrev=False)
    parser.add_argument("--primary", type=Path, required=True)
    parser.add_argument("--primary-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    primary = json.loads(args.primary.read_text())
    rows = []
    stage_failures = []
    for result in primary["results"]:
        if result["endpoint"] != "TOOL_EXECUTION_FAILURE": continue
        log = args.primary_root / "jobs" / result["job_id"] / "vivado.log"
        text = log.read_text(errors="replace")
        if "not supported yet for post-route physical synthesis" not in text: continue
        if result["action_kind"] == "phys_option":
            action_kind = "phys_option_placed"
        elif result["action_kind"] == "phys_force_replication":
            action_kind = "phys_force_replication_placed"
        else:
            raise RuntimeError("unexpected_post_route_failure_kind")
        stage_failures.append(result["job_id"])
        rows.append(job(result["root_id"], action_kind, result["action_value"], 2,
                        "post_route_failure_placed_stage_recheck", result["timeout_seconds"]))
    if len(rows) != 8:
        raise RuntimeError(f"expected_eight_post_route_failures:{len(rows)}")
    rows.append(job("vexriscv_re-place", "phys_option_combo", "slr_crossing_opt,tns_cleanup", 1,
                    "documented_option_dependency_recheck", 600))
    for kind, value, family in (
        ("pblock_exclude_placement", "first_clock_region", "corrected_pblock_property_probe"),
        ("pblock_assign_cell", "top_timing_cell_clock_region", "corrected_pblock_target_probe"),
        ("place_cell_roundtrip", "top_timing_placed_cell", "corrected_targeted_placement_probe"),
    ):
        row = job("vexriscv_re-place", kind, value, 0, family, 300)
        row["job_id"] += "--corrected-v2"
        rows.append(row)
    core = {
        "schema_version": "dof.wp014b-real-admission-supplement.v1",
        "continuation_identity": "wp014b-action-surface-continuation-v1",
        "freeze_basis_commit": subprocess.run(("git", "rev-parse", "HEAD"), cwd=REPO, check=True, capture_output=True, text=True).stdout.strip(),
        "primary_aggregate_sha256": primary["aggregate_sha256"],
        "selection_rule": "only primary typed failures: post-route stage incompatibility, documented option dependency, and three controller target-expression defects; no QoR selection",
        "triggered_primary_jobs": stage_failures,
        "concurrency": {"weekday_max": 8, "weekend_max": 16, "active_max": 8},
        "caps": {"implementation_heavy_calls_total": 96, "primary_actual_heavy_calls": primary["actual_heavy_calls"], "supplement_planned_heavy_calls": sum(row["planned_heavy_calls"] for row in rows)},
        "jobs": rows, "counts": {"jobs": len(rows), "planned_heavy_calls": sum(row["planned_heavy_calls"] for row in rows)},
        "scientific_repeat_1": False, "sealed_holdout_access": False,
    }
    if core["caps"]["primary_actual_heavy_calls"] + core["counts"]["planned_heavy_calls"] > 96: raise RuntimeError("cumulative_heavy_cap_exceeded")
    result = core | {"plan_sha256": digest(core)}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, sort_keys=True, indent=2) + "\n")
    print(json.dumps(result["counts"] | {"cumulative_max": core["caps"]["primary_actual_heavy_calls"] + result["counts"]["planned_heavy_calls"], "plan_sha256": result["plan_sha256"]}, sort_keys=True))
    return 0


if __name__ == "__main__": raise SystemExit(main())
