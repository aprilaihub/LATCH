#!/usr/bin/env python3
"""Run two exact, public-safe zero-live audits for a WP-012-01 queue."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from pathlib import Path
from typing import Any


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from optimizer_pipeline.dof.process_registry import (
    audit_attempt_path_supervisors,
    audit_registry,
)


def _read(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("controller ledger must be a JSON object")
    return value


def _hash(value: Any) -> str:
    raw = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _audit(ledger: dict[str, Any], index: int) -> dict[str, Any]:
    registry_rows: list[dict[str, Any]] = []
    path_rows: list[dict[str, Any]] = []
    for attempt in ledger.get("attempts") or []:
        attempt_dir = Path(attempt["output_directory"])
        registry_dir = attempt_dir / "process-ownership"
        job_id = str(attempt["job_id"])
        attempt_id = str(attempt["attempt_id"])
        registry_rows.append(audit_registry(registry_dir, job_id=job_id, attempt_id=attempt_id))
        path_rows.append(
            audit_attempt_path_supervisors(
                attempt_dir, registry_dir, job_id=job_id, attempt_id=attempt_id
            )
        )
    core = {
        "audit_index": index,
        "attempts_scanned": len(registry_rows),
        "registry_record_count": sum(row["record_count"] for row in registry_rows),
        "registry_live_count": sum(row["live_record_count"] for row in registry_rows),
        "path_live_supervisor_count": sum(row["live_supervisor_count"] for row in path_rows),
        "registry_error_count": sum(len(row["errors"]) for row in registry_rows),
        "path_error_count": sum(len(row["errors"]) for row in path_rows),
        "all_registries_quiescent": all(row["quiescent"] for row in registry_rows),
        "all_attempt_paths_quiescent": all(row["quiescent"] for row in path_rows),
        "all_attempt_paths_registry_complete": all(row["registry_complete"] for row in path_rows),
    }
    core["audit_sha256"] = _hash(core)
    return core


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--controller-ledger", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--interval-seconds", type=float, default=1.0)
    args = parser.parse_args()
    ledger = _read(args.controller_ledger)
    audits = [_audit(ledger, 1)]
    time.sleep(max(0.0, args.interval_seconds))
    audits.append(_audit(ledger, 2))
    core = {
        "schema_version": "research-flow.wp012-01-process-audits.v1",
        "controller_ledger_sha256": hashlib.sha256(args.controller_ledger.read_bytes()).hexdigest(),
        "audits": audits,
        "accepted": all(
            row["attempts_scanned"] == 30
            and row["registry_live_count"] == 0
            and row["path_live_supervisor_count"] == 0
            and row["all_registries_quiescent"]
            and row["all_attempt_paths_quiescent"]
            and row["all_attempt_paths_registry_complete"]
            for row in audits
        ),
    }
    core["summary_sha256"] = _hash(core)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(core, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    if not core["accepted"]:
        raise SystemExit("WP-012-01 process audit failed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
