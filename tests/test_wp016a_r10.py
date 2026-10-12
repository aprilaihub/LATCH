from __future__ import annotations

import asyncio
from dataclasses import replace
import json
import os
from pathlib import Path
from typing import Any

import pytest

from optimizer_pipeline.dof.multi_provider import (
    MAX_CANONICAL_INPUT_BYTES, PROFILES, ProviderInferenceContractError,
    compile_request_body,
)
from optimizer_pipeline.dof.provider import (
    ProviderBindingError, ProviderRequestBinding, ProviderSettings,
)
from optimizer_pipeline.dof.wp016 import sha256
import optimizer_pipeline.dof.wp016_canary as canary
from optimizer_pipeline.dof.wp016_completion import (
    COMPLETION_REQUEST_CAP, COMPLETION_SOURCE_IDS, R10_OFFLINE_REPLAY_NONCE,
    attempt2_bridge_receipt, combine_r10_completion_evidence,
    r09_bridge_receipt, r10_completion_manifest, r10_offline_replay_receipt,
    run_completion,
)
from optimizer_pipeline.dof.wp016_outcomes import (
    IntegrationStatus, TerminalOrigin, classify_terminal, taxonomy_manifest,
)


ROOT = Path(__file__).resolve().parents[1]


def _scientific_manifest() -> Path:
    value = os.environ.get("WP016A_TEST_SCIENTIFIC_INPUT_MANIFEST")
    if not value:
        pytest.skip("external WP-016A replay capsule not supplied")
    return Path(value)


def _r10_sources() -> Path:
    value = os.environ.get("WP016A_R10_SOURCE_DIR")
    if not value:
        pytest.skip("R10 reviewed source directory not supplied")
    return Path(value)


def _binding(*, prompt: bytes = b"system", context: bytes = b"context") -> ProviderRequestBinding:
    token = "1" * 64
    return ProviderRequestBinding(
        run_id="r10-unit", spec_hash=token, experiment_id="r10-unit",
        condition_id="r10-condition", repeat_id="r10-repeat",
        decision_id="decision-r10", context_bytes=context, prompt_bytes=prompt,
        prompt_version="r10", action_interface_version="r10",
        action_interface_hash=token, context_interface_version="r10",
        context_interface_hash=token, catalogue_version="r10",
        catalogue_hash=token, capability_hash=token, mask_evidence_hash=token,
        provider_role="r10", provider="openai",
        model_id="openai/gpt-5.6-luna",
        adapter_version="dof.wp014e.openai-direct.v1",
        settings=ProviderSettings(
            temperature=None, max_tokens=4096, seed=None,
            response_format="json_object", timeout_seconds=300.0,
            max_retries=0, retry_on=(), max_requests=1,
            max_usd=0.5, max_cost_per_request_usd=0.5,
            unknown_cost_policy="reject", unknown_usage_policy="reject",
        ),
    )


def _attempt2_source() -> dict[str, Any]:
    source = os.environ.get("WP016A_TEST_ATTEMPT2_CANARY_RESULT")
    if source:
        return json.loads(Path(source).read_text())
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
    return {"status": status, "result": {
        "status": status, "provider_calls": 4, "backend_calls": 0,
        "charged_cost_usd": "0.0054008", "cells": [{
            "configuration_id": "wp016a-configuration__2a21905a4b7af17be7fc07f9",
            "cell_id": "wp016a-cell__e82de9d70d5442fb54e2ed45",
            "session_id": "wp016a-session__46321b6867b68574f2900258",
            "run_id": "wp016a-run__77130f13f55da6f7a90871e0",
            "log_id": "wp016a-log__44493377d06e914374eeb349",
            "output_id": "wp016a-output__cce3c0b75398460e47e648ef",
            "source_r8_configuration_id": "c1r4-config__1a4c02ecb009dbf4e43c03bf",
            "capability": "G", "arm": "COMPACT_RETRIEVAL_LOW_RESERVE",
            "terminal": "TURN_LIMIT_EXHAUSTED", "request_count": 4,
            "request_ids": [{key: turn[key] for key in (
                "client_request_id", "openai_x_request_id", "response_object_id",
            )} for turn in turns], "turns": turns,
        }],
    }}


