"""WP-016A-R08 five-cell completion-canary authority surface."""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any, Mapping, Sequence

from jsonschema import Draft202012Validator

from .wp016 import sha256
from .wp016_canary import (
    LITERAL_USD_CAP, TURN_CAP, _run_rows, budget_guard_receipt,
    clean_repository_identity, controller_source_sha256, file_sha256,
    load_source_rows, provider_binding_receipt, read_json,
    validate_credential_binding, wp016_identity,
)
from .wp016_inputs import (
    verify_environment_manifest, verify_runtime_manifest,
    verify_scientific_input_manifest,
)
from .wp016_outcomes import (
    IntegrationStatus, classify_terminal, taxonomy_manifest,
)


ATTEMPT2_PACKAGE = "codex_to_pro_C001_WP016A-ATTEMPT2.zip"
ATTEMPT2_PACKAGE_SHA256 = "9107d88cca73c04d07e13c9269cf55a6df0cb8100ef5113315dc824349061b42"
ATTEMPT2_G_LOW_SOURCE = "c1r4-config__1a4c02ecb009dbf4e43c03bf"
ATTEMPT2_CANARY_RESULT_SHA256 = "bb8e12418b22ef233c0151c2715cc3443321c9afac7432550a621bc68d032e6b"
ATTEMPT2_PARTIAL_JOURNAL_SHA256 = "72030ac47c4f9c9599aa6326d552bea982fa48f4d7b6959fca1c475a868b0a45"
ATTEMPT2_SOURCE_COMMIT = "68b477d8263c54cf16c7aee4cdaaaf3df8258b06"
ATTEMPT2_SOURCE_TREE = "0347fa983396f696ae55a0172a1021e6510cbe0e"

R08_REVISION = "R08"
OFFLINE_REPLAY_NONCE = "wp016a-r08-five-cell-offline-replay"
ROOT = Path(__file__).resolve().parents[2]
COMPLETION_TOKEN_SCHEMA = (
    ROOT / "configs" / "wp016a" / "authority"
    / "WP-016A-R08-COMPLETION-LIVE-TOKEN-V1.schema.json"
)
R08_COMPLETION_MANIFEST = "WP-016A-R08-five-cell-completion-manifest.json"
R08_TERMINAL_TAXONOMY = "WP-016A-R08-terminal-outcome-taxonomy.json"
R08_ATTEMPT2_BRIDGE = "WP-016A-R08-attempt2-bridge-v2.json"
R08_COMBINED_TEMPLATE = "WP-016A-R08-combined-ledger-template-v2.json"
R08_PROVIDER_BINDING = "WP-016A-R08-provider-binding.json"
R08_BUDGET_GUARD = "WP-016A-R08-completion-budget-guard.json"
R08_OFFLINE_REPLAY = "WP-016A-R08-five-cell-offline-replay.json"

R09_PACKAGE = "codex_to_pro_C001_WP016A-R09.zip"
R09_PACKAGE_SHA256 = "dcc81730843a64ee8121c2d0009d24755504dbbc727b50d85051a61b6a834484"
R09_COMPLETION_RESULT_SHA256 = "733356f11c30fabd5dceae554d94ee8a57ece8b57a0c74674398c47c8be7a54a"
R09_PARTIAL_FAILURE_SHA256 = "a0101d7cbf8919f0a3c41561e2a6865ff18c0e32cce00bb945b6a6c8ff55336b"
R09_RESPONSE_TURN1_SHA256 = "71bfe95fedf77b23e301cb4a873bde82d39ed018c335979c3d33b50f41b71ab7"
R10_REVISION = "R10"
R10_OFFLINE_REPLAY_NONCE = "wp016a-r10-five-cell-offline-replay"

COMPLETION_SOURCE_IDS = (
    "c1r4-config__e03630fa96b9b578012f8411",
    "c1r4-config__bba09bfda82f35481a8abf7b",
    "c1r4-config__55cd0a630fa292e539ee6c64",
    "c1r4-config__9e4f5244e16f37c2a456bc08",
    "c1r4-config__3a0beced6449ec4c80e1882d",
)
COMPLETION_REQUEST_CAP = len(COMPLETION_SOURCE_IDS) * TURN_CAP


