#!/usr/bin/env python3
"""Generate the immutable WP-013A v2 overlays and E0-v4 plan."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys
from typing import Any


REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from optimizer_pipeline.dof.single_submission.contract_v2 import (  # noqa: E402
    M_ACTION_KEYS,
    M_HEAVY_CALLS,
    contract_for_level,
)
from optimizer_pipeline.dof.single_submission.specs import canonical_json_bytes  # noqa: E402


def digest(value: Any) -> str:
    raw = value if isinstance(value, bytes) else canonical_json_bytes(value)
    return hashlib.sha256(raw).hexdigest()


def file_digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def read(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text())
    if not isinstance(value, dict):
        raise ValueError(f"object_required:{path}")
    return value


def write(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, sort_keys=True, indent=2) + "\n")


def git(*args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=REPO, text=True, check=True, capture_output=True
    ).stdout.strip()


def freeze(out: Path) -> dict[str, str]:
    # Generated protocol files may already exist as untracked output during a
    # deterministic refresh.  Only tracked product changes invalidate the
    # source commit/tree binding.
    if git("status", "--short", "--untracked-files=no"):
        raise SystemExit("tracked_tree_must_be_clean_before_protocol_generation")
    source_commit = git("rev-parse", "HEAD")
    source_tree = git("rev-parse", "HEAD^{tree}")
    base_m_path = REPO / "configs/wp013/final-m-v1.json"
    base_l1_path = REPO / "configs/wp013/l1-heuristic-v1.json"
    erratum_path = REPO / "configs/wp013/macro-heavy-accounting-erratum-v1.json"
    base_m, base_l1, erratum = read(base_m_path), read(base_l1_path), read(erratum_path)

    observed = {
        "macro." + key: value["observed_calls_per_job"]
        for key, value in erratum["actions"].items()
    }
    reconciled = []
    for row in base_m["frozen_actions"]:
        key = row["action_key"]
        actual = observed[key]
        if actual != M_HEAVY_CALLS[key]:
            raise SystemExit("overlay_cost_not_bound_to_trace:" + key)
        reconciled.append(
            {
                "action_key": key,
                "flow_name": row["flow_name"],
                "v1_declared_heavy_calls": row["heavy_call_range"][0],
                "v2_declared_heavy_calls": actual,
                "evidence_jobs": erratum["actions"][row["flow_name"]]["jobs"],
                "evidence_total_calls": erratum["actions"][row["flow_name"]]["observed_total"],
                "authority": "immutable exact host-call start markers",
            }
        )
    trace_core = {
        "schema_version": "dof.wp013a-macro-trace-reconciliation.v1",
        "source_commit": source_commit,
        "panel_rerun": False,
        "base_manifest_sha256": file_digest(base_m_path),
        "erratum_sha256": file_digest(erratum_path),
        "actions": reconciled,
        "accepted": [row["action_key"] for row in reconciled] == list(M_ACTION_KEYS),
    }
    trace = trace_core | {"reconciliation_sha256": digest(trace_core)}
    trace_path = out / "macro-trace-reconciliation-v2.json"
    write(trace_path, trace)

    m_core = {
        "schema_version": "dof.wp013a-final-m-overlay.v2",
        "source_commit": source_commit,
        "source_tree": source_tree,
        "base_manifest_sha256": file_digest(base_m_path),
        "semantics_changed": False,
        "membership_changed": False,
        "panel_rerun": False,
        "canonical_candidate": {"language": "M", "action_key": "<one admitted action_key>"},
        "actions": [
            {
                "action_key": key,
                "flow_name": key.removeprefix("macro."),
                "heavy_call_range": [M_HEAVY_CALLS[key], M_HEAVY_CALLS[key]],
            }
            for key in M_ACTION_KEYS
        ],
        "trace_reconciliation_sha256": trace["reconciliation_sha256"],
        "accounting_failure": "ACCOUNTING_INTEGRITY_FAILURE when actual immutable starts exceed reservation",
    }
    m_overlay = m_core | {"overlay_sha256": digest(m_core)}
    m_path = out / "final-m-v2-overlay.json"
    write(m_path, m_overlay)

    l1_core = {
        "schema_version": "dof.wp013a-l1-overlay.v2",
        "source_commit": source_commit,
        "base_heuristic_file_sha256": file_digest(base_l1_path),
        "base_heuristic_sha256": base_l1["heuristic_sha256"],
        "kind": base_l1["kind"],
        "global_ranking": base_l1["global_ranking"],
        "selected_rule": base_l1["selected_rule"],
        "ranking_changed": False,
        "retrained": False,
        "m_v2_overlay_sha256": m_overlay["overlay_sha256"],
    }
    l1_overlay = l1_core | {"overlay_sha256": digest(l1_core)}
    l1_path = out / "l1-heuristic-v2-overlay.json"
    write(l1_path, l1_overlay)

    protocol_core = {
        "schema_version": "dof.wp013a-protocol-freeze.v2",
        "source_commit": source_commit,
        "source_tree": source_tree,
        "old_epoch_preserved": "dof.single-submission.protocol-freeze.v1",
        "contracts": {level: contract_for_level(level) for level in ("L2", "L3", "L4")},
        "contract_hashes": {level: digest(contract_for_level(level)) for level in ("L2", "L3", "L4")},
        "m_overlay_sha256": m_overlay["overlay_sha256"],
        "l1_overlay_sha256": l1_overlay["overlay_sha256"],
        "trace_reconciliation_sha256": trace["reconciliation_sha256"],
        "resource_envelope": {
            "implementation_heavy_calls": 64,
            "openrouter_usd": 1.0,
            "queue_wall_seconds": 28_800,
            "weekday_complete_job_concurrency": 8,
        },
        "primary_aggregate_eligible": False,
    }
    protocol = protocol_core | {"freeze_sha256": digest(protocol_core)}
    protocol_path = out / "protocol-freeze-v2.json"
    write(protocol_path, protocol)

    old_plan = read(REPO / "configs/wp013/e0-v3-plan.json")
    plan_core = {
        "schema_version": "dof.wp013a-e0-v4-plan.v1",
        "source_commit": source_commit,
        "roots": old_plan["roots"],
        "models": old_plan["models"],
        "protocol_freeze_path": str(protocol_path.relative_to(REPO)),
        "macro_manifest_path": str(m_path.relative_to(REPO)),
        "l1_heuristic_path": str(l1_path.relative_to(REPO)),
        "trace_reconciliation_path": str(trace_path.relative_to(REPO)),
        "guards": {
            "model_turns_per_session": 4,
            "read_only_queries_per_session": 8,
            "construction_feedback_rounds": 3,
            "valid_commits": 1,
            "planning_input_byte_ceiling": 32_768,
            "provider_retries": 0,
            "provider_fallbacks": 0,
            "openrouter_cost_cap_usd": 1.0,
            "implementation_heavy_call_cap": 64,
            "max_parallel_weekday": 8,
            "queue_wall_seconds": 28_800,
        },
        "matrix": {
            "roots": 2,
            "jobs": 16,
            "levels": ["L0", "L1", "L2", "L3", "L4"],
            "fresh_protocol_epoch": True,
        },
        "non_scope": {
            "macro_panel_rerun": False,
            "deterministic_pg_admission_rerun": False,
            "primary_matrix": False,
            "sealed_holdout_access": False,
        },
        "completion": {
            "status": "NEEDS_PRO_EVIDENCE_REVIEW",
            "next_actor": "GPT_PRO",
            "primary_matrix_auto_start": False,
        },
    }
    plan = plan_core | {"plan_sha256": digest(plan_core)}
    plan_path = out / "e0-v4-plan.json"
    write(plan_path, plan)
    return {
        "trace": str(trace_path),
        "m_overlay": str(m_path),
        "l1_overlay": str(l1_path),
        "protocol": str(protocol_path),
        "plan": str(plan_path),
        "protocol_freeze_sha256": protocol["freeze_sha256"],
    }


def main() -> int:
    parser = argparse.ArgumentParser(allow_abbrev=False)
    parser.add_argument("--out", type=Path, default=REPO / "configs/wp013a")
    args = parser.parse_args()
    print(json.dumps(freeze(args.out.resolve()), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
