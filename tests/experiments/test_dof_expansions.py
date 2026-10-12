from __future__ import annotations

import asyncio
from dataclasses import replace
import json

import pytest

from optimizer_pipeline.dof.catalogues import (
    clean_macro_catalogue,
    strict_l3_catalogue,
)
from optimizer_pipeline.dof.contracts import DesignStage, DoFLevel, EventClass
from optimizer_pipeline.dof.expansions import (
    declared_action_call_plan,
    expand_real_action,
    real_action_expansion_manifest,
)
from optimizer_pipeline.dof.toolcalls import (
    ActualCallBudget,
    ObservedCallExecutor,
    ToolCallContractError,
    default_tool_call_registry,
)


def _limits() -> dict[EventClass, int]:
    return {item: 20 for item in EventClass}


def test_manifest_is_hash_addressed_closed_and_separate_from_catalogues() -> None:
    registry = default_tool_call_registry()
    manifest = real_action_expansion_manifest(registry)
    manifest.validate_against(registry)

    assert clean_macro_catalogue(DoFLevel.L1).canonical_bytes() == (
        clean_macro_catalogue(DoFLevel.L2).canonical_bytes()
    )
    assert manifest.shared_macro_catalogue_hash == clean_macro_catalogue(
        DoFLevel.L1
    ).sha256
    assert manifest.strict_l3_catalogue_hash == strict_l3_catalogue().sha256
    assert manifest.registry_hash == registry.sha256
    assert len(manifest.sha256) == 64
    assert manifest.canonical_bytes() == json.dumps(
        manifest.to_dict(),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode()
    serialized = manifest.canonical_bytes().decode()
    for forbidden in (
        "mock.",
        "flow.",
        "run_tcl",
        "shell",
        ".dcp",
        "benchmark",
    ):
        assert forbidden not in serialized


@pytest.mark.parametrize("level", (DoFLevel.L1, DoFLevel.L2))
def test_shared_macro_expansions_have_exact_required_real_meanings(level) -> None:
    phys = expand_real_action(
        level,
        "macro.phys_opt",
        {},
        current_stage=DesignStage.ROUTED,
    )
    assert [(call.operation_id, dict(call.arguments)) for call in phys.calls] == [
        ("vivado.phys_opt_design", {"directive": "Explore"})
    ]
    assert (phys.light_call_count, phys.heavy_call_count) == (0, 1)

    retime = expand_real_action(
        level,
        "macro.retime",
        {},
        current_stage=DesignStage.ROUTED,
    )
    assert [(call.operation_id, dict(call.arguments)) for call in retime.calls] == [
        ("vivado.phys_opt_design", {"directive": "AddRetime"})
    ]
    assert (retime.light_call_count, retime.heavy_call_count) == (0, 1)

    place_route = expand_real_action(
        level,
        "macro.place_route",
        {},
        current_stage=DesignStage.ROUTED,
    )
    assert [item.operation_id for item in place_route.calls] == [
        "vivado.unroute_all",
        "vivado.unplace_all",
        "vivado.place_design",
        "vivado.route_design",
    ]
    assert [dict(item.arguments) for item in place_route.calls] == [
        {},
        {},
        {"directive": "Default"},
        {"directive": "Default"},
    ]
    assert (place_route.light_call_count, place_route.heavy_call_count) == (2, 2)
    assert place_route.final_stage is DesignStage.ROUTED


def test_l1_and_l2_bind_byte_identical_manifest_and_expansion_semantics() -> None:
    manifest_l1 = real_action_expansion_manifest()
    manifest_l2 = real_action_expansion_manifest()
    assert manifest_l1.canonical_bytes() == manifest_l2.canonical_bytes()
    for action_key in ("macro.phys_opt", "macro.retime", "macro.place_route"):
        l1 = expand_real_action(
            DoFLevel.L1,
            action_key,
            {},
            current_stage=DesignStage.ROUTED,
            manifest=manifest_l1,
        )
        l2 = expand_real_action(
            DoFLevel.L2,
            action_key,
            {},
            current_stage=DesignStage.ROUTED,
            manifest=manifest_l2,
        )
        assert [item.to_dict() for item in l1.calls] == [
            item.to_dict() for item in l2.calls
        ]
        assert l1.expansion_manifest_hash == l2.expansion_manifest_hash


def test_every_l3_directive_expands_to_one_allowlisted_registered_operation() -> None:
    catalogue = strict_l3_catalogue()
    initial_stage = {
        "manual.unroute_all": DesignStage.ROUTED,
        "manual.unplace_all": DesignStage.PLACED,
        "manual.place_design": DesignStage.UNPLACED,
        "manual.route_design": DesignStage.PLACED,
        "manual.phys_opt_design": DesignStage.ROUTED,
    }
    expected_operation = {
        name: "vivado." + name.removeprefix("manual.")
        for name in initial_stage
    }
    for action in catalogue.actions:
        schema = action.to_dict()["parameters_schema"]
        directives = schema["properties"].get("directive", {}).get("enum", [None])
        for directive in directives:
            parameters = {} if directive is None else {"directive": directive}
            expanded = expand_real_action(
                DoFLevel.L3,
                action.name,
                parameters,
                current_stage=initial_stage[action.name],
            )
            assert len(expanded.calls) == 1
            assert expanded.calls[0].operation_id == expected_operation[action.name]
            assert dict(expanded.calls[0].arguments) == parameters


def test_legal_l3_sequence_progresses_only_after_each_expanded_step() -> None:
    stage = DesignStage.ROUTED
    sequence = (
        ("manual.unroute_all", {}),
        ("manual.unplace_all", {}),
        ("manual.place_design", {"directive": "Explore"}),
        ("manual.route_design", {"directive": "Default"}),
        ("manual.phys_opt_design", {"directive": "AddRetime"}),
    )
    observed = [stage]
    for action_key, parameters in sequence:
        expanded = expand_real_action(
            DoFLevel.L3,
            action_key,
            parameters,
            current_stage=stage,
        )
        stage = expanded.final_stage
        observed.append(stage)
    assert observed == [
        DesignStage.ROUTED,
        DesignStage.PLACED,
        DesignStage.UNPLACED,
        DesignStage.PLACED,
        DesignStage.ROUTED,
        DesignStage.ROUTED,
    ]


def test_invalid_level_stage_directive_and_parameters_fail_before_plan() -> None:
    with pytest.raises(ToolCallContractError, match="action_not_available_at_level"):
        expand_real_action(
            DoFLevel.L2,
            "manual.phys_opt_design",
            {"directive": "Explore"},
            current_stage=DesignStage.ROUTED,
        )
    with pytest.raises(ToolCallContractError, match="stage_precondition"):
        expand_real_action(
            DoFLevel.L3,
            "manual.route_design",
            {"directive": "Default"},
            current_stage=DesignStage.ROUTED,
        )
    with pytest.raises(ToolCallContractError, match="directive_not_allowed"):
        expand_real_action(
            DoFLevel.L3,
            "manual.phys_opt_design",
            {"directive": "AggressiveExplore"},
            current_stage=DesignStage.ROUTED,
        )
    with pytest.raises(ToolCallContractError, match="parameter_shape"):
        expand_real_action(
            DoFLevel.L1,
            "macro.phys_opt",
            {"directive": "Explore"},
            current_stage=DesignStage.ROUTED,
        )


def test_manifest_registry_tamper_and_unknown_operations_fail_closed() -> None:
    manifest = real_action_expansion_manifest()
    with pytest.raises(ToolCallContractError, match="registry_hash_mismatch"):
        replace(manifest, registry_hash="0" * 64).validate_against(
            default_tool_call_registry()
        )


def test_expanded_call_plan_hash_binds_actual_order_and_observer_budget() -> None:
    expanded = expand_real_action(
        DoFLevel.L1,
        "macro.place_route",
        {},
        current_stage=DesignStage.ROUTED,
    )
    registry = default_tool_call_registry()
    plan = declared_action_call_plan(expanded, registry)
    entered: list[tuple[str, dict]] = []

    async def invoke(operation, arguments):
        entered.append((operation.operation_id, dict(arguments)))
        return {"ok": True}

    budget = ActualCallBudget(
        _limits(),
        whole_run_timeout_seconds=5.0,
    )
    observer = ObservedCallExecutor(
        registry=registry,
        plan=plan,
        budget=budget,
        invoke=invoke,
    )

    async def run() -> None:
        for call in expanded.calls:
                await observer.call(
                    call.operation_id,
                    dict(call.arguments),
                    current_stage=call.input_stage,
                )

    asyncio.run(run())
    reconciliation = observer.reconcile_or_raise()
    assert entered == [
        (item.operation_id, dict(item.arguments)) for item in expanded.calls
    ]
    assert reconciliation.plan_hash == plan.sha256
    assert budget.used[EventClass.MUTATION_LIGHT] == 2
    assert budget.used[EventClass.IMPLEMENTATION_HEAVY] == 2
