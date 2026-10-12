from __future__ import annotations

import json

import pytest

from optimizer_pipeline.dof.catalogues import clean_macro_catalogue, strict_l3_catalogue
from optimizer_pipeline.dof.contracts import DesignStage, DoFLevel, EndpointCode, EventClass, UnknownRegime
from optimizer_pipeline.dof.gate import (
    ACTION_PROPOSAL_SCHEMA_VERSION,
    ActionProposal,
    ExperimentActionGate,
    ProposalParseError,
    parse_action_proposal,
)
from optimizer_pipeline.dof.projector import DecisionContextProjector, ProjectionError
from optimizer_pipeline.dof.spec import MaskSpec


def digest(catalogue) -> str:
    value = catalogue.sha256
    return value() if callable(value) else value


def proposal(catalogue, kind: str, steps: list[dict], **updates) -> dict:
    payload = {
        "schema_version": ACTION_PROPOSAL_SCHEMA_VERSION,
        "proposal_id": "proposal-001",
        "decision_id": "decision-001",
        "catalogue_version": catalogue.version,
        "catalogue_hash": digest(catalogue),
        "kind": kind,
        "steps": steps,
    }
    payload.update(updates)
    return payload


def step(step_id: str, action_key: str, parameters: dict | None = None) -> dict:
    return {
        "step_id": step_id,
        "action_key": action_key,
        "parameters": dict(parameters or {}),
    }


def budgets(*, heavy: int = 20, light: int = 20) -> dict:
    return {
        EventClass.IMPLEMENTATION_HEAVY: heavy,
        EventClass.MUTATION_LIGHT: light,
    }


def mask(
    *hidden: str,
    version: str = "mask.v1",
    forbidden_sentinels: tuple[str, ...] = (),
    known_prior_allowlist: tuple[str, ...] = (),
):
    return MaskSpec.create(
        version=version,
        hidden_macro_families=hidden,
        forbidden_sentinels=forbidden_sentinels,
        known_prior_allowlist=known_prior_allowlist,
    )


def context_source() -> dict:
    return {
        "condition": {"pseudonym": "condition-001"},
        "state": {
            "pseudonym": "state-001",
            "lineage_pseudonym": "lineage-001",
            "stage": "ROUTED",
            "measurements": {
                "timing": {
                    "wns_ns": -0.125,
                    "tns_ns": -1.5,
                    "failing_endpoints": 7,
                    "clock_period_ns": 2.5,
                    "estimated_fmax_mhz": 380.0,
                },
                "route": {"status": "fully routed", "unrouted_nets": 0},
                "placement": {"status": "placed", "placed_cells": 1234},
                "utilization": {"lut": 1234, "ff": 2345, "dsp": 8, "bram": 12},
                "congestion": {"level": "low", "hotspots": 1},
            },
        },
        "history": [
            {
                "decision_index": 0,
                "actions": [{"action_key": "manual.phys_opt_design", "parameters": {"directive": "Explore"}}],
                "parent_qor": {"fmax_mhz": 379.0, "wns_ns": -0.135},
                "child_qor": {"fmax_mhz": 380.0, "wns_ns": -0.125},
                "delta_qor": {"fmax_mhz": 1.0, "wns_ns": 0.01},
                "endpoint": "NO_IMPROVEMENT",
                "valid": True,
                "actual_heavy_calls": 1,
            }
        ],
        "budget_remaining": {
            "IMPLEMENTATION_HEAVY": 3,
            "MUTATION_LIGHT": 2,
            "READ_ONLY": 5,
            "LLM_REQUEST": 1,
            "WALL_TIME_SECONDS": 120.0,
        },
        "validation": {"status": "CLEAN", "errors": [], "warnings": []},
        "stop_conditions": ["heavy budget exhausted"],
        "known_priors": {},
    }


