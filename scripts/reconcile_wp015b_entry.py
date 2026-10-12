#!/usr/bin/env python3
"""Mechanically reconcile the WP-015B entry and preserved WP-015A stop."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import xml.etree.ElementTree as ET
from typing import Any


REPO = Path(__file__).resolve().parents[1]
ENTRY_COMMIT = "487622b3ad2c4999a54bee1abcadac5d2e995874"
ENTRY_TREE = "45f9f2de7b68096d7383c2aa1182384490d0c7e4"


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
            rows.append({
                "pid": pid,
                "command_sha256": hashlib.sha256(command.encode()).hexdigest(),
            })
    return rows


def test_counts(path: Path) -> tuple[int, int, int]:
    root = ET.parse(path).getroot()
    suite = next(item for item in root.iter() if item.tag.endswith("testsuite"))
    return (
        int(suite.attrib.get("tests", 0)),
        int(suite.attrib.get("failures", 0)),
        int(suite.attrib.get("errors", 0)),
    )


def main() -> int:
    parser = argparse.ArgumentParser(allow_abbrev=False)
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument("--wp015a-aggregate", type=Path, required=True)
    parser.add_argument("--wp015a-stop", type=Path, required=True)
    parser.add_argument("--test-report", type=Path, required=True)
    parser.add_argument("--scientific-dir", type=Path, required=True)
    parser.add_argument("--entry-clean-observed", action="store_true")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    bundle = read(args.bundle)
    aggregate = read(args.wp015a_aggregate)
    stop = read(args.wp015a_stop)
    tests, failures, errors = test_counts(args.test_report)
    root_checks = {
        root_id: sha256(Path(row["root_path"])) == row["sha256"]
        for root_id, row in bundle["roots"].items()
    }
    scientific_files = (
        sorted(str(path) for path in args.scientific_dir.rglob("*") if path.is_file())
        if args.scientific_dir.exists() else []
    )
    processes = active_processes()
    checks = {
        "entry_head": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=REPO, text=True).strip() == ENTRY_COMMIT,
        "entry_tree": subprocess.check_output(["git", "rev-parse", "HEAD^{tree}"], cwd=REPO, text=True).strip() == ENTRY_TREE,
        "entry_branch": subprocess.check_output(["git", "branch", "--show-current"], cwd=REPO, text=True).strip() == "phase0-policy-evidence-replan",
        "entry_clean_observed_before_overlay": args.entry_clean_observed,
        "wp015a_21_sessions": aggregate.get("session_count") == 21,
        "wp015a_74_requests": aggregate.get("physical_model_requests") == 74,
        "wp015a_cost": abs(float(aggregate.get("api_cost_usd") or 0) - 3.0237843716) < 1e-12,
        "wp015a_terminals": aggregate.get("terminal_counts") == {"NO_VALID_SUBMISSION": 12, "PLANNED_COMMIT": 9},
        "wp015a_zero_backend": aggregate.get("backend_dispatches") == 0,
        "wp015a_stop_preserved": stop.get("status") == "STOP_SHARED_GATE",
        "tests_1263": (tests, failures, errors) == (1263, 0, 0),
        "root_hashes_25": len(root_checks) == 25 and all(root_checks.values()),
        "scientific_files_zero": not scientific_files,
        "zero_surviving_processes": not processes,
    }
    core = {
        "schema_version": "dof.wp015b-entry-reconciliation.v1",
        "status": "PASS" if all(checks.values()) else "FAIL",
        "new_protocol_id": "WP015_LEAN_R1_V3",
        "new_prelaunch_identity": "WP015B_EXACT_COMPATIBILITY_V1",
        "checks": checks,
        "root_hash_match_count": sum(root_checks.values()),
        "scientific_files": scientific_files,
        "active_processes": processes,
        "superseded_evidence": [
            "WP015A_HARDBLOCK_FEASIBILITY_V1:SUPERSEDED_COMPATIBILITY_UNPROVEN",
            "WP014H_HARDBLOCK_STRICT_WITNESS:SUPERSEDED_COMPATIBILITY_UNPROVEN",
        ],
        "evidence": {
            "wp015a_aggregate_sha256": sha256(args.wp015a_aggregate),
            "wp015a_stop_sha256": sha256(args.wp015a_stop),
            "test_report_sha256": sha256(args.test_report),
        },
    }
    result = core | {"reconciliation_sha256": hashlib.sha256(
        json.dumps(core, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    print(json.dumps({"status": result["status"], "checks": checks}, sort_keys=True))
    return 0 if result["status"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
