"""Frozen manifest and controller support for the WP-016B Core study.

This module deliberately reuses the accepted WP-016A treatment controller.
It expands only the explicit root/model/repeat axes and keeps every EDA path
outside the executable dependency graph.
"""

from __future__ import annotations

import copy
import csv
from dataclasses import replace
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import random
from typing import Any, Mapping, Sequence

from .multi_provider import PROFILES
from .wp016 import canonical_bytes, compact_model_visible_protocol, compact_protocol_definition, sha256
from .wp016_canary import (
    CanaryController, LiteralBudget, ScientificInputs, _frozen_session,
    _wp016_request_body, file_sha256, read_json,
)


WP_ID = "WP-016B"
ROOT = Path(__file__).resolve().parents[2]
CONFIG_ROOT = ROOT / "configs" / "wp016b"
PAYLOAD_ROOT = CONFIG_ROOT / "source-payloads"
SOURCE_LEDGER = CONFIG_ROOT / "source-configurations.csv"
SOURCE_BUNDLE = CONFIG_ROOT / "scientific-bundle-subset.json"
MANIFEST = CONFIG_ROOT / "core48-manifest.json"
SCHEDULE = CONFIG_ROOT / "core48-schedule.json"
ANALYSIS_CONTRACT = CONFIG_ROOT / "analysis-contract.json"
AUTHORITY = CONFIG_ROOT / "authority" / "WP016B_DIRECT_EXECUTION_AUTHORITY.json"
CONCURRENCY_AMENDMENT = CONFIG_ROOT / "authority" / "WP016B_CONCURRENCY20_AMENDMENT.json"

ROOTS = ("transfer_fir_symmetric", "transfer_boom_soc_v2")
MODELS = ("openai/gpt-5.6-luna", "openai/gpt-5.6-sol")
CAPABILITIES = ("P", "G")
ARMS = (
    "LEGACY_STATIC", "COMPACT_RETRIEVAL_LOW_RESERVE",
    "COMPACT_RETRIEVAL_MATCHED_RESERVE",
)
REPEATS = (1, 2)
SEED = 271550774
TURN_CAP = 4
REQUEST_CAP = 192
USD_CAP = Decimal("25.00")
MAX_CONCURRENCY = 20
INPUT_GUARD = 65_536
OUTPUT_CAP = 4_096
SESSION_OUTPUT_CAP = 16_384
COMPACT_PROMPT_BYTES = 399
COMPACT_CONTEXT_BYTES = 6_859
MATCHED_RESERVE_BYTES = INPUT_GUARD - COMPACT_PROMPT_BYTES - COMPACT_CONTEXT_BYTES


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def exact_authority() -> dict[str, Any]:
    value = read_json(AUTHORITY)
    required = {
        "schema_version": "research-flow.wp016b-direct-core-authority.v1",
        "wp_id": WP_ID, "decision": "AUTHORISE_IMPLEMENT_TEST_FREEZE_AND_EXECUTE_ONCE",
        "base_commit": "adb737328c7b24fc46f4204d008e5cf387a7e9a1",
        "base_tree": "2a670bac6a051fd68ea9dda390686553af88fdab",
        "live_cells": 48, "blocks": 16, "maximum_provider_requests": REQUEST_CAP,
        "literal_usd_cap": 25.0, "randomisation_seed": SEED,
        "maximum_concurrent_provider_requests": 1, "backend_calls_authorised": 0,
        "extension_authorised": False, "full_96_cell_matrix_authorised": False,
        "wp016c_authorised": False,
    }
    mismatch = {key: {"expected": expected, "observed": value.get(key)}
                for key, expected in required.items() if value.get(key) != expected}
    if mismatch:
        raise RuntimeError("wp016b_authority_drift:" + json.dumps(mismatch, sort_keys=True))
    if tuple(value["roots"]) != ROOTS or tuple(value["model_systems"]) != MODELS:
        raise RuntimeError("wp016b_authorised_factor_drift")
    amendment = read_json(CONCURRENCY_AMENDMENT)
    required_amendment = {
        "schema_version": "research-flow.wp016b-concurrency20-amendment.v1",
        "wp_id": WP_ID, "decision": "AUTHORISE_CORE48_OPENAI_EXPORT_AND_CONCURRENCY_20",
        "provider": "OPENAI_FIRST_PARTY", "maximum_concurrent_provider_requests": 20,
        "maximum_concurrent_cells": 20, "maximum_provider_requests": REQUEST_CAP,
        "literal_usd_cap": 25.0, "backend_calls_authorised": 0,
        "retry_count": 0, "response_repair_count": 0,
        "block_serial_execution_required": False,
    }
    mismatch = {key: {"expected": expected, "observed": amendment.get(key)}
                for key, expected in required_amendment.items() if amendment.get(key) != expected}
    if mismatch:
        raise RuntimeError("wp016b_concurrency_amendment_drift:" + json.dumps(mismatch, sort_keys=True))
    return value | {"execution_amendment": amendment}


