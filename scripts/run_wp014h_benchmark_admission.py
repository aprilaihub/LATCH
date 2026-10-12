#!/usr/bin/env python3
"""Read-only WP-014H continuation benchmark admission and golden=self V1."""

from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tarfile
import time
from typing import Any

try:
    from scripts.run_wp014f_validation_recon import file_sha256, object_sha256, run_cases
except ModuleNotFoundError:  # Direct ``python scripts/...`` execution.
    from run_wp014f_validation_recon import file_sha256, object_sha256, run_cases


REPO = Path(__file__).resolve().parents[1]


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")


def parse_probe(path: Path) -> dict[str, Any]:
    rows: dict[str, Any] = {}
    clocks: list[dict[str, Any]] = []
    for line in path.read_text(errors="replace").splitlines():
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        if key == "clock":
            name, period = value.split("|", 1)
            clocks.append({"name": name, "period_ns": float(period)})
        else:
            rows[key] = value
    rows["clocks"] = clocks
    route = str(rows.get("route_status", ""))
    rows["fully_routed"] = bool(
        re.search(r"fully routed nets[^:]*:\s*[1-9][0-9]*", route, re.IGNORECASE)
        and re.search(r"nets with routing errors[^:]*:\s*0(?:\D|$)", route, re.IGNORECASE)
    )
    return rows


def admission_v1_terminal(result: dict[str, Any], log_text: str) -> str:
    terminal = str(result["terminal"])
    if terminal == "VALIDATOR_INFRASTRUCTURE_FAILURE" and "No clock signal found in design" in log_text:
        return "VALIDATOR_INCOMPATIBLE_NO_CLOCK_PORT"
    return terminal


