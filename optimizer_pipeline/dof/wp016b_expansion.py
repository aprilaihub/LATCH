"""Frozen source, manifest and schedule support for WP-016B Expansion."""

from __future__ import annotations

import csv
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import random
from typing import Any, Mapping, Sequence

from .wp016 import sha256
from .wp016_canary import ScientificInputs, file_sha256, read_json
from .wp016b import (
    ARMS, CAPABILITIES, COMPACT_CONTEXT_BYTES, COMPACT_PROMPT_BYTES,
    INPUT_GUARD, MATCHED_RESERVE_BYTES, OUTPUT_CAP, SESSION_OUTPUT_CAP,
    TURN_CAP, CoreController, _compact_body_from_legacy, _legacy_body,
    _source_job, write_json,
)


WP_ID = "WP-016B"
ROOT = Path(__file__).resolve().parents[2]
CONFIG_ROOT = ROOT / "configs" / "wp016b-expansion"
PAYLOAD_ROOT = CONFIG_ROOT / "source-payloads"
SOURCE_LEDGER = CONFIG_ROOT / "source-configurations.csv"
SOURCE_BUNDLE = CONFIG_ROOT / "scientific-bundle-subset.json"
MANIFEST = CONFIG_ROOT / "expansion96-manifest.json"
SCHEDULE = CONFIG_ROOT / "expansion96-schedule.json"
ANALYSIS_CONTRACT = CONFIG_ROOT / "analysis-contract.json"
AUTHORITY = CONFIG_ROOT / "authority" / "WP016B_EXPANSION_DIRECT_AUTHORITY.json"
LEADER_DECISION = CONFIG_ROOT / "authority" / "WP016B_EXPANSION_LEADER_DECISION.json"

CORE_ROOTS = ("transfer_fir_symmetric", "transfer_boom_soc_v2")
NEW_ROOTS = ("titan23_orig_gsm_x6", "mlcad_d181")
MODELS = (
    "openai/gpt-5.6-luna", "openai/gpt-5.6-terra", "openai/gpt-5.6-sol",
)
REPEATS = (1, 2)
BASE_SEED = 271550774
PHASE_LABEL = "WP016B_EXPANSION_96"
DERIVED_SEED_HEX = hashlib.sha256(
    f"{BASE_SEED}{PHASE_LABEL}".encode("ascii")
).hexdigest()
DERIVED_SEED = int(DERIVED_SEED_HEX, 16) % (2**63)
REQUEST_CAP = 384
USD_CAP = Decimal("50.00")
MAX_CONCURRENCY = 20
WALL_CLOCK_CAP_SECONDS = 4 * 3600


def exact_authority() -> dict[str, Any]:
    value = read_json(AUTHORITY)
    required = {
        "schema_version": "research-flow.wp016b-expansion-direct-authority.v1",
        "wp_id": WP_ID,
        "phase": "EXPANSION",
        "decision": "AUTHORISE_ADDITIONAL_96_CELLS_AND_COMBINED_144_ANALYSIS",
        "base_commit": "fafda067066aa3a04eda9038ea84dcecf3260fd6",
        "base_tree": "2c636700fccaab5728c1b1c760b399d8051c6bb3",
        "accepted_core_cells": 48,
        "new_live_cells": 96,
        "new_blocks": 32,
        "combined_cells": 144,
        "maximum_new_provider_requests": REQUEST_CAP,
        "maximum_provider_concurrency": MAX_CONCURRENCY,
        "literal_expansion_usd_cap": 50.0,
        "backend_calls_authorised": 0,
        "retry_count": 0,
        "fallback_count": 0,
        "response_repair_count": 0,
        "one_attempt_only": True,
        "further_expansion_authorised": False,
        "wp016c_authorised": False,
    }
    mismatch = {
        key: {"expected": expected, "observed": value.get(key)}
        for key, expected in required.items() if value.get(key) != expected
    }
    if mismatch:
        raise RuntimeError("wp016b_expansion_authority_drift:" + json.dumps(mismatch, sort_keys=True))
    if tuple(value["core_roots"]) != CORE_ROOTS or tuple(value["new_roots"]) != NEW_ROOTS:
        raise RuntimeError("wp016b_expansion_root_authority_drift")
    if tuple(value["model_systems"]) != MODELS:
        raise RuntimeError("wp016b_expansion_model_authority_drift")
    decision = read_json(LEADER_DECISION)
    if (
        decision.get("decision") != "EXPAND_MODEL_ONLY_STUDY_TO_144_TOTAL_CELLS"
        or decision.get("concurrency", {}).get("maximum_provider_requests_in_flight") != 20
        or decision.get("core_rerun_forbidden") is not True
    ):
        raise RuntimeError("wp016b_expansion_leader_decision_drift")
    return value | {"leader_decision": decision}


