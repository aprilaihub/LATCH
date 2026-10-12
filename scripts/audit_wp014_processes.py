#!/usr/bin/env python3
"""Perform two public-safe zero-live process audits for a WP-014 phase."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys
import time


REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from scripts.audit_wp013_e0_processes import digest, scan  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(allow_abbrev=False)
    parser.add_argument("--run", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--interval-seconds", type=float, default=10.0)
    args = parser.parse_args()
    aggregate = args.run / "phase-aggregate.json"
    if not aggregate.is_file():
        raise SystemExit("phase_aggregate_missing")
    audits = [scan(args.run, 1)]
    time.sleep(max(0.0, args.interval_seconds))
    audits.append(scan(args.run, 2))
    core = {
        "schema_version": "dof.wp014-process-audit.v1",
        "aggregate_sha256": hashlib.sha256(aggregate.read_bytes()).hexdigest(),
        "audits": audits, "accepted": all(row["accepted"] for row in audits),
    }
    value = core | {"summary_sha256": digest(core)}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(value, sort_keys=True, indent=2) + "\n")
    print(json.dumps({"accepted": value["accepted"], "counts": [[row["exact_run_live_count"], row["global_relevant_live_count"]] for row in audits]}))
    return 0 if value["accepted"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
