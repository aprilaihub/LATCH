#!/usr/bin/env python3
"""Audit clean repository, development roots and processes at WP-014B return."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import time


REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from scripts.audit_wp013_e0_processes import digest, scan  # noqa: E402


def file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(allow_abbrev=False)
    parser.add_argument("--roots", type=Path, required=True)
    parser.add_argument("--process-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--interval-seconds", type=float, default=10.0)
    args = parser.parse_args()
    status = subprocess.run(
        ("git", "status", "--porcelain=v1"), cwd=REPO,
        check=True, capture_output=True, text=True,
    ).stdout
    head = subprocess.run(
        ("git", "rev-parse", "HEAD"), cwd=REPO,
        check=True, capture_output=True, text=True,
    ).stdout.strip()
    tree = subprocess.run(
        ("git", "rev-parse", "HEAD^{tree}"), cwd=REPO,
        check=True, capture_output=True, text=True,
    ).stdout.strip()
    roots = json.loads(args.roots.read_text())
    prefix = Path(roots["root_path_prefix"])
    root_rows = []
    for row in roots["roots"]:
        path = prefix / row["filename"]
        observed = file_hash(path)
        root_rows.append({
            "root_id": row["root_id"], "expected_sha256": row["sha256"],
            "observed_sha256": observed, "unchanged": observed == row["sha256"],
        })
    audits = [scan(args.process_root, 1)]
    time.sleep(max(0.0, args.interval_seconds))
    audits.append(scan(args.process_root, 2))
    core = {
        "schema_version": "dof.wp014b-handoff-integrity.v1",
        "repository": {"head_commit": head, "tree": tree, "clean": status == ""},
        "development_roots": root_rows,
        "all_development_roots_unchanged": all(row["unchanged"] for row in root_rows),
        "process_audits": audits,
        "two_zero_live_audits": all(row["accepted"] for row in audits),
    }
    core["accepted"] = bool(
        core["repository"]["clean"]
        and core["all_development_roots_unchanged"]
        and core["two_zero_live_audits"]
    )
    result = core | {"summary_sha256": digest(core)}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, sort_keys=True, indent=2) + "\n")
    print(json.dumps({
        "accepted": result["accepted"],
        "root_count": len(root_rows),
        "process_counts": [[row["exact_run_live_count"], row["global_relevant_live_count"]] for row in audits],
    }, sort_keys=True))
    return 0 if result["accepted"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