def _source_axes() -> list[tuple[str, str, str]]:
    return [
        *[(root, "openai/gpt-5.6-terra", capability)
          for root in CORE_ROOTS for capability in CAPABILITIES],
        *[(root, model, capability)
          for root in NEW_ROOTS for model in MODELS for capability in CAPABILITIES],
    ]


def build_source_bundle(full_bundle_path: Path) -> dict[str, Any]:
    full = read_json(full_bundle_path)
    roots_in_scope = (*CORE_ROOTS, *NEW_ROOTS)
    jobs = []
    for root, model, capability in _source_axes():
        job = _source_job(full, root, model, capability)
        job.pop("root_dcp", None)
        jobs.append(job)
    roots = {}
    for key in roots_in_scope:
        root = dict(full["roots"][key])
        for path_key in ("root_path", "timing_contract_evidence_path"):
            if path_key in root:
                root[path_key + "_identity_sha256"] = sha256(str(root.pop(path_key)).encode())
        roots[key] = root
    core = {
        "schema_version": "wp016b.expansion-frozen-source-bundle-subset.v1",
        "protocol_id": "WP016_CONTEXT_BALANCED_EXPANSION_V1",
        "roots": roots,
        "observations": {key: full["observations"][key] for key in roots_in_scope},
        "jobs": jobs,
        "source_full_bundle_sha256": file_sha256(full_bundle_path),
        "root_strata": {
            **{key: "SOURCE_TIMED_TRANSFER" for key in CORE_ROOTS},
            **{key: "HELDOUT_PLUS_SYNTHETIC_TIMING" for key in NEW_ROOTS},
        },
    }
    return core | {"bundle_sha256": sha256(core)}


def _configuration_id(root: str, model: str, capability: str, arm: str) -> str:
    return "wp016bexp-source-config__" + sha256({
        "root": root, "model": model, "capability": capability, "arm": arm,
        "treatment": "accepted-wp016b-core",
    })[:24]


def _base_row(job: Mapping[str, Any], capability: str, arm: str) -> dict[str, Any]:
    root, model = str(job["root_id"]), str(job["model_key"])
    return {
        "configuration_id": _configuration_id(root, model, capability, arm),
        "configuration_job_id": "wp016bexp-source-job__" + sha256({
            "root": root, "model": model, "capability": capability,
        })[:24],
        "historical_reference_job_id": str(job["job_id"]),
        "root_id": root, "model_system": model, "capability_task": capability,
        "arm": arm, "turn_limit": TURN_CAP, "per_request_output_cap": OUTPUT_CAP,
        "history_limit_bytes": 8_192, "retrieval_result_limit_bytes": 4_096,
        "query_limit": 8, "session_output_cap": SESSION_OUTPUT_CAP,
    }


def _inputs(capture_root: Path, hardblock_scan: Path) -> ScientificInputs:
    return ScientificInputs(
        manifest_path=SOURCE_BUNDLE, manifest_sha256=file_sha256(SOURCE_BUNDLE),
        scientific_bundle=SOURCE_BUNDLE, capture_root=capture_root,
        hardblock_scan=hardblock_scan,
        retrieval_registry=ROOT / "configs/wp016a/closed-retrieval-registry.json",
    )