def test_proposal_parser_is_exact_typed_and_bounded():
    catalogue = strict_l3_catalogue()
    parsed = parse_action_proposal(
        proposal(
            catalogue,
            "ACTION",
            [step("step-01", "manual.route_design", {"directive": "Explore"})],
        )
    )
    assert isinstance(parsed, ActionProposal)
    assert parsed.steps[0].action_key == "manual.route_design"
    assert len(parsed.proposal_hash) == 64

    bad_alias = proposal(
        catalogue,
        "ACTION",
        [{"step_id": "step-01", "primitive": "manual.route_design", "parameters": {}}],
    )
    with pytest.raises(ProposalParseError) as alias_error:
        parse_action_proposal(bad_alias)
    assert "step_0_unknown_fields:primitive" in alias_error.value.reasons

    duplicate = proposal(
        catalogue,
        "PLAN",
        [step("same", "manual.unroute_all"), step("same", "manual.unplace_all")],
    )
    with pytest.raises(ProposalParseError, match="duplicate_step_ids"):
        parse_action_proposal(duplicate)

    too_long = proposal(
        catalogue,
        "PLAN",
        [step(f"step-{index}", "manual.phys_opt_design", {"directive": "Explore"}) for index in range(3)],
    )
    with pytest.raises(ProposalParseError, match="plan_too_long"):
        parse_action_proposal(too_long, max_plan_steps=2)


def test_strict_l3_full_plan_simulates_stage_and_declared_costs():
    catalogue = strict_l3_catalogue()
    payload = proposal(
        catalogue,
        "PLAN",
        [
            step("unroute", "manual.unroute_all"),
            step("unplace", "manual.unplace_all"),
            step("place", "manual.place_design", {"directive": "Explore"}),
            step("pre-opt", "manual.phys_opt_design", {"directive": "AddRetime"}),
            step("route", "manual.route_design", {"directive": "Explore"}),
            step("post-opt", "manual.phys_opt_design", {"directive": "Explore"}),
        ],
    )
    decision = ExperimentActionGate().evaluate(
        payload,
        level=DoFLevel.L3,
        expected_decision_id="decision-001",
        catalogue=catalogue,
        expected_catalogue_hash=digest(catalogue),
        current_stage=DesignStage.ROUTED,
        budget_remaining=budgets(),
        mask=mask(),
    )
    assert decision.accepted
    assert decision.endpoint is None
    assert decision.required_units == {
        "IMPLEMENTATION_HEAVY": 4,
        "MUTATION_LIGHT": 2,
    }
    assert decision.final_stage is DesignStage.ROUTED
    assert [item.stage_before for item in decision.gated_steps] == [
        DesignStage.ROUTED,
        DesignStage.PLACED,
        DesignStage.UNPLACED,
        DesignStage.PLACED,
        DesignStage.PLACED,
        DesignStage.ROUTED,
    ]


@pytest.mark.parametrize(
    "action_key",
    [
        "flow.retime",
        "auto_top3",
        "pblock",
        "vexriscv",
        "retime",
        "phys_opt",
        "rescue_place_reset_retime",
        "synthesized_recipe",
        "manual_action",
        "run_tcl",
        "raw_tcl",
        "shell",
        "bash",
        "write_checkpoint",
        "macro.unroute_design",
        "vivado.route_design",
    ],
)
def test_l3_rejects_flow_meta_raw_and_alias_surfaces(action_key: str):
    catalogue = strict_l3_catalogue()
    decision = ExperimentActionGate().evaluate(
        proposal(catalogue, "ACTION", [step("bad", action_key)]),
        level=DoFLevel.L3,
        expected_decision_id="decision-001",
        catalogue=catalogue,
        expected_catalogue_hash=digest(catalogue),
        current_stage=DesignStage.ROUTED,
        budget_remaining=budgets(),
        mask=mask(),
    )
    assert not decision.accepted
    assert decision.endpoint is EndpointCode.CAPABILITY_ACTION_REJECTION
    assert decision.gated_steps == ()
    assert decision.to_dict()["fallback_used"] is False