def test_split_contract_predicates_are_typed() -> None:
    profile = PROFILES["openai/gpt-5.6-luna"]
    cases = (
        (
            _binding(context=b"x" * MAX_CANONICAL_INPUT_BYTES),
            "CANONICAL_INPUT_LIMIT_EXCEEDED",
        ),
        (
            replace(_binding(), settings=replace(_binding().settings, max_tokens=4097)),
            "OUTPUT_TOKEN_CONTRACT_MISMATCH",
        ),
        (
            replace(_binding(), settings=replace(_binding().settings, temperature=0.0)),
            "TEMPERATURE_OR_SEED_CONTRACT_MISMATCH",
        ),
        (
            replace(_binding(), settings=replace(
                _binding().settings, max_retries=1, max_requests=2,
                retry_on=("PROVIDER_ERROR",),
            )),
            "RETRY_POLICY_CONTRACT_MISMATCH",
        ),
        (
            replace(_binding(), settings=replace(_binding().settings, max_requests=2)),
            "PHYSICAL_REQUEST_CONTRACT_MISMATCH",
        ),
    )
    for binding, expected in cases:
        with pytest.raises(ProviderInferenceContractError) as raised:
            compile_request_body(profile, binding)
        assert raised.value.contract_code == expected


def test_canonical_overflow_records_exact_safe_evidence() -> None:
    prompt = b"p" * 17
    context = b"c" * (MAX_CANONICAL_INPUT_BYTES - len(prompt) + 23)
    with pytest.raises(ProviderInferenceContractError) as raised:
        compile_request_body(
            PROFILES["openai/gpt-5.6-luna"],
            _binding(prompt=prompt, context=context),
        )
    evidence = dict(raised.value.evidence)
    assert evidence == {
        "prompt_bytes": len(prompt),
        "context_bytes": len(context),
        "canonical_input_bytes": len(prompt) + len(context),
        "limit_bytes": 65_536,
        "overage_bytes": 23,
        "prompt_sha256": __import__("hashlib").sha256(prompt).hexdigest(),
        "context_sha256": __import__("hashlib").sha256(context).hexdigest(),
    }


