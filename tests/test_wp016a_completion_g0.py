from __future__ import annotations

import asyncio
import copy
from decimal import Decimal
import json
import os
from pathlib import Path
from typing import Any

import pytest

from optimizer_pipeline.dof.wp016 import canonical_bytes, sha256
from optimizer_pipeline.dof.wp016_canary import (
    CanaryController, _load_fixture_sequences, conservative_next_request_usd,
    file_sha256,
)
from optimizer_pipeline.dof.wp016_completion import (
    ATTEMPT2_G_LOW_SOURCE, COMPLETION_REQUEST_CAP, COMPLETION_SOURCE_IDS,
    attempt2_bridge_receipt, combine_completion_evidence,
    combined_ledger_template, completion_identity_bindings,
    completion_manifest, load_completion_rows, run_completion,
)
from optimizer_pipeline.dof.wp016_inputs import verify_scientific_input_manifest
from optimizer_pipeline.dof.wp016_outcomes import (
    IntegrationStatus, TerminalOrigin, classify_terminal, taxonomy_manifest,
)


ROOT = Path(__file__).resolve().parents[1]


def _scientific_manifest() -> Path:
    value = os.environ.get("WP016A_TEST_SCIENTIFIC_INPUT_MANIFEST")
    if not value:
        pytest.skip("external WP-016A replay capsule not supplied")
    return Path(value)


def _attempt2_source() -> dict[str, Any]:
    turns = [{
        "returned_model": "openai/gpt-5.6-luna",
        "returned_route": "openai_direct",
        "returned_service_tier": "default",
        "provider_prompt_cache_read_tokens": 0,
        "provider_prompt_cache_write_tokens": 0,
        "client_request_id": f"wp016a-client__attempt2-{index}",
        "openai_x_request_id": f"req_attempt2-{index}",
        "response_object_id": f"chatcmpl-attempt2-{index}",
    } for index in range(4)]
    status = "NO_GO_INVALID_CANARY_TERMINAL_TURN_LIMIT_EXHAUSTED"
    return {
        "status": status,
        "result": {
            "status": status,
            "provider_calls": 4,
            "backend_calls": 0,
            "charged_cost_usd": "0.0054008",
            "cells": [{
                "configuration_id": "wp016a-configuration__2a21905a4b7af17be7fc07f9",
                "cell_id": "wp016a-cell__e82de9d70d5442fb54e2ed45",
                "session_id": "wp016a-session__46321b6867b68574f2900258",
                "run_id": "wp016a-run__77130f13f55da6f7a90871e0",
                "log_id": "wp016a-log__44493377d06e914374eeb349",
                "output_id": "wp016a-output__cce3c0b75398460e47e648ef",
                "source_r8_configuration_id": ATTEMPT2_G_LOW_SOURCE,
                "capability": "G",
                "arm": "COMPACT_RETRIEVAL_LOW_RESERVE",
                "terminal": "TURN_LIMIT_EXHAUSTED",
                "request_count": 4,
                "request_ids": [{
                    key: turn[key] for key in (
                        "client_request_id", "openai_x_request_id",
                        "response_object_id",
                    )
                } for turn in turns],
                "turns": turns,
            }],
        },
    }


def _mutated_completion_sequences(
    endpoint: Any, *, completion_tokens: int = 8, finish_reason: str = "stop",
) -> dict[str, list[bytes]]:
    fixtures = {key: list(value) for key, value in _load_fixture_sequences().items()}
    source = COMPLETION_SOURCE_IDS[0]
    template = json.loads(fixtures[source][0])
    template["choices"][0]["message"]["content"] = (
        endpoint if isinstance(endpoint, str) else json.dumps(endpoint)
    )
    template["choices"][0]["finish_reason"] = finish_reason
    template["usage"]["completion_tokens"] = completion_tokens
    fixtures[source] = [canonical_bytes(template)]
    return fixtures


def test_turn_limit_is_integration_complete_but_proposal_negative() -> None:
    value = classify_terminal("TURN_LIMIT_EXHAUSTED")
    assert value.integration_status is IntegrationStatus.COMPLETE_TYPED_CELL_OUTCOME
    assert value.terminal_origin is TerminalOrigin.FINITE_PROTOCOL_BUDGET
    assert value.proposal_outcome == "NO_PROPOSAL_TURN_LIMIT"
    assert value.shared_integration_defect is False


def test_semantic_limit_is_integration_complete_finite_budget() -> None:
    value = classify_terminal("SEMANTIC_RESERVE_EXHAUSTED")
    assert value.integration_status is IntegrationStatus.COMPLETE_TYPED_CELL_OUTCOME
    assert value.proposal_outcome == "NO_PROPOSAL_SEMANTIC_LIMIT"