def build_sources(*, full_bundle_path: Path, capture_root: Path,
                  hardblock_scan: Path) -> list[dict[str, Any]]:
    bundle = build_source_bundle(full_bundle_path)
    write_json(SOURCE_BUNDLE, bundle)
    inputs = _inputs(capture_root, hardblock_scan)
    rows: list[dict[str, Any]] = []
    PAYLOAD_ROOT.mkdir(parents=True, exist_ok=True)
    for root, model, capability in _source_axes():
        job = _source_job(bundle, root, model, capability)
        legacy_row = _base_row(job, capability, "LEGACY_STATIC")
        legacy, legacy_context = _legacy_body(legacy_row, inputs)
        legacy_guard = sum(len(str(item["content"]).encode()) for item in legacy["messages"])
        legacy_reserve = INPUT_GUARD - legacy_guard
        if legacy_reserve <= 0:
            raise RuntimeError("wp016b_expansion_legacy_source_exceeds_input_guard")
        compact = _compact_body_from_legacy(legacy, legacy_context, capability)
        compact_guard = sum(len(str(item["content"]).encode()) for item in compact["messages"])
        if compact_guard != COMPACT_PROMPT_BYTES + COMPACT_CONTEXT_BYTES:
            raise RuntimeError("wp016b_expansion_compact_guard_size_drift")
        for arm in ARMS:
            row = _base_row(job, capability, arm)
            body = legacy if arm == "LEGACY_STATIC" else compact
            reserve = legacy_reserve if arm != "COMPACT_RETRIEVAL_MATCHED_RESERVE" else MATCHED_RESERVE_BYTES
            path = PAYLOAD_ROOT / f"{row['configuration_id']}.json"
            write_json(path, body)
            prompt, context = (str(item["content"]).encode() for item in body["messages"])
            row.update({
                "active_dynamic_capacity_bytes": reserve,
                "legacy_equivalent_capacity_bytes": legacy_reserve,
                "matched_common_capacity_bytes": MATCHED_RESERVE_BYTES,
                "canonical_guard_input_bytes": len(prompt) + len(context),
                "canonical_payload_sha256": sha256(body),
                "prompt_bytes": len(prompt),
                "prompt_hash": hashlib.sha256(prompt).hexdigest(),
                "context_bytes": len(context),
                "context_hash": hashlib.sha256(context).hexdigest(),
                "serialized_payload_relpath": str(path.relative_to(ROOT)),
                "serialized_file_sha256": file_sha256(path),
                "provider_guard_bytes": INPUT_GUARD,
                "root_stratum": (
                    "SOURCE_TIMED_TRANSFER" if root in CORE_ROOTS
                    else "HELDOUT_PLUS_SYNTHETIC_TIMING"
                ),
            })
            rows.append(row)
    fields = sorted({key for row in rows for key in row})
    SOURCE_LEDGER.parent.mkdir(parents=True, exist_ok=True)
    with SOURCE_LEDGER.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, lineterminator="\n")
        writer.writeheader(); writer.writerows(rows)
    if len(rows) != 48:
        raise RuntimeError("wp016b_expansion_exact_48_source_configurations_required")
    return rows


