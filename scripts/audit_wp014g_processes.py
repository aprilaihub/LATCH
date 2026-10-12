#!/usr/bin/env python3
"""Record two public-safe zero-survivor scans for WP-014G workers."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import time


EXACT_EXECUTABLES = {"vivado", "xsim", "xelab", "xvlog"}
WORKER_MARKERS = (
    "rapidwright",
    "validate_dcps.py",
    "run_wp014g_validation.py",
    "run_wp014g_probes.py",
)


def ancestors() -> set[int]:
    result = {os.getpid()}
    current = os.getppid()
    while current >= 1 and current not in result:
        result.add(current)
        if current == 1:
            break
        try:
            current = int(Path(f"/proc/{current}/stat").read_text().split()[3])
        except (OSError, ValueError, IndexError):
            break
    return result


def scan() -> dict:
    excluded = ancestors()
    matches = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit() or int(entry.name) in excluded:
            continue
        try:
            fields = (entry / "cmdline").read_bytes().split(b"\0")
            argv = [item.decode(errors="replace") for item in fields if item]
        except OSError:
            continue
        if not argv:
            continue
        executable = Path(argv[0]).name.lower()
        joined = " ".join(argv).lower()
        marker = None
        if executable in EXACT_EXECUTABLES:
            marker = executable
        elif executable == "java" and "rapidwright" in joined:
            marker = "rapidwright-java"
        else:
            marker = next((item for item in WORKER_MARKERS if item in joined), None)
        if marker:
            matches.append({"pid": int(entry.name), "class": marker})
    return {
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "survivor_count": len(matches),
        "survivors": sorted(matches, key=lambda row: row["pid"]),
        "lmgrd_excluded_by_design": True,
    }


def main() -> int:
    parser = argparse.ArgumentParser(allow_abbrev=False)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--interval-seconds", type=float, default=2.0)
    args = parser.parse_args()
    scans = [scan()]
    time.sleep(max(0.0, min(args.interval_seconds, 10.0)))
    scans.append(scan())
    value = {
        "schema_version": "dof.wp014g-process-audit.v1",
        "scans": scans,
        "zero_survivors_twice": all(row["survivor_count"] == 0 for row in scans),
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")
    return 0 if value["zero_survivors_twice"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
