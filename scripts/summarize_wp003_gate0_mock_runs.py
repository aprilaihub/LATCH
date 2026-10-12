#!/usr/bin/env python3
"""Verify the eight selected Gate-0 mock runs and observed Git provenance."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import sys
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from optimizer_pipeline.core.store import SQLiteRunStore  # noqa: E402
from optimizer_pipeline.dof.evidence import verify_registered_evidence  # noqa: E402
from optimizer_pipeline.dof.spec import load_experiment_spec  # noqa: E402


SCHEMA_VERSION = "research-flow.wp003-gate0-mock-summary.v1"
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
_SHA1_RE = re.compile(r"^[0-9a-f]{40}$")
_EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()


def _json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise TypeError(f"expected JSON object: {path}")
    return value


def summarize(root: Path, *, expected_head: str) -> dict[str, Any]:
    if not _SHA1_RE.fullmatch(expected_head):
        raise ValueError("expected_head must be a full lowercase Git SHA-1")
    root = root.resolve()
    runs: list[dict[str, Any]] = []
    for name, expected_endpoint in EXPECTED_ENDPOINTS.items():
        run_dir = root / name
        db_path = run_dir / "run.db"
        spec = load_experiment_spec(CONFIG_ROOT / f"{name}.json")
        result = _json(run_dir / "evidence" / "result_summary.json")
        experiment = _json(run_dir / "evidence" / "experiment_manifest.json")
        repository = _json(run_dir / "evidence" / "repository_provenance.json")
        tool_environment = _json(
            run_dir / "evidence" / "tool_environment_manifest.json"
        )

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

        assert quick_check == "ok", name
        assert foreign_key_errors == 0, name
        assert verification.ok, (name, verification.errors)
        assert final_event.status.value == "FINALIZED", name
        assert result["endpoint"] == expected_endpoint, name
        assert result["runner_version"] == "dof.mock-runner.v3", name
        assert repository["head_commit"] == expected_head, name
        assert repository["tracked_tree_dirty"] is False, name
        assert repository["untracked_summary"]["entry_count"] == 0, name
        assert repository["tracked_status_sha256"] == _EMPTY_SHA256, name
        assert set(repository["tracked_status_summary"].values()) == {0}, name
        assert "git_commit" not in experiment["spec"]["provenance"], name
        assert "dirty_tree" not in experiment["spec"]["provenance"], name
        assert "git_commit" not in tool_environment, name
        assert "dirty_tree" not in tool_environment, name
        assert result["real_tools_started"] is False, name
        assert result["external_model_requests"] == 0, name
        runs.append(
            {
                "name": name,
                "run_id": spec.run_id,
                "spec_hash": spec.spec_hash,
                "endpoint": result["endpoint"],
                "observed_head": repository["head_commit"],
                "observed_tree": repository["head_tree"],
                "ref_state": repository["ref_state"],
                "tracked_tree_dirty": repository["tracked_tree_dirty"],
                "repository_provenance_sha256": result[
                    "repository_provenance"
                ]["sha256"],
                "quick_check": quick_check,
                "foreign_key_errors": foreign_key_errors,
                "evidence_verification": "PASS",
                "final_event": final_event.status.value,
            }
        )

    return {
        "schema_version": SCHEMA_VERSION,
        "work_package": "WP-003",
        "gate": "GATE_0_RUNTIME_OBSERVED_REPOSITORY_PROVENANCE",
        "execution_mode": "mock_only",
        "expected_head": expected_head,
        "run_count": len(runs),
        "all_observed_heads_match": True,
        "all_tracked_trees_clean": True,
        "all_untracked_trees_empty": True,
        "all_evidence_verifications": "PASS",
        "canonical_schema_version": 2,
        "execution_boundary": {
            "real_dcp_attempts": 0,
            "vivado_starts": 0,
            "rapidwright_starts": 0,
            "mcp_starts": 0,
            "external_model_requests": 0,
            "schema_migration": False,
            "l4_execution": False,
        },
        "runs": runs,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Verify WP-003 Gate-0 selected mock provenance."
    )
    parser.add_argument("run_root", type=Path)
    parser.add_argument("--expected-head", required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = summarize(args.run_root, expected_head=args.expected_head)
    rendered = json.dumps(result, indent=2, sort_keys=True) + "\n"
    if args.output is not None:
        args.output.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
