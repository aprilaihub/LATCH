#!/usr/bin/env python3
"""Verify and summarize the eight selected offline WP-002A mock runs."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sqlite3
import sys
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from optimizer_pipeline.core.store import SQLiteRunStore  # noqa: E402
from optimizer_pipeline.dof.evidence import (  # noqa: E402
    verify_registered_evidence,
)
from optimizer_pipeline.dof.spec import load_experiment_spec  # noqa: E402


SCHEMA_VERSION = "research-flow.wp002a-mock-summary.v1"
CONFIG_ROOT = REPO_ROOT / "configs" / "experiments" / "dof_contract"
EXPECTED_ENDPOINTS = {
    "01_l0_known": "VALID_COMPLETION",
    "02_l1_known": "VALID_COMPLETION",
    "03_l2_legal_known": "VALID_COMPLETION",
    "04_l2_invalid_raw": "INVALID_POLICY_OUTPUT",
    "05_l3_legal_plan": "VALID_COMPLETION",
    "06_l3_forbidden_flow": "CAPABILITY_ACTION_REJECTION",
    "07_l2_forced_unknown_masked": "MASK_VIOLATION",
    "08_l4_reference_pending": "L4_REFERENCE_PENDING",
}


def _json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _jsonl(path: Path) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def summarize(root: Path) -> dict[str, Any]:
    root = root.resolve()
    runs: list[dict[str, Any]] = []
    for name, expected_endpoint in EXPECTED_ENDPOINTS.items():
        run_dir = root / name
        db_path = run_dir / "run.db"
        spec = load_experiment_spec(CONFIG_ROOT / f"{name}.json")
        result = _json(run_dir / "evidence" / "result_summary.json")
        trace = _jsonl(run_dir / "evidence" / "decision_events.jsonl")

        with sqlite3.connect(db_path) as connection:
            quick_check = connection.execute("PRAGMA quick_check").fetchone()[0]
            foreign_key_errors = len(
                connection.execute("PRAGMA foreign_key_check").fetchall()
            )
        with SQLiteRunStore(db_path, migrate=False) as store:
            verification = verify_registered_evidence(
                store,
                run_dir=run_dir,
                expected_run_id=spec.run_id,
                expected_spec_hash=spec.spec_hash,
            )
            final_event = store.run_events()[-1]
            canonical_counts = {
                "actions": len(store.actions()),
                "proposals": len(store.proposals()),
                "attempts": len(store.attempts()),
                "outcomes": len(store.outcomes()),
            }

        assert quick_check == "ok", name
        assert foreign_key_errors == 0, name
        assert verification.ok, (name, verification.errors)
        assert final_event.status.value == "FINALIZED", name
        assert result["endpoint"] == expected_endpoint, name
        assert result["canonical_integrity_errors"] == [], name
        assert result["canonical_schema_version"] == 2, name
        assert result["runner_version"] == "dof.mock-runner.v2", name
        assert result["real_tools_started"] is False, name
        assert result["external_model_requests"] == 0, name
        assert result["fallback_used"] is False, name

        model_contexts = [
            row for row in trace if row.get("record_type") == "MODEL_CONTEXT"
        ]
        runs.append(
            {
                "name": name,
                "run_id": spec.run_id,
                "spec_hash": spec.spec_hash,
                "endpoint": result["endpoint"],
                "accepted_steps": result["accepted_steps"],
                "event_counts": result["budget"]["used"],
                "canonical_counts": canonical_counts,
                "quick_check": quick_check,
                "foreign_key_errors": foreign_key_errors,
                "evidence_verification": "PASS",
                "final_event": final_event.status.value,
                "context_schema_version": (
                    model_contexts[0]["context_schema_version"]
                    if model_contexts
                    else "NOT_RUN"
                ),
            }
        )

    forced_dir = root / "07_l2_forced_unknown_masked" / "evidence"
    forced_spec = load_experiment_spec(
        CONFIG_ROOT / "07_l2_forced_unknown_masked.json"
    )
    forced_trace = _jsonl(forced_dir / "decision_events.jsonl")
    forced_context = next(
        row["serialized_context"]
        for row in forced_trace
        if row.get("record_type") == "MODEL_CONTEXT"
    )
    forced_manifest = _json(forced_dir / "mask_manifest.json")
    assert forced_manifest == forced_spec.mask.to_manifest_dict()
    forbidden_context_values = (
        forced_spec.mask.version,
        forced_spec.mask.hash,
        *forced_spec.mask.hidden_macro_families,
        *forced_spec.mask.forbidden_sentinels,
    )
    assert not any(value.lower() in forced_context.lower() for value in forbidden_context_values)
    decoded_context = json.loads(forced_context)
    assert "mask_version" not in decoded_context["condition"]
    assert "macro.retime" not in decoded_context["legal_actions"]

    summary = {
        "schema_version": SCHEMA_VERSION,
        "work_package": "WP-002A",
        "execution_mode": "mock_only",
        "canonical_schema_version": 2,
        "runner_version": "dof.mock-runner.v2",
        "context_schema_version": "dof_decision_context.v2",
        "run_count": len(runs),
        "all_sqlite_integrity_checks": "PASS",
        "all_foreign_key_checks": "PASS",
        "all_evidence_verifications": "PASS",
        "all_final_events": "FINALIZED",
        "forced_unknown_context_mask_metadata_absent": True,
        "forced_unknown_full_mask_manifest_present": True,
        "execution_boundary": {
            "real_dcp_attempts": 0,
            "vivado_starts": 0,
            "rapidwright_starts": 0,
            "mcp_starts": 0,
            "external_model_requests": 0,
            "l4_execution": False,
            "schema_migration": False,
        },
        "runs": runs,
    }
    assert summary["run_count"] == 8
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Verify and summarize selected WP-002A mock evidence."
    )
    parser.add_argument("run_root", type=Path)
    parser.add_argument(
        "--output",
        type=Path,
        help="Optional JSON output path; parent directory must already exist.",
    )
    args = parser.parse_args()
    result = summarize(args.run_root)
    rendered = json.dumps(result, indent=2, sort_keys=True) + "\n"
    if args.output is not None:
        args.output.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