def load_source_rows() -> list[dict[str, str]]:
    with SOURCE_LEDGER.open(encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    if len(rows) != 48:
        raise RuntimeError("wp016b_expansion_source_ledger_incomplete")
    for row in rows:
        path = ROOT / row["serialized_payload_relpath"]
        row["serialized_payload_path"] = str(path)
        if file_sha256(path) != row["serialized_file_sha256"]:
            raise RuntimeError("wp016b_expansion_source_payload_hash_drift")
    return rows


def expansion_manifest_and_schedule(
    rows: Sequence[Mapping[str, Any]],
) -> tuple[dict[str, Any], dict[str, Any]]:
    keyed = {(row["root_id"], row["model_system"], row["capability_task"], row["arm"]): row
             for row in rows}
    blocks = []
    axes = [
        *[(root, "openai/gpt-5.6-terra") for root in CORE_ROOTS],
        *[(root, model) for root in NEW_ROOTS for model in MODELS],
    ]
    for root, model in axes:
        for capability in CAPABILITIES:
            for repeat in REPEATS:
                block_key = {"root_id": root, "model_system": model,
                             "capability": capability, "repeat": repeat}
                block_id = "wp016bexp-block__" + sha256(block_key)[:20]
                cells = []
                for arm in ARMS:
                    source = keyed[(root, model, capability, arm)]
                    identity = block_key | {"arm": arm, "source": source["configuration_id"]}
                    cell_id = "wp016bexp-cell__" + sha256(identity)[:24]
                    cells.append({
                        **identity, "block_id": block_id, "cell_id": cell_id,
                        **{name + "_id": "wp016bexp-" + name + "__" + sha256(
                            identity | {"kind": name}
                        )[:24] for name in ("configuration", "session", "run", "log", "output")},
                        "source_payload_sha256": source["canonical_payload_sha256"],
                        "root_stratum": source["root_stratum"],
                    })
                blocks.append({**block_key, "block_id": block_id, "cells": cells})
    rng = random.Random(DERIVED_SEED)
    rng.shuffle(blocks)
    launch_rows = []
    for block_position, block in enumerate(blocks, 1):
        cells = list(block["cells"]); rng.shuffle(cells); block["cells"] = cells
        for arm_position, cell in enumerate(cells, 1):
            launch_rows.append(cell | {
                "block_position": block_position,
                "arm_position": arm_position,
                "launch_rank": len(launch_rows) + 1,
            })
    core = {
        "schema_version": "wp016b.expansion96-scientific-manifest.v1",
        "wp_id": WP_ID, "phase": "EXPANSION",
        "scientific_analysis_eligible": True,
        "core_roots_terra_only": list(CORE_ROOTS), "new_roots": list(NEW_ROOTS),
        "models": list(MODELS), "capabilities": list(CAPABILITIES),
        "arms": list(ARMS), "repeats": list(REPEATS),
        "cell_count": 96, "block_count": 32,
        "maximum_provider_requests": REQUEST_CAP,
        "literal_usd_cap": str(USD_CAP), "backend_calls": 0,
        "maximum_concurrent_provider_requests": MAX_CONCURRENCY,
        "base_seed": BASE_SEED, "phase_label": PHASE_LABEL,
        "derived_seed_hex": DERIVED_SEED_HEX, "derived_seed": DERIVED_SEED,
        "source_ledger_sha256": file_sha256(SOURCE_LEDGER),
        "source_bundle_sha256": file_sha256(SOURCE_BUNDLE),
        "closed_retrieval_registry_sha256": file_sha256(
            ROOT / "configs/wp016a/closed-retrieval-registry.json"
        ),
        "accepted_core_raw_sha256": "0453c94feba05fca47d1b67827e4832b1bf7096e6a39be321988a3b7a8b1509a",
        "accepted_core_derived_sha256": "2521616250e3aaecd199255cdfffb8c4ae1777753b940088a89995ece1eebde1",
        "cells": launch_rows,
        "core_cells_rerun": False, "canary_data_included": False,
    }
    schedule = {
        "schema_version": "wp016b.expansion96-blocked-randomised-schedule.v1",
        "base_seed": BASE_SEED, "phase_label": PHASE_LABEL,
        "derived_seed_hex": DERIVED_SEED_HEX, "derived_seed": DERIVED_SEED,
        "maximum_concurrency": MAX_CONCURRENCY,
        "complete_block_before_next": False,
        "configured_cap_is_observed_peak": False,
        "blocks": blocks,
    }
    return core | {"manifest_sha256": sha256(core)}, schedule | {"schedule_sha256": sha256(schedule)}


def materialize_manifests(rows: Sequence[Mapping[str, Any]]) -> None:
    manifest, schedule = expansion_manifest_and_schedule(rows)
    write_json(MANIFEST, manifest); write_json(SCHEDULE, schedule)
    analysis = {
        "schema_version": "wp016b.expansion-analysis-contract.v1",
        "primary_endpoint": "proposal_named_family_valid_commit_v4",
        "sequential_provenance": ["CORE_48", "EXPANSION_96", "COMBINED_144"],
        "root_strata": ["SOURCE_TIMED_TRANSFER", "HELDOUT_PLUS_SYNTHETIC_TIMING"],
        "paired_units_per_capability_contrast": 24,
        "g_minus_p_compact_matched_pairs": 24,
        "intervals": [0.80, 0.95],
        "physical_functional_fmax_claims": False,
        "clean_family_heldout_claim": False,
        "evidence_label": (
            "MODEL-ONLY PROPOSAL-CONSTRUCTION STUDY / "
            "NO PHYSICAL / FUNCTIONAL / FMAX OUTCOME"
        ),
    }
    write_json(ANALYSIS_CONTRACT, analysis | {
        "analysis_contract_sha256": sha256(analysis),
    })


class ExpansionController(CoreController):
    """Accepted treatment controller with Expansion identities and inputs."""


__all__ = [
    "ANALYSIS_CONTRACT", "AUTHORITY", "BASE_SEED", "CONFIG_ROOT",
    "CORE_ROOTS", "DERIVED_SEED", "DERIVED_SEED_HEX", "ExpansionController",
    "LEADER_DECISION", "MANIFEST", "MAX_CONCURRENCY", "MODELS", "NEW_ROOTS",
    "PAYLOAD_ROOT", "PHASE_LABEL", "REQUEST_CAP", "SCHEDULE", "SOURCE_BUNDLE",
    "SOURCE_LEDGER", "USD_CAP", "WALL_CLOCK_CAP_SECONDS", "build_sources",
    "exact_authority", "expansion_manifest_and_schedule", "load_source_rows",
    "materialize_manifests",
]