def probe(candidate: dict[str, Any], output: Path, vivado: str) -> dict[str, Any]:
    candidate_id = candidate["candidate_id"]
    case_dir = output / "v0" / candidate_id
    case_dir.mkdir(parents=True, exist_ok=False)
    dcp = Path(candidate["dcp"])
    before = file_sha256(dcp)
    if before != candidate["sha256"]:
        raise ValueError(f"input_hash_mismatch:{candidate_id}")
    result_path = case_dir / "probe.txt"
    log_path = case_dir / "vivado.log"
    command = [vivado, "-mode", "batch", "-nolog", "-nojournal", "-source", str(REPO / "scripts" / "wp014h_root_admission.tcl"), "-tclargs", str(dcp), str(result_path)]
    started = time.time()
    with log_path.open("w") as log:
        completed = subprocess.run(command, cwd=case_dir, stdout=log, stderr=subprocess.STDOUT, timeout=3600, check=False)
    parsed = parse_probe(result_path) if result_path.is_file() else {"status": "NO_PROBE_OUTPUT", "clocks": [], "fully_routed": False}
    matching = [row for row in parsed["clocks"] if row["name"] == candidate["target_clock"]]
    period_ok = True
    if "expected_period_ns" in candidate:
        period_ok = len(matching) == 1 and abs(matching[0]["period_ns"] - float(candidate["expected_period_ns"])) <= 1e-6
    part = str(parsed.get("part", ""))
    terminal = "V0_PASS" if completed.returncode == 0 and part.startswith("xcvu3p") and len(matching) == 1 and period_ok and parsed["fully_routed"] else "V0_FAIL"
    core = {
        "candidate": candidate,
        "returncode": completed.returncode,
        "wall_seconds": time.time() - started,
        "probe": parsed,
        "target_clock_matches": len(matching),
        "target_period_matches_if_predeclared": period_ok,
        "terminal": terminal,
        "input_sha256_before": before,
        "input_sha256_after": file_sha256(dcp),
        "optimization_calls": 0,
        "log_sha256": file_sha256(log_path),
    }
    result = core | {"result_sha256": object_sha256(core)}
    write_json(case_dir / "result.json", result)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(allow_abbrev=False)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--jobs", type=int, default=16)
    parser.add_argument("--skip-v1", action="store_true")
    args = parser.parse_args()
    if args.output.exists():
        raise FileExistsError(args.output)
    if not 1 <= args.jobs <= 16:
        raise ValueError("jobs_must_be_between_1_and_16")
    config = json.loads(args.config.read_text())
    vivado = os.environ.get("VIVADO_EXEC")
    rapidwright = os.environ.get("RAPIDWRIGHT_PATH")
    if not vivado or not Path(vivado).is_file() or not os.access(vivado, os.X_OK):
        raise RuntimeError("VIVADO_EXEC_not_executable")
    if not rapidwright or not Path(rapidwright).is_dir():
        raise RuntimeError("RAPIDWRIGHT_PATH_not_directory")
    args.output.mkdir(parents=True)

    for asset in config["official_assets"].values():
        archive = Path(asset["archive"])
        if file_sha256(archive) != asset["archive_sha256"]:
            raise ValueError(f"archive_hash_mismatch:{archive}")
    fpga_archive = Path(config["official_assets"]["fpga24_evaluation"]["archive"])
    with tarfile.open(fpga_archive, "r:gz") as archive:
        members = set(archive.getnames())
    source_constraint_members = sorted(
        member for member in members if member.lower().endswith((".xdc", ".sdc"))
    )
    fpga_rows = []
    for row in config["fpga24_no_timing_candidates"]:
        present = all(member in members for member in row["members"])
        fpga_rows.append(dict(row) | {
            "source_members_present": present,
            "source_constraint_members": source_constraint_members,
            "terminal": "NO_SOURCE_SUPPORTED_TIMING_CONTRACT" if present and not source_constraint_members else "SOURCE_MEMBERS_MISSING" if not present else "SOURCE_CONSTRAINT_REVIEW_REQUIRED",
            "optimization_calls": 0,
        })

    candidates = config["dcp_candidates"]
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(args.jobs, len(candidates))) as pool:
        v0 = list(pool.map(lambda row: probe(row, args.output, vivado), candidates))
    admitted_v0 = [row for row in v0 if row["terminal"] == "V0_PASS"]

    validation = None
    if not args.skip_v1 and admitted_v0:
        validation_plan = json.loads((REPO / "configs" / "wp014f" / "validation-recon-v1.json").read_text())
        cases = []
        for row in admitted_v0:
            candidate = row["candidate"]
            cases.append({
                "case_id": "golden_self__" + candidate["candidate_id"],
                "root_id": candidate["candidate_id"],
                "mechanism": "golden_self",
                "golden_dcp": candidate["dcp"],
                "revised_dcp": candidate["dcp"],
                "golden_sha256": candidate["sha256"],
                "revised_sha256": candidate["sha256"],
                "resolved_input_sha256": {"golden": candidate["sha256"], "revised": candidate["sha256"]},
                "sealed_holdout_accesses": 1 if candidate.get("sealed_read_only") else 0,
            })
        validation = run_cases(cases, validation_plan, args.output / "v1", "multiseed", min(args.jobs, len(cases)))

    v1_by_root = {}
    if validation:
        for row in validation["results"]:
            log_path = args.output / "v1" / row["case"]["case_id"] / "validator.log"
            v1_by_root[row["case"]["root_id"]] = admission_v1_terminal(
                row, log_path.read_text(errors="replace")
            )
    admissions = []
    for row in v0:
        candidate = row["candidate"]
        v1_terminal = v1_by_root.get(candidate["candidate_id"])
        admitted = row["terminal"] == "V0_PASS" and v1_terminal == "MULTISEED_FUNCTIONAL_PASS"
        admissions.append({
            "candidate_id": candidate["candidate_id"],
            "portfolio": candidate["portfolio"],
            "source": candidate["source"],
            "v0": row["terminal"],
            "v1": v1_terminal,
            "admitted": admitted,
            "optimization_calls": 0,
        })
    admissions.extend(fpga_rows)
    transfer = sum(row.get("admitted", False) and row.get("portfolio") == "same_family_temporal" for row in admissions)
    heldout = sum(row.get("admitted", False) and row.get("portfolio") == "true_family_heldout" for row in admissions)
    heldout_sources = sorted({row["source"] for row in admissions if row.get("admitted") and row.get("portfolio") == "true_family_heldout"})
    gate = "GO" if 7 <= transfer <= 10 and heldout >= 4 and {"RAPIDWRIGHT", "FPGA24"}.issubset(heldout_sources) else "NO_GO"
    core = {
        "schema_version": "dof.wp014h-benchmark-admission-continuation.v1",
        "identity": config["identity"],
        "config_sha256": file_sha256(args.config),
        "gate": gate,
        "admissions": admissions,
        "counts": {"transfer": transfer, "true_family_heldout": heldout, "heldout_sources": heldout_sources},
        "requirements": {"transfer_min": 7, "transfer_max": 10, "heldout_min": 4, "heldout_sources": ["RAPIDWRIGHT", "FPGA24"]},
        "provider_or_llm_calls": 0,
        "optimization_calls": 0,
        "sealed_read_only_accesses": sum(int(row.get("sealed_holdout_accesses", 0)) for row in (validation or {}).get("results", [])),
        "all_dcp_inputs_unchanged": all(row["input_sha256_before"] == row["input_sha256_after"] for row in v0) and (validation is None or validation["all_inputs_unchanged"]),
        "v1_aggregate_sha256": validation.get("aggregate_sha256") if validation else None,
    }
    summary = core | {"summary_sha256": object_sha256(core)}
    write_json(args.output / "benchmark-admission-summary.json", summary)
    print(json.dumps({"gate": gate, "counts": summary["counts"]}, sort_keys=True))
    return 0 if gate == "GO" else 2


if __name__ == "__main__":
    raise SystemExit(main())
