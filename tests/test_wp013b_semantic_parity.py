from __future__ import annotations

import hashlib
import json

import pytest

from optimizer_pipeline.dof.openrouter import (
    OPENROUTER_WP013B_MAX_BILLABLE_INPUT_BYTES, build_openrouter_outbound,
)
from optimizer_pipeline.dof.provider import ProviderRequestBinding, ProviderSettings
from optimizer_pipeline.dof.single_submission.contract_v3 import (
    CARD_FIELDS, COMMON_OBJECTIVE, G_CARDS, M_CARDS, P_CARDS,
    QUERY_RESULT_MAX_BYTES, contract_for_level,
)
from optimizer_pipeline.dof.single_submission.handles import TrialHandleRegistry
from optimizer_pipeline.dof.single_submission.observation import ObservationError
from optimizer_pipeline.dof.single_submission.observation_v3 import CommonQuerySurfaceV3


def _registry() -> TrialHandleRegistry:
    return TrialHandleRegistry(
        trial_id="wp013b-test", root_sha256=hashlib.sha256(b"root").hexdigest(),
        secret=b"x" * 32,
    )


def test_semantic_cards_and_inheritance_are_symmetric() -> None:
    contracts = {level: contract_for_level(level) for level in ("L2", "L3", "L4")}
    assert len(M_CARDS) == 8
    assert len(P_CARDS) == 16
    assert len(G_CARDS) == 20
    assert len({row["key"] for row in G_CARDS}) == 20
    assert all(tuple(row) == CARD_FIELDS for rows in (M_CARDS, P_CARDS, G_CARDS) for row in rows)
    assert contracts["L2"]["M"] == contracts["L3"]["M"] == contracts["L4"]["M"]
    assert contracts["L3"]["P"] == contracts["L4"]["P"]
    assert all(value["common_objective"] == COMMON_OBJECTIVE for value in contracts.values())
    assert b"RELOCATE_HARDBLOCK_GENERATED" not in json.dumps(contracts["L4"], sort_keys=True).encode()


def test_macro_challenge_mask_is_byte_identical_and_excludes_masked_actions() -> None:
    admitted = ("macro.phys_opt", "macro.retime", "macro.net_swap", "macro.congestion_spread")
    contracts = {level: contract_for_level(level, admitted_action_keys=admitted) for level in ("L2", "L3", "L4")}
    assert contracts["L2"]["M"] == contracts["L3"]["M"] == contracts["L4"]["M"]
    assert [row["key"] for row in contracts["L2"]["M"]["cards"]] == list(admitted)
    assert contracts["L2"]["M"]["candidate_schema"]["properties"]["action_key"]["enum"] == list(admitted)


def test_query_v3_enforces_top_k_rows_and_bytes_with_explicit_truncation() -> None:
    surface = CommonQuerySurfaceV3(_registry())
    rows = [
        {"canonical_identity": f"net-{index:03}", "handle_type": "net", "fanout": 1000 - index, "row_semantics": "x" * 900}
        for index in range(40)
    ]
    result = surface.materialize(query_name="high_fanout_net_details", top_k=16, private_rows=rows)
    payload = result.public_payload()
    assert len(result.canonical_bytes) <= QUERY_RESULT_MAX_BYTES
    assert payload["requested_count"] == 16
    assert payload["returned_count"] == len(payload["rows"])
    assert payload["truncated"] is True
    assert "individual_byte_limit" in payload["reason"]
    assert payload["private_source_sha256"]
    assert payload["selected_rows_sha256"]
    with pytest.raises(ObservationError, match="query_top_k_out_of_range"):
        surface.materialize(query_name="high_fanout_net_details", top_k=17, private_rows=rows)


def _binding(*, context_size: int, context_version: str, prompt_version: str, action_version: str) -> ProviderRequestBinding:
    context = b"x" * context_size
    prompt = b"p"
    return ProviderRequestBinding(
        run_id="run", spec_hash="a" * 64, experiment_id="exp", condition_id="condition",
        repeat_id="repeat", decision_id="decision", context_bytes=context, prompt_bytes=prompt,
        prompt_version=prompt_version, action_interface_version=action_version,
        action_interface_hash="b" * 64, context_interface_version=context_version,
        context_interface_hash=hashlib.sha256(context).hexdigest(), catalogue_version="catalogue",
        catalogue_hash="c" * 64, capability_hash="d" * 64, mask_evidence_hash="e" * 64,
        provider_role="test", provider="openrouter", model_id="openai/gpt-5.6-luna",
        adapter_version="dof.openrouter-chat-completions.v3",
        settings=ProviderSettings(temperature=None, max_tokens=4096, seed=None, timeout_seconds=180, max_retries=0, retry_on=(), max_requests=1, max_usd=1.0, max_cost_per_request_usd=1.0, unknown_cost_policy="reject", unknown_usage_policy="reject"),
    )


def test_65536_ceiling_is_exactly_bound_to_v22_identity() -> None:
    good = _binding(context_size=40_000, context_version="dof.single-submission.planning-context.v2.2", prompt_version="dof.wp013b-one-submission-prompt.v2.2", action_version="dof.single-submission.planner-message.v3")
    assert OPENROUTER_WP013B_MAX_BILLABLE_INPUT_BYTES == 65_536
    assert build_openrouter_outbound(good).body["model"] == "openai/gpt-5.6-luna"
    old = _binding(context_size=40_000, context_version="dof.single-submission.planning-context.v2", prompt_version="dof.wp013a-one-submission-prompt.v2", action_version="dof.single-submission.planner-message.v2")
    with pytest.raises(Exception, match="input_byte_bound_exceeded"):
        build_openrouter_outbound(old)
