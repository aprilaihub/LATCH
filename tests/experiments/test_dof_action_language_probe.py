from __future__ import annotations

import pytest

from optimizer_pipeline.dof.action_language_probe import (
    ActionLanguageProbeError,
    g_ast_adversarial_cases,
    place_route_decomposition_program,
    validate_g_ast,
    validate_p_program,
)
from optimizer_pipeline.dof.contracts import DesignStage, DoFLevel
from optimizer_pipeline.dof.expansions import (
    expand_real_action,
    real_action_expansion_manifest_v2,
)
from optimizer_pipeline.dof.toolcalls import e1v2_tool_call_registry


def test_p_macro_embedding_is_exact() -> None:
    result = validate_p_program(
        {
            "program_id": "embed_phys_opt",
            "steps": [{"op": "CALL_MACRO", "parameters": {"name": "phys_opt"}}],
        }
    )
    registry = e1v2_tool_call_registry()
    expected = expand_real_action(
        DoFLevel.L2,
        "macro.phys_opt",
        {},
        current_stage=DesignStage.ROUTED,
        registry=registry,
        manifest=real_action_expansion_manifest_v2(registry),
    )
    assert result["compiled_steps"][0]["expanded"] == expected.to_dict()
    assert result["primary_aggregate_eligible"] is False


def test_p_place_route_decomposition_matches_macro_calls_and_budget() -> None:
    result = validate_p_program(place_route_decomposition_program())
    registry = e1v2_tool_call_registry()
    macro = expand_real_action(
        DoFLevel.L2,
        "macro.place_route",
        {},
        current_stage=DesignStage.ROUTED,
        registry=registry,
        manifest=real_action_expansion_manifest_v2(registry),
    )
    observed = [
        call["operation_id"]
        for step in result["compiled_steps"]
        for call in step["expanded"]["calls"]
    ]
    assert observed == [call.operation_id for call in macro.calls]
    assert result["heavy_calls"] == macro.heavy_call_count == 2
    assert result["final_stage"] == "ROUTED"


def test_p_novel_standard_target_and_region() -> None:
    result = validate_p_program(
        {
            "program_id": "typed_spatial",
            "steps": [
                {
                    "op": "SELECT_TARGET",
                    "parameters": {"selector": "high_fanout_cells", "limit": 16},
                },
                {
                    "op": "CREATE_STANDARD_PBLOCK",
                    "parameters": {
                        "pblock_id": "typed_region",
                        "region": "critical_path_window",
                    },
                },
            ],
        }
    )
    assert result["compiled_steps"][-1]["action_key"] == "generated.pblock_region"
    assert result["compiled_steps"][-1]["typed_region"] == "critical_path_window"


@pytest.mark.parametrize(
    "program,reason",
    [
        (
            {
                "program_id": "bad_stage",
                "steps": [{"op": "UNPLACE_ALL", "parameters": {}}],
            },
            "stage_precondition",
        ),
        (
            {
                "program_id": "bad_directive",
                "steps": [{"op": "PHYS_OPT", "parameters": {"directive": "Aggressive"}}],
            },
            "directive_invalid",
        ),
        (
            {
                "program_id": "bad_region",
                "steps": [
                    {"op": "SELECT_TARGET", "parameters": {"selector": "sequential_cells", "limit": 4}},
                    {"op": "CREATE_STANDARD_PBLOCK", "parameters": {"pblock_id": "x", "region": "arbitrary"}},
                ],
            },
            "region_not_standard",
        ),
        (
            {
                "program_id": "bad_budget",
                "steps": [
                    {"op": "PHYS_OPT", "parameters": {"directive": "Explore"}},
                    {"op": "PHYS_OPT", "parameters": {"directive": "Explore"}},
                    {"op": "PHYS_OPT", "parameters": {"directive": "Explore"}},
                    {"op": "PHYS_OPT", "parameters": {"directive": "Explore"}},
                ],
            },
            "heavy_budget_exceeded",
        ),
    ],
)
def test_p_containment(program: dict, reason: str) -> None:
    with pytest.raises(ActionLanguageProbeError, match=reason):
        validate_p_program(program)


def test_g_ast_generated_selector_region_and_wrapper() -> None:
    result = validate_g_ast(
        {
            "program_id": "generated_region_then_opt",
            "steps": [
                {
                    "op": "SELECT",
                    "parameters": {
                        "object_type": "cell",
                        "predicates": [
                            {"property": "FANOUT", "operator": "GE", "value": 64},
                            {"property": "SLACK_NS", "operator": "LE", "value": 0.0},
                        ],
                        "limit": 24,
                    },
                },
                {
                    "op": "CREATE_PBLOCK",
                    "parameters": {"pblock_id": "novel", "x0": 11, "y0": 7, "x1": 37, "y1": 83},
                },
                {"op": "PHYS_OPT", "parameters": {"directive": "Explore"}},
            ],
        }
    )
    assert result["compiled_steps"][1]["source_predicates"][0]["property"] == "FANOUT"
    assert result["compiled_steps"][2]["action_key"] == "manual.phys_opt_design"
    assert result["heavy_calls"] == 1
    assert not any(result["containment"].values())


def test_g_ast_adversarial_operations_all_fail_closed() -> None:
    for program in g_ast_adversarial_cases():
        with pytest.raises(ActionLanguageProbeError, match="operation_not_allowed"):
            validate_g_ast(program)
