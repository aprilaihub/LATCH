#!/usr/bin/env python3
"""Mechanically reconcile WP-015A against the stopped WP-015 canary."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
from typing import Any


REPO = Path(__file__).resolve().parents[1]


def read(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("json_object_required:" + str(path))
    return value


def sha256(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def ancestors() -> set[int]:
    result = {os.getpid()}
    current = os.getppid()
    while current > 1 and current not in result:
        result.add(current)
        try:
            current = int(Path(f"/proc/{current}/stat").read_text().split()[3])
        except (OSError, ValueError, IndexError):
            break
    return result


def active_processes() -> list[dict[str, Any]]:
    ignored = ancestors()
    raw = subprocess.check_output(["ps", "-eo", "pid=,args="], text=True)
    needles = ("vivado", "rapidwright", "run_wp015", "validate_dcps.py")
    rows = []
    for line in raw.splitlines():
        fields = line.strip().split(maxsplit=1)
        if len(fields) != 2:
            continue
        pid, command = int(fields[0]), fields[1]
        if pid not in ignored and any(item in command.lower() for item in needles):
            rows.append({"pid": pid, "command_sha256": hashlib.sha256(command.encode()).hexdigest()})
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(allow_abbrev=False)
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument("--inventory", type=Path, required=True)
    parser.add_argument("--prior-canary", type=Path, required=True)
    parser.add_argument("--prior-functional", type=Path, required=True)
    parser.add_argument("--prior-audit", type=Path, required=True)
    parser.add_argument("--test-report", type=Path, required=True)
    parser.add_argument("--scientific-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    bundle, inventory = read(args.bundle), read(args.inventory)
    canary, functional, audit = read(args.prior_canary), read(args.prior_functional), read(args.prior_audit)
    roots = bundle["roots"]
    root_checks = {
        root_id: sha256(Path(row["root_path"])) == row["sha256"]
        for root_id, row in roots.items()
    }
    inventory_jobs = inventory.get("jobs", [])
    scientific_files = sorted(str(path) for path in args.scientific_dir.rglob("*") if path.is_file())
    tests_text = args.test_report.read_text(encoding="utf-8", errors="replace")
    checks = {
        "entry_head": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=REPO, text=True).strip() == "9fd5f48c328375b8af5959ecb1f89f0538b07f39",
        "entry_tree": subprocess.check_output(["git", "rev-parse", "HEAD^{tree}"], cwd=REPO, text=True).strip() == "acb0aac6c79277fef4e67d3f53fe946957fd94a0",
        "prior_canary_28_terminal": canary.get("job_count") == canary.get("completed_job_count") == 28,
        "prior_canary_v1_14_children": functional.get("functional_terminal_counts", {}).get("MULTISEED_FUNCTIONAL_PASS") == 14,
        "prior_canary_seed_units_70": (
            functional.get("functional_valid_children") == 14
            and functional.get("validation_aggregate_sha256") is not None
        ),
        "prior_tests_1256": 'tests="1256"' in tests_text and 'failures="0"' in tests_text and 'errors="0"' in tests_text,
        "root_hashes_25": len(root_checks) == 25 and all(root_checks.values()),
        "prior_stop_preserved": audit.get("status") == "STOP_SHARED_GATE",
        "inventory_1134_unexecuted": len(inventory_jobs) == 1134 and not scientific_files,
        "sealed_holdout_optimisations_zero": not scientific_files,
    }
    processes = active_processes()
    checks["zero_surviving_processes"] = not processes
    core = {
        "schema_version": "dof.wp015a-entry-reconciliation.v1",
        "status": "PASS" if all(checks.values()) else "FAIL",
        "new_protocol_id": "WP015_LEAN_R1_V2",
        "prior_canary_identity": "WP015_FINAL_CANARY_V1_DIAGNOSTIC",
        "prior_canary_scientific_reuse": 0,
        "checks": checks,
        "prior_terminal_counts": canary.get("terminal_counts"),
        "prior_functional_terminal_counts": functional.get("functional_terminal_counts"),
        "root_hash_match_count": sum(root_checks.values()),
        "scientific_files": scientific_files,
        "active_processes": processes,
        "evidence": {
            "prior_canary_sha256": sha256(args.prior_canary),
            "prior_functional_sha256": sha256(args.prior_functional),
            "prior_audit_sha256": sha256(args.prior_audit),
            "test_report_sha256": sha256(args.test_report),
        },
    }
    raw = json.dumps(core, sort_keys=True, separators=(",", ":")).encode()
    result = core | {"reconciliation_sha256": hashlib.sha256(raw).hexdigest()}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({"status": result["status"], "checks": checks}, sort_keys=True))
    return 0 if result["status"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