def load_completion_rows() -> list[dict[str, str]]:
    rows = {row["configuration_id"]: row for row in load_source_rows()}
    if set(COMPLETION_SOURCE_IDS) - set(rows):
        raise RuntimeError("completion_source_configuration_missing")
    selected = [rows[source] for source in COMPLETION_SOURCE_IDS]
    expected = [
        ("G", "LEGACY_STATIC"),
        ("G", "COMPACT_RETRIEVAL_MATCHED_RESERVE"),
        ("P", "LEGACY_STATIC"),
        ("P", "COMPACT_RETRIEVAL_LOW_RESERVE"),
        ("P", "COMPACT_RETRIEVAL_MATCHED_RESERVE"),
    ]
    observed = [(row["capability_task"], row["arm"]) for row in selected]
    if observed != expected or ATTEMPT2_G_LOW_SOURCE in COMPLETION_SOURCE_IDS:
        raise RuntimeError("completion_five_cell_inventory_drift")
    return selected


def completion_manifest() -> dict[str, Any]:
    cells = []
    for position, row in enumerate(load_completion_rows(), start=1):
        source = row["configuration_id"]
        cells.append({
            "block_position": position,
            "source_r8_configuration_id": source,
            "capability": row["capability_task"],
            "arm": row["arm"],
            "identity_templates": {
                kind: (
                    f"wp016a-{kind}__sha256(wp,source,kind,"
                    "completion_token.run_nonce)[:24]"
                )
                for kind in ("configuration", "cell", "session", "run", "log", "output")
            },
            "client_request_identity_template": (
                "wp016a-client__sha256(completion_token.run_nonce,cell,turn)"
            ),
            "scientific_analysis_eligible": False,
        })
    core = {
        "schema_version": "wp016a.r08.five-cell-completion-manifest.v2",
        "wp_id": "WP-016A",
        "revision": R08_REVISION,
        "status": "READY_FOR_FIVE_CELL_LIVE_TOKEN_REVIEW",
        "root_id": "transfer_boom_soc_v2",
        "model_system": "openai/gpt-5.6-luna",
        "cells": cells,
        "excluded_attempt2_configuration": ATTEMPT2_G_LOW_SOURCE,
        "maximum_provider_requests": COMPLETION_REQUEST_CAP,
        "literal_usd_cap": float(LITERAL_USD_CAP),
        "turn_cap": TURN_CAP,
        "retry_count": 0,
        "fallback_count": 0,
        "repair_count": 0,
        "offline_replay_nonce": OFFLINE_REPLAY_NONCE,
        "provider_calls_at_r08": 0,
        "backend_calls_at_r08": 0,
        "scientific_analysis_eligible": False,
        "wp016b_authorised": False,
    }
    return core | {"manifest_sha256": sha256(core)}


def r10_completion_manifest() -> dict[str, Any]:
    historical = completion_manifest()
    core = {
        key: value for key, value in historical.items()
        if key != "manifest_sha256"
    }
    core.update({
        "schema_version": "wp016a.r10.five-cell-completion-manifest.v1",
        "revision": R10_REVISION,
        "status": "DIRECT_SMALL_EXPERIMENT_AUTHORISED",
        "identity_authority": "R10_DIRECT_AUTHORITY_RUN_NONCE",
        "provider_calls_before_live_execution": 0,
        "backend_calls_before_live_execution": 0,
    })
    core.pop("provider_calls_at_r08", None)
    core.pop("backend_calls_at_r08", None)
    return core | {"manifest_sha256": sha256(core)}