def _source_job(bundle: Mapping[str, Any], root: str, model: str, capability: str) -> dict[str, Any]:
    level = "L3" if capability == "P" else "L4"
    condition = "P_BOUNDARY" if capability == "P" else "G_STRICT_BOUNDARY"
    matches = [row for row in bundle["jobs"] if row.get("root_id") == root
               and row.get("model_key") == model and row.get("level") == level
               and row.get("condition") == condition and row.get("track") == "C_CONDITIONED"]
    if len(matches) != 1:
        raise RuntimeError(f"source_job_not_unique:{root}:{model}:{capability}:{len(matches)}")
    return dict(matches[0])


def build_source_bundle(full_bundle_path: Path, accepted_capsule_bundle_path: Path) -> dict[str, Any]:
    full = read_json(full_bundle_path)
    accepted = read_json(accepted_capsule_bundle_path)
    jobs = []
    for root in ROOTS:
        for model in MODELS:
            for capability in CAPABILITIES:
                source = accepted if (root, model) == ("transfer_boom_soc_v2", "openai/gpt-5.6-luna") else full
                job = _source_job(source, root, model, capability)
                job.pop("root_dcp", None)
                jobs.append(job)
    roots = {}
    for key in ROOTS:
        root = dict(full["roots"][key])
        for path_key in ("root_path", "timing_contract_evidence_path"):
            if path_key in root:
                root[path_key + "_identity_sha256"] = sha256(str(root.pop(path_key)).encode())
        roots[key] = root
    core = {
        "schema_version": "wp016b.frozen-source-bundle-subset.v1",
        "protocol_id": "WP016_CONTEXT_BALANCED_CORE_V1",
        "roots": roots,
        "observations": {key: full["observations"][key] for key in ROOTS},
        "jobs": jobs,
        "source_full_bundle_sha256": file_sha256(full_bundle_path),
        "source_accepted_r10_capsule_bundle_sha256": file_sha256(accepted_capsule_bundle_path),
    }
    return core | {"bundle_sha256": sha256(core)}


def _configuration_job_id(root: str, model: str, capability: str) -> str:
    # Preserve the exact accepted C1/R10 identity for the byte-level anchor.
    if (root, model, capability) == ("transfer_boom_soc_v2", "openai/gpt-5.6-luna", "P"):
        return "c1r4___7ac0864b61d24d252fca34f13a009fc8"
    if (root, model, capability) == ("transfer_boom_soc_v2", "openai/gpt-5.6-luna", "G"):
        return "c1r4___5500affa09d674540f3d3070cc54f596"
    return "wp016b-source-job__" + sha256({"root": root, "model": model, "capability": capability})[:24]


def _configuration_id(root: str, model: str, capability: str, arm: str) -> str:
    return "wp016b-source-config__" + sha256({
        "root": root, "model": model, "capability": capability, "arm": arm,
        "treatment": "accepted-r10",
    })[:24]


def _inputs(capture_root: Path, hardblock_scan: Path) -> ScientificInputs:
    return ScientificInputs(
        manifest_path=SOURCE_BUNDLE, manifest_sha256=file_sha256(SOURCE_BUNDLE),
        scientific_bundle=SOURCE_BUNDLE, capture_root=capture_root,
        hardblock_scan=hardblock_scan,
        retrieval_registry=ROOT / "configs/wp016a/closed-retrieval-registry.json",
    )


