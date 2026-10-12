#!/usr/bin/env python3
"""Historical WP-014I evidence helpers retained for import compatibility.

The original report-building command requires its external historical bundle.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess
from typing import Any

REPO = Path(__file__).resolve().parents[1]


def read(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text())
    if not isinstance(value, dict):
        raise ValueError("json_object_required:" + str(path))
    return value


def file_hash(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def git(*args: str) -> str:
    return subprocess.check_output(["git", *args], cwd=REPO, text=True).strip()


def _synthetic_admissions(run: Path) -> dict[str, Any]:
    summary = read(run / "synthetic-timing-recon-summary.json")
    original_v1 = run / "v1/aggregate.json"
    continuations = sorted(run.glob("v1-continuation-*/aggregate.json"))
    aggregate_path = continuations[-1] if continuations else original_v1
    aggregate = read(aggregate_path)
    functional = {
        row["case"]["root_id"]: row["terminal"] for row in aggregate["results"]
    }
    records = {}
    for candidate_id in summary["cohort"]:
        final = run / "final" / candidate_id / "final-result.json"
        if not final.is_file():
            records[candidate_id] = {"admitted": False, "reason": "FINAL_RECORD_MISSING"}
            continue
        row = read(final)
        certificate = row.get("synthetic_timing_certificate")
        records[candidate_id] = {
            "admitted": (
                row.get("terminal") == "SYNTHETIC_V0_SCORE_READY"
                and functional.get(candidate_id) == "MULTISEED_FUNCTIONAL_PASS"
            ),
            "v0_terminal": row.get("terminal"),
            "v1_terminal": functional.get(candidate_id),
            "final_dcp_sha256": row.get("final_dcp_sha256"),
            "certificate": certificate,
            "certificate_sha256": digest(certificate) if certificate else None,
            "period_ns": row.get("period_ns"),
        }
    return {
        "summary_sha256": summary["summary_sha256"],
        "classification": summary["classification"],
        "routing_attempts": summary["routing_attempts"],
        "route_terminals": summary["route_terminals"],
        "v1_terminal_counts": aggregate["terminal_counts"],
        "v1_evidence_path": str(aggregate_path),
        "v1_evidence_sha256": aggregate["aggregate_sha256"],
        "superseded_v1_sha256": (
            read(original_v1)["aggregate_sha256"]
            if continuations and original_v1.is_file() else None
        ),
        "records": records,
        "admitted_ids": sorted(key for key, row in records.items() if row["admitted"]),
        "provider_or_llm_calls": summary["provider_or_llm_calls"],
        "optimization_calls": summary["optimization_calls"],
        "holdout_optimization_calls": summary["holdout_optimization_calls"],
        "owned_process_survivors": summary["owned_process_survivors"],
    }


if __name__ == "__main__":
    raise SystemExit("Use this module as evidence helpers; the historical report CLI requires its external bundle.")