def attempt2_bridge_receipt(
    canary_result: Mapping[str, Any], *,
    source_file_sha256: str = ATTEMPT2_CANARY_RESULT_SHA256,
) -> dict[str, Any]:
    if source_file_sha256 != ATTEMPT2_CANARY_RESULT_SHA256:
        raise RuntimeError("attempt2_source_result_hash_mismatch")
    original = str(canary_result.get("status"))
    result = canary_result.get("result")
    if (
        original != "NO_GO_INVALID_CANARY_TERMINAL_TURN_LIMIT_EXHAUSTED"
        or not isinstance(result, Mapping)
        or result.get("status") != original
        or result.get("provider_calls") != 4
        or result.get("backend_calls") != 0
        or result.get("charged_cost_usd") != "0.0054008"
    ):
        raise RuntimeError("attempt2_bridge_source_mismatch")
    cells = result.get("cells")
    if not isinstance(cells, list) or len(cells) != 1:
        raise RuntimeError("attempt2_bridge_cell_count_mismatch")
    cell = cells[0]
    if (
        cell.get("source_r8_configuration_id") != ATTEMPT2_G_LOW_SOURCE
        or cell.get("capability") != "G"
        or cell.get("arm") != "COMPACT_RETRIEVAL_LOW_RESERVE"
        or cell.get("terminal") != "TURN_LIMIT_EXHAUSTED"
        or cell.get("request_count") != 4
    ):
        raise RuntimeError("attempt2_bridge_cell_identity_mismatch")
    identities = {
        key: cell.get(key) for key in (
            "configuration_id", "cell_id", "session_id", "run_id", "log_id",
            "output_id",
        )
    }
    if any(
        not isinstance(value, str) or not value.startswith("wp016a-")
        for value in identities.values()
    ):
        raise RuntimeError("attempt2_bridge_runtime_identity_missing")
    turns = cell.get("turns")
    if not isinstance(turns, list) or len(turns) != 4:
        raise RuntimeError("attempt2_bridge_turn_count_mismatch")
    if any(
        turn.get("returned_model") != "openai/gpt-5.6-luna"
        or turn.get("returned_route") != "openai_direct"
        or turn.get("returned_service_tier") != "default"
        or turn.get("provider_prompt_cache_read_tokens") != 0
        or turn.get("provider_prompt_cache_write_tokens") != 0
        for turn in turns
    ):
        raise RuntimeError("attempt2_bridge_provider_firewall_mismatch")
    request_ids = cell.get("request_ids")
    turn_request_ids = [{
        key: turn.get(key) for key in (
            "client_request_id", "openai_x_request_id", "response_object_id",
        )
    } for turn in turns]
    if not isinstance(request_ids, list) or len(request_ids) != 4:
        request_ids = turn_request_ids
    elif request_ids != turn_request_ids:
        raise RuntimeError("attempt2_bridge_request_identity_inconsistent")
    if any(
        not isinstance(row, Mapping)
        or any(not isinstance(row.get(key), str) or not row.get(key) for key in (
            "client_request_id", "openai_x_request_id", "response_object_id",
        ))
        for row in request_ids
    ):
        raise RuntimeError("attempt2_bridge_request_identity_missing")
    classification = classify_terminal("TURN_LIMIT_EXHAUSTED")
    core = {
        "schema_version": "wp016a.r08.attempt2-bridge-receipt.v2",
        "source_package": ATTEMPT2_PACKAGE,
        "source_package_sha256": ATTEMPT2_PACKAGE_SHA256,
        "source_canary_result_sha256": source_file_sha256,
        "source_partial_journal_sha256": ATTEMPT2_PARTIAL_JOURNAL_SHA256,
        "source_commit": ATTEMPT2_SOURCE_COMMIT,
        "source_tree": ATTEMPT2_SOURCE_TREE,
        "original_status": original,
        "original_status_rewritten": False,
        "source_r8_configuration_id": ATTEMPT2_G_LOW_SOURCE,
        "capability": "G",
        "arm": "COMPACT_RETRIEVAL_LOW_RESERVE",
        "requests": 4,
        "cost_usd": "0.0054008",
        "terminal": "TURN_LIMIT_EXHAUSTED",
        **identities,
        "request_ids": request_ids,
        "request_id_summary": {
            "request_count": len(request_ids),
            "client_request_ids_unique": len({row["client_request_id"] for row in request_ids}),
            "openai_request_ids_unique": len({row["openai_x_request_id"] for row in request_ids}),
            "response_object_ids_unique": len({row["response_object_id"] for row in request_ids}),
            "canonical_sha256": sha256(request_ids),
        },
        **classification.public(),
        "provider_parser_guard_cache_identity_firewalls": "PASS",
        "attempt2_token_consumed": True,
    }
    return core | {"receipt_sha256": sha256(core)}