def _base_row(job: Mapping[str, Any], capability: str, arm: str) -> dict[str, Any]:
    root, model = str(job["root_id"]), str(job["model_key"])
    return {
        "configuration_id": _configuration_id(root, model, capability, arm),
        "configuration_job_id": _configuration_job_id(root, model, capability),
        "historical_reference_job_id": str(job["job_id"]),
        "root_id": root, "model_system": model, "capability_task": capability,
        "arm": arm, "turn_limit": TURN_CAP, "per_request_output_cap": OUTPUT_CAP,
        "history_limit_bytes": 8_192, "retrieval_result_limit_bytes": 4_096,
        "query_limit": 8, "session_output_cap": SESSION_OUTPUT_CAP,
    }


def _legacy_body(row: Mapping[str, Any], inputs: ScientificInputs) -> tuple[dict[str, Any], dict[str, Any]]:
    session, _compile, contract, observation, job, wp015 = _frozen_session(
        row, inputs=inputs, run_nonce="wp016b-source-build", new_identity=False,
    )
    token = wp015.legacy.CURRENT_JOB.set(job)
    try:
        prompt = wp015.base._prompt(job["level"])
        context = wp015.base._planner_context(
            observation=observation, session=session, contract=contract, last_result=None,
        )
        binding = wp015.base._binding(
            job=job, model=str(row["model_system"]), turn=0, context=context,
            prompt=prompt, catalogue_hash=wp015.digest(contract), reserve=float(USD_CAP),
        )
        binding = replace(binding, settings=replace(binding.settings, max_tokens=OUTPUT_CAP))
        body = _wp016_request_body(PROFILES[str(row["model_system"])], binding)
    finally:
        wp015.legacy.CURRENT_JOB.reset(token)
    return body, json.loads(context.decode("utf-8"))


def _compact_body_from_legacy(
    legacy: Mapping[str, Any], legacy_context: Mapping[str, Any], capability: str,
) -> dict[str, Any]:
    template_name = (
        "c1r4-config__9e4f5244e16f37c2a456bc08.json" if capability == "P"
        else "c1r4-config__1a4c02ecb009dbf4e43c03bf.json"
    )
    template = read_json(ROOT / "configs/wp016a/source-payloads" / template_name)
    template_context = json.loads(template["messages"][1]["content"])
    for key in ("common_observation", "common_observation_sha256", "common_query_catalogue"):
        template_context[key] = copy.deepcopy(legacy_context[key])
    template_context["padding"] = ""
    unpadded = canonical_bytes(template_context).decode("utf-8")
    padding = COMPACT_CONTEXT_BYTES - len(unpadded.encode("utf-8"))
    if padding < 0:
        raise RuntimeError(f"compact_initial_context_exceeds_frozen_size:{capability}:{-padding}")
    template_context["padding"] = "0" * padding
    context = canonical_bytes(template_context).decode("utf-8")
    if len(context.encode("utf-8")) != COMPACT_CONTEXT_BYTES:
        raise AssertionError("compact_context_size_drift")
    body = copy.deepcopy(dict(legacy))
    body["messages"] = [
        {"role": "system", "content": template["messages"][0]["content"]},
        {"role": "user", "content": context},
    ]
    return body