def test_l3_rejects_unknown_directive_path_parameters_and_stage_invalid_plan():
    catalogue = strict_l3_catalogue()
    gate = ExperimentActionGate()
    common = dict(
        level=DoFLevel.L3,
        expected_decision_id="decision-001",
        catalogue=catalogue,
        expected_catalogue_hash=digest(catalogue),
        current_stage=DesignStage.ROUTED,
        budget_remaining=budgets(),
        mask=mask(),
    )
    directive = gate.evaluate(
        proposal(catalogue, "ACTION", [step("bad", "manual.phys_opt_design", {"directive": "AggressiveExplore"})]),
        **common,
    )
    assert directive.endpoint is EndpointCode.CAPABILITY_ACTION_REJECTION
    assert any("parameter_not_allowed" in reason for reason in directive.reasons)

    path = gate.evaluate(
        proposal(catalogue, "ACTION", [step("bad", "manual.unroute_all", {"output_path": "/tmp/out.dcp"})]),
        **common,
    )
    assert any("forbidden_parameter_key" in reason for reason in path.reasons)

    route_first = gate.evaluate(
        proposal(catalogue, "ACTION", [step("bad", "manual.route_design", {"directive": "Explore"})]),
        **common,
    )
    assert any("stage_precondition_failed" in reason for reason in route_first.reasons)


def test_catalogue_binding_stop_and_budget_fail_closed():
    catalogue = strict_l3_catalogue()
    gate = ExperimentActionGate()
    stop = proposal(catalogue, "STOP", [])
    stop["catalogue_hash"] = "b" * 64
    rejected_stop = gate.evaluate(
        stop,
        level=DoFLevel.L3,
        expected_decision_id="decision-001",
        catalogue=catalogue,
        expected_catalogue_hash=digest(catalogue),
        current_stage=DesignStage.ROUTED,
        budget_remaining=budgets(),
        mask=mask(),
    )
    assert rejected_stop.endpoint is EndpointCode.CAPABILITY_ACTION_REJECTION

    over_budget = gate.evaluate(
        proposal(
            catalogue,
            "ACTION",
            [step("opt", "manual.phys_opt_design", {"directive": "Explore"})],
        ),
        level=DoFLevel.L3,
        expected_decision_id="decision-001",
        catalogue=catalogue,
        expected_catalogue_hash=digest(catalogue),
        current_stage=DesignStage.ROUTED,
        budget_remaining=budgets(heavy=0),
        mask=mask(),
    )
    assert over_budget.endpoint is EndpointCode.BUDGET_REJECTION
    assert over_budget.gated_steps == ()
    assert any("budget_would_exceed" in reason for reason in over_budget.reasons)


def test_composite_macro_cost_is_internal_events_not_one_outer_action():
    catalogue = clean_macro_catalogue(DoFLevel.L2)
    composite = next(action for action in catalogue.actions if action.name == "macro.place_route")
    gate = ExperimentActionGate()
    payload = proposal(catalogue, "ACTION", [step("macro", composite.name)])
    accepted = gate.evaluate(
        payload,
        level=DoFLevel.L2,
        expected_decision_id="decision-001",
        catalogue=catalogue,
        expected_catalogue_hash=digest(catalogue),
        current_stage=DesignStage.ROUTED,
        budget_remaining=budgets(heavy=2),
        mask=mask(),
    )
    assert accepted.accepted
    assert accepted.required_units == {"IMPLEMENTATION_HEAVY": 2}
    assert [event.operation for event in accepted.gated_steps[0].events] == [
        "mock.place",
        "mock.route",
    ]

    rejected = gate.evaluate(
        payload,
        level=DoFLevel.L2,
        expected_decision_id="decision-001",
        catalogue=catalogue,
        expected_catalogue_hash=digest(catalogue),
        current_stage=DesignStage.ROUTED,
        budget_remaining=budgets(heavy=1),
        mask=mask(),
    )
    assert rejected.endpoint is EndpointCode.BUDGET_REJECTION
    assert rejected.gated_steps == ()