def r09_bridge_receipt(
    completion_terminal: Mapping[str, Any],
    partial_failure: Mapping[str, Any],
    response_turn1: Mapping[str, Any],
    *,
    completion_result_sha256: str = R09_COMPLETION_RESULT_SHA256,
    partial_failure_sha256: str = R09_PARTIAL_FAILURE_SHA256,
    response_turn1_sha256: str = R09_RESPONSE_TURN1_SHA256,
) -> dict[str, Any]:
    """Preserve R09 while deriving only its independently supported boundary."""
    if (
        completion_result_sha256 != R09_COMPLETION_RESULT_SHA256
        or partial_failure_sha256 != R09_PARTIAL_FAILURE_SHA256
        or response_turn1_sha256 != R09_RESPONSE_TURN1_SHA256
    ):
        raise RuntimeError("r09_source_hash_mismatch")
    result = completion_terminal.get("result")
    safe = response_turn1.get("safe_transport_evidence")
    if (
        completion_terminal.get("status") != "NO_GO_PROVIDER_OR_TRANSPORT"
        or not isinstance(result, Mapping)
        or result.get("status") != "NO_GO_PROVIDER_OR_TRANSPORT"
        or result.get("provider_calls") != 1
        or result.get("backend_calls") != 0
        or result.get("charged_cost_usd") != "0.0034606"
        or partial_failure.get("status") != "NO_GO_PROVIDER_OR_TRANSPORT"
        or partial_failure.get("failed_turn") != 2
        or partial_failure.get("last_provider_turn") != 1
        or partial_failure.get("stop_reason")
        != "provider_common_inference_contract_mismatch"
        or response_turn1.get("turn") != 1
        or not isinstance(safe, Mapping)
        or safe.get("http_status") != 200
        or safe.get("parser_status") != "PASS"
        or safe.get("returned_api_model_id") != "gpt-5.6-luna"
        or safe.get("returned_service_tier") != "default"
    ):
        raise RuntimeError("r09_source_evidence_mismatch")
    core = {
        "schema_version": "wp016a.r10.r09-derived-bridge.v1",
        "wp_id": "WP-016A",
        "revision": R10_REVISION,
        "source_package": R09_PACKAGE,
        "source_package_sha256": R09_PACKAGE_SHA256,
        "source_hashes": {
            "completion_run_result": completion_result_sha256,
            "partial_run_failure": partial_failure_sha256,
            "response_turn1": response_turn1_sha256,
        },
        "original_status": "NO_GO_PROVIDER_OR_TRANSPORT",
        "original_status_rewritten": False,
        "integration_evidence_turn1": "COMPLETE",
        "turn2_failure_locus": "LOCAL_CONTROLLER_REQUEST_CONSTRUCTION",
        "exact_typed_guard_evidence_available": False,
        "used_as_six_configuration_completion_evidence": False,
        "provider_calls": 1,
        "backend_calls": 0,
        "known_cost_usd": "0.0034606",
        "scientific_analysis_eligible": False,
        "wp016b_authorised": False,
    }
    return core | {"receipt_sha256": sha256(core)}


def combined_ledger_template(bridge: Mapping[str, Any]) -> dict[str, Any]:
    if bridge.get("receipt_sha256") != sha256({
        key: value for key, value in bridge.items() if key != "receipt_sha256"
    }):
        raise RuntimeError("attempt2_bridge_receipt_hash_invalid")
    pending = [{
        "source_r8_configuration_id": row["configuration_id"],
        "capability": row["capability_task"],
        "arm": row["arm"],
        "evidence_status": "PENDING_COMPLETION_LIVE_TOKEN",
        "scientific_analysis_eligible": False,
    } for row in load_completion_rows()]
    core = {
        "schema_version": "wp016a.r08.combined-six-cell-ledger.v2",
        "status": "TEMPLATE_PENDING_FIVE_CELL_COMPLETION",
        "attempt2_bridge_receipt_sha256": bridge["receipt_sha256"],
        "cells": [{
            "source_r8_configuration_id": bridge["source_r8_configuration_id"],
            "capability": bridge["capability"],
            "arm": bridge["arm"],
            "terminal": bridge["terminal"],
            "integration_status": bridge["integration_status"],
            "proposal_outcome": bridge["proposal_outcome"],
            "terminal_origin": bridge["terminal_origin"],
            **{
                key: bridge[key] for key in (
                    "configuration_id", "cell_id", "session_id", "run_id",
                    "log_id", "output_id",
                )
            },
            "shared_integration_defect": False,
            "receipt_firewalls_complete": True,
            "evidence_status": "HASH_BOUND_ATTEMPT2_EVIDENCE",
            "scientific_analysis_eligible": False,
        }, *pending],
        "combined_pass_rule": {
            "configuration_count": 6,
            "all_integration_status_complete": True,
            "shared_integration_failures": 0,
            "all_six_runtime_identities_unique": True,
            "all_receipt_firewalls_complete": True,
            "proposal_commit_not_required": True,
        },
        "scientific_analysis_eligible": False,
        "wp016b_authorised": False,
    }
    return core | {"ledger_sha256": sha256(core)}