@pytest.mark.parametrize(
    "endpoint,tokens,finish,terminal",
    [
        ({"endpoint": "LIST_CAPABILITIES"}, 4097, "stop", "OUTPUT_LIMIT_EXHAUSTED"),
        ({"endpoint": "LIST_CAPABILITIES"}, 8, "length", "TRUNCATED_RESPONSE"),
        ("not-json", 8, "stop", "MODEL_CONTENT_JSON_INVALID"),
    ],
)
def test_typed_negative_model_or_output_outcome_does_not_abort_later_cells(
    endpoint: Any, tokens: int, finish: str, terminal: str,
) -> None:
    result = asyncio.run(run_completion(
        live=False,
        scientific_input_manifest=_scientific_manifest(),
        run_nonce="wp016a-completion-negative-fixture",
        fixture_sequences=_mutated_completion_sequences(
            endpoint, completion_tokens=tokens, finish_reason=finish,
        ),
    ))
    assert result["status"] == "PASS_COMPLETION_INTEGRATION"
    assert result["cells"][0]["terminal"] == terminal
    assert result["cells"][0]["shared_integration_defect"] is False
    assert result["configurations_replayed"] == 5
    assert result["later_cells_not_executed"] == 0


@pytest.mark.parametrize(
    "message,origin",
    [
        ("provider_identity_mismatch", "PROVIDER_OR_TRANSPORT"),
        ("NO_GO_PROVIDER_CACHE_POLICY_DRIFT", "PROVIDER_OR_TRANSPORT"),
        ("compact_response_schema_invalid", "CONTROLLER_INVARIANT"),
    ],
)
def test_shared_failure_aborts_later_cells(
    monkeypatch: pytest.MonkeyPatch, message: str, origin: str,
) -> None:
    async def fail(*args: Any, **kwargs: Any) -> dict[str, Any]:
        raise RuntimeError(message)

    monkeypatch.setattr(CanaryController, "run_cell", fail)
    result = asyncio.run(run_completion(
        live=False,
        scientific_input_manifest=_scientific_manifest(),
        run_nonce="wp016a-completion-shared-failure",
    ))
    assert result["status"].startswith("NO_GO_")
    assert result["terminal_origin"] == origin
    assert result["configurations_replayed"] == 0
    assert result["later_cells_not_executed"] == 5
    assert result["unresolved_shared_integration_defect"] is True


