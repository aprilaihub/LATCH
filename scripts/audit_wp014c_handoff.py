#!/usr/bin/env python3
"""Independent, read-only handoff audit for WP-014C evidence."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
from typing import Any


EXPECTED_PHASES = {
    "CALIBRATION": (18, 18, 0),
    "N_K": (90, 90, 0),
    "S_N": (36, 36, 0),
    "C_S": (36, 18, 18),
}
EXPECTED_SOURCE_COMMIT = "dcc415aa0b9bfc45512ead7440bbbc1c684baa4f"


def canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def digest(value: Any) -> str:
    return hashlib.sha256(value if isinstance(value, bytes) else canonical(value)).hexdigest()


def read(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text())
    if not isinstance(value, dict):
        raise ValueError(f"json_object_required:{path}")
    return value


def verify_embedded(value: dict[str, Any], key: str) -> bool:
    claimed = value.get(key)
    if not isinstance(claimed, str):
        return False
    body = {name: item for name, item in value.items() if name != key}
    return digest(body) == claimed


def git(repo: Path, *args: str) -> str:
    return subprocess.check_output(["git", *args], cwd=repo, text=True).strip()


def live_owned_processes(markers: list[str]) -> list[dict[str, Any]]:
    excluded = {1, os.getpid()}
    current = os.getppid()
    while current > 1 and current not in excluded:
        excluded.add(current)
        try:
            fields = (Path("/proc") / str(current) / "stat").read_text().split()
            current = int(fields[3])
        except (FileNotFoundError, PermissionError, ProcessLookupError, ValueError, IndexError):
            break
    result = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit() or int(entry.name) in excluded:
            continue
        try:
            raw = (entry / "cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace")
        except (FileNotFoundError, PermissionError, ProcessLookupError):
            continue
        matched = [marker for marker in markers if marker in raw]
        if matched:
            result.append({"pid": int(entry.name), "markers": matched})
    return sorted(result, key=lambda row: row["pid"])


def main() -> int:
    parser = argparse.ArgumentParser(allow_abbrev=False)
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--capture", type=Path, required=True)
    parser.add_argument("--calibration", type=Path, required=True)
    parser.add_argument("--nk", type=Path, required=True)
    parser.add_argument("--sn", type=Path, required=True)
    parser.add_argument("--cs", type=Path, required=True)
    parser.add_argument("--analysis", type=Path, required=True)
    parser.add_argument("--ledger", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    errors: list[str] = []
    capture = read(args.capture / "capture-aggregate.json")
    if not capture.get("accepted") or not verify_embedded(capture, "capture_sha256"):
        errors.append("capture_not_accepted_or_hash_invalid")
    if capture.get("root_count") != 10:
        errors.append("capture_root_count_not_10")

    phase_dirs = {
        "CALIBRATION": args.calibration,
        "N_K": args.nk,
        "S_N": args.sn,
        "C_S": args.cs,
    }
    phase_evidence: dict[str, Any] = {}
    for phase, directory in phase_dirs.items():
        aggregate = read(directory / "phase-aggregate.json")
        audit = read(directory / "independent-audit.json")
        planned, fresh, reused = EXPECTED_PHASES[phase]
        checks = {
            "aggregate_hash_valid": verify_embedded(aggregate, "aggregate_sha256"),
            "audit_hash_valid": verify_embedded(audit, "audit_sha256"),
            "aggregate_accepted": aggregate.get("accepted_for_evidence_review") is True,
            "independent_audit_accepted": audit.get("accepted") is True,
            "audit_binding_valid": audit.get("aggregate_sha256") == aggregate.get("aggregate_sha256"),
            "matrix_exact": (
                aggregate.get("planned_jobs"), aggregate.get("fresh_jobs"), aggregate.get("reused_jobs")
            ) == (planned, fresh, reused),
            "canonical_terminals_exact": aggregate.get("canonical_terminal_jobs") == planned,
            "root_integrity": aggregate.get("all_roots_unchanged") is True,
            "metadata_complete": aggregate.get("metadata_complete") is True,
            "no_holdout_access": aggregate.get("sealed_holdouts_accessed") is False,
            "no_track_pooling": aggregate.get("track_pooling_performed") is False,
            "source_commit_exact": aggregate.get("source_commit") == EXPECTED_SOURCE_COMMIT,
            "scientific_tree_clean": (
                aggregate.get("repository_before", {}).get("tracked_tree_dirty") is False
                and aggregate.get("repository_after", {}).get("tracked_tree_dirty") is False
            ),
        }
        errors.extend(f"{phase}:{name}" for name, accepted in checks.items() if not accepted)
        phase_evidence[phase] = {
            "checks": checks,
            "aggregate_sha256": aggregate.get("aggregate_sha256"),
            "audit_sha256": audit.get("audit_sha256"),
            "terminal_counts": aggregate.get("terminal_counts"),
            "fresh_heavy_calls": aggregate.get("fresh_heavy_calls"),
            "fresh_cost_usd": aggregate.get("fresh_cost_usd"),
            "phase_wall_seconds": aggregate.get("phase_wall_seconds"),
        }

    analysis = read(args.analysis / "repeat1-analysis.json")
    analysis_hash_valid = verify_embedded(analysis, "analysis_sha256")
    if not analysis_hash_valid:
        errors.append("analysis_hash_invalid")
    if analysis.get("jobs") != {"C_S": 36, "N_K": 90, "S_N": 36, "total": 162}:
        errors.append("analysis_matrix_not_exact")
    if analysis.get("track_pooling_performed") is not False:
        errors.append("analysis_track_pooling")

    ledger = read(args.ledger)
    caps = {
        "cost_usd": {"actual": ledger.get("cost_usd"), "cap": 12.0},
        "scientific_heavy_calls": {"actual": ledger.get("heavy_calls"), "cap": 760},
        "fresh_queue_wall_seconds": {"actual": ledger.get("queue_wall_seconds"), "cap": 129600.0},
        "sealed_holdout_accesses": {"actual": 0, "cap": 0},
    }
    for name, values in caps.items():
        if values["actual"] is None or values["actual"] > values["cap"]:
            errors.append(f"cap_exceeded:{name}")

    status = git(args.repo, "status", "--porcelain")
    repo = {
        "branch": git(args.repo, "branch", "--show-current"),
        "head": git(args.repo, "rev-parse", "HEAD"),
        "tree": git(args.repo, "rev-parse", "HEAD^{tree}"),
        "tracked_and_untracked_clean": not status,
    }
    if status:
        errors.append("handoff_repo_not_clean")

    markers = [str(path.resolve()) for path in phase_dirs.values()] + [str(args.capture.resolve())]
    live = live_owned_processes(markers)
    if live:
        errors.append("owned_processes_live")

    core = {
        "schema_version": "dof.wp014c-handoff-audit.v1",
        "accepted": not errors,
        "errors": errors,
        "capture": {
            "capture_sha256": capture.get("capture_sha256"),
            "accepted": capture.get("accepted"),
            "root_count": capture.get("root_count"),
        },
        "phases": phase_evidence,
        "analysis": {
            "analysis_sha256": analysis.get("analysis_sha256"),
            "hash_valid": analysis_hash_valid,
            "jobs": analysis.get("jobs"),
            "terminal_counts": analysis.get("terminal_counts"),
        },
        "caps": caps,
        "repository": repo,
        "live_owned_processes": live,
    }
    result = core | {"audit_sha256": digest(core)}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, sort_keys=True, indent=2) + "\n")
    print(json.dumps({"accepted": result["accepted"], "errors": errors, "audit_sha256": result["audit_sha256"]}, sort_keys=True))
    return 0 if result["accepted"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
