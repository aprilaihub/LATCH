#!/usr/bin/env python3
"""WP-015 Gate-2 serial-versus-seed-parallel validation canary."""

from __future__ import annotations

import argparse
import collections
import concurrent.futures
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
from typing import Any


REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from optimizer_pipeline.dof.seed_validation import (  # noqa: E402
    aggregate_seed_reports, canonical_hash, file_sha256, run_prepared_seed,
    tree_manifest,
)
from optimizer_pipeline.dof.validation_scheduler import (  # noqa: E402
    GIB, MemoryAwareSeedScheduler, ValidationSeed, linux_memory_bytes,
)
from scripts.run_wp014i_validation_canary import resolve  # noqa: E402
from scripts.run_wp014f_validation_recon import WORKDIR_RE  # noqa: E402


CASE_IDS = (
    "g__amd_positive",
    "p__logicnets_fanout",
    "development_self__boom_soc",
)


def read(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("json_object_required:" + str(path))
    return value


def write(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def terminate_process_group(process: subprocess.Popen[Any]) -> list[dict[str, Any]]:
    events = []
    if process.poll() is None:
        os.killpg(process.pid, signal.SIGTERM)
        events.append({"signal": "SIGTERM", "epoch_seconds": time.time()})
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            events.append({"signal": "SIGKILL", "epoch_seconds": time.time()})
            process.wait(timeout=10)
    return events


def owned_survivors(markers: list[str]) -> list[dict[str, Any]]:
    rows = []
    own_pid = os.getpid()
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit() or int(entry.name) == own_pid:
            continue
        try:
            command = (entry / "cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace")
            status = (entry / "status").read_text(errors="replace")
        except (FileNotFoundError, PermissionError, ProcessLookupError):
            continue
        uid_line = next((line for line in status.splitlines() if line.startswith("Uid:")), "")
        if uid_line and int(uid_line.split()[1]) == os.getuid() and any(marker in command for marker in markers):
            rows.append({"pid": int(entry.name), "command_sha256": hashlib.sha256(command.encode()).hexdigest()})
    return sorted(rows, key=lambda row: row["pid"])


def prepare_case(case: dict[str, Any], plan: dict[str, Any], output: Path) -> dict[str, Any]:
    case_dir = output / "prepared" / case["case_id"]
    case_dir.mkdir(parents=True, exist_ok=False)
    log_path = case_dir / "validator.log"
    seeds = [int(value) for value in plan["functional"]["seeds"]]
    command = [
        sys.executable, str(REPO / "validate_dcps.py"),
        case["golden_dcp"], case["revised_dcp"],
        "--vectors", str(plan["functional"]["vectors_per_seed"]),
        "--precheck-vectors", str(plan["functional"]["precheck_vectors"]),
        "--prepare-seed-workspace-only",
    ]
    for seed in seeds:
        command.extend(["--seed", hex(seed)])
    before = {
        "golden": file_sha256(Path(case["golden_dcp"])),
        "revised": file_sha256(Path(case["revised_dcp"])),
    }
    if before != case["resolved_input_sha256"]:
        raise ValueError("prepare_input_hash_mismatch:" + case["case_id"])
    started = time.time()
    with log_path.open("w") as log:
        process = subprocess.Popen(
            command, cwd=REPO, stdout=log, stderr=subprocess.STDOUT,
            start_new_session=True, env=os.environ.copy(),
        )
        try:
            returncode = process.wait(timeout=int(plan["boundaries"]["per_case_timeout_seconds"]))
            cleanup = []
        except subprocess.TimeoutExpired:
            cleanup = terminate_process_group(process)
            returncode = 124
    log_text = log_path.read_text(errors="replace")
    matches = WORKDIR_RE.findall(log_text)
    validator_dir = Path(matches[-1]) if matches else None
    report_path = validator_dir / "validation_report.json" if validator_dir else None
    report = read(report_path) if report_path and report_path.is_file() else None
    if (
        returncode != 0 or report is None or not report.get("phase1_passed")
        or not report.get("seed_workspace_prepared")
        or not (report.get("simulation_report") or {}).get("precheck_passed")
    ):
        raise RuntimeError("seed_workspace_preparation_failed:" + case["case_id"])
    workspace = Path(report["simulation_report"]["prepared_seed_workspace"])
    manifest = tree_manifest(workspace)
    write(case_dir / "prepared-workspace-manifest.json", manifest)
    after = {
        "golden": file_sha256(Path(case["golden_dcp"])),
        "revised": file_sha256(Path(case["revised_dcp"])),
    }
    result = {
        "case": case, "returncode": returncode, "wall_seconds": time.time() - started,
        "validator_directory": str(validator_dir), "prepared_workspace": str(workspace),
        "prepared_workspace_manifest_sha256": canonical_hash(manifest),
        "prepared_workspace_file_count": len(manifest),
        "prepared_workspace_bytes": sum(int(row.get("bytes", 0)) for row in manifest),
        "validator_report": report, "input_sha256_before": before,
        "input_sha256_after": after, "cleanup_events": cleanup,
    }
    write(case_dir / "prepare-result.json", result)
    return result


def run_mode(
    name: str, preparations: list[dict[str, Any]], plan: dict[str, Any],
    output: Path, target: int,
) -> dict[str, Any]:
    seeds = [int(value) for value in plan["functional"]["seeds"]]
    units = [
        ValidationSeed(
            prep["case"]["case_id"], prep["case"]["root_id"], f"seed-{index:02d}",
            seed, prep["case"]["memory_class"],
        )
        for prep in preparations for index, seed in enumerate(seeds)
    ]
    estimates = {"normal": 8 * GIB, "high": 64 * GIB, "unknown": 64 * GIB}
    scheduler = MemoryAwareSeedScheduler(
        units, maximum=16, target=target, reserve_fraction=0.20, estimates=estimates,
    )
    prep_by_case = {row["case"]["case_id"]: row for row in preparations}
    results: list[dict[str, Any]] = []
    running: dict[concurrent.futures.Future[dict[str, Any]], ValidationSeed] = {}
    events = []
    started = time.time()
    mode_root = output / name
    mode_root.mkdir(parents=True, exist_ok=False)
    with concurrent.futures.ThreadPoolExecutor(max_workers=target) as pool:
        while scheduler.pending or running:
            total, available = linux_memory_bytes()
            admitted = scheduler.refill(
                running=running.values(), total_memory=total, available_memory=available,
            )
            events.append({
                "epoch_seconds": time.time(), "running": len(running),
                "pending": len(scheduler.pending) + len(admitted),
                "admitted": [[unit.case_id, unit.seed_id] for unit in admitted],
                "memory_total_bytes": total, "memory_available_bytes": available,
                "reserve_bytes": int(total * 0.20),
            })
            for unit in admitted:
                prep = prep_by_case[unit.case_id]
                destination = mode_root / unit.case_id / unit.seed_id
                future = pool.submit(
                    run_prepared_seed, prepared_workspace=Path(prep["prepared_workspace"]),
                    output=destination, stimulus_seed=unit.stimulus_seed,
                    vectors=int(plan["functional"]["vectors_per_seed"]),
                    timeout_seconds=3600,
                )
                running[future] = unit
            if not running:
                time.sleep(1)
                continue
            done, _ = concurrent.futures.wait(
                running, timeout=2, return_when=concurrent.futures.FIRST_COMPLETED,
            )
            for future in done:
                unit = running.pop(future)
                result = future.result()
                result.update({"case_id": unit.case_id, "root_id": unit.root_id, "seed_id": unit.seed_id})
                results.append(result)
    case_aggregates = {}
    for prep in preparations:
        case_id = prep["case"]["case_id"]
        rows = [row for row in results if row["case_id"] == case_id]
        case_aggregates[case_id] = aggregate_seed_reports(
            prepared_report=prep["validator_report"], seed_reports=rows,
            expected_seeds=seeds,
        )
    result = {
        "mode": name, "target_concurrency": target, "scheduled_units": len(units),
        "completed_units": len(results), "wall_seconds": time.time() - started,
        "events": events, "seed_results": results, "case_aggregates": case_aggregates,
        "terminal_counts": dict(collections.Counter(row["terminal"] for row in case_aggregates.values())),
        "peak_seed_rss_bytes": max((int(row.get("resource", {}).get("max_rss_bytes", 0)) for row in results), default=0),
    }
    write(mode_root / "aggregate.json", result)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(allow_abbrev=False)
    parser.add_argument("--config", type=Path, default=REPO / "configs/wp014i/validation-canary-v1.json")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--reuse-prepared", type=Path,
        help="Reuse hash-verified prepared case workspaces from an earlier infrastructure attempt.",
    )
    args = parser.parse_args()
    if args.output.exists():
        raise FileExistsError(args.output)
    config = read(args.config)
    legacy_plan = read(REPO / config["legacy_plan"])
    cases = {row["case_id"]: row for row in resolve(config)}
    selected = [cases[case_id] for case_id in CASE_IDS]
    if [row["memory_class"] for row in selected] != ["normal", "normal", "high"]:
        raise RuntimeError("small_medium_large_memory_classes_not_frozen")
    args.output.mkdir(parents=True)
    write(args.output / "resolved-plan.json", {
        "schema_version": "dof.wp015-seed-parallel-canary-plan.v1",
        "case_ids": list(CASE_IDS), "cases": selected,
        "functional": legacy_plan["functional"], "provider_or_llm_calls": 0,
        "optimization_calls": 0, "sealed_holdout_accesses": 0,
    })
    started = time.time()
    if args.reuse_prepared:
        preparations = []
        for case in selected:
            source = args.reuse_prepared / "prepared" / case["case_id"]
            prior = read(source / "prepare-result.json")
            workspace = Path(prior["prepared_workspace"])
            observed = canonical_hash(tree_manifest(workspace))
            if observed != prior["prepared_workspace_manifest_sha256"]:
                raise RuntimeError("reused_prepared_workspace_hash_mismatch:" + case["case_id"])
            if prior["case"]["resolved_input_sha256"] != case["resolved_input_sha256"]:
                raise RuntimeError("reused_prepared_input_mismatch:" + case["case_id"])
            destination = args.output / "prepared" / case["case_id"]
            destination.mkdir(parents=True)
            reused = dict(prior)
            reused["reused_from"] = str(source)
            write(destination / "prepare-result.json", reused)
            write(destination / "prepared-workspace-manifest.json", tree_manifest(workspace))
            preparations.append(reused)
    else:
        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
            preparations = list(pool.map(
                lambda case: prepare_case(case, legacy_plan, args.output), selected,
            ))
    prepared_before = {
        row["case"]["case_id"]: row["prepared_workspace_manifest_sha256"]
        for row in preparations
    }
    serial = run_mode("serial", preparations, legacy_plan, args.output, 1)
    parallel = run_mode("parallel", preparations, legacy_plan, args.output, 16)
    prepared_after = {
        row["case"]["case_id"]: canonical_hash(tree_manifest(Path(row["prepared_workspace"])))
        for row in preparations
    }
    serial_units = {(row["case_id"], row["stimulus_seed"]): row for row in serial["seed_results"]}
    parallel_units = {(row["case_id"], row["stimulus_seed"]): row for row in parallel["seed_results"]}
    unit_semantic_equal = {
        f"{case_id}:{seed:08X}": serial_units[(case_id, seed)]["semantic_result_sha256"]
        == parallel_units[(case_id, seed)]["semantic_result_sha256"]
        for case_id, seed in sorted(serial_units)
    }
    aggregate_equal = {
        case_id: serial["case_aggregates"][case_id]["semantic_result_sha256"]
        == parallel["case_aggregates"][case_id]["semantic_result_sha256"]
        for case_id in CASE_IDS
    }
    input_after = {
        row["case"]["case_id"]: {
            "golden": file_sha256(Path(row["case"]["golden_dcp"])),
            "revised": file_sha256(Path(row["case"]["revised_dcp"])),
        } for row in preparations
    }
    markers = [str(args.output)] + [row["prepared_workspace"] for row in preparations]
    audit_1 = owned_survivors(markers)
    time.sleep(2)
    audit_2 = owned_survivors(markers)
    pass_gate = (
        all(unit_semantic_equal.values()) and all(aggregate_equal.values())
        and prepared_before == prepared_after
        and all(row["input_sha256_before"] == input_after[row["case"]["case_id"]] for row in preparations)
        and serial["completed_units"] == parallel["completed_units"] == 15
        and all(value["all_seeds_present_exactly_once"] for value in serial["case_aggregates"].values())
        and all(value["all_seeds_present_exactly_once"] for value in parallel["case_aggregates"].values())
        and all(value["terminal"] == "MULTISEED_FUNCTIONAL_PASS" for value in serial["case_aggregates"].values())
        and all(value["terminal"] == "MULTISEED_FUNCTIONAL_PASS" for value in parallel["case_aggregates"].values())
        and not audit_1 and not audit_2
    )
    summary = {
        "schema_version": "dof.wp015-seed-parallel-canary.v1",
        "gate": "PASS" if pass_gate else "FAIL", "cases": list(CASE_IDS),
        "case_count": len(CASE_IDS), "seed_units_per_mode": 15,
        "prepared_once_per_case": True, "prepared_workspace_hashes_before": prepared_before,
        "prepared_workspace_hashes_after": prepared_after,
        "serial": {key: serial[key] for key in ("wall_seconds", "terminal_counts", "peak_seed_rss_bytes")},
        "parallel": {key: parallel[key] for key in ("wall_seconds", "terminal_counts", "peak_seed_rss_bytes")},
        "unit_semantic_equal": unit_semantic_equal, "case_aggregate_equal": aggregate_equal,
        "owned_process_audit_1": audit_1, "owned_process_audit_2": audit_2,
        "input_hashes_unchanged": all(row["input_sha256_before"] == input_after[row["case"]["case_id"]] for row in preparations),
        "queue_wall_seconds": time.time() - started, "provider_or_llm_calls": 0,
        "optimization_calls": 0, "sealed_holdout_accesses": 0,
    }
    summary["summary_sha256"] = canonical_hash(summary)
    write(args.output / "summary.json", summary)
    print(json.dumps({key: summary[key] for key in (
        "gate", "cases", "serial", "parallel", "input_hashes_unchanged"
    )}, sort_keys=True))
    return 0 if pass_gate else 2


if __name__ == "__main__":
    raise SystemExit(main())
