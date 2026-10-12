#!/usr/bin/env python3
"""Read-only integrity and zero-survival audit for WP-014D admission evidence."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
from typing import Any


EXPECTED_ADMISSION_COMMIT = "b1cf168bcde6cac9399737fb67cfdd5ca749bd5d"
EXPECTED_LANGUAGE_COUNTS = {"CONTROL": 1, "G": 2, "M": 4, "P": 4}


def canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def digest(value: Any) -> str:
    return hashlib.sha256(value if isinstance(value, bytes) else canonical(value)).hexdigest()


def file_digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def read(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text())
    if not isinstance(value, dict):
        raise ValueError(f"json_object_required:{path}")
    return value


def verify_embedded(value: dict[str, Any], key: str) -> bool:
    claimed = value.get(key)
    if not isinstance(claimed, str):
        return False
    return digest({name: item for name, item in value.items() if name != key}) == claimed


def strict_report_pass(result: dict[str, Any]) -> bool:
    report = result.get("validator_report") or {}
    simulation = report.get("simulation_report") or {}
    full = simulation.get("full_report") or {}
    return all([
        result.get("strict_classification") == "FUNCTIONAL_PASS",
        result.get("returncode") == 0,
        result.get("timed_out") is False,
        report.get("phase1_passed") is True,
        report.get("phase2_passed") is True,
        report.get("phase2_skipped") is False,
        report.get("infrastructure_failure") is False,
        report.get("num_vectors") == 200,
        simulation.get("cycles_simulated") == 200,
        simulation.get("mismatch_count") == 0,
        simulation.get("protocol_mismatch_count") == 0,
        full.get("vectors_requested") == 200,
        full.get("cycles_simulated") == 200,
        full.get("mismatch_count") == 0,
        full.get("protocol_mismatch_count") == 0,
        full.get("passed") is True,
    ])


def live_owned_processes(markers: list[str]) -> list[dict[str, Any]]:
    excluded = {1, os.getpid()}
    current = os.getppid()
    while current > 1 and current not in excluded:
        excluded.add(current)
        try:
            current = int((Path("/proc") / str(current) / "stat").read_text().split()[3])
        except (FileNotFoundError, PermissionError, ProcessLookupError, ValueError, IndexError):
            break
    rows = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit() or int(entry.name) in excluded:
            continue
        try:
            command = (entry / "cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace")
        except (FileNotFoundError, PermissionError, ProcessLookupError):
            continue
        matched = [marker for marker in markers if marker and marker in command]
        if matched:
            rows.append({"pid": int(entry.name), "markers": matched})
    return sorted(rows, key=lambda row: row["pid"])


def git(repo: Path, *args: str) -> str:
    return subprocess.check_output(["git", *args], cwd=repo, text=True).strip()


def main() -> int:
    parser = argparse.ArgumentParser(allow_abbrev=False)
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--run", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    aggregate = read(args.run / "aggregate.json")
    errors: list[str] = []
    checks = {
        "aggregate_hash_valid": verify_embedded(aggregate, "aggregate_sha256"),
        "plan_hash_valid": aggregate.get("plan_sha256") == file_digest(args.plan),
        "matrix_exact": aggregate.get("planned_cases") == aggregate.get("executed_cases") == 11,
        "all_strict_pass": aggregate.get("classification_counts") == {"FUNCTIONAL_PASS": 11},
        "language_counts_exact": aggregate.get("language_counts") == EXPECTED_LANGUAGE_COUNTS,
        "no_stop": aggregate.get("stopped_reason") is None,
        "all_inputs_unchanged": aggregate.get("all_inputs_unchanged") is True,
        "no_recorded_survivors": aggregate.get("owned_process_survivors") == [],
        "no_new_scientific_jobs": aggregate.get("new_scientific_jobs") == 0,
        "no_openrouter_calls": aggregate.get("openrouter_calls") == 0,
        "no_holdout_access": aggregate.get("sealed_holdout_accesses") == 0,
        "vivado_2025_1": any("2025.1" in line for line in aggregate.get("environment", {}).get("vivado_version_lines", [])),
        "admission_source_exact": all(
            aggregate.get(name, {}).get("head") == EXPECTED_ADMISSION_COMMIT
            for name in ("repository_before", "repository_after")
        ),
        "admission_tree_clean": all(
            aggregate.get(name, {}).get("tracked_and_untracked_clean") is True
            for name in ("repository_before", "repository_after")
        ),
    }
    errors.extend(name for name, accepted in checks.items() if not accepted)

    case_audit: list[dict[str, Any]] = []
    for result in aggregate.get("results", []):
        case_id = result.get("case", {}).get("case_id")
        stored_path = args.run / "cases" / str(case_id) / "result.json"
        stored = read(stored_path) if stored_path.is_file() else {}
        row = {
            "case_id": case_id,
            "embedded_hash_valid": verify_embedded(result, "result_sha256"),
            "stored_result_matches": stored == result,
            "strict_report_pass": strict_report_pass(result),
            "input_hashes_unchanged": result.get("input_sha256_before") == result.get("input_sha256_after"),
            "no_case_survivors": result.get("cleanup", {}).get("survivors_after") == [],
        }
        errors.extend(f"{case_id}:{name}" for name, accepted in row.items() if name != "case_id" and not accepted)
        case_audit.append(row)

    markers = [str(args.run.resolve()), "run_wp014d_functional_admission.py"]
    markers.extend(
        str(result.get("validator_directory"))
        for result in aggregate.get("results", [])
        if result.get("validator_directory")
    )
    live = live_owned_processes(markers)
    if live:
        errors.append("owned_processes_live")

    status = git(args.repo, "status", "--porcelain")
    repository = {
        "branch": git(args.repo, "branch", "--show-current"),
        "head": git(args.repo, "rev-parse", "HEAD"),
        "tree": git(args.repo, "rev-parse", "HEAD^{tree}"),
        "tracked_and_untracked_clean": not status,
    }
    if status:
        errors.append("handoff_repo_not_clean")

    core = {
        "schema_version": "dof.wp014d-functional-admission-audit.v1",
        "accepted": not errors,
        "errors": errors,
        "checks": checks,
        "aggregate_sha256": aggregate.get("aggregate_sha256"),
        "case_audit": case_audit,
        "live_owned_processes": live,
        "repository": repository,
    }
    value = core | {"audit_sha256": digest(core)}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(value, sort_keys=True, indent=2) + "\n")
    print(json.dumps({"accepted": value["accepted"], "errors": errors, "audit_sha256": value["audit_sha256"]}, sort_keys=True))
    return 0 if value["accepted"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
