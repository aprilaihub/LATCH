#!/usr/bin/env python3
"""Build and freeze the WP-015 14 x M10 development reference panel.

The command has two deliberately separate phases.  ``plan`` reconciles the
three physical evidence sources and emits the exact V1 validation inventory.
``finalize`` consumes a seed-parallel validation aggregate and derives the
functional-valid panel, Macro oracle, fixed L1 ranking and reduced-M masks.
Neither phase launches an optimiser, a provider request or a validator.
"""

from __future__ import annotations

import argparse
import collections
import hashlib
import itertools
import json
import os
from pathlib import Path
import statistics
import subprocess
import sys
from typing import Any, Iterable, Mapping


REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))


M10 = (
    "macro.phys_opt", "macro.place_route", "macro.auto_pblock", "macro.retime",
    "macro.net_swap", "macro.memory_path_opt", "macro.hardblock_relocate",
    "macro.congestion_spread", "macro.fanout", "macro.lut_cone",
)
PRIORITY = (
    "logicnets_jscl", "corescore_500_mod", "finn_radioml", "amd_mini-isp",
    "rosetta_optical-flow", "rosetta_digit-recognition", "vexriscv_re-place",
    "vexriscv_re-place_v2", "rosetta_3d-rendering", "rosetta_spam-filter",
    "boom_soc", "ispd16_example2", "vtr_mcml",
    "fir_systolic_transposed_routed",
)
SPATIAL = {"macro.place_route", "macro.auto_pblock"}
HARDBLOCK = {"macro.memory_path_opt", "macro.hardblock_relocate"}
MECHANISM_BEARING = SPATIAL | HARDBLOCK | {"macro.fanout", "macro.lut_cone"}


