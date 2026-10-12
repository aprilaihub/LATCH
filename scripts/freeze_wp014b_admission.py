#!/usr/bin/env python3
"""Freeze the stratified WP-014B real-admission matrix before execution."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import subprocess
from typing import Any


REPO = Path(__file__).resolve().parents[1]


def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def job(root: str, kind: str, value: str, heavy: int, family: str, timeout: int) -> dict[str, Any]:
    token = value.lower().replace("_", "-").replace(",", "-") if value else "default"
    return {
        "job_id": f"{root}--{kind.replace('_', '-')}--{token}", "root_id": root,
        "action_kind": kind, "action_value": value, "semantic_family": family,
        "planned_heavy_calls": heavy, "timeout_seconds": timeout,
    }


def matrix() -> list[dict[str, Any]]:
    vex, logic, core = "vexriscv_re-place", "logicnets_jscl", "corescore_500_mod"
    rows: list[dict[str, Any]] = []
    for value in ["Explore", "AggressiveExplore", "NoTimingRelaxation", "MoreGlobalIterations", "HigherDelayCost", "AdvancedSkewModeling", "AlternateCLBRouting", "RuntimeOptimized", "Quick", "Default"]:
        rows.append(job(vex, "route_directive", value, 1, "whole_design_route_strategy", 600))
    for value in ["Explore", "AggressiveExplore", "RuntimeOptimized", "Quick", "Default"]:
        rows.append(job(vex, "place_directive", value, 2, "whole_design_place_strategy", 600))
    for value in ["low", "medium", "high"]:
        rows.append(job(vex, "place_net_delay_weight", value, 2, "placement_net_delay_parameter", 600))
    rows.append(job(vex, "place_directive_net_delay", "Explore,high", 2, "placement_compatible_combination", 600))
    for value in ["Explore", "ExploreWithHoldFix", "ExploreWithAggressiveHoldFix", "AggressiveExplore", "AlternateReplication", "AggressiveFanoutOpt", "AlternateFlowWithRetiming", "AddRetime", "RuntimeOptimized", "RQS", "Default"]:
        rows.append(job(vex, "phys_directive", value, 1, "physical_optimisation_directive", 600))
    for value in ["fanout_opt", "placement_opt", "routing_opt", "slr_crossing_opt", "restruct_opt", "critical_cell_opt", "dsp_register_opt", "bram_register_opt", "uram_register_opt", "bram_enable_opt", "shift_register_opt", "hold_fix", "aggressive_hold_fix", "retime", "tns_cleanup"]:
        rows.append(job(vex, "phys_option", value, 1, "physical_optimisation_specific_option", 600))
    rows.extend([
        job(vex, "phys_force_replication", "fanout_ge_64_top1", 1, "targeted_replication", 600),
        job(vex, "targeted_route_delay", "fanout_ge_16_top1", 1, "targeted_routing", 600),
        job(vex, "property_cell_loc_fixed", "top_timing_placed_cell", 0, "cell_property", 300),
        job(vex, "property_net_dont_touch", "fanout_ge_16_top1", 0, "net_property", 300),
        job(vex, "property_site_prohibit", "first_unoccupied_slice", 0, "site_property", 300),
        job(vex, "pblock_exclude_placement", "first_clock_region", 0, "pblock_property", 300),
        job(vex, "pblock_assign_cell", "top_timing_cell_clock_region", 0, "pblock_targeting", 300),
        job(vex, "place_cell_roundtrip", "top_timing_placed_cell", 0, "targeted_placement", 300),
        job(vex, "swap_locs", "compatible_placed_pair", 0, "targeted_placement", 300),
    ])
    rows.extend([
        job(logic, "route_directive", "AggressiveExplore", 1, "cross_root_route", 900),
        job(logic, "route_directive", "NoTimingRelaxation", 1, "cross_root_route", 900),
        job(core, "route_directive", "HigherDelayCost", 1, "cross_root_route", 1200),
        job(logic, "place_directive", "AggressiveExplore", 2, "cross_root_place", 900),
        job(logic, "phys_directive", "AggressiveFanoutOpt", 1, "cross_root_phys", 900),
        job(logic, "phys_option", "slr_crossing_opt", 1, "cross_root_phys", 900),
        job(core, "phys_option", "bram_register_opt", 1, "cross_root_hardblock_phys", 1200),
    ])
    if len({row["job_id"] for row in rows}) != len(rows):
        raise RuntimeError("duplicate_job_id")
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(allow_abbrev=False)
    parser.add_argument("--roots", type=Path, required=True)
    parser.add_argument("--registry", type=Path, required=True)
    parser.add_argument("--properties", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    roots = json.loads(args.roots.read_text())
    registry = json.loads(args.registry.read_text())
    properties = json.loads(args.properties.read_text())
    rows = matrix()
    known_roots = {row["root_id"] for row in roots["roots"]}
    if any(row["root_id"] not in known_roots for row in rows): raise RuntimeError("unknown_root")
    planned_heavy = sum(row["planned_heavy_calls"] for row in rows)
    if planned_heavy > 96: raise RuntimeError("heavy_call_cap_exceeded")
    core = {
        "schema_version": "dof.wp014b-real-admission-plan.v1",
        "continuation_identity": "wp014b-action-surface-continuation-v1",
        "freeze_basis_commit": subprocess.run(("git", "rev-parse", "HEAD"), cwd=REPO, check=True, capture_output=True, text=True).stdout.strip(),
        "authority_registry_sha256": registry["registry_sha256"],
        "property_analysis_sha256": properties["analysis_sha256"],
        "concurrency": {"weekday_max": 8, "weekend_max": 16, "active_max": 8},
        "caps": {"implementation_heavy_calls": 96, "aggregate_wall_seconds": 43200, "openrouter_requests": 0},
        "selection_rule": "full installed directive values on small root; representative option/target/property families; selected cross-root applicability checks; no QoR-based inclusion",
        "jobs": rows, "counts": {"jobs": len(rows), "planned_heavy_calls": planned_heavy},
        "scientific_repeat_1": False, "sealed_holdout_access": False,
    }
    result = core | {"plan_sha256": digest(core)}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, sort_keys=True, indent=2) + "\n")
    print(json.dumps(result["counts"] | {"plan_sha256": result["plan_sha256"]}, sort_keys=True))
    return 0


if __name__ == "__main__": raise SystemExit(main())
