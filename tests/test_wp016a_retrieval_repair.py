from __future__ import annotations

import csv
import hashlib
import json
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator
import pytest

from optimizer_pipeline.dof.wp016 import (
    COMPACT_RECOVERABLE_STATUSES, COMPACT_TERMINAL_STATUSES,
    RETRIEVAL_FRAGMENTATION_VERSION, RETRIEVAL_FRONTIER_SEMANTICS,
    RetrievalBudgetState, canonical_bytes, compact_model_visible_protocol,
    compact_protocol_definition, fragment_retrieval_registry, retrieve,
)
from scripts.audit_wp016a_metered_frontier import audit as audit_metered_frontier


ROOT = Path(__file__).resolve().parents[1]
REGISTRY = ROOT / "configs/wp016a/closed-retrieval-registry.json"
SOURCE_CONFIGS = ROOT / "configs/wp016a/source-configurations.csv"
SOURCE_PAYLOADS = ROOT / "configs/wp016a/source-payloads"


@pytest.fixture(scope="module")
def registry() -> dict[str, Any]:
    source = json.loads(REGISTRY.read_text())
    return fragment_retrieval_registry(
        source, protocol=compact_protocol_definition(),
    )


def _turn(state: RetrievalBudgetState, request: dict[str, Any],
          *, output_tokens: int = 8) -> dict[str, Any]:
    assert state.begin_turn()["status"] == "TURN_STARTED"
    assert state.record_output_tokens(output_tokens)["status"] == "OUTPUT_ACCOUNTED"
    assert state.add_history(request)["status"] == "OK"
    return state.dispatch(request)


def test_protocol_separates_recoverable_item_size_from_fatal_reserve() -> None:
    protocol = compact_protocol_definition()
    assert COMPACT_RECOVERABLE_STATUSES == ("RETRIEVAL_ITEM_TOO_LARGE",)
    assert "RETRIEVAL_ITEM_TOO_LARGE" not in COMPACT_TERMINAL_STATUSES
    assert "SEMANTIC_RESERVE_EXHAUSTED" in COMPACT_TERMINAL_STATUSES
    assert "CONTEXT_RESERVE_EXHAUSTED" not in protocol["reachable_error_statuses"]
    assert protocol["recoverable_statuses"] == ["RETRIEVAL_ITEM_TOO_LARGE"]
    visible = compact_model_visible_protocol(protocol)
    assert visible["schema_version"] == "wp016a.model-visible-protocol.v3"
    assert "immediate child" not in canonical_bytes(visible).decode("utf-8")
    assert RETRIEVAL_FRONTIER_SEMANTICS in canonical_bytes(visible).decode("utf-8")


def test_generic_fragmentation_covers_p_and_g_without_trajectory_special_case(
    registry: dict[str, Any],
) -> None:
    entries = registry["entries"]
    assert registry["fragmentation"]["version"] == RETRIEVAL_FRAGMENTATION_VERSION
    assert registry["fragmentation"]["rule"] == (
        "size-aware recursive JSON mapping-key/list-index boundary"
    )
    assert registry["fragmentation"]["target_fragment_bytes"] == 2_048
    assert "SCHEMA/P/program_schema/properties/steps/items/oneOf/0" in entries
    assert "SCHEMA/G/program_schema/properties/steps/items/oneOf/0" in entries
    for content_id, entry in entries.items():
        assert entry["retrieval_size_bytes"] == len(canonical_bytes(
            retrieve(registry, ("M", "P", "G"), content_id)
            if entry["capability"] in {"M", "P", "G"}
            else None
        ))
    generated = [entry for entry in entries.values()
                 if entry.get("fragment_parent_content_id")]
    assert generated and all(entry["model_usable"] is True for entry in generated)
    assert all(entry["discoverable"] is False for entry in generated)


def test_list_index_has_exact_sizes_but_hides_recursive_flat_explosion(
    registry: dict[str, Any],
) -> None:
    state = RetrievalBudgetState(
        registry, ("M", "P", "G"), {}, 58_278, 8_192, 4_096,
    )
    response = _turn(state, {"endpoint": "LIST_CAPABILITIES"})
    assert response["status"] == "OK"
    index = response["content"]["content_index"]
    assert index
    assert all(set(row) == {"content_id", "size_bytes"} for row in index)
    assert not any(
        row["content_id"] == "SCHEMA/G/program_schema/properties/steps/items/oneOf/0"
        for row in index
    )
    for row in index:
        assert row["size_bytes"] == registry["entries"][row["content_id"]]["retrieval_size_bytes"]


