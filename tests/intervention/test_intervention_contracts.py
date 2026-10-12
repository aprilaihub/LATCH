from __future__ import annotations

from datetime import datetime, timezone
import asyncio
from types import SimpleNamespace

import pytest

from optimizer_pipeline.backends import (
    ActionCatalog,
    ActionDefinition,
    ActionParameter,
    ParameterType,
    UnsafeManagedActionError,
    UnsafeManagedActionError,
)
from optimizer_pipeline.capabilities.intervention import (
    InterventionCatalog,
    InterventionCapability,
    InterventionContractError,
    InterventionDiagnosis,
    InterventionKind,
    InterventionLoopPolicy,
    InterventionParentPolicy,
    InterventionPrimitive,
    InterventionSpec,
    InterventionStep,
    InterventionSnapshot,
    NoImprovementInterventionStrategy,
    authorize_intervention_parent,
)
from optimizer_pipeline.core.domain import (
    DesignState,
    ManagedStateFacts,
    ParentUseMode,
    StateEligibility,
    ValidationLevel,
    ValidationRecord,
    ValidationStatus,
)


NOW = datetime(2026, 1, 2, tzinfo=timezone.utc)
BACKEND = ActionCatalog(
    (
        ActionDefinition("phys_opt", "Existing optimizer flow", allow_extra_parameters=True),
        ActionDefinition("unroute_design", "Typed fixed unroute action"),
        ActionDefinition("unplace_design", "Typed fixed unplace action"),
        ActionDefinition(
            "place_design", "Typed Vivado placement",
            (ActionParameter("directive", ParameterType.STRING),),
        ),
        ActionDefinition(
            "phys_opt_design", "Typed Vivado physical optimization",
            (ActionParameter("directive", ParameterType.STRING),),
        ),
        ActionDefinition(
            "route_design", "Typed Vivado routing",
            (ActionParameter("directive", ParameterType.STRING),),
        ),
        ActionDefinition("vendor_escape", "Unmanaged vendor escape hatch", managed_safe=False),
    )
)
CATALOG = InterventionCatalog(
    (
        InterventionPrimitive("flow.phys_opt", "phys_opt"),
        InterventionPrimitive("manual.unroute_all", "unroute_design"),
        InterventionPrimitive("manual.unplace_all", "unplace_design"),
        InterventionPrimitive("manual.place_design", "place_design"),
        InterventionPrimitive("manual.phys_opt_design", "phys_opt_design"),
        InterventionPrimitive("manual.route_design", "route_design"),
    )
)


def compile_spec(spec: InterventionSpec):
    return CATALOG.compile(
        spec,
        run_id="run-1",
        scope_id="scope-intervention",
        backend_catalog=BACKEND,
        action_id=lambda index, step: f"action-{index}-{step.step_id}",
    )


def state_facts(status: ValidationStatus, *, eligible: bool) -> ManagedStateFacts:
    state = DesignState(
        "state-1", "run-1", "scope-intervention", "artifact-1",
        "state-root", "attempt-1", created_at=NOW,
    )
    validation = ValidationRecord(
        "validation-1", "run-1", "scope-intervention", state.state_id,
        ValidationLevel.ATTEMPT, status, "attempt-1", NOW,
    )
    eligibility = StateEligibility(
        "eligibility-1", "run-1", state.state_id,
        eligible, eligible, status.value, NOW,
    )
    return ManagedStateFacts(state, validation, eligibility)


def test_deterministic_recipe_compiles_to_existing_backend_action_name():
    spec = InterventionSpec(
        "intervention-1",
        InterventionKind.DETERMINISTIC_RECIPE,
        "state-1",
        (InterventionStep("step-1", "flow.phys_opt", {"directive": "Explore"}),),
        "deterministic_policy",
        hypothesis="Physical optimization may recover timing.",
    )
    compiled = compile_spec(spec)
    assert [action.action_type for action in compiled.actions] == ["phys_opt"]
    assert compiled.actions[0].parameters.to_dict() == {"directive": "Explore"}


def test_synthesized_typed_recipe_can_express_reset_place_opt_route_sequence():
    spec = InterventionSpec(
        "intervention-2",
        InterventionKind.SYNTHESIZED_RECIPE,
        "state-1",
        (
            InterventionStep("unroute", "manual.unroute_all"),
            InterventionStep("unplace", "manual.unplace_all"),
            InterventionStep("place", "manual.place_design", {"directive": "Explore"}),
            InterventionStep("opt", "manual.phys_opt_design", {"directive": "AddRetime"}),
            InterventionStep("route", "manual.route_design", {"directive": "Explore"}),
        ),
        "typed_recipe_synthesis",
    )
    assert [action.action_type for action in compile_spec(spec).actions] == [
        "unroute_design", "unplace_design", "place_design",
        "phys_opt_design", "route_design",
    ]


def test_manual_multi_step_plan_retains_revision_and_abort_conditions():
    spec = InterventionSpec(
        "manual-1",
        InterventionKind.MANUAL_PLAN,
        "state-1",
        (
            InterventionStep(
                "unroute", "manual.unroute_all",
                rationale="Reset routing only.",
                expected_observation="Placement remains available.",
            ),
            InterventionStep("route", "manual.route_design", {"directive": "Explore"}),
        ),
        "operator",
        hypothesis="Routing is the bottleneck.",
        revise_conditions=("congestion worsens",),
        abort_conditions=("attempt budget exhausted",),
    )
    compiled = compile_spec(spec)
    assert len(compiled.actions) == 2
    assert spec.revise_conditions == ("congestion worsens",)
    assert spec.abort_conditions == ("attempt budget exhausted",)


