#!/usr/bin/env python3
"""Perform two public-safe zero-live process audits for WP-014E."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import time
from typing import Any


MARKERS = (
    "run_wp014e",
    "wp014e-runs",
    "validate_dcps.py",
    "dcp_validation_",
    "run_wp014c_scientific.py",
    "run_wp014c.py",
    "rapidwright",
    "vivado",
)

# Site-wide license daemons are pre-existing infrastructure, not experiment
# descendants. Their installation path can contain the word ``Vivado`` and
# must not be counted as a surviving implementation process.
INFRASTRUCTURE_DAEMONS = {"lmgrd"}


def digest(value: Any) -> str:
    encoded = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def matched_markers(executable: str, command: str) -> list[str]:
    if executable in INFRASTRUCTURE_DAEMONS:
        return []
    lowered = command.lower()
    return sorted({marker for marker in MARKERS if marker in lowered})


def scan(index: int) -> dict[str, Any]:
    live = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit() or int(entry.name) in {1, os.getpid(), os.getppid()}:
            continue
        try:
            executable = (entry / "comm").read_text().strip()
            command = (entry / "cmdline").read_bytes().replace(b"\0", b" ").decode(
                errors="replace"
            )
        except (FileNotFoundError, PermissionError, ProcessLookupError):
            continue
        matched = matched_markers(executable, command)
        if matched:
            live.append({"pid": int(entry.name), "markers": matched})
    return {
        "index": index,
        "live_count": len(live),
        "live_processes": sorted(live, key=lambda row: row["pid"]),
        "accepted": not live,
    }


def main() -> int:
    parser = argparse.ArgumentParser(allow_abbrev=False)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--interval-seconds", type=float, default=10.0)
    args = parser.parse_args()
    audits = [scan(1)]
    time.sleep(max(0.0, args.interval_seconds))
    audits.append(scan(2))
    core = {
        "schema_version": "dof.wp014e-process-audit.v1",
        "audits": audits,
        "accepted": all(row["accepted"] for row in audits),
    }
    result = core | {"audit_sha256": digest(core)}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, sort_keys=True, indent=2) + "\n")
    print(json.dumps({"accepted": result["accepted"], "counts": [row["live_count"] for row in audits]}))
    return 0 if result["accepted"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