@pytest.mark.parametrize(
    "capability,whole_schema,example",
    [
        ("G", "SCHEMA/G/program_schema", "EXAMPLE/G/G_BOUNDARY_NONPREFIX_MULTI_NET"),
        ("P", "SCHEMA/P/program_schema", "EXAMPLE/P/P_BOUNDARY"),
    ],
)
def test_oversize_schema_charges_only_stable_descendant_frontier(
    registry: dict[str, Any], capability: str, whole_schema: str, example: str,
) -> None:
    state = RetrievalBudgetState(
        registry, ("M", "P", capability), {}, 6_214, 8_192, 4_096,
    )
    first = _turn(state, {"endpoint": "GET_EXAMPLE", "content_id": example})
    assert first["status"] == "RETRIEVAL_OK"
    before_retrieval = state.retrieval_bytes
    before_remaining = state.remaining_reserve_bytes
    request = {
        "endpoint": "GET_SCHEMA_FRAGMENT", "content_id": whole_schema,
    }
    oversize = _turn(state, request)
    assert oversize["status"] == "RETRIEVAL_ITEM_TOO_LARGE"
    assert oversize["frontier_semantics"] == RETRIEVAL_FRONTIER_SEMANTICS
    assert oversize["requested_content_bytes_charged"] == 0
    assert oversize["frontier_metadata_bytes"] > 0
    assert oversize["consumed_bytes"] == oversize["frontier_metadata_bytes"]
    assert state.retrieval_bytes == before_retrieval + oversize["frontier_metadata_bytes"]
    assert oversize["remaining_bytes"] == (
        before_remaining - len(canonical_bytes(request))
        - oversize["frontier_metadata_bytes"]
    )
    assert state.terminal is None
    assert "content" not in oversize
    assert oversize["requested_size_bytes"] > oversize["maximum_retrievable_bytes"]
    assert oversize["available_child_fragments"]
    assert [row["content_id"] for row in oversize["available_child_fragments"]] == sorted(
        row["content_id"] for row in oversize["available_child_fragments"]
    )
    assert all(
        row["retrievable_now"] == (row["size_bytes"] <= oversize["remaining_bytes"])
        for row in oversize["available_child_fragments"]
    )
    assert any(row["retrievable_now"] for row in oversize["available_child_fragments"])
    assert any("/properties/steps/" in row["content_id"]
               for row in oversize["available_child_fragments"])
    schema = compact_protocol_definition()["response_schemas_by_status"][oversize["status"]]
    assert not list(Draft202012Validator(schema).iter_errors(oversize))

    frontier = {
        key: oversize[key] for key in (
            "frontier_semantics", "requested_content_id", "requested_size_bytes",
            "requested_content_sha256", "maximum_retrievable_bytes",
            "available_child_fragments",
        )
    }
    assert len(canonical_bytes(frontier)) == oversize["frontier_metadata_bytes"]

    child = next(row["content_id"] for row in oversize["available_child_fragments"]
                 if row["retrievable_now"])
    child_response = _turn(state, {
        "endpoint": "GET_SCHEMA_FRAGMENT", "content_id": child,
    })
    assert child_response["status"] == "RETRIEVAL_OK"
    assert child_response["content_id"] == child
    final = _turn(state, {
        "endpoint": "NO_VALID_SUBMISSION",
        "reason_code": "DYNAMIC_BUDGET_EXHAUSTED",
    })
    assert final["status"] == "NO_VALID_SUBMISSION_RECEIVED"


def test_oversize_schema_can_immediately_recover_to_nvs(
    registry: dict[str, Any],
) -> None:
    state = RetrievalBudgetState(
        registry, ("M", "P", "G"), {}, 6_214, 8_192, 4_096,
    )
    response = _turn(state, {
        "endpoint": "GET_SCHEMA_FRAGMENT", "content_id": "SCHEMA/G/program_schema",
    })
    assert response["status"] == "RETRIEVAL_ITEM_TOO_LARGE"
    assert response["frontier_metadata_bytes"] == response["consumed_bytes"] > 0
    assert state.terminal is None
    nvs = _turn(state, {
        "endpoint": "NO_VALID_SUBMISSION",
        "reason_code": "DYNAMIC_BUDGET_EXHAUSTED",
    })
    assert nvs["status"] == "NO_VALID_SUBMISSION_RECEIVED"


@pytest.mark.parametrize(
    "capability,reserve,whole_schema",
    [
        ("G", 58_278, "SCHEMA/G/program_schema"),
        ("P", 25_351, "SCHEMA/P/program_schema"),
    ],
)
def test_matched_g_and_actual_low_p_charge_whole_schema_normally(
    registry: dict[str, Any], capability: str, reserve: int, whole_schema: str,
) -> None:
    state = RetrievalBudgetState(
        registry, ("M", "P", capability), {}, reserve, 8_192, 4_096,
    )
    before = state.retrieval_bytes
    response = _turn(state, {
        "endpoint": "GET_SCHEMA_FRAGMENT", "content_id": whole_schema,
    })
    assert response["status"] == "RETRIEVAL_OK"
    assert response["consumed_bytes"] == len(canonical_bytes({
        "status": "RETRIEVAL_OK", "content_id": whole_schema,
        "content": response["content"],
        "content_sha256": response["content_sha256"],
    }))
    assert state.retrieval_bytes == before + response["consumed_bytes"]
    assert "frontier_metadata_bytes" not in response


