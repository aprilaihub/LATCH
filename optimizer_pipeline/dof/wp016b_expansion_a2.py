"""Fresh-identity manifest and exact metadata join for WP-016B Expansion A2."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping, Sequence

from .wp016 import sha256
from .wp016_canary import file_sha256, read_json
from .wp016b_expansion import (
    ANALYSIS_CONTRACT as ATTEMPT1_ANALYSIS_CONTRACT,
    MANIFEST as ATTEMPT1_MANIFEST,
    SCHEDULE as ATTEMPT1_SCHEDULE,
    SOURCE_BUNDLE, SOURCE_LEDGER, write_json,
)


ROOT = Path(__file__).resolve().parents[2]
CONFIG_ROOT = ROOT / "configs/wp016b-expansion-a2"
AUTHORITY = CONFIG_ROOT / "authority/WP016B_EXPANSION_A2_DIRECT_AUTHORITY.json"
ATTEMPT1_ACCEPTANCE = CONFIG_ROOT / "authority/WP016B_EXPANSION_ATTEMPT1_ACCEPTANCE.json"
MANIFEST = CONFIG_ROOT / "expansion96-attempt2-manifest.json"
SCHEDULE = CONFIG_ROOT / "expansion96-attempt2-schedule.json"
ENVIRONMENT_CONTRACT = CONFIG_ROOT / "environment-contract.json"
ORCHESTRATION_REPLAY_RECEIPT = CONFIG_ROOT / "full-orchestrator-replay-receipt.json"
PREFLIGHT_RECEIPT = CONFIG_ROOT / "preflight-receipt.json"
AUTHORITY_LEDGER = CONFIG_ROOT / "AUTHORITY_LEDGER.jsonl"
DECISION_LEDGER = CONFIG_ROOT / "DECISION_AND_DEVIATION_LEDGER.jsonl"
ATTEMPT_LEDGER = CONFIG_ROOT / "ATTEMPT_LEDGER.json"

ATTEMPT = 2
ATTEMPT_LABEL = "WP016B_EXPANSION_ATTEMPT_2"
ATTEMPT1_MANIFEST_SHA256 = "6e443ea88c286e5d5258ff390c7b5e7aa3d02e84d76a686ce463792bef57c689"
ATTEMPT1_SCHEDULE_SHA256 = "6026f7697f2cb3b1a4c60ab81514f9a6c9db219e16345fdd0bf00103eb706cf6"
ANALYSIS_CONTRACT_SHA256 = "19d23e023250f3aff588919b7abb8ffb556eaeec1317fc4d97c6500f560113a3"
SOURCE_LEDGER_SHA256 = "9b05553987b92c57f53380411785e4357d8146a8b3d207e97d46dd9896642004"
SOURCE_BUNDLE_SHA256 = "1468107ce3aa5f26470b1c361592ec0ebc1b7d50038f0ddd1b4a9f96afd6e469"
ATTEMPT1_PACKAGE_SHA256 = "c861e7226e7e4ba53291b29bd85255da98f90f35f17a2035725ce92bc7b45cc6"
A2_BUNDLE_SHA256 = "e58a7470245f8501169d472d32284de9891d3cbf1423090ca85c51060d884ba6"

IDENTITY_KEYS = (
    "configuration_id", "cell_id", "session_id", "run_id", "log_id", "output_id",
)
SCIENTIFIC_METADATA_KEYS = (
    "root_id", "root_stratum", "model_system", "capability", "arm", "repeat",
    "source", "source_payload_sha256", "block_position", "arm_position", "launch_rank",
)


def exact_authority() -> dict[str, Any]:
    authority = read_json(AUTHORITY)
    expected = {
        "schema_version": "research-flow.wp016b-expansion-a2-direct-authority.v1",
        "wp_id": "WP-016B", "phase": "EXPANSION", "attempt": 2,
        "new_scientific_cells": 96, "accepted_core_cells": 48,
        "combined_target_cells": 144, "maximum_provider_concurrency": 20,
        "maximum_provider_requests_per_attempt": 384,
        "cumulative_literal_usd_cap": 50.0, "backend_calls_authorised": 0,
        "retry_count": 0, "fallback_count": 0, "response_repair_count": 0,
        "fresh_attempt_required": True, "selective_resume_authorised": False,
        "engineering_self_repair_authorised": True,
        "attempt3_engineering_only_fallback_authorised": True,
        "maximum_total_expansion_live_attempts": 3,
        "scientific_definition_change_authorised": False,
        "critical_schema_change_authorised": False, "wp016c_authorised": False,
    }
    mismatches = {key: {"expected": value, "observed": authority.get(key)}
                  for key, value in expected.items() if authority.get(key) != value}
    if mismatches:
        raise RuntimeError("wp016b_expansion_a2_authority_drift:" + json.dumps(mismatches, sort_keys=True))
    accepted = read_json(ATTEMPT1_ACCEPTANCE)
    if (
        accepted.get("status") != "ABORTED_SHARED_INTEGRATION_DEFECT"
        or accepted.get("durably_finalised_scientific_cells") != 0
        or accepted.get("scientific_analysis_eligible") is not False
    ):
        raise RuntimeError("wp016b_expansion_attempt1_acceptance_drift")
    return authority | {"attempt1_acceptance": accepted}


def verify_attempt1_treatment_sources() -> None:
    expected = {
        ATTEMPT1_MANIFEST: ATTEMPT1_MANIFEST_SHA256,
        ATTEMPT1_SCHEDULE: ATTEMPT1_SCHEDULE_SHA256,
        ATTEMPT1_ANALYSIS_CONTRACT: ANALYSIS_CONTRACT_SHA256,
        SOURCE_LEDGER: SOURCE_LEDGER_SHA256,
        SOURCE_BUNDLE: SOURCE_BUNDLE_SHA256,
    }
    mismatch = {str(path): {"expected": digest, "observed": file_sha256(path)}
                for path, digest in expected.items() if file_sha256(path) != digest}
    if mismatch:
        raise RuntimeError("wp016b_expansion_attempt1_treatment_source_drift:" + json.dumps(mismatch, sort_keys=True))


def _fresh_id(kind: str, attempt1_id: str) -> str:
    return f"wp016bexp-a2-{kind}__" + sha256({
        "attempt_label": ATTEMPT_LABEL, "kind": kind, "attempt1_id": attempt1_id,
    })[:24]


def build_attempt2_manifest_and_schedule() -> tuple[dict[str, Any], dict[str, Any]]:
    exact_authority()
    verify_attempt1_treatment_sources()
    old_manifest, old_schedule = read_json(ATTEMPT1_MANIFEST), read_json(ATTEMPT1_SCHEDULE)
    old_cells = list(old_manifest["cells"])
    if len(old_cells) != 96 or len(old_schedule["blocks"]) != 32:
        raise RuntimeError("wp016b_expansion_attempt1_inventory_drift")
    block_ids = {
        block["block_id"]: _fresh_id("block", block["block_id"])
        for block in old_schedule["blocks"]
    }
    cells: list[dict[str, Any]] = []
    for old in old_cells:
        new = {
            **{key: old[key] for key in SCIENTIFIC_METADATA_KEYS},
            "attempt": ATTEMPT,
            "attempt1_block_id": old["block_id"],
            "attempt1_cell_id": old["cell_id"],
            "attempt1_configuration_id": old["configuration_id"],
            "block_id": block_ids[old["block_id"]],
        }
        for key in IDENTITY_KEYS:
            kind = key.removesuffix("_id")
            new[key] = _fresh_id(kind, str(old[key]))
        cells.append(new)
    by_attempt1_cell = {cell["attempt1_cell_id"]: cell for cell in cells}
    if len(by_attempt1_cell) != 96:
        raise RuntimeError("wp016b_expansion_a2_duplicate_attempt1_cell_identity")
    blocks = []
    for position, old_block in enumerate(old_schedule["blocks"], 1):
        block_cells = []
        for old_cell in old_block["cells"]:
            cell = by_attempt1_cell.get(old_cell["cell_id"])
            if cell is None:
                raise RuntimeError("wp016b_expansion_a2_schedule_manifest_missing_cell")
            block_cells.append(dict(cell))
        blocks.append({
            **{key: old_block[key] for key in (
                "root_id", "model_system", "capability", "repeat",
            )},
            "attempt": ATTEMPT,
            "attempt1_block_id": old_block["block_id"],
            "block_id": block_ids[old_block["block_id"]],
            "block_position": position,
            "cells": block_cells,
        })
    manifest_core = {
        "schema_version": "wp016b.expansion96-attempt2-scientific-manifest.v1",
        "wp_id": "WP-016B", "phase": "EXPANSION", "attempt": ATTEMPT,
        "scientific_analysis_eligible": True,
        "cell_count": 96, "block_count": 32,
        "attempt1_scientific_cells_reused": 0,
        "attempt1_provider_responses_reused": 0,
        "logical_treatment_source_manifest_sha256": ATTEMPT1_MANIFEST_SHA256,
        "logical_order_source_schedule_sha256": ATTEMPT1_SCHEDULE_SHA256,
        "analysis_contract_sha256": ANALYSIS_CONTRACT_SHA256,
        "identity_namespace": ATTEMPT_LABEL,
        "cells": cells,
    }
    schedule_core = {
        "schema_version": "wp016b.expansion96-attempt2-schedule.v1",
        "wp_id": "WP-016B", "phase": "EXPANSION", "attempt": ATTEMPT,
        "maximum_concurrency": 20,
        "logical_order_source_schedule_sha256": ATTEMPT1_SCHEDULE_SHA256,
        "metadata_source": "ATTEMPT2_MANIFEST_EXACT_CELL_ID_JOIN",
        "blocks": blocks,
    }
    return (
        manifest_core | {"manifest_sha256": sha256(manifest_core)},
        schedule_core | {"schedule_sha256": sha256(schedule_core)},
    )


def materialize_attempt2() -> None:
    manifest, schedule = build_attempt2_manifest_and_schedule()
    write_json(MANIFEST, manifest)
    write_json(SCHEDULE, schedule)


def joined_schedule_cells(
    manifest: Mapping[str, Any], schedule: Mapping[str, Any],
) -> list[tuple[int, Mapping[str, Any], dict[str, Any]]]:
    """Fail-closed exact join; launch order is never used to infer metadata."""
    manifest_rows = list(manifest.get("cells") or ())
    by_id: dict[str, Mapping[str, Any]] = {}
    for cell in manifest_rows:
        cell_id = str(cell.get("cell_id"))
        if not cell_id or cell_id in by_id:
            raise RuntimeError("wp016b_expansion_a2_duplicate_manifest_cell_id")
        by_id[cell_id] = cell
    if len(by_id) != 96:
        raise RuntimeError("wp016b_expansion_a2_manifest_requires_96_cells")
    joined = []
    observed: set[str] = set()
    for position, block in enumerate(schedule.get("blocks") or (), 1):
        for scheduled in block.get("cells") or ():
            cell_id = str(scheduled.get("cell_id"))
            if cell_id in observed:
                raise RuntimeError("wp016b_expansion_a2_duplicate_schedule_cell_id")
            canonical = by_id.get(cell_id)
            if canonical is None:
                raise RuntimeError("wp016b_expansion_a2_schedule_cell_not_in_manifest")
            for key in (*SCIENTIFIC_METADATA_KEYS, *IDENTITY_KEYS, "block_id", "attempt"):
                if scheduled.get(key) != canonical.get(key):
                    raise RuntimeError("wp016b_expansion_a2_schedule_manifest_metadata_mismatch:" + key)
            if block.get("block_id") != canonical.get("block_id"):
                raise RuntimeError("wp016b_expansion_a2_block_join_mismatch")
            observed.add(cell_id)
            joined.append((position, block, dict(canonical)))
    if observed != set(by_id):
        raise RuntimeError("wp016b_expansion_a2_schedule_manifest_cell_set_mismatch")
    joined.sort(key=lambda item: int(item[2]["launch_rank"]))
    if [int(item[2]["launch_rank"]) for item in joined] != list(range(1, 97)):
        raise RuntimeError("wp016b_expansion_a2_launch_rank_not_exact_permutation")
    return joined


__all__ = [
    "ANALYSIS_CONTRACT_SHA256", "ATTEMPT", "ATTEMPT1_ACCEPTANCE",
    "ATTEMPT1_MANIFEST_SHA256", "ATTEMPT1_PACKAGE_SHA256",
    "ATTEMPT1_SCHEDULE_SHA256", "ATTEMPT_LABEL", "ATTEMPT_LEDGER",
    "AUTHORITY", "AUTHORITY_LEDGER", "CONFIG_ROOT", "DECISION_LEDGER",
    "ENVIRONMENT_CONTRACT", "IDENTITY_KEYS", "MANIFEST",
    "ORCHESTRATION_REPLAY_RECEIPT", "PREFLIGHT_RECEIPT", "SCHEDULE",
    "SCIENTIFIC_METADATA_KEYS", "build_attempt2_manifest_and_schedule",
    "exact_authority", "joined_schedule_cells", "materialize_attempt2",
    "verify_attempt1_treatment_sources",
]