def test_gate_accounts_per_step_outer_attempt_and_validation_before_dispatch():
    catalogue = strict_l3_catalogue()
    payload = proposal(
        catalogue,
        "PLAN",
        [
            step("unroute", "manual.unroute_all"),
            step("unplace", "manual.unplace_all"),
        ],
    )
    remaining = budgets()
    remaining.update({EventClass.OUTER_ATTEMPT: 2, EventClass.VALIDATION: 2})
    accepted = ExperimentActionGate().evaluate(
        payload,
        level=DoFLevel.L3,
        expected_decision_id="decision-001",
        catalogue=catalogue,
        expected_catalogue_hash=digest(catalogue),
        current_stage=DesignStage.ROUTED,
        budget_remaining=remaining,
        mask=mask(),
        infrastructure_events_per_step={
            EventClass.OUTER_ATTEMPT: 1,
            EventClass.VALIDATION: 1,
        },
    )
    assert accepted.accepted
    assert accepted.required_units == {
        "MUTATION_LIGHT": 2,
        "OUTER_ATTEMPT": 2,
        "VALIDATION": 2,
    }
    assert [event.event_class for event in accepted.gated_steps[0].events] == [
        EventClass.OUTER_ATTEMPT,
        EventClass.MUTATION_LIGHT,
        EventClass.VALIDATION,
    ]

    remaining[EventClass.VALIDATION] = 1
    rejected = ExperimentActionGate().evaluate(
        payload,
        level=DoFLevel.L3,
        expected_decision_id="decision-001",
        catalogue=catalogue,
        expected_catalogue_hash=digest(catalogue),
        current_stage=DesignStage.ROUTED,
        budget_remaining=remaining,
        mask=mask(),
        infrastructure_events_per_step={
            EventClass.OUTER_ATTEMPT: 1,
            EventClass.VALIDATION: 1,
        },
    )
    assert rejected.endpoint is EndpointCode.BUDGET_REJECTION
    assert rejected.gated_steps == ()
    assert any("budget_would_exceed:VALIDATION:2>1" in reason for reason in rejected.reasons)


@pytest.mark.parametrize(
    ("stage_value", "action_key", "parameters", "accepted"),
    [
        (DesignStage.ROUTED, "manual.unroute_all", {}, True),
        (DesignStage.PLACED, "manual.unroute_all", {}, False),
        (DesignStage.UNPLACED, "manual.unroute_all", {}, False),
        (DesignStage.ROUTED, "manual.unplace_all", {}, False),
        (DesignStage.PLACED, "manual.unplace_all", {}, True),
        (DesignStage.UNPLACED, "manual.unplace_all", {}, False),
        (DesignStage.ROUTED, "manual.place_design", {"directive": "Explore"}, False),
        (DesignStage.PLACED, "manual.place_design", {"directive": "Explore"}, False),
        (DesignStage.UNPLACED, "manual.place_design", {"directive": "Explore"}, True),
        (DesignStage.ROUTED, "manual.route_design", {"directive": "Explore"}, False),
        (DesignStage.PLACED, "manual.route_design", {"directive": "Explore"}, True),
        (DesignStage.UNPLACED, "manual.route_design", {"directive": "Explore"}, False),
        (DesignStage.ROUTED, "manual.phys_opt_design", {"directive": "Explore"}, True),
        (DesignStage.PLACED, "manual.phys_opt_design", {"directive": "Explore"}, True),
        (DesignStage.UNPLACED, "manual.phys_opt_design", {"directive": "Explore"}, False),
    ],
)
def test_l3_complete_stage_legality_matrix(stage_value, action_key, parameters, accepted):
    catalogue = strict_l3_catalogue()
    decision = ExperimentActionGate().evaluate(
        proposal(catalogue, "ACTION", [step("candidate", action_key, parameters)]),
        level=DoFLevel.L3,
        expected_decision_id="decision-001",
        catalogue=catalogue,
        expected_catalogue_hash=digest(catalogue),
        current_stage=stage_value,
        budget_remaining=budgets(),
        mask=mask(),
    )
    assert decision.accepted is accepted
    if not accepted:
        assert decision.endpoint is EndpointCode.CAPABILITY_ACTION_REJECTION
        assert decision.gated_steps == ()


