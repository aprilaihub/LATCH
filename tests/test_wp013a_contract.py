from __future__ import annotations

import hashlib
import json

import pytest

from optimizer_pipeline.dof.single_submission.contract_v2 import (
    MAX_INPUT_BYTES,
    M_ACTION_KEYS,
    M_HEAVY_CALLS,
    P_ARGUMENT_SCHEMAS,
    G_GENERATED_ARGUMENT_SCHEMAS,
    audit_contract_completeness,
    candidate_templates,
    compile_candidate_v2,
    contract_for_level,
    macro_from_candidate_v2,
)
from optimizer_pipeline.dof.single_submission.handles import TrialHandleRegistry
from optimizer_pipeline.dof.single_submission.language import CompileContext, ConstructionError
from optimizer_pipeline.dof.single_submission.specs import canonical_json_bytes


def context() -> CompileContext:
    root = hashlib.sha256(b"wp013a-contract-root").hexdigest()
    registry = TrialHandleRegistry(
        trial_id="wp013a-contract",
        root_sha256=root,
        secret=hashlib.sha256(b"wp013a-contract-secret").digest(),
    )
    return CompileContext(registry, "ROUTED", (), (0, 3, 0, 3))


def test_canonical_action_key_is_accepted_and_hidden_macro_field_is_rejected() -> None:
    compiled = compile_candidate_v2(
        {"language": "M", "action_key": "macro.phys_opt"}, context()
    )
    assert compiled.language == "M" and compiled.heavy_calls == 3
    with pytest.raises(ConstructionError, match="macro_candidate_fields_invalid"):
        compile_candidate_v2({"language": "M", "macro": "macro.phys_opt"}, context())


@pytest.mark.parametrize("action_key", M_ACTION_KEYS)
def test_m_v2_costs_equal_actual_overlay(action_key: str) -> None:
    compiled = compile_candidate_v2(
        {"language": "M", "action_key": action_key}, context()
    )
    assert compiled.heavy_calls == M_HEAVY_CALLS[action_key]


@pytest.mark.parametrize("language", ["P", "G"])
def test_inherited_macro_uses_same_action_key_and_four_call_cost(language: str) -> None:
    action_key = "macro.memory_path_opt"
    candidate = (
        {
            "language": "P",
            "steps": [
                {"step_id": "s1", "op": "CALL_MACRO", "args": {"action_key": action_key}}
            ],
        }
        if language == "P"
        else {
            "language": "G",
            "statements": [
                {"statement_id": "g1", "node": "CALL_MACRO", "args": {"action_key": action_key}}
            ],
        }
    )
    compiled = compile_candidate_v2(candidate, context())
    assert compiled.heavy_calls == 4
    assert b"stage_preparation" in compiled.public_bytes
    assert macro_from_candidate_v2(candidate) == "memory_path_opt"


def test_contract_is_nested_complete_and_under_context_budget() -> None:
    l2, l3, l4 = (contract_for_level(level) for level in ("L2", "L3", "L4"))
    assert l2["M"] == l3["M"] == l4["M"]
    assert l3["P"] == l4["P"]
    assert set(P_ARGUMENT_SCHEMAS) <= set(l4["P"]["operation_argument_schemas"])
    assert set(G_GENERATED_ARGUMENT_SCHEMAS) <= set(l4["G"]["generated_node_argument_schemas"])
    assert len(canonical_json_bytes(l4)) < MAX_INPUT_BYTES
    encoded = canonical_json_bytes(l4)
    assert b'"action_key"' in encoded
    assert b'"macro":"frozen' not in encoded


def test_syntax_templates_compile_and_completeness_audit_passes() -> None:
    templates = candidate_templates()
    assert set(templates) == {"M", "P", "G"}
    assert all(compile_candidate_v2(value, context()) for value in templates.values())
    audit = audit_contract_completeness(context())
    assert audit["accepted"] is True
    assert all(audit["checks"].values())


def test_unknown_fields_and_budget_overflow_fail_closed() -> None:
    with pytest.raises(ConstructionError):
        compile_candidate_v2(
            {"language": "M", "action_key": "macro.place_route", "extra": True},
            context(),
        )
    with pytest.raises(ConstructionError, match="static_heavy_budget_overflow"):
        compile_candidate_v2(
            {
                "language": "P",
                "steps": [
                    {"step_id": "s1", "op": "CALL_MACRO", "args": {"action_key": "macro.memory_path_opt"}},
                    {"step_id": "s2", "op": "PHYS_OPT", "args": {"directive": "Explore", "options": []}},
                ],
            },
            context(),
        )


def test_contract_contains_no_asset_or_outcome_prior() -> None:
    text = json.dumps(contract_for_level("L4"), sort_keys=True).lower()
    assert "vexriscv" not in text
    assert "external_compatibility" not in text
    assert "historical_winner" not in text
    assert "observed_qor" not in text