def combine_completion_evidence(
    bridge: Mapping[str, Any], completion_result: Mapping[str, Any],
) -> dict[str, Any]:
    count = int(completion_result.get("configurations_replayed", -1))
    if not 0 <= count <= 5:
        raise RuntimeError("completion_result_cell_count_invalid")
    completion_cells = list(completion_result.get("cells") or ())
    expected_sources = list(COMPLETION_SOURCE_IDS[:count])
    if (
        len(completion_cells) != count
        or [cell.get("source_r8_configuration_id") for cell in completion_cells]
        != expected_sources
    ):
        raise RuntimeError("completion_result_source_set_mismatch")
    first = combined_ledger_template(bridge)["cells"][0]
    rows = [first, *completion_cells]
    sources = [str(row["source_r8_configuration_id"]) for row in rows]
    shared = sum(bool(row.get("shared_integration_defect")) for row in rows)
    complete = sum(
        row.get("integration_status")
        == IntegrationStatus.COMPLETE_TYPED_CELL_OUTCOME.value
        for row in rows
    )
    identity_keys = ("configuration_id", "cell_id", "session_id", "run_id", "log_id", "output_id")
    identities_unique = all(
        all(isinstance(row.get(key), str) and row.get(key) for row in rows)
        and len({str(row[key]) for row in rows}) == len(rows)
        for key in identity_keys
    )
    identity_counts = {
        key: len({str(row.get(key)) for row in rows}) for key in identity_keys
    }
    live = str(completion_result.get("mode", "")).startswith("LIVE_")
    completion_firewalls = all(
        _completion_cell_firewalls_complete(cell, live=live)
        for cell in completion_cells
    )
    firewalls_complete = (
        bridge.get("provider_parser_guard_cache_identity_firewalls") == "PASS"
        and completion_firewalls
    )
    all_non_scientific = all(
        row.get("scientific_analysis_eligible") is False for row in rows
    )
    status = (
        "PASS_WP016A_INTEGRATION_COVERAGE"
        if (
            len(rows) == 6 and len(set(sources)) == 6 and complete == 6
            and shared == 0 and identities_unique and firewalls_complete
            and all_non_scientific
        )
        else "NO_GO_WP016A_INTEGRATION_COVERAGE"
    )
    core = {
        "schema_version": "wp016a.r08.combined-six-cell-ledger.v2",
        "status": status,
        "attempt2_bridge_receipt_sha256": bridge["receipt_sha256"],
        "cells": rows,
        "configuration_count": len(set(sources)),
        "integration_complete_count": complete,
        "shared_integration_failures": shared,
        "proposal_outcome_counts": dict(Counter(str(row.get("proposal_outcome")) for row in rows)),
        "all_present_runtime_identities_unique": identities_unique,
        "runtime_identity_unique_counts": identity_counts,
        "all_receipt_firewalls_complete": firewalls_complete,
        "all_evidence_non_scientific": all_non_scientific,
        "scientific_analysis_eligible": False,
        "wp016b_authorised": False,
    }
    return core | {"ledger_sha256": sha256(core)}


def combine_r10_completion_evidence(
    attempt2_bridge: Mapping[str, Any],
    r09_bridge: Mapping[str, Any],
    completion_result: Mapping[str, Any],
) -> dict[str, Any]:
    if (
        r09_bridge.get("schema_version") != "wp016a.r10.r09-derived-bridge.v1"
        or r09_bridge.get("original_status_rewritten") is not False
        or r09_bridge.get("used_as_six_configuration_completion_evidence") is not False
    ):
        raise RuntimeError("r09_bridge_invalid")
    historical = combine_completion_evidence(attempt2_bridge, completion_result)
    core = {
        key: value for key, value in historical.items() if key != "ledger_sha256"
    }
    core.update({
        "schema_version": "wp016a.r10.combined-six-cell-ledger.v1",
        "revision": R10_REVISION,
        "r09_historical_bridge_receipt_sha256": r09_bridge["receipt_sha256"],
        "r09_used_as_coverage_cell": False,
    })
    return core | {"ledger_sha256": sha256(core)}


