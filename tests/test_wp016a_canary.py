from __future__ import annotations

import asyncio
from decimal import Decimal
import json
import os
from pathlib import Path

import pytest

from optimizer_pipeline.dof.named_family import named_family_evidence_v2
from optimizer_pipeline.dof.wp016 import compact_protocol_definition, sha256
from optimizer_pipeline.dof.wp016_canary import (
    LITERAL_USD_CAP, REQUEST_CAP, LiteralBudget, budget_guard_receipt,
    canary_manifest, conservative_next_request_usd, current_openai_derived_cost,
    historical_first_turn_parity,
    load_scientific_inputs, load_source_rows, provider_binding_receipt, run_all,
    validate_live_token, wp016_identity,
)


def _scientific_manifest() -> Path:
    value = os.environ.get("WP016A_TEST_SCIENTIFIC_INPUT_MANIFEST")
    if not value:
        pytest.skip("external WP-016A replay capsule not supplied")
    return Path(value)


def _nonprefix_path() -> tuple[dict, dict]:
    candidate = {"language": "G", "steps": [
        {"step_id": "d1", "op": "DIFF",
         "args": {"left": {"ref": "left"}, "right": {"ref": "right"}}, "bind": "result"},
        {"step_id": "b1", "op": "BATCH_ROUTE_NETS",
         "args": {"typed_nets": {"ref": "result"}, "delay_mode": "AUTO_DELAY"}},
    ]}
    compiled = {
        "binding_records": [{
            "binding_id": "result", "binding_type": "net", "binding_cardinality": 3,
            "binding_content_hash": sha256(["b", "c", "d"]), "derivation": "DIFF",
            "left_binding_id": "left", "right_binding_id": "right",
            "left_content_hash": sha256(["a", "b", "c", "d"]),
            "removed_content_hash": sha256(["a"]),
            "left_cardinality": 4, "removed_cardinality": 1,
            "right_source_indices": [0], "result_source_indices": [1, 2, 3],
            "selected_source_indices": [1, 2, 3],
            "right_is_nonempty_proper_prefix_of_left": True,
            "result_is_exact_left_minus_right": True, "result_is_source_prefix": False,
        }],
        "host_steps": [{"source_step_id": "b1", "source_construct": "BATCH_ROUTE_NETS",
                        "input_bindings": [{"binding_id": "result"}]}],
    }
    return candidate, compiled


def test_exact_six_cell_manifest_and_fresh_identities() -> None:
    manifest = canary_manifest()
    assert len(manifest["cells"]) == 6
    assert manifest["literal_usd_cap"] == 0.5
    assert manifest["maximum_provider_requests"] == 24
    assert manifest["identity_nonce_source"].endswith("run_nonce")
    assert all(set(row["identity_templates"]) == {
        "configuration", "cell", "session", "run", "log", "output"
    } for row in manifest["cells"])
    assert all(row["source_r8_configuration_id"].startswith("c1r4-config__") for row in manifest["cells"])
    source = manifest["cells"][0]["source_r8_configuration_id"]
    assert wp016_identity("cell", source, "nonce-a") != wp016_identity("cell", source, "nonce-b")


def test_legacy_first_turn_exact_historical_parity() -> None:
    rows = [row for row in load_source_rows() if row["arm"] == "LEGACY_STATIC"]
    inputs = load_scientific_inputs(_scientific_manifest())
    receipts = [historical_first_turn_parity(row, inputs=inputs) for row in rows]
    assert {row["source_configuration_id"] for row in receipts} == {
        "c1r4-config__55cd0a630fa292e539ee6c64",
        "c1r4-config__e03630fa96b9b578012f8411",
    }
    assert all(row["exact_model_visible_prompt_context_parity"] for row in receipts)
    assert all(row["transport_policy_normalized"] for row in receipts)


def test_all_eight_compact_endpoints_remain_visible() -> None:
    protocol = compact_protocol_definition()
    assert set(protocol["request_schemas"]) == {
        "LIST_CAPABILITIES", "GET_ACTION_SPEC", "GET_SCHEMA_FRAGMENT", "GET_EXAMPLE",
        "QUERY_DESIGN", "VALIDATE_PROGRAM", "COMMIT", "NO_VALID_SUBMISSION",
    }


def test_redundant_diff_indices_must_match() -> None:
    candidate, compiled = _nonprefix_path()
    valid = named_family_evidence_v2(candidate, compiled)
    assert valid["named_family_valid"] is True
    compiled["binding_records"][0]["selected_source_indices"] = [0, 2, 3]
    invalid = named_family_evidence_v2(candidate, compiled)
    assert invalid["named_family_valid"] is False
    assert "redundant_result_selected_indices_mismatch" in (
        invalid["all_evaluated_paths"][0]["proposal_rejection_reasons"]
    )