def build_sources(*, full_bundle_path: Path, accepted_capsule_bundle_path: Path,
                  capture_root: Path, hardblock_scan: Path) -> list[dict[str, Any]]:
    bundle = build_source_bundle(full_bundle_path, accepted_capsule_bundle_path)
    write_json(SOURCE_BUNDLE, bundle)
    inputs = _inputs(capture_root, hardblock_scan)
    rows: list[dict[str, Any]] = []
    PAYLOAD_ROOT.mkdir(parents=True, exist_ok=True)
    for root in ROOTS:
        for model in MODELS:
            for capability in CAPABILITIES:
                job = _source_job(bundle, root, model, capability)
                legacy_row = _base_row(job, capability, "LEGACY_STATIC")
                legacy, legacy_context = _legacy_body(legacy_row, inputs)
                legacy_guard = sum(len(str(item["content"]).encode()) for item in legacy["messages"])
                legacy_reserve = INPUT_GUARD - legacy_guard
                if legacy_reserve <= 0:
                    raise RuntimeError("legacy_source_exceeds_input_guard")
                compact = _compact_body_from_legacy(legacy, legacy_context, capability)
                compact_guard = sum(len(str(item["content"]).encode()) for item in compact["messages"])
                if compact_guard != COMPACT_PROMPT_BYTES + COMPACT_CONTEXT_BYTES:
                    raise RuntimeError("compact_guard_size_drift")
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
                        "prompt_bytes": len(prompt), "prompt_hash": hashlib.sha256(prompt).hexdigest(),
                        "context_bytes": len(context), "context_hash": hashlib.sha256(context).hexdigest(),
                        "serialized_payload_relpath": str(path.relative_to(ROOT)),
                        "serialized_file_sha256": file_sha256(path),
                        "provider_guard_bytes": INPUT_GUARD,
                    })
                    rows.append(row)
    fields = sorted({key for row in rows for key in row})
    SOURCE_LEDGER.parent.mkdir(parents=True, exist_ok=True)
    with SOURCE_LEDGER.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, lineterminator="\n")
        writer.writeheader(); writer.writerows(rows)
    _assert_r10_anchor(rows)
    return rows


def _assert_r10_anchor(rows: Sequence[Mapping[str, Any]]) -> None:
    old_rows = list(csv.DictReader((ROOT / "configs/wp016a/source-configurations.csv").open()))
    old = {(row["capability_task"], row["arm"]): row for row in old_rows}
    anchors = [row for row in rows if row["root_id"] == "transfer_boom_soc_v2"
               and row["model_system"] == "openai/gpt-5.6-luna"]
    for row in anchors:
        reference = old[(str(row["capability_task"]), str(row["arm"]))]
        body = read_json(ROOT / str(row["serialized_payload_relpath"]))
        reference_body = read_json(
            ROOT / "configs/wp016a/source-payloads" / f"{reference['configuration_id']}.json"
        )
        # R10 froze model-visible prompt/context parity while explicitly
        # normalising the transport with no-cache/default-tier fields.  Those
        # transport-only keys are therefore intentionally not part of this
        # treatment anchor.
        if body.get("messages") != reference_body.get("messages"):
            raise RuntimeError("accepted_r10_model_visible_payload_anchor_drift:" + str(row["arm"]))
        if int(row["active_dynamic_capacity_bytes"]) != int(reference["active_dynamic_capacity_bytes"]):
            raise RuntimeError("accepted_r10_reserve_anchor_drift")


