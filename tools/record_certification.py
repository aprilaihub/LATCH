#!/usr/bin/env python3
"""Append an external DCP certification report to a canonical managed run."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from optimizer_pipeline.run.certification import record_certification


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_db", type=Path)
    parser.add_argument("report", type=Path)
    args = parser.parse_args()
    report = json.loads(args.report.read_text())
    fact = record_certification(args.run_db, report, evidence_uri=str(args.report.resolve()))
    print(f"{fact.subject_id}: {fact.level.value}={fact.status.value}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