def test_context_limit_after_turn_is_typed_and_continues(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = canary._wp016_request_body
    calls = 0

    def inject(profile: Any, binding: Any) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        # Two historical-parity compilations precede the first cell; the
        # first cell then compiles turn 1 twice.  The fifth call is turn 2.
        if calls == 5:
            raise ProviderInferenceContractError(
                "CANONICAL_INPUT_LIMIT_EXCEEDED", {
                    "prompt_bytes": 1179,
                    "context_bytes": 65000,
                    "canonical_input_bytes": 66179,
                    "limit_bytes": 65536,
                    "overage_bytes": 643,
                    "prompt_sha256": sha256(b"prompt"),
                    "context_sha256": sha256(b"context"),
                },
            )
        return original(profile, binding)

    monkeypatch.setattr(canary, "_wp016_request_body", inject)
    result = asyncio.run(run_completion(
        live=False, scientific_input_manifest=_scientific_manifest(),
        run_nonce="wp016a-r10-context-limit-unit",
    ))
    assert result["status"] == "PASS_COMPLETION_INTEGRATION"
    assert result["configurations_replayed"] == 5
    first = result["cells"][0]
    assert first["terminal"] == "CONTEXT_LIMIT_AFTER_MODEL_TURN"
    assert first["terminal_origin"] == "FINITE_PROTOCOL_BUDGET"
    assert first["proposal_outcome"] == "NO_PROPOSAL_CONTEXT_LIMIT"
    assert first["integration_status"] == "COMPLETE_TYPED_CELL_OUTCOME"
    assert first["shared_integration_defect"] is False
    assert first["request_count"] == 1
    assert [row["turn"] for row in first["turns"]] == [1]
    assert first["context_limit_evidence"]["attempted_planning_turn"] == 2
    assert first["context_limit_evidence"]["last_provider_turn"] == 1
    assert first["context_limit_evidence"]["blocked_turn_request_identity_created"] is False
    assert result["provider_calls"] == result["backend_calls"] == 0


def test_non_context_contract_error_remains_shared_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = canary._wp016_request_body
    calls = 0

    def reject(_profile: Any, _binding: Any) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        if calls >= 3:
            raise ProviderInferenceContractError(
                "OUTPUT_TOKEN_CONTRACT_MISMATCH", {"binding_max_tokens": 1},
            )
        return original(_profile, _binding)

    monkeypatch.setattr(canary, "_wp016_request_body", reject)
    result = asyncio.run(run_completion(
        live=False, scientific_input_manifest=_scientific_manifest(),
        run_nonce="wp016a-r10-shared-contract-unit",
    ))
    assert result["status"] == "NO_GO_CONTROLLER_OR_ADAPTER"
    assert result["configurations_replayed"] == 0
    assert result["unresolved_shared_integration_defect"] is True


def test_unknown_contract_error_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = canary._wp016_request_body
    calls = 0

    def reject(_profile: Any, _binding: Any) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        if calls >= 3:
            raise ProviderBindingError("UNKNOWN_COMPOUND_CONTRACT_ERROR")
        return original(_profile, _binding)

    monkeypatch.setattr(canary, "_wp016_request_body", reject)
    result = asyncio.run(run_completion(
        live=False, scientific_input_manifest=_scientific_manifest(),
        run_nonce="wp016a-r10-unknown-contract-unit",
    ))
    assert result["status"] == "NO_GO_CONTROLLER_OR_ADAPTER"
    assert result["configurations_replayed"] == 0
    assert result["unresolved_shared_integration_defect"] is True


def test_taxonomy_contains_context_limit_and_unknown_fails_closed() -> None:
    outcome = classify_terminal("CONTEXT_LIMIT_AFTER_MODEL_TURN")
    assert outcome.terminal_origin is TerminalOrigin.FINITE_PROTOCOL_BUDGET
    assert outcome.integration_status is IntegrationStatus.COMPLETE_TYPED_CELL_OUTCOME
    assert outcome.proposal_outcome == "NO_PROPOSAL_CONTEXT_LIMIT"
    assert outcome.shared_integration_defect is False
    assert "CONTEXT_LIMIT_AFTER_MODEL_TURN" in taxonomy_manifest()["known_terminal_table"]
    assert classify_terminal("UNKNOWN_COMPOUND_CONTRACT_ERROR").shared_integration_defect is True


def test_r09_bridge_preserves_original_and_is_not_coverage() -> None:
    source = _r10_sources()
    bridge = r09_bridge_receipt(
        json.loads((source / "sources/completion-run-result.json").read_text()),
        json.loads((source / "sources/partial-run-failure.json").read_text()),
        json.loads((source / "sources/response-turn1.json").read_text()),
    )
    assert bridge["original_status"] == "NO_GO_PROVIDER_OR_TRANSPORT"
    assert bridge["original_status_rewritten"] is False
    assert bridge["turn2_failure_locus"] == "LOCAL_CONTROLLER_REQUEST_CONSTRUCTION"
    assert bridge["exact_typed_guard_evidence_available"] is False
    assert bridge["used_as_six_configuration_completion_evidence"] is False


def test_r10_offline_replay_and_combined_ledger_are_complete() -> None:
    result = asyncio.run(run_completion(
        live=False, scientific_input_manifest=_scientific_manifest(),
        run_nonce=R10_OFFLINE_REPLAY_NONCE,
    ))
    replay = r10_offline_replay_receipt(result)
    source = _r10_sources()
    r09 = r09_bridge_receipt(
        json.loads((source / "sources/completion-run-result.json").read_text()),
        json.loads((source / "sources/partial-run-failure.json").read_text()),
        json.loads((source / "sources/response-turn1.json").read_text()),
    )
    ledger = combine_r10_completion_evidence(
        attempt2_bridge_receipt(_attempt2_source()), r09, result,
    )
    assert replay["provider_calls"] == replay["backend_calls"] == 0
    assert ledger["status"] == "PASS_WP016A_INTEGRATION_COVERAGE"
    assert ledger["configuration_count"] == ledger["integration_complete_count"] == 6
    assert set(ledger["runtime_identity_unique_counts"].values()) == {6}
    assert ledger["r09_used_as_coverage_cell"] is False
    assert all(row["scientific_analysis_eligible"] is False for row in ledger["cells"])
    assert ledger["wp016b_authorised"] is False


def test_r10_frozen_inventory_and_absolute_budget() -> None:
    manifest = r10_completion_manifest()
    assert tuple(row["source_r8_configuration_id"] for row in manifest["cells"]) == COMPLETION_SOURCE_IDS
    assert manifest["maximum_provider_requests"] == COMPLETION_REQUEST_CAP == 20
    assert manifest["literal_usd_cap"] == 0.5
    assert manifest["retry_count"] == manifest["fallback_count"] == manifest["repair_count"] == 0
    assert manifest["scientific_analysis_eligible"] is False
    assert manifest["wp016b_authorised"] is False