def test_raw_shell_or_tcl_bypass_cannot_enter_managed_intervention():
    with pytest.raises(InterventionContractError, match="raw command action"):
        InterventionPrimitive("unsafe", "run_tcl")
    with pytest.raises(InterventionContractError, match="raw command parameters"):
        InterventionStep("unsafe", "manual.route_design", {"tcl_source": "route_design"})
    with pytest.raises(InterventionContractError, match="raw command action"):
        InterventionSpec(
            "unsafe-spec", InterventionKind.MANUAL_PLAN, "state-1",
            (InterventionStep("step-1", "shell"),), "operator",
        )

    unsafe_backend = ActionCatalog(
        (ActionDefinition("internal_tcl_adapter", "Internal only", managed_safe=False),)
    )
    unsafe_catalog = InterventionCatalog(
        (InterventionPrimitive("manual.internal", "internal_tcl_adapter"),)
    )
    safe_syntax_unsafe_backend = InterventionSpec(
        "unsafe-backend", InterventionKind.MANUAL_PLAN, "state-1",
        (InterventionStep("step-1", "manual.internal"),), "operator",
    )
    with pytest.raises(UnsafeManagedActionError, match="managed path"):
        unsafe_catalog.compile(
            safe_syntax_unsafe_backend,
            run_id="run-1",
            scope_id="scope-intervention",
            backend_catalog=unsafe_backend,
            action_id=lambda index, step: f"unsafe-{index}",
        )
    unsafe_catalog = InterventionCatalog(
        (InterventionPrimitive("unsafe.escape", "vendor_escape"),)
    )
    unsafe_spec = InterventionSpec(
        "unsafe-backend", InterventionKind.MANUAL_PLAN, "state-1",
        (InterventionStep("step-1", "unsafe.escape"),), "operator",
    )
    with pytest.raises(UnsafeManagedActionError, match="not allowed in a managed path"):
        unsafe_catalog.compile(
            unsafe_spec,
            run_id="run-1",
            scope_id="scope-intervention",
            backend_catalog=BACKEND,
            action_id=lambda index, step: f"unsafe-{index}",
        )


def test_invalid_parent_requires_explicit_intervention_opt_in_and_reason():
    spec = InterventionSpec(
        "intervention-3", InterventionKind.MANUAL_PLAN, "state-1",
        (InterventionStep("route", "manual.route_design"),), "operator",
    )
    invalid = state_facts(ValidationStatus.INVALID, eligible=False)
    with pytest.raises(ValueError, match="not eligible"):
        authorize_intervention_parent(spec, invalid, InterventionParentPolicy())
    authorization = authorize_intervention_parent(
        spec,
        invalid,
        InterventionParentPolicy(
            ParentUseMode.EXPLICIT_INTERVENTION,
            "inspect a canonical invalid scratch state",
        ),
    )
    assert authorization.mode is ParentUseMode.EXPLICIT_INTERVENTION


def test_diagnosis_and_loop_policy_express_feedback_without_provider_metadata():
    diagnosis = InterventionDiagnosis(
        "diagnosis-1", "state-1", "Routing regressed after a clean attempt.",
        {"delta_mhz": -2.0},
        prefer_primitives=("manual.unroute_all",),
        avoid_primitives=("flow.phys_opt",),
    )
    policy = InterventionLoopPolicy(
        max_attempts=4, max_episodes=2,
        stop_on_first_selection_eligible=False,
        allow_invalid_continuation=True,
    )
    assert diagnosis.evidence.to_dict() == {"delta_mhz": -2.0}
    assert policy.allow_invalid_continuation
    with pytest.raises(InterventionContractError, match="both preferred and avoided"):
        InterventionDiagnosis(
            "bad", "state-1", "conflict",
            prefer_primitives=("same",), avoid_primitives=("same",),
        )


@pytest.mark.parametrize(
    ("trigger", "delta", "threshold", "execute", "reason"),
    (
        ("always", 100.0, 0.1, True, "always_trigger"),
        ("no_improvement", None, 0.1, False, "missing_fmax_for_no_improvement_gate"),
        ("no_improvement", 0.1, 0.1, True, "no_improvement_threshold_met"),
        ("no_improvement", 0.2, 0.1, False, "main_profile_already_improved"),
    ),
)
def test_intervention_strategy_owns_trigger_gate(trigger, delta, threshold, execute, reason):
    snapshot = InterventionSnapshot(400.0, None, delta, 3, False)

    decision = NoImprovementInterventionStrategy(trigger, threshold).decide(snapshot)

    assert decision.execute is execute
    assert decision.reason == reason


def test_intervention_capability_passes_strategy_decision_to_guarded_executor():
    root = SimpleNamespace(metrics={"final_fmax_mhz": 400.0})
    ctx = SimpleNamespace(
        state=SimpleNamespace(best_node_id="root"),
        nodes={"root": root},
        current_node=root,
        root_metrics={"initial_fmax_mhz": 400.0},
        budget=SimpleNamespace(remaining_eda_runs=2),
        finalized=False,
    )
    capability = InterventionCapability(ctx)
    calls = []

    class GuardedExecutor:
        async def run(self, **kwargs):
            calls.append(kwargs)
            return kwargs

    capability._executor = GuardedExecutor()
    result = asyncio.run(
        capability.run(
            NoImprovementInterventionStrategy("no_improvement", 0.1),
            mode="deterministic",
        )
    )

    assert result["gate_decision"] == (True, "no_improvement_threshold_met")
    assert calls == [result]