def test_l0_and_l4_attempts_are_explicit_non_success_without_fallback():
    catalogue = strict_l3_catalogue()
    payload = proposal(catalogue, "ACTION", [step("unroute", "manual.unroute_all")])
    gate = ExperimentActionGate()
    l0 = gate.evaluate(
        payload,
        level=DoFLevel.L0,
        expected_decision_id="decision-001",
        catalogue=catalogue,
        expected_catalogue_hash=digest(catalogue),
        current_stage=DesignStage.ROUTED,
        budget_remaining=budgets(),
    )
    l4 = gate.evaluate(
        payload,
        level=DoFLevel.L4,
        expected_decision_id="decision-001",
        catalogue=catalogue,
        expected_catalogue_hash=digest(catalogue),
        current_stage=DesignStage.ROUTED,
        budget_remaining=budgets(),
    )
    assert l0.endpoint is EndpointCode.CAPABILITY_ACTION_REJECTION
    assert l4.endpoint is EndpointCode.L4_REFERENCE_PENDING
    assert l0.gated_steps == l4.gated_steps == ()
    assert not l0.to_dict()["fallback_used"]
    assert not l4.to_dict()["fallback_used"]


def test_l2_mask_is_enforced_by_gate_and_projector_while_l3_is_independent():
    l2_catalogue = clean_macro_catalogue(DoFLevel.L2)
    hidden_action = l2_catalogue.actions[0]
    hidden_mask = mask(hidden_action.family)
    gate = ExperimentActionGate()
    decision = gate.evaluate(
        proposal(l2_catalogue, "ACTION", [step("macro", hidden_action.name)]),
        level=DoFLevel.L2,
        expected_decision_id="decision-001",
        catalogue=l2_catalogue,
        expected_catalogue_hash=digest(l2_catalogue),
        current_stage=DesignStage.ROUTED,
        budget_remaining=budgets(),
        mask=hidden_mask,
    )
    assert decision.endpoint is EndpointCode.MASK_VIOLATION

    l1_injection = gate.evaluate(
        proposal(l2_catalogue, "PLAN", [step("macro", hidden_action.name)]),
        level=DoFLevel.L1,
        expected_decision_id="decision-001",
        catalogue=l2_catalogue,
        expected_catalogue_hash=digest(l2_catalogue),
        current_stage=DesignStage.ROUTED,
        budget_remaining=budgets(),
        mask=hidden_mask,
    )
    assert l1_injection.endpoint is EndpointCode.MASK_VIOLATION
    assert l1_injection.gated_steps == ()

    projected = DecisionContextProjector().project(
        level=DoFLevel.L2,
        regime=UnknownRegime.FORCED_UNKNOWN,
        source=context_source(),
        catalogue=l2_catalogue,
        mask=hidden_mask,
    )
    assert hidden_action.name not in projected.context["legal_actions"]

    l3_catalogue = strict_l3_catalogue()
    l3 = gate.evaluate(
        proposal(l3_catalogue, "ACTION", [step("unroute", "manual.unroute_all")]),
        level=DoFLevel.L3,
        expected_decision_id="decision-001",
        catalogue=l3_catalogue,
        expected_catalogue_hash=digest(l3_catalogue),
        current_stage=DesignStage.ROUTED,
        budget_remaining=budgets(),
        mask=hidden_mask,
    )
    assert l3.accepted