def test_unknown_terminal_fails_closed_and_stops_later_cells(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def unknown(self: CanaryController, **kwargs: Any) -> dict[str, Any]:
        outcome = classify_terminal("UNRECOGNISED_NEW_TERMINAL")
        ids = {
            kind + "_id": "wp016a-" + kind + "__unknown"
            for kind in ("configuration", "cell", "session", "run", "log", "output")
        }
        return {
            "terminal": "UNRECOGNISED_NEW_TERMINAL",
            **outcome.public(), **ids, "turns": [],
        }

    monkeypatch.setattr(CanaryController, "run_cell", unknown)
    result = asyncio.run(run_completion(
        live=False,
        scientific_input_manifest=_scientific_manifest(),
        run_nonce="wp016a-completion-unknown-terminal",
    ))
    assert result["status"] == "NO_GO_SHARED_INTEGRATION_FAILURE_UNRECOGNISED_NEW_TERMINAL"
    assert result["cells"][0]["terminal_origin"] == "UNKNOWN"
    assert result["later_cells_not_executed"] == 4


def test_attempt2_bridge_preserves_original_no_go_status() -> None:
    bridge = attempt2_bridge_receipt(_attempt2_source())
    assert bridge["original_status"] == "NO_GO_INVALID_CANARY_TERMINAL_TURN_LIMIT_EXHAUSTED"
    assert bridge["original_status_rewritten"] is False
    assert bridge["integration_status"] == "COMPLETE_TYPED_CELL_OUTCOME"
    assert bridge["proposal_outcome"] == "NO_PROPOSAL_TURN_LIMIT"
    assert bridge["scientific_analysis_eligible"] is False


def test_five_cell_manifest_excludes_attempt2_g_low() -> None:
    manifest = completion_manifest()
    assert len(manifest["cells"]) == 5
    assert tuple(row["source_r8_configuration_id"] for row in manifest["cells"]) == COMPLETION_SOURCE_IDS
    assert ATTEMPT2_G_LOW_SOURCE not in COMPLETION_SOURCE_IDS
    assert manifest["excluded_attempt2_configuration"] == ATTEMPT2_G_LOW_SOURCE


def test_completion_identities_are_fresh_and_collision_free() -> None:
    first = completion_identity_bindings("completion-token-a")
    second = completion_identity_bindings("completion-token-b")
    assert len(first) == len(second) == 5
    for key in ("configuration_id", "cell_id", "session_id", "run_id", "log_id", "output_id"):
        assert len({row[key] for row in first}) == 5
        assert {row[key] for row in first}.isdisjoint({row[key] for row in second})
    assert all(row["cell_id"] != "wp016a-cell__e82de9d70d5442fb54e2ed45" for row in first)


def test_completion_request_and_literal_budget_are_exact() -> None:
    assert COMPLETION_REQUEST_CAP == 20
    assert conservative_next_request_usd() * COMPLETION_REQUEST_CAP == Decimal("0.4143040")
    assert conservative_next_request_usd() * COMPLETION_REQUEST_CAP < Decimal("0.50")
    assert completion_manifest()["literal_usd_cap"] == 0.5


def test_all_five_configurations_replay_offline_with_zero_calls() -> None:
    result = asyncio.run(run_completion(
        live=False,
        scientific_input_manifest=_scientific_manifest(),
        run_nonce="wp016a-completion-five-offline",
    ))
    assert result["schema_version"] == "wp016a.completion-controller-replay.v1"
    assert result["status"] == "PASS_COMPLETION_INTEGRATION"
    assert result["configurations_replayed"] == 5
    assert result["provider_calls"] == result["backend_calls"] == 0
    assert all(cell["integration_status"] == "COMPLETE_TYPED_CELL_OUTCOME" for cell in result["cells"])


def test_combined_ledger_has_exact_six_non_scientific_cells() -> None:
    bridge = attempt2_bridge_receipt(_attempt2_source())
    result = asyncio.run(run_completion(
        live=False,
        scientific_input_manifest=_scientific_manifest(),
        run_nonce="wp016a-completion-combined-offline",
    ))
    ledger = combine_completion_evidence(bridge, result)
    assert ledger["status"] == "PASS_WP016A_INTEGRATION_COVERAGE"
    assert ledger["configuration_count"] == ledger["integration_complete_count"] == 6
    assert ledger["shared_integration_failures"] == 0
    assert len(ledger["cells"]) == 6
    assert all(cell["scientific_analysis_eligible"] is False for cell in ledger["cells"])
    assert ledger["wp016b_authorised"] is False


def test_combined_template_references_bridge_and_five_pending_cells() -> None:
    bridge = attempt2_bridge_receipt(_attempt2_source())
    template = combined_ledger_template(bridge)
    assert template["status"] == "TEMPLATE_PENDING_FIVE_CELL_COMPLETION"
    assert template["attempt2_bridge_receipt_sha256"] == bridge["receipt_sha256"]
    assert len(template["cells"]) == 6
    assert sum(row["evidence_status"] == "PENDING_COMPLETION_LIVE_TOKEN" for row in template["cells"]) == 5


def test_taxonomy_is_explicit_and_unknown_fails_closed() -> None:
    manifest = taxonomy_manifest()
    assert set(manifest["terminal_origins"]) == {item.value for item in TerminalOrigin}
    assert manifest["unknown_terminal_policy"]["action"] == "STOP_ALL_LATER_CELLS"
    assert classify_terminal("NEVER_SEEN").shared_integration_defect is True


def test_completion_rows_preserve_root_model_turn_and_reserves() -> None:
    rows = load_completion_rows()
    assert {row["root_id"] for row in rows} == {"transfer_boom_soc_v2"}
    assert {row["model_system"] for row in rows} == {"openai/gpt-5.6-luna"}
    assert {int(row["turn_limit"]) for row in rows} == {4}
    by_arm = {(row["capability_task"], row["arm"]): int(row["active_dynamic_capacity_bytes"]) for row in rows}
    assert by_arm[("P", "COMPACT_RETRIEVAL_LOW_RESERVE")] == 25351
    assert by_arm[("G", "COMPACT_RETRIEVAL_MATCHED_RESERVE")] == 58278
    assert by_arm[("P", "COMPACT_RETRIEVAL_MATCHED_RESERVE")] == 58278


def test_protected_root_inputs_remain_hash_bound() -> None:
    roles = verify_scientific_input_manifest(_scientific_manifest())
    expected = {
        "real_observation": "0061f034eca26734a81403cb9faa9e10515c12f6a833b48cce15dc6f5c7d772d",
        "resolved_private_objects": "2bcec4b4078efe2fe39d982163acb76f6b24908dcdc4f99c8c6e8194b943971a",
        "rapidwright_net_census": "50a2560fa618cadcb10c627be1c9bf21da5b4f47b6320047930ec5b892084178",
    }
    assert {role: file_sha256(roles[role]) for role in expected} == expected


def test_negative_fixture_registry_is_non_scientific_and_zero_call() -> None:
    path = ROOT / "configs/wp016a/completion/outcome-negative-fixtures.json"
    value = json.loads(path.read_text())
    assert value["provider_calls"] == value["backend_calls"] == 0
    assert value["scientific_analysis_eligible"] is False
    assert value["wp016b_authorised"] is False
    assert {row["expected_terminal_origin"] for row in value["fixtures"]} >= {
        "FINITE_PROTOCOL_BUDGET", "MODEL_OUTPUT", "PROVIDER_OR_TRANSPORT",
        "CONTROLLER_INVARIANT", "UNKNOWN",
    }


def test_live_completion_is_not_authorised_at_g0() -> None:
    manifest = completion_manifest()
    assert manifest["status"] == "READY_FOR_FIVE_CELL_LIVE_TOKEN_REVIEW"
    assert manifest["provider_calls_at_r08"] == manifest["backend_calls_at_r08"] == 0
    assert manifest["wp016b_authorised"] is False