def _completion_cell_firewalls_complete(cell: Mapping[str, Any], *, live: bool) -> bool:
    cache = cell.get("cache_evidence")
    tier = cell.get("service_tier_evidence")
    guards = cell.get("guard_maxima")
    request_ids = cell.get("request_ids")
    if not all(isinstance(value, Mapping) for value in (cache, tier, guards)):
        return False
    returned_tiers = tier.get("returned")
    if not isinstance(returned_tiers, list):
        return False
    try:
        scientific_bytes = int(guards.get("scientific_guard_input_bytes"))
        request_body_bytes = int(guards.get("canonical_request_body_bytes"))
    except (TypeError, ValueError):
        return False
    if (
        cache.get("application_response_cache") is not False
        or cache.get("payload_response_deduplication") is not False
        or cache.get("prior_response_reuse") is not False
        or cache.get("provider_prompt_cache_read_tokens") != 0
        or cache.get("provider_prompt_cache_write_tokens") != 0
        or tier.get("requested") != "default"
        or any(value != "default" for value in returned_tiers)
        or len(returned_tiers) != cell.get("request_count")
        or scientific_bytes > 65_536
        or request_body_bytes > 79_000
        or cell.get("unknown_cost_attempts") != 0
        or not isinstance(request_ids, list)
        or len(request_ids) != cell.get("request_count")
    ):
        return False
    for row in request_ids:
        if (
            not isinstance(row, Mapping)
            or not row.get("client_request_id")
            or not row.get("response_object_id")
            or (live and not row.get("openai_x_request_id"))
        ):
            return False
    return True


async def run_completion(*, live: bool, scientific_input_manifest: Path,
                         run_nonce: str,
                         journal: Any | None = None,
                         fixture_sequences: Mapping[str, Sequence[bytes]] | None = None) -> dict[str, Any]:
    return await _run_rows(
        live=live,
        scientific_input_manifest=scientific_input_manifest,
        rows=load_completion_rows(),
        expected_cells=5,
        request_cap=COMPLETION_REQUEST_CAP,
        result_kind="COMPLETION_CANARY",
        run_nonce=run_nonce,
        journal=journal,
        fixture_sequences=fixture_sequences,
    )


def offline_replay_receipt(result: Mapping[str, Any]) -> dict[str, Any]:
    if (
        result.get("status") != "PASS_COMPLETION_INTEGRATION"
        or result.get("configurations_replayed") != 5
        or result.get("provider_calls") != 0
        or result.get("backend_calls") != 0
        or result.get("run_nonce_sha256") != sha256(OFFLINE_REPLAY_NONCE.encode())
    ):
        raise RuntimeError("r08_offline_replay_result_invalid")
    core = {
        "schema_version": "wp016a.r08.five-cell-offline-replay.v1",
        "wp_id": "WP-016A",
        "revision": R08_REVISION,
        "offline_replay_nonce": OFFLINE_REPLAY_NONCE,
        "result": dict(result),
        "provider_calls": 0,
        "backend_calls": 0,
        "scientific_analysis_eligible": False,
        "wp016b_authorised": False,
    }
    return core | {"receipt_sha256": sha256(core)}


def r10_offline_replay_receipt(result: Mapping[str, Any]) -> dict[str, Any]:
    if (
        result.get("status") != "PASS_COMPLETION_INTEGRATION"
        or result.get("configurations_replayed") != 5
        or result.get("provider_calls") != 0
        or result.get("backend_calls") != 0
        or result.get("run_nonce_sha256")
        != sha256(R10_OFFLINE_REPLAY_NONCE.encode())
    ):
        raise RuntimeError("r10_offline_replay_result_invalid")
    core = {
        "schema_version": "wp016a.r10.five-cell-offline-replay.v1",
        "wp_id": "WP-016A",
        "revision": R10_REVISION,
        "offline_replay_nonce": R10_OFFLINE_REPLAY_NONCE,
        "result": dict(result),
        "provider_calls": 0,
        "backend_calls": 0,
        "scientific_analysis_eligible": False,
        "wp016b_authorised": False,
    }
    return core | {"receipt_sha256": sha256(core)}


