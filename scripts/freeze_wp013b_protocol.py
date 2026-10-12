#!/usr/bin/env python3
"""Freeze the WP-013B v3 representation and development E0-v5 plan."""

from __future__ import annotations

import argparse
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
from typing import Any

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path: sys.path.insert(0, str(REPO))

from optimizer_pipeline.dof.single_submission.contract_v3 import (  # noqa: E402
    MAX_INPUT_BYTES, M_ACTION_KEYS, contract_for_level,
)
from optimizer_pipeline.dof.single_submission.specs import canonical_json_bytes  # noqa: E402

CHALLENGE = ("macro.phys_opt", "macro.retime", "macro.net_swap", "macro.congestion_spread")
MASKED = tuple(key for key in M_ACTION_KEYS if key not in CHALLENGE)


def digest(value: Any) -> str:
    raw = value if isinstance(value, bytes) else canonical_json_bytes(value)
    return hashlib.sha256(raw).hexdigest()


def file_digest(path: Path) -> str:
    return digest(path.read_bytes())


def git(*args: str) -> str:
    return subprocess.run(["git", *args], cwd=REPO, check=True, text=True, capture_output=True).stdout.strip()


def write(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, sort_keys=True, indent=2) + "\n")


def freeze(out: Path) -> dict[str, Any]:
    if not os.environ.get("LATCH_WP013B_LOGICNETS_DCP"):
        raise RuntimeError("LATCH_WP013B_LOGICNETS_DCP must identify the external frozen checkpoint")
    logicnets_dcp = Path(os.environ["LATCH_WP013B_LOGICNETS_DCP"]).resolve(strict=True)
    if not logicnets_dcp.is_file():
        raise RuntimeError("logicnets_checkpoint_must_be_a_file")
    if git("status", "--short", "--untracked-files=no"):
        raise SystemExit("tracked_tree_must_be_clean_before_protocol_generation")
    source_commit, source_tree = git("rev-parse", "HEAD"), git("rev-parse", "HEAD^{tree}")
    base_l1_path = REPO / "configs/wp013/l1-heuristic-v1.json"
    base_l1 = json.loads(base_l1_path.read_text())
    l1_core = {
        "schema_version": "dof.wp013b-l1-public-overlay.v3", "source_commit": source_commit,
        "base_heuristic_file_sha256": file_digest(base_l1_path),
        "base_heuristic_sha256": base_l1["heuristic_sha256"],
        "public_name": "Fixed Global Macro Prior", "kind": base_l1["kind"],
        "global_ranking": base_l1["global_ranking"], "selected_rule": "first admitted Macro in unchanged global_ranking",
        "ranking_changed": False, "weights_changed": False, "retrained": False,
    }
    l1 = l1_core | {"overlay_sha256": digest(l1_core)}
    l1_path = out / "l1-fixed-global-macro-prior-v3.json"; write(l1_path, l1)

    challenge_core = {
        "schema_version": "dof.wp013b-development-macro-challenge.v1",
        "condition": "development_macro_challenge_v1", "available_action_keys": list(CHALLENGE),
        "masked_action_keys": list(MASKED), "mask_applies_to": ["L1", "L2", "L3.CALL_MACRO", "L4.CALL_MACRO"],
        "model_disclosure": "only available cards are shown; removal rationale and history are not disclosed",
        "l1_selected_action_key": "macro.phys_opt", "scientific_role": "development-only usability challenge",
    }
    challenge = challenge_core | {"challenge_sha256": digest(challenge_core)}
    challenge_path = out / "development-macro-challenge-v1.json"; write(challenge_path, challenge)

    contracts = {level: contract_for_level(level) for level in ("L2", "L3", "L4")}
    challenge_contracts = {level: contract_for_level(level, admitted_action_keys=CHALLENGE) for level in ("L2", "L3", "L4")}
    protocol_core = {
        "schema_version": "dof.wp013b-protocol-freeze.v3", "source_commit": source_commit,
        "source_tree": source_tree, "accepted_execution_semantics_commit": "e1bc036c02213e59cdcf1141212f7d2c267ee936",
        "previous_epoch_preserved": "dof.wp013a-protocol-freeze.v2",
        "common_contracts": contracts, "common_contract_hashes": {level: digest(value) for level, value in contracts.items()},
        "challenge_contracts": challenge_contracts, "challenge_contract_hashes": {level: digest(value) for level, value in challenge_contracts.items()},
        "l1_overlay_sha256": l1["overlay_sha256"], "challenge_sha256": challenge["challenge_sha256"],
        "resource_envelope": {"implementation_heavy_calls": 64, "openrouter_usd": 1.25, "queue_wall_seconds": 28_800, "weekday_complete_job_concurrency": 8},
        "context_envelope": {"canonical_input_bytes": MAX_INPUT_BYTES, "query_items": 8, "query_top_k": 16, "individual_query_bytes": 4096, "aggregate_query_bytes": 24576, "aggregate_query_rows": 64},
        "primary_aggregate_eligible": False,
    }
    protocol = protocol_core | {"freeze_sha256": digest(protocol_core)}
    protocol_path = out / "protocol-freeze-v3.json"; write(protocol_path, protocol)

    old = json.loads((REPO / "configs/wp013a/e0-v4-plan.json").read_text())
    vex = deepcopy(old["roots"][0])
    vex["regime"] = "development_full_m8"; vex["admitted_action_keys"] = list(M_ACTION_KEYS)
    logic = {
        "root_id": "logicnets_jscl", "root_path": str(logicnets_dcp),
        "sha256": "a359141a00ce58255650645732cba0033c6255290ce289b4818f96f2e4fecc02",
        "target_period_ns": 2.5, "target_period_evidence": "frozen FPL26 development scorer configuration",
        "regime": "development_macro_challenge_v1", "admitted_action_keys": list(CHALLENGE),
    }
    plan_core = {
        "schema_version": "dof.wp013b-e0-v5-plan.v1", "source_commit": source_commit,
        "roots": [vex, logic], "models": old["models"],
        "protocol_freeze_path": str(protocol_path.relative_to(REPO)),
        "macro_manifest_path": "configs/wp013a/final-m-v2-overlay.json",
        "l1_heuristic_path": str(l1_path.relative_to(REPO)),
        "trace_reconciliation_path": "configs/wp013a/macro-trace-reconciliation-v2.json",
        "challenge_path": str(challenge_path.relative_to(REPO)),
        "guards": {"model_turns_per_session": 4, "read_only_queries_per_session": 8, "query_top_k_max": 16, "individual_query_result_max_bytes": 4096, "aggregate_query_result_max_bytes": 24576, "aggregate_query_result_max_rows": 64, "construction_feedback_rounds": 3, "valid_commits": 1, "planning_input_byte_ceiling": 65536, "provider_retries": 0, "provider_fallbacks": 0, "openrouter_cost_cap_usd": 1.25, "implementation_heavy_call_cap": 64, "max_parallel_weekday": 8, "queue_wall_seconds": 28800},
        "matrix": {"roots": 2, "jobs": 16, "levels": ["L0", "L1", "L2", "L3", "L4"], "fresh_protocol_epoch": True},
        "non_scope": {"macro_panel_rerun": False, "deterministic_pg_admission_rerun": False, "primary_matrix": False, "sealed_holdout_access": False},
        "completion": {"status": "NEEDS_PRO_EVIDENCE_REVIEW", "next_actor": "GPT_PRO", "primary_matrix_auto_start": False},
    }
    plan = plan_core | {"plan_sha256": digest(plan_core)}
    plan_path = out / "e0-v5-plan.json"; write(plan_path, plan)
    return {"protocol": str(protocol_path), "l1": str(l1_path), "challenge": str(challenge_path), "plan": str(plan_path), "protocol_freeze_sha256": protocol["freeze_sha256"]}


def main() -> int:
    parser = argparse.ArgumentParser(allow_abbrev=False); parser.add_argument("--out", type=Path, default=REPO / "configs/wp013b")
    args = parser.parse_args(); print(json.dumps(freeze(args.out.resolve()), sort_keys=True)); return 0


if __name__ == "__main__": raise SystemExit(main())
