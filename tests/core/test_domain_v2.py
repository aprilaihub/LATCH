from dataclasses import FrozenInstanceError
from datetime import timedelta

import pytest

from optimizer_pipeline.core.domain import (
    ActionSpecification, ArtifactKind, ArtifactReference, Attempt,
    AttemptEventKind, AttemptLifecycleEvent, BudgetEvent, BudgetEventKind,
    DesignState, DomainInvariantError, ExecutionOutcome, ExecutionScope,
    FrozenMap, ManagedStateFacts, OutcomeStatus, ParentUseMode, Proposal,
    ResultSelection, Run, ScopeKind, SelectionKind, StateEligibility,
    ValidationLevel, ValidationRecord, ValidationStatus, authorize_parent,
    frozen_map, utc_now, validate_selection,
)


def state_facts(status=ValidationStatus.CLEAN, *, parent=True, selection=True):
    state = DesignState(
        state_id="state-1", run_id="run-1", scope_id="scope-1",
        artifact_id="artifact-1", parent_state_id="state-root",
        produced_by_attempt_id="attempt-1", managed=True,
    )
    validation = ValidationRecord(
        validation_id="validation-1", run_id=state.run_id,
        scope_id=state.scope_id, subject_id=state.state_id,
        level=ValidationLevel.ATTEMPT, status=status,
        attempt_id=state.produced_by_attempt_id,
    )
    eligibility = StateEligibility(
        eligibility_id="eligibility-1", run_id=state.run_id,
        state_id=state.state_id, parent_eligible=parent,
        selection_eligible=selection, reason="test eligibility",
    )
    return ManagedStateFacts(state, validation, eligibility)


def test_facts_and_nested_metadata_are_immutable():
    run = Run(run_id="run-1", mode="managed", metadata={"policy": {"model": "external"}, "rounds": [1, 2]})
    assert isinstance(run.metadata, FrozenMap)
    assert run.metadata.to_dict() == {"policy": {"model": "external"}, "rounds": [1, 2]}
    with pytest.raises(FrozenInstanceError):
        run.mode = "changed"
    with pytest.raises(TypeError):
        frozen_map({"bad": object()})


def test_scope_hierarchy_is_explicit_inside_one_run():
    root = ExecutionScope("root", "run-1", ScopeKind.ROOT, "root")
    child = ExecutionScope("frontier", "run-1", ScopeKind.FRONTIER, "frontier-0", root.scope_id)
    assert child.parent_scope_id == root.scope_id
    with pytest.raises(DomainInvariantError, match="root scope"):
        ExecutionScope("bad", "run-1", ScopeKind.ROOT, "bad", "parent")
    with pytest.raises(DomainInvariantError, match="requires parent"):
        ExecutionScope("bad", "run-1", ScopeKind.FIXED, "bad")


def test_invalid_scratch_is_canonical_but_never_normally_eligible():
    facts = state_facts(ValidationStatus.INVALID, parent=False, selection=False)
    assert facts.state.managed and facts.state.artifact_id
    assert facts.state.parent_state_id and facts.state.produced_by_attempt_id
    with pytest.raises(DomainInvariantError, match="not eligible"):
        authorize_parent(facts, mode=ParentUseMode.NORMAL, reason="frontier")
    explicit = authorize_parent(
        facts, mode=ParentUseMode.EXPLICIT_INTERVENTION,
        reason="manual episode continues invalid scratch",
    )
    assert explicit.state_id == facts.state.state_id
    with pytest.raises(DomainInvariantError, match="requires a reason"):
        authorize_parent(facts, mode=ParentUseMode.EXPLICIT_INTERVENTION, reason=" ")


@pytest.mark.parametrize("parent,selection", [(True, False), (False, True), (True, True)])
def test_invalid_validation_rejects_any_eligibility_smuggling(parent, selection):
    with pytest.raises(DomainInvariantError, match="INVALID state"):
        state_facts(ValidationStatus.INVALID, parent=parent, selection=selection)


def test_selection_matches_state_artifact_and_rejects_invalid():
    clean = state_facts()
    selected = ResultSelection(
        "selection-1", clean.state.run_id, clean.state.scope_id,
        clean.state.state_id, clean.state.artifact_id,
        SelectionKind.BEST_VALID, "best valid QoR",
    )
    validate_selection(selected, clean)
    invalid = state_facts(ValidationStatus.INVALID, parent=False, selection=False)
    with pytest.raises(DomainInvariantError, match="selection-eligible"):
        validate_selection(selected, invalid)


def test_attempt_lifecycle_links_all_stable_fact_types():
    now = utc_now()
    artifact = ArtifactReference("artifact-1", "run-1", "scope-1", ArtifactKind.DCP, "output.dcp", size_bytes=12)
    action = ActionSpecification("action-1", "run-1", "scope-1", "phys_opt", {"directive": "Explore"})
    proposal = Proposal(
        "proposal-1", "run-1", "scope-1", "state-root", action.action_id,
        "deterministic", policy_metadata={"rank": 1},
    )
    attempt = Attempt("attempt-1", "run-1", "scope-1", "state-root", action.action_id, proposal.proposal_id)
    events = (
        AttemptLifecycleEvent("event-1", attempt.attempt_id, 0, AttemptEventKind.SCHEDULED),
        AttemptLifecycleEvent("event-2", attempt.attempt_id, 1, AttemptEventKind.STARTED),
    )
    outcome = ExecutionOutcome(
        "outcome-1", attempt.attempt_id, OutcomeStatus.SUCCEEDED,
        now, now + timedelta(seconds=2), artifact.artifact_id,
        metrics={"wns_ns": -0.9},
    )
    budget = BudgetEvent("budget-1", "run-1", "scope-1", attempt.attempt_id, BudgetEventKind.COMPLETED)
    state = DesignState("state-1", "run-1", "scope-1", artifact.artifact_id, "state-root", attempt.attempt_id)
    assert [event.sequence for event in events] == [0, 1]
    assert outcome.output_artifact_id == state.artifact_id
    assert budget.attempt_id == state.produced_by_attempt_id


def test_success_and_attempt_validation_require_links():
    now = utc_now()
    with pytest.raises(DomainInvariantError, match="output_artifact_id"):
        ExecutionOutcome("outcome", "attempt", OutcomeStatus.SUCCEEDED, now, now)
    with pytest.raises(DomainInvariantError, match="attempt_id"):
        ValidationRecord(
            "validation", "run", "scope", "state",
            ValidationLevel.ATTEMPT, ValidationStatus.CLEAN,
        )
