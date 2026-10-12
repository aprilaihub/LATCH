#!/usr/bin/env python3
"""Perform two public-safe zero-live process audits for a completed WP-013 E0."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import time
from typing import Any


SCHEMA = "dof.wp013-e0-process-audit.v1"
GLOBAL_EXECUTABLE_MARKERS = (
    "vivado/bin/unwrapped/lnx64.o/vivado",
    "vivado_mutation_worker.py",
    "run_flow_matrix.py",
    "dcp_optimizer.py",
    "rapidwright",
)


def digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def ancestor_pids(pid: int) -> set[int]:
    result = {pid}
    current = pid
    while current > 1:
        try:
            fields = (Path("/proc") / str(current) / "stat").read_text().split()
            parent = int(fields[3])
        except (FileNotFoundError, PermissionError, ProcessLookupError, IndexError, ValueError):
            break
        if parent in result or parent <= 0:
            break
        result.add(parent)
        current = parent
    return result


def scan(run_dir: Path, audit_index: int) -> dict[str, Any]:
    excluded_pids = ancestor_pids(os.getpid())
    exact_run_marker = os.fspath(run_dir.resolve())
    matching: list[dict[str, Any]] = []
    global_relevant: list[dict[str, Any]] = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit() or int(entry.name) in excluded_pids:
            continue
        try:
            raw = (entry / "cmdline").read_bytes()
            start_ticks = (entry / "stat").read_text().split()[21]
        except (FileNotFoundError, PermissionError, ProcessLookupError, IndexError):
            continue
        command = raw.replace(b"\0", b" ").decode("utf-8", "replace").strip()
        if not command:
            continue
        public = {
            "pid": int(entry.name),
            "start_ticks": start_ticks,
            "command_sha256": hashlib.sha256(raw).hexdigest(),
            "executable": Path(command.split()[0]).name,
        }
        if exact_run_marker in command:
            matching.append(public)
        if any(marker.lower() in command.lower() for marker in GLOBAL_EXECUTABLE_MARKERS):
            global_relevant.append(public)
    core = {
        "audit_index": audit_index,
        "exact_run_live_count": len(matching),
        "global_relevant_live_count": len(global_relevant),
        "exact_run_live": matching,
        "global_relevant_live": global_relevant,
        "accepted": not matching and not global_relevant,
    }
    return core | {"audit_sha256": digest(core)}


def main() -> int:
    parser = argparse.ArgumentParser(allow_abbrev=False)
    parser.add_argument("--run", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--interval-seconds", type=float, default=2.0)
    args = parser.parse_args()
    aggregate = args.run / "e0-aggregate.json"
    if not aggregate.is_file():
        raise SystemExit("e0_aggregate_missing")
    audits = [scan(args.run, 1)]
    time.sleep(max(0.0, args.interval_seconds))
    audits.append(scan(args.run, 2))
    core = {
        "schema_version": SCHEMA,
        "aggregate_sha256": hashlib.sha256(aggregate.read_bytes()).hexdigest(),
        "audits": audits,
        "accepted": all(row["accepted"] for row in audits),
    }
    result = core | {"summary_sha256": digest(core)}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, sort_keys=True, indent=2) + "\n")
    print(json.dumps({"accepted": result["accepted"], "counts": [[row["exact_run_live_count"], row["global_relevant_live_count"]] for row in audits]}))
    return 0 if result["accepted"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