def completion_static_manifests() -> dict[str, Any]:
    """Compatibility surface for the accepted R07 generic filenames."""
    historical = ROOT / "configs" / "wp016a" / "completion"
    names = (
        "terminal-outcome-taxonomy.json",
        "five-cell-completion-manifest.json",
        "completion-budget-guard.json",
    )
    return {name: read_json(historical / name) for name in names}


def r08_static_manifests(
    *, bridge: Mapping[str, Any], provider_binding: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        R08_COMPLETION_MANIFEST: completion_manifest(),
        R08_TERMINAL_TAXONOMY: taxonomy_manifest(),
        R08_ATTEMPT2_BRIDGE: dict(bridge),
        R08_COMBINED_TEMPLATE: combined_ledger_template(bridge),
        R08_PROVIDER_BINDING: dict(provider_binding or provider_binding_receipt()),
        R08_BUDGET_GUARD: budget_guard_receipt(COMPLETION_REQUEST_CAP),
    }


def completion_identity_bindings(run_nonce: str) -> list[dict[str, str]]:
    return [{
        kind + "_id": wp016_identity(kind, row["configuration_id"], run_nonce)
        for kind in ("configuration", "cell", "session", "run", "log", "output")
    } for row in load_completion_rows()]


def completion_controller_source_sha256(entrypoint: Path) -> str:
    return sha256({
        "common_controller_sha256": controller_source_sha256(entrypoint),
        "completion_module_sha256": file_sha256(Path(__file__).resolve()),
        "outcome_taxonomy_module_sha256": file_sha256(
            Path(__file__).with_name("wp016_outcomes.py")
        ),
        "completion_token_schema_sha256": file_sha256(COMPLETION_TOKEN_SCHEMA),
    })


def _validate_time_window(token: Mapping[str, Any]) -> None:
    try:
        issued = datetime.fromisoformat(str(token["issued_at"]).replace("Z", "+00:00"))
        expires = datetime.fromisoformat(str(token["expires_at"]).replace("Z", "+00:00"))
    except (KeyError, ValueError) as exc:
        raise RuntimeError("completion_token_time_invalid") from exc
    now = datetime.now(timezone.utc)
    if issued.tzinfo is None or expires.tzinfo is None or not issued <= now < expires:
        raise RuntimeError("completion_token_expired_or_not_yet_valid")


def _verify_json_exact(path: Path, expected: Mapping[str, Any], reason: str) -> None:
    if read_json(path) != expected:
        raise RuntimeError(reason)