def test_frontier_that_cannot_fit_fails_closed_without_free_evidence(
    registry: dict[str, Any],
) -> None:
    state = RetrievalBudgetState(
        registry, ("M", "P", "G"), {}, 220, 8_192, 4_096,
    )
    response = _turn(state, {
        "endpoint": "GET_SCHEMA_FRAGMENT", "content_id": "SCHEMA/G/program_schema",
    })
    assert response["status"] == "SEMANTIC_RESERVE_EXHAUSTED"
    assert response["reason"] == "retrieval_frontier_cannot_fit"
    assert state.terminal == "SEMANTIC_RESERVE_EXHAUSTED"
    assert state.retrieval_bytes == 0
    assert "available_child_fragments" not in response
    assert "requested_content_sha256" not in response


def test_actual_fatal_semantic_reserve_status_is_distinct(
    registry: dict[str, Any],
) -> None:
    state = RetrievalBudgetState(
        registry, ("M", "P", "G"), {}, 6_214, 1, 4_096,
    )
    assert state.begin_turn()["status"] == "TURN_STARTED"
    assert state.record_output_tokens(1)["status"] == "OUTPUT_ACCOUNTED"
    response = state.add_history({"endpoint": "LIST_CAPABILITIES"})
    assert response["status"] == "SEMANTIC_RESERVE_EXHAUSTED"
    assert response["reason"] == "history_cap"
    assert state.terminal == "SEMANTIC_RESERVE_EXHAUSTED"
    schema = compact_protocol_definition()["response_schemas_by_status"][response["status"]]
    assert not list(Draft202012Validator(schema).iter_errors(response))


def test_machine_audit_closes_g_low_paths_controls_and_zero_call_boundary() -> None:
    result = audit_metered_frontier()
    assert result["status"] == "PASS_ZERO_CALL_METERED_FRONTIER"
    assert result["provider_calls"] == result["backend_calls"] == 0
    assert result["attempt2_token_issued"] is False
    assert result["wp016b_authorised"] is False
    assert [row["terminal_status"] for row in result["g_low_paths"]] == [
        "NO_VALID_SUBMISSION_RECEIVED", "NO_VALID_SUBMISSION_RECEIVED",
    ]
    assert [row["status"] for row in result["controls"]] == [
        "RETRIEVAL_OK", "RETRIEVAL_OK",
    ]
    assert result["frontier_cannot_fit"] == {
        "status": "SEMANTIC_RESERVE_EXHAUSTED",
        "reason": "retrieval_frontier_cannot_fit",
        "frontier_evidence_absent": True,
        "retrieval_bytes": 0,
    }


def test_r1_payload_regeneration_preserves_treatments_and_legacy_bytes() -> None:
    with SOURCE_CONFIGS.open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    assert len(rows) == 6
    expected_reserve = {
        ("G", "COMPACT_RETRIEVAL_LOW_RESERVE"): 6_214,
        ("G", "COMPACT_RETRIEVAL_MATCHED_RESERVE"): 58_278,
        ("P", "COMPACT_RETRIEVAL_LOW_RESERVE"): 25_351,
        ("P", "COMPACT_RETRIEVAL_MATCHED_RESERVE"): 58_278,
    }
    legacy_hashes = {
        "P": "dc2d3d45c2c91f8ed4aa631bf6de886f3ea236f8b3ab3b467f1ca2911f3b498d",
        "G": "008ada992390b3afbcb374bbe82b3b1cca9b1c11016101d497299c3ef4ecfb87",
    }
    for row in rows:
        assert row["root_id"] == "transfer_boom_soc_v2"
        assert row["model_system"] == "openai/gpt-5.6-luna"
        assert int(row["turn_limit"]) == 4
        path = SOURCE_PAYLOADS / f"{row['configuration_id']}.json"
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        assert digest == row["serialized_file_sha256"]
        if row["arm"] == "LEGACY_STATIC":
            assert digest == legacy_hashes[row["capability_task"]]
            continue
        assert int(row["active_dynamic_capacity_bytes"]) == expected_reserve[
            (row["capability_task"], row["arm"])
        ]
        assert int(row["context_bytes"]) == 6_859
        assert row["configuration_status"] == (
            "WP016A_METERED_FRONTIER_REPAIR_R1_PENDING_GPT_PRO_REVIEW"
        )
        payload = json.loads(path.read_text(encoding="utf-8"))
        context_text = str(payload["messages"][1]["content"])
        context = json.loads(context_text)
        assert len(context_text.encode("utf-8")) == 6_859
        assert context["schema_version"] == "wp016a.c1-compact-initial.v3"
        assert context["protocol"]["schema_version"] == "wp016a.model-visible-protocol.v3"