def test_forced_unknown_recursively_masks_identity_path_sha_and_prior_but_keeps_live_metrics():
    catalogue = strict_l3_catalogue()
    source = context_source()
    sentinel = "secret_benchmark"
    secret_sha = "0123456789abcdef" * 4
    source["condition"]["pseudonym"] = sentinel
    source["state"]["pseudonym"] = sentinel + "_state"
    source["state"]["measurements"]["route"]["status"] = f"report from /archive/{sentinel}.dcp"
    source["history"][0]["endpoint"] = f"known winner {sentinel} {secret_sha}"
    source["validation"]["warnings"] = [f"see results/{sentinel}/winner.json"]
    source["stop_conditions"] = [f"known recipe for {sentinel}"]
    source["known_priors"] = {
        "expected_fmax": 999.0,
        "winner": {"recipe": "flow.retime", "source": f"/tmp/{sentinel}.csv"},
    }

    result = DecisionContextProjector().project(
        level=DoFLevel.L3,
        regime=UnknownRegime.FORCED_UNKNOWN,
        source=source,
        catalogue=catalogue,
        mask=mask(
            forbidden_sentinels=(sentinel, secret_sha, "flow.retime"),
        ),
    )
    serialized = result.serialized.lower()
    assert sentinel not in serialized
    assert secret_sha not in serialized
    assert "flow.retime" not in serialized
    assert "known_priors" not in result.context
    assert result.context["current_state"]["measurements"]["timing"]["wns_ns"] == -0.125
    assert result.context["current_state"]["measurements"]["utilization"]["lut"] == 1234
    assert len(result.context_hash) == 64
    assert json.loads(result.serialized)["legal_actions"] == list(result.context["legal_actions"])
    assert "run_tcl" not in result.serialized


def test_projector_rejects_unknown_nested_fields_and_known_prior_not_explicitly_allowed():
    catalogue = strict_l3_catalogue()
    source = context_source()
    source["state"]["measurements"]["timing"]["benchmark_name"] = "leak"
    with pytest.raises(ProjectionError, match="unknown_fields:benchmark_name"):
        DecisionContextProjector().project(
            level=DoFLevel.L3,
            regime=UnknownRegime.FORCED_UNKNOWN,
            source=source,
            catalogue=catalogue,
            mask=mask(),
        )

    known = context_source()
    known["known_priors"] = {"declared_note": "operator supplied", "winner": "flow.retime"}
    with pytest.raises(ProjectionError, match="known_priors_not_allowlisted:winner"):
        DecisionContextProjector().project(
            level=DoFLevel.L3,
                regime=UnknownRegime.KNOWN,
                source=known,
                catalogue=catalogue,
                mask=mask(known_prior_allowlist=("declared_note",)),
        )


def test_known_projector_exposes_only_explicitly_declared_prior_fields():
    catalogue = strict_l3_catalogue()
    source = context_source()
    source["known_priors"] = {"operator_note": {"source": "declared", "weight": 0.25}}
    projected = DecisionContextProjector().project(
        level=DoFLevel.L3,
        regime=UnknownRegime.KNOWN,
        source=source,
        catalogue=catalogue,
        mask=mask(known_prior_allowlist=("operator_note",)),
    )
    assert projected.context["known_priors"] == {
        "operator_note": {"source": "declared", "weight": 0.25}
    }


def test_projected_serialization_and_hash_are_reproducible():
    catalogue = strict_l3_catalogue()
    kwargs = dict(
        level=DoFLevel.L3,
        regime=UnknownRegime.FORCED_UNKNOWN,
        source=context_source(),
        catalogue=catalogue,
        mask=mask(),
    )
    first = DecisionContextProjector().project(**kwargs)
    second = DecisionContextProjector().project(**kwargs)
    assert first.serialized == second.serialized
    assert first.context_hash == second.context_hash