def test_literal_budget_is_pre_request_and_never_exceeds_cap() -> None:
    next_max = conservative_next_request_usd()
    assert next_max * REQUEST_CAP <= LITERAL_USD_CAP
    budget = LiteralBudget(charged=LITERAL_USD_CAP - next_max, requests=REQUEST_CAP - 1)
    body = {"messages": [{"role": "system", "content": "p"},
                         {"role": "user", "content": "c"}]}
    budget.guard(b"p", b"c", body)
    budget.record(next_max)
    assert budget.charged == LITERAL_USD_CAP
    with pytest.raises(RuntimeError, match="request_cap_exhausted"):
        budget.guard(b"p", b"c", body)
    assert budget_guard_receipt()["literal_usd_cap"] == "0.50"


def test_current_openai_cost_separates_cache_read_write_and_output() -> None:
    cost = current_openai_derived_cost({
        "input_tokens": 1_000_000,
        "uncached_input_tokens": 500_000,
        "cached_input_tokens": 250_000,
        "cache_write_input_tokens": 250_000,
        "output_tokens": 1_000_000,
    })
    assert cost == Decimal("2.135")


def test_provider_binding_is_exact_direct_luna_with_no_recovery() -> None:
    receipt = provider_binding_receipt(credential="unit-test-secret")
    assert receipt["canonical_model_id"] == "openai/gpt-5.6-luna"
    assert receipt["provider"] == "openai" and receipt["route"] == "direct"
    assert receipt["retry_count"] == receipt["fallback_count"] == receipt["repair_count"] == 0
    assert receipt["application_cache"] is receipt["payload_response_deduplication"] is False
    assert receipt["prompt_cache_mode"] == "EXPLICIT_NO_BREAKPOINTS"
    assert receipt["service_tier"] == "default"


def test_six_configuration_replay_uses_exact_controller_and_zero_calls() -> None:
    result = asyncio.run(run_all(
        live=False, scientific_input_manifest=_scientific_manifest(),
    ))
    assert result["configurations_replayed"] == 6
    assert result["provider_calls"] == result["backend_calls"] == 0
    assert result["offline_parsed_requests"] == 22
    assert result["maximum_guard_usage_bytes"] <= 65_536
    assert set(result["unique_identity_counts"].values()) == {6}
    assert all(cell["terminal"] == "COMMIT_RECEIVED" for cell in result["cells"])
    compact = [cell for cell in result["cells"] if cell["arm"].startswith("COMPACT")]
    assert all([turn["canonical_event"] for turn in cell["turns"]] ==
               ["RETRIEVAL_OK", "OK", "OK", "COMMIT_RECEIVED"] for cell in compact)
    g_low = next(cell for cell in compact if cell["capability"] == "G" and "LOW" in cell["arm"])
    assert min(turn["semantic_remaining_bytes"] for turn in g_low["turns"]) >= 0
    legacy = [cell for cell in result["cells"] if cell["arm"] == "LEGACY_STATIC"]
    assert all(cell["request_count"] >= 2 for cell in legacy)
    assert all(cell["turns"][0]["previous_result_present"] is False for cell in legacy)
    assert all(cell["turns"][1]["previous_result_present"] is True for cell in legacy)
    assert max(cell["request_count"] for cell in result["cells"]) == 4
    assert result["unknown_cost_attempts"] == 0
    assert all(
        turn["pricing_snapshot_id"] == "wp016a-openai-current-pricing-2026-08-20"
        for cell in result["cells"] for turn in cell["turns"]
    )


def test_live_token_fails_closed_before_any_transport(tmp_path: Path) -> None:
    token = tmp_path / "token.json"
    replay = tmp_path / "replay.json"
    manifests = tmp_path / "manifests"
    controller = tmp_path / "controller.py"
    manifests.mkdir()
    token.write_text("{}")
    replay.write_text("{}")
    controller.write_text("# fixture\n")
    for name in ("canary-manifest.json", "provider-binding.json", "budget-guard.json"):
        (manifests / name).write_text("{}")
    with pytest.raises(RuntimeError, match="activation_token_schema_invalid"):
        validate_live_token(
            token, replay, manifests, controller,
            scientific_input_manifest=tmp_path / "science.json",
            environment_manifest=tmp_path / "environment.json",
            runtime_manifest_path=tmp_path / "runtime.json",
            runtime_base=tmp_path / "runtime-base",
        )