def validate_completion_live_token(
    token_path: Path, *, controller_path: Path, manifest_dir: Path,
    attempt2_canary_result: Path, attempt2_bridge_path: Path,
    combined_ledger_template_path: Path, offline_replay_receipt_path: Path,
    scientific_input_manifest: Path, environment_manifest: Path,
    runtime_manifest_path: Path, runtime_base: Path,
) -> dict[str, Any]:
    token, schema = read_json(token_path), read_json(COMPLETION_TOKEN_SCHEMA)
    errors = sorted(
        error.message for error in Draft202012Validator(schema).iter_errors(token)
    )
    if errors:
        raise RuntimeError(
            "completion_activation_token_schema_invalid:" + "|".join(errors)
        )
    _validate_time_window(token)
    commit, tree = clean_repository_identity()

    expected_bridge = attempt2_bridge_receipt(
        read_json(attempt2_canary_result),
        source_file_sha256=file_sha256(attempt2_canary_result),
    )
    _verify_json_exact(
        attempt2_bridge_path, expected_bridge, "attempt2_bridge_v2_drift",
    )
    expected_template = combined_ledger_template(expected_bridge)
    _verify_json_exact(
        combined_ledger_template_path, expected_template,
        "combined_ledger_template_v2_drift",
    )
    _verify_json_exact(
        manifest_dir / R08_COMPLETION_MANIFEST, completion_manifest(),
        "completion_five_cell_manifest_drift",
    )
    _verify_json_exact(
        manifest_dir / R08_TERMINAL_TAXONOMY, taxonomy_manifest(),
        "completion_terminal_taxonomy_drift",
    )
    _verify_json_exact(
        manifest_dir / R08_BUDGET_GUARD,
        budget_guard_receipt(COMPLETION_REQUEST_CAP),
        "completion_budget_guard_drift",
    )
    validate_credential_binding(manifest_dir / R08_PROVIDER_BINDING)
    _verify_json_exact(
        manifest_dir / R08_PROVIDER_BINDING, provider_binding_receipt(),
        "completion_provider_binding_contract_drift",
    )
    if file_sha256(manifest_dir / R08_PROVIDER_BINDING) != token.get(
        "provider_binding_sha256"
    ):
        raise RuntimeError("completion_provider_binding_hash_drift")
    verify_scientific_input_manifest(scientific_input_manifest)
    verify_environment_manifest(environment_manifest, repository_root=ROOT)
    runtime = verify_runtime_manifest(runtime_manifest_path, runtime_base=runtime_base)
    if runtime.get("cell_subdirectory_count") != 5:
        raise RuntimeError("completion_runtime_manifest_not_five_cells")

    replay = read_json(offline_replay_receipt_path)
    if (
        replay.get("schema_version") != "wp016a.r08.five-cell-offline-replay.v1"
        or replay.get("offline_replay_nonce") != OFFLINE_REPLAY_NONCE
        or replay.get("provider_calls") != 0
        or replay.get("backend_calls") != 0
        or replay.get("receipt_sha256") != sha256({
            key: value for key, value in replay.items() if key != "receipt_sha256"
        })
    ):
        raise RuntimeError("completion_offline_replay_receipt_invalid")

    expected = {
        "approved_commit": commit,
        "approved_tree": tree,
        "controller_sha256": completion_controller_source_sha256(controller_path),
        "completion_manifest_sha256": file_sha256(
            manifest_dir / R08_COMPLETION_MANIFEST
        ),
        "terminal_taxonomy_sha256": file_sha256(
            manifest_dir / R08_TERMINAL_TAXONOMY
        ),
        "attempt2_bridge_receipt_sha256": file_sha256(attempt2_bridge_path),
        "combined_ledger_template_sha256": file_sha256(
            combined_ledger_template_path
        ),
        "provider_binding_sha256": file_sha256(
            manifest_dir / R08_PROVIDER_BINDING
        ),
        "budget_guard_sha256": file_sha256(manifest_dir / R08_BUDGET_GUARD),
        "offline_replay_receipt_sha256": file_sha256(
            offline_replay_receipt_path
        ),
        "scientific_input_manifest_sha256": file_sha256(
            scientific_input_manifest
        ),
        "environment_manifest_sha256": file_sha256(environment_manifest),
        "runtime_manifest_sha256": file_sha256(runtime_manifest_path),
        "literal_usd_cap": float(LITERAL_USD_CAP),
        "maximum_provider_requests": COMPLETION_REQUEST_CAP,
    }
    mismatch = {
        key: {"expected": value, "observed": token.get(key)}
        for key, value in expected.items() if token.get(key) != value
    }
    if mismatch:
        raise RuntimeError(
            "completion_activation_token_hash_mismatch:"
            + json.dumps(mismatch, sort_keys=True)
        )
    return token


__all__ = [
    "ATTEMPT2_G_LOW_SOURCE", "ATTEMPT2_PACKAGE_SHA256", "COMPLETION_REQUEST_CAP",
    "COMPLETION_SOURCE_IDS", "COMPLETION_TOKEN_SCHEMA", "OFFLINE_REPLAY_NONCE",
    "R08_ATTEMPT2_BRIDGE", "R08_BUDGET_GUARD", "R08_COMBINED_TEMPLATE",
    "R08_COMPLETION_MANIFEST", "R08_OFFLINE_REPLAY", "R08_PROVIDER_BINDING",
    "R08_TERMINAL_TAXONOMY", "attempt2_bridge_receipt",
    "R10_OFFLINE_REPLAY_NONCE", "R10_REVISION", "combine_completion_evidence",
    "combine_r10_completion_evidence", "combined_ledger_template",
    "completion_controller_source_sha256", "completion_identity_bindings",
    "completion_manifest", "completion_static_manifests", "load_completion_rows",
    "offline_replay_receipt", "r09_bridge_receipt", "r10_completion_manifest",
    "r10_offline_replay_receipt", "r08_static_manifests", "run_completion",
    "validate_completion_live_token",
]