def read(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def write(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def canonical_hash(value: Any) -> str:
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    return hashlib.sha256(raw).hexdigest()


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def git(*args: str) -> str:
    return subprocess.check_output(["git", *args], cwd=REPO, text=True).strip()


def _commit_file_sha256(commit: str, path: str) -> str:
    raw = subprocess.check_output(["git", "show", f"{commit}:{path}"], cwd=REPO)
    return hashlib.sha256(raw).hexdigest()


def implementation_reuse_audit(cells: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    by_source = {
        "WP013_M8_EXACT_IMPLEMENTATION_REUSE": (
            "5e9444f066c7e277a7fa7347444294431f7ec71f",
            ("optimizer_pipeline/backends/xilinx/legacy.py", "scripts/run_flow_matrix.py"),
        ),
        "WP014I_M10_EXTENSION_EXACT_IMPLEMENTATION_REUSE": (
            "d3c5d2d27e1621e0cbfed08b2388fad4f19f38fa",
            ("optimizer_pipeline/backends/xilinx/legacy.py", "scripts/run_flow_matrix.py"),
        ),
        "WP015_M10_FRESH": (
            next(row["physical_source_commit"] for row in cells if row["physical_source"] == "WP015_M10_FRESH"),
            ("optimizer_pipeline/backends/xilinx/legacy.py", "scripts/run_flow_matrix.py"),
        ),
    }
    rows = []
    for source, (commit, paths) in by_source.items():
        if not any(row["physical_source"] == source for row in cells):
            raise ValueError("physical_reuse_source_missing:" + source)
        for path in paths:
            old = _commit_file_sha256(commit, path)
            current = file_sha256(REPO / path)
            rows.append({
                "physical_source": source, "source_commit": commit, "path": path,
                "source_sha256": old, "current_sha256": current, "exact_match": old == current,
            })
    if not all(row["exact_match"] for row in rows):
        raise ValueError("physical_implementation_reuse_hash_mismatch")
    return rows


def _assert_file(path: Path, expected: str, label: str) -> None:
    if not path.is_file() or file_sha256(path) != expected:
        raise ValueError(label + "_hash_mismatch")


def _roots(config: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    manifest = REPO / str(config["development_roots"])
    _assert_file(manifest, str(config["development_roots_sha256"]), "development_roots")
    rows = read(manifest)["roots"]
    roots = {
        row["candidate_id"]: {
            "root_id": row["candidate_id"], "family": row["family"],
            "resource_class": row["resource_class"], "root_path": row["dcp"],
            "root_sha256": row["sha256"],
        }
        for row in rows
    }
    if tuple(root for root in PRIORITY if root not in roots):
        raise ValueError("development_root_priority_incomplete")
    for row in roots.values():
        _assert_file(Path(row["root_path"]), row["root_sha256"], "root:" + row["root_id"])
    return roots


def _m8_cells(config: Mapping[str, Any], roots: Mapping[str, dict[str, Any]]) -> list[dict[str, Any]]:
    evidence = Path(config["reuse_evidence"]["m8_validation"]["path"])
    _assert_file(evidence, config["reuse_evidence"]["m8_validation"]["sha256"], "m8_validation")
    validation = read(evidence)
    if validation.get("terminal_counts") != {"MULTISEED_FUNCTIONAL_PASS": 80}:
        raise ValueError("m8_prior_validation_not_complete")
    validation_by_job = {
        row["case"]["case_id"].removeprefix("m8__"): row for row in validation["results"]
    }
    if not os.environ.get("LATCH_WP013_PANEL_PATH"):
        raise RuntimeError("LATCH_WP013_PANEL_PATH must identify the external frozen panel")
    panel_path = Path(os.environ["LATCH_WP013_PANEL_PATH"]).resolve(strict=True)
    panel = read(panel_path)
    cells = []
    for row in panel:
        prior = validation_by_job[row["job_id"]]
        case = prior["case"]
        root = roots[row["root_id"]]
        cells.append({
            "cell_id": row["root_id"] + "__" + row["action_key"].removeprefix("macro."),
            "root_id": row["root_id"], "family": root["family"],
            "resource_class": root["resource_class"], "action_key": row["action_key"],
            "flow_name": row["flow_name"], "physical_endpoint": row["canonical_endpoint"],
            "initial_fmax_mhz": row["initial_fmax_mhz"],
            "final_fmax_mhz": row["final_fmax_mhz"],
            "initial_wns_ns": row.get("initial_wns_ns"), "final_wns_ns": row.get("final_wns_ns"),
            "runtime_seconds": row.get("duration_seconds"),
            "root_dcp": root["root_path"], "root_sha256": root["root_sha256"],
            "child_dcp": case["revised_dcp"], "child_sha256": case["revised_sha256"],
            "physical_source": "WP013_M8_EXACT_IMPLEMENTATION_REUSE",
            "physical_source_commit": "5e9444f066c7e277a7fa7347444294431f7ec71f",
            "physical_evidence_path": str(panel_path),
            "prior_v1_evidence_path": str(evidence),
        })
    return cells


def _attempt_cells(
    aggregate_path: Path, source: str, roots: Mapping[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    aggregate = read(aggregate_path)
    cells = []
    for row in aggregate["rows"]:
        root = roots[row["root_id"]]
        attempt = row["attempt"]
        cells.append({
            "cell_id": row["root_id"] + "__" + row["action_key"].removeprefix("macro."),
            "root_id": row["root_id"], "family": root["family"],
            "resource_class": root["resource_class"], "action_key": row["action_key"],
            "flow_name": row["flow_name"],
            "physical_endpoint": attempt.get("canonical_endpoint"),
            "initial_fmax_mhz": attempt.get("initial_fmax_mhz"),
            "final_fmax_mhz": attempt.get("final_fmax_mhz"),
            "initial_wns_ns": attempt.get("initial_wns_ns"),
            "final_wns_ns": attempt.get("final_wns_ns"),
            "runtime_seconds": attempt.get("outer_duration_seconds") or attempt.get("duration_seconds"),
            "root_dcp": root["root_path"], "root_sha256": root["root_sha256"],
            "child_dcp": row.get("actual_child"), "child_sha256": row.get("actual_child_sha256"),
            "physical_source": source,
            "physical_source_commit": aggregate.get("source_commit"),
            "physical_evidence_path": str(aggregate_path),
            "process_cleanup_ok": bool(row.get("process", {}).get("cleanup_ok")),
        })
    return cells


def collect_cells(config_path: Path, fresh_path: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    config = read(config_path)
    roots = _roots(config)
    old_m10_path = Path(config["reuse_evidence"]["m10_extension"]["path"])
    _assert_file(old_m10_path, config["reuse_evidence"]["m10_extension"]["sha256"], "m10_extension")
    _assert_file(fresh_path, file_sha256(fresh_path), "fresh_m10")
    fresh = read(fresh_path)
    if fresh.get("completed_jobs") != 30 or not fresh.get("all_roots_unchanged") or not fresh.get("all_processes_clean"):
        raise ValueError("fresh_m10_not_complete_clean")
    cells = [
        *_m8_cells(config, roots),
        *_attempt_cells(old_m10_path, "WP014I_M10_EXTENSION_EXACT_IMPLEMENTATION_REUSE", roots),
        *_attempt_cells(fresh_path, "WP015_M10_FRESH", roots),
    ]
    keys = {(row["root_id"], row["action_key"]) for row in cells}
    if len(cells) != 140 or len(keys) != 140:
        raise ValueError("m10_panel_not_exact_140_unique_cells")
    if set(root for root, _ in keys) != set(PRIORITY):
        raise ValueError("m10_panel_root_set_mismatch")
    for root in PRIORITY:
        if {action for candidate, action in keys if candidate == root} != set(M10):
            raise ValueError("m10_action_set_mismatch:" + root)
    for row in cells:
        if row["physical_endpoint"] == "VALID_COMPLETION":
            if not row["child_dcp"] or not row["child_sha256"]:
                raise ValueError("valid_child_missing:" + row["cell_id"])
            _assert_file(Path(row["child_dcp"]), row["child_sha256"], "child:" + row["cell_id"])
    return roots, sorted(cells, key=lambda row: (PRIORITY.index(row["root_id"]), M10.index(row["action_key"])))


def validation_cases(cells: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    cases = []
    for row in cells:
        if row["physical_endpoint"] != "VALID_COMPLETION":
            continue
        cases.append({
            "case_id": "m10__" + row["cell_id"], "cell_id": row["cell_id"],
            "root_id": row["root_id"],
            "memory_class": "high" if row["resource_class"] == "LARGE" else "normal",
            "mechanism": row["action_key"], "golden_dcp": row["root_dcp"],
            "revised_dcp": row["child_dcp"], "golden_sha256": row["root_sha256"],
            "revised_sha256": row["child_sha256"],
            "resolved_input_sha256": {"golden": row["root_sha256"], "revised": row["child_sha256"]},
            "sealed_holdout_accesses": 0,
        })
    return cases


def plan(args: argparse.Namespace) -> int:
    if git("status", "--porcelain"):
        raise RuntimeError("repository_must_be_clean")
    roots, cells = collect_cells(args.config, args.fresh)
    cases = validation_cases(cells)
    reuse_audit = implementation_reuse_audit(cells)
    result = {
        "schema_version": "dof.wp015-m10-validation-plan.v1",
        "identity": "WP015_GATE3_M10_FINAL_V1", "config_path": str(args.config),
        "config_sha256": file_sha256(args.config), "fresh_path": str(args.fresh),
        "fresh_sha256": file_sha256(args.fresh),
        "repository": {"head": git("rev-parse", "HEAD"), "tree": git("rev-parse", "HEAD^{tree}")},
        "root_count": len(roots), "cell_count": len(cells), "validation_case_count": len(cases),
        "physical_endpoint_counts": dict(sorted(collections.Counter(row["physical_endpoint"] for row in cells).items())),
        "cells": cells, "cases": cases, "physical_implementation_reuse_audit": reuse_audit,
        "functional": {"precheck_vectors": 100, "vectors_per_seed": 200,
                       "seeds": [0xC0FFEE, 0x5EED1234, 0x1BADB002, 0xD15EA5E, 0xA11CE55]},
        "limits": {"preparation_parallel": 16, "seed_parallel": 16,
                   "reserve_fraction": 0.20, "per_case_timeout_seconds": 10800,
                   "per_seed_timeout_seconds": 3600},
        "provider_or_llm_calls": 0, "optimization_calls": 0,
        "transfer_or_holdout_accesses": 0,
    }
    result["plan_sha256"] = canonical_hash(result)
    write(args.output, result)
    print(json.dumps({key: result[key] for key in (
        "cell_count", "validation_case_count", "physical_endpoint_counts", "plan_sha256"
    )}, sort_keys=True))
    return 0


def _stress(panel: list[dict[str, Any]], roots: Mapping[str, dict[str, Any]]) -> dict[str, Any]:
    by_root = {root: [row for row in panel if row["root_id"] == root] for root in PRIORITY}
    derivations = []
    for root in PRIORITY:
        gains = {row["action_key"]: row["root_fallback_gain_mhz"] for row in by_root[root]}
        best = max(gains.values())
        threshold = max(0.90 * best, best - 5.0)
        removed = [action for action in M10 if gains[action] >= threshold]
        retained = [action for action in M10 if action not in removed]
        derivations.append({
            "root_id": root, "family": roots[root]["family"],
            "root_sha256": roots[root]["root_sha256"], "gain_mhz_by_action": gains,
            "best_gain_mhz": best, "threshold_mhz": threshold,
            "removed_action_keys": removed, "retained_action_keys": retained,
            "eligible": best >= 5.0 and len(retained) >= 3 and bool(set(removed) & MECHANISM_BEARING),
            "mechanism_flags": {
                "spatial_or_flow_removed": bool(set(removed) & SPATIAL),
                "hardblock_or_memory_removed": bool(set(removed) & HARDBLOCK),
            },
        })
    eligible = [row for row in derivations if row["eligible"]]
    if len(eligible) < 4:
        raise ValueError("fewer_than_four_eligible_stress_roots")
    flags = ("spatial_or_flow_removed", "hardblock_or_memory_removed")
    selected = next((
        list(candidate) for candidate in itertools.combinations(eligible, 4)
        if all(any(row["mechanism_flags"][flag] for row in candidate) for flag in flags)
    ), None)
    if selected is None:
        raise ValueError("stress_mechanism_coverage_unsatisfied")
    core = {
        "schema_version": "dof.wp015-reduced-m-stress.v1",
        "rule": "gain=max(0,functional-valid child-root); threshold=max(0.90*best,best-5MHz)",
        "selection_priority": list(PRIORITY), "m10": list(M10),
        "derivation_table": derivations, "selected_stress_roots": selected,
        "selected_root_ids": [row["root_id"] for row in selected],
        "model_disclosure": "retained_action_keys_only",
    }
    return core | {"stress_masks_sha256": canonical_hash(core)}


def finalize(args: argparse.Namespace) -> int:
    plan_value = read(args.plan)
    validation = read(args.validation)
    if validation.get("plan_sha256") != plan_value.get("plan_sha256"):
        raise ValueError("validation_plan_hash_mismatch")
    terminals = validation["case_aggregates"]
    roots = _roots(read(Path(plan_value["config_path"])))
    panel = []
    for source in plan_value["cells"]:
        row = dict(source)
        case_id = "m10__" + row["cell_id"]
        aggregate = terminals.get(case_id)
        functional_terminal = aggregate["terminal"] if aggregate else "NOT_APPLICABLE_PHYSICAL_TERMINAL"
        functionally_valid = functional_terminal == "MULTISEED_FUNCTIONAL_PASS"
        raw_delta = (
            float(row["final_fmax_mhz"]) - float(row["initial_fmax_mhz"])
            if row["physical_endpoint"] == "VALID_COMPLETION" and row["final_fmax_mhz"] is not None
            else None
        )
        if row.get("child_sha256") == row["root_sha256"] and raw_delta is not None:
            raw_delta = 0.0
            row["byte_identical_child_delta_forced_zero"] = True
        else:
            row["byte_identical_child_delta_forced_zero"] = False
        row.update({
            "functional_terminal": functional_terminal,
            "functionally_valid": functionally_valid,
            "raw_child_delta_fmax_mhz": raw_delta,
            "valid_child_gain_mhz": max(0.0, raw_delta) if functionally_valid and raw_delta is not None else None,
            "root_fallback_gain_mhz": max(0.0, raw_delta) if functionally_valid and raw_delta is not None else 0.0,
            "validation_case_id": case_id if aggregate else None,
            "validation_semantic_sha256": aggregate.get("semantic_result_sha256") if aggregate else None,
        })
        panel.append(row)
    if len(panel) != 140:
        raise ValueError("final_panel_not_140")
    oracle = {}
    for root in PRIORITY:
        rows = [row for row in panel if row["root_id"] == root]
        winner = min(rows, key=lambda row: (-row["root_fallback_gain_mhz"], row["action_key"]))
        oracle[root] = {"action_key": winner["action_key"], "gain_mhz": winner["root_fallback_gain_mhz"]}
    action_stats = []
    for action in M10:
        rows = [row for row in panel if row["action_key"] == action]
        regrets = [oracle[row["root_id"]]["gain_mhz"] - row["root_fallback_gain_mhz"] for row in rows]
        runtimes = [float(row["runtime_seconds"]) for row in rows if row.get("runtime_seconds") is not None]
        action_stats.append({
            "action_key": action, "mean_oracle_regret_mhz": statistics.mean(regrets),
            "median_oracle_regret_mhz": statistics.median(regrets),
            "functional_valid_count": sum(row["functionally_valid"] for row in rows),
            "physical_valid_count": sum(row["physical_endpoint"] == "VALID_COMPLETION" for row in rows),
            "median_runtime_seconds": statistics.median(runtimes) if runtimes else None,
        })
    l1 = sorted(action_stats, key=lambda row: (row["mean_oracle_regret_mhz"], row["action_key"]))
    stress = _stress(panel, roots)
    coverage = {
        root: {
            "macro_oracle_gain_mhz": oracle[root]["gain_mhz"],
            "macro_oracle_action_key": oracle[root]["action_key"],
            "full_m10_oracle_gain_mhz": oracle[root]["gain_mhz"],
        }
        for root in PRIORITY
    }
    core = {
        "schema_version": "dof.wp015-m10-development-freeze.v1",
        "plan_sha256": plan_value["plan_sha256"],
        "validation_aggregate_sha256": validation["aggregate_sha256"],
        "root_count": 14, "action_count": 10, "cell_count": 140,
        "functional_terminal_counts": dict(sorted(collections.Counter(row["functional_terminal"] for row in panel).items())),
        "physical_endpoint_counts": dict(sorted(collections.Counter(row["physical_endpoint"] for row in panel).items())),
        "m10": list(M10), "panel": panel, "macro_oracle_by_root": oracle,
        "l1_fixed_global_macro_prior": [row["action_key"] for row in l1],
        "action_runtime_reliability": l1, "expert_coverage_deficit_inputs": coverage,
        "reduced_m_stress": stress,
        "provider_or_llm_calls": 0, "optimization_calls": 0,
        "transfer_or_holdout_accesses": 0,
    }
    result = core | {"freeze_sha256": canonical_hash(core)}
    write(args.output, result)
    write(args.stress_output, stress)
    print(json.dumps({
        "cell_count": 140, "functional_terminal_counts": result["functional_terminal_counts"],
        "l1_first": result["l1_fixed_global_macro_prior"][0],
        "stress_roots": stress["selected_root_ids"], "freeze_sha256": result["freeze_sha256"],
    }, sort_keys=True))
    return 0


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(allow_abbrev=False)
    sub = value.add_subparsers(dest="command", required=True)
    build = sub.add_parser("plan", allow_abbrev=False)
    build.add_argument("--config", type=Path, default=REPO / "configs/wp015/m10-development-closure-v1.json")
    build.add_argument("--fresh", type=Path, required=True)
    build.add_argument("--output", type=Path, required=True)
    build.set_defaults(function=plan)
    final = sub.add_parser("finalize", allow_abbrev=False)
    final.add_argument("--plan", type=Path, required=True)
    final.add_argument("--validation", type=Path, required=True)
    final.add_argument("--output", type=Path, required=True)
    final.add_argument("--stress-output", type=Path, required=True)
    final.set_defaults(function=finalize)
    return value


if __name__ == "__main__":
    arguments = parser().parse_args()
    raise SystemExit(arguments.function(arguments))