def load_source_rows() -> list[dict[str, str]]:
    with SOURCE_LEDGER.open(encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    if len(rows) != 24:
        raise RuntimeError("wp016b_exact_24_source_configurations_required")
    for row in rows:
        path = ROOT / row["serialized_payload_relpath"]
        row["serialized_payload_path"] = str(path)
        if file_sha256(path) != row["serialized_file_sha256"]:
            raise RuntimeError("wp016b_source_payload_hash_drift")
    return rows


def core_manifest_and_schedule(rows: Sequence[Mapping[str, Any]]) -> tuple[dict[str, Any], dict[str, Any]]:
    keyed = {(row["root_id"], row["model_system"], row["capability_task"], row["arm"]): row
             for row in rows}
    blocks = []
    for root in ROOTS:
        for model in MODELS:
            for capability in CAPABILITIES:
                for repeat in REPEATS:
                    block_key = {"root_id": root, "model_system": model,
                                 "capability": capability, "repeat": repeat}
                    block_id = "wp016b-block__" + sha256(block_key)[:20]
                    cells = []
                    for arm in ARMS:
                        source = keyed[(root, model, capability, arm)]
                        identity = block_key | {"arm": arm, "source": source["configuration_id"]}
                        cell_id = "wp016b-cell__" + sha256(identity)[:24]
                        cells.append({
                            **identity, "block_id": block_id, "cell_id": cell_id,
                            **{name + "_id": "wp016b-" + name + "__" + sha256(identity | {"kind": name})[:24]
                               for name in ("configuration", "session", "run", "log", "output")},
                            "source_payload_sha256": source["canonical_payload_sha256"],
                        })
                    blocks.append({**block_key, "block_id": block_id, "cells": cells})
    rng = random.Random(SEED)
    rng.shuffle(blocks)
    schedule_rows = []
    for block_position, block in enumerate(blocks, 1):
        cells = list(block["cells"]); rng.shuffle(cells)
        block["cells"] = cells
        for arm_position, cell in enumerate(cells, 1):
            schedule_rows.append(cell | {"block_position": block_position, "arm_position": arm_position})
    manifest_core = {
        "schema_version": "wp016b.core48-scientific-manifest.v1", "wp_id": WP_ID,
        "scientific_analysis_eligible": True, "roots": list(ROOTS), "models": list(MODELS),
        "capabilities": list(CAPABILITIES), "arms": list(ARMS), "repeats": list(REPEATS),
        "cell_count": 48, "block_count": 16, "maximum_provider_requests": REQUEST_CAP,
        "literal_usd_cap": str(USD_CAP), "backend_calls": 0,
        "maximum_concurrent_provider_requests": MAX_CONCURRENCY,
        "execution_amendment_sha256": file_sha256(CONCURRENCY_AMENDMENT),
        "source_ledger_sha256": file_sha256(SOURCE_LEDGER),
        "source_bundle_sha256": file_sha256(SOURCE_BUNDLE),
        "closed_retrieval_registry_sha256": file_sha256(ROOT / "configs/wp016a/closed-retrieval-registry.json"),
        "cells": schedule_rows,
        "canary_data_included": False, "extension_roots_included": False,
    }
    schedule_core = {
        "schema_version": "wp016b.blocked-randomised-schedule.v1", "seed": SEED,
        "maximum_concurrency": MAX_CONCURRENCY, "complete_block_before_next": False,
        "stop_only_between_blocks": False,
        "concurrency_amendment": "USER_AUTHORISED_BEFORE_FIRST_LIVE_REQUEST",
        "blocks": blocks,
    }
    return (manifest_core | {"manifest_sha256": sha256(manifest_core)},
            schedule_core | {"schedule_sha256": sha256(schedule_core)})


def materialize_manifests(rows: Sequence[Mapping[str, Any]]) -> None:
    manifest, schedule = core_manifest_and_schedule(rows)
    write_json(MANIFEST, manifest); write_json(SCHEDULE, schedule)
    analysis = {
        "schema_version": "wp016b.analysis-contract.v1",
        "primary_endpoint": "proposal_named_family_valid_commit_v4",
        "contrasts": ["COMPACT_LOW_MINUS_LEGACY_BY_CAPABILITY",
                      "COMPACT_MATCHED_MINUS_COMPACT_LOW_BY_CAPABILITY",
                      "G_MINUS_P_UNDER_COMPACT_MATCHED"],
        "paired_units_per_contrast": 8,
        "intervals": [0.80, 0.95], "physical_functional_fmax_claims": False,
        "evidence_label": "MODEL-ONLY PROPOSAL-CONSTRUCTION STUDY / NO PHYSICAL / FUNCTIONAL / FMAX OUTCOME",
    }
    write_json(ANALYSIS_CONTRACT, analysis | {"analysis_contract_sha256": sha256(analysis)})


class CoreController(CanaryController):
    """Accepted controller with WP-016B's explicit model/payload rows."""


__all__ = [
    "ANALYSIS_CONTRACT", "ARMS", "AUTHORITY", "CAPABILITIES", "CONFIG_ROOT",
    "CONCURRENCY_AMENDMENT", "CoreController", "INPUT_GUARD", "MANIFEST",
    "MATCHED_RESERVE_BYTES", "MAX_CONCURRENCY", "MODELS",
    "PAYLOAD_ROOT", "REPEATS", "REQUEST_CAP", "ROOTS", "SCHEDULE", "SEED",
    "SOURCE_BUNDLE", "SOURCE_LEDGER", "USD_CAP", "build_sources",
    "core_manifest_and_schedule", "exact_authority", "load_source_rows",
    "materialize_manifests",
]
