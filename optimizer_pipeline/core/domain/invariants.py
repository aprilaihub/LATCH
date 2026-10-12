"""Cross-fact invariants that enforce publication and parent safety."""

from __future__ import annotations

from dataclasses import dataclass

from .enums import ParentUseMode, ValidationLevel, ValidationStatus
from .facts import (
    DesignState,
    DomainInvariantError,
    ResultSelection,
    StateEligibility,
    ValidationRecord,
)


@dataclass(frozen=True, slots=True)
class ManagedStateFacts:
    state: DesignState
    attempt_validation: ValidationRecord
    eligibility: StateEligibility

    def __post_init__(self) -> None:
        if not self.state.managed:
            raise DomainInvariantError("managed state facts require state.managed=true")
        run_ids = {self.state.run_id, self.attempt_validation.run_id, self.eligibility.run_id}
        if len(run_ids) != 1:
            raise DomainInvariantError("state, validation, and eligibility must belong to one run")
        if self.attempt_validation.level is not ValidationLevel.ATTEMPT:
            raise DomainInvariantError("managed state requires Attempt Validation")
        if self.attempt_validation.subject_id != self.state.state_id:
            raise DomainInvariantError("Attempt Validation subject must be the state")
        if self.eligibility.state_id != self.state.state_id:
            raise DomainInvariantError("eligibility must name the state")
        if self.attempt_validation.status is ValidationStatus.INVALID and (
            self.eligibility.parent_eligible or self.eligibility.selection_eligible
        ):
            raise DomainInvariantError("INVALID state cannot be parent-eligible or selection-eligible")


@dataclass(frozen=True, slots=True)
class ParentAuthorization:
    state_id: str
    mode: ParentUseMode
    reason: str


def authorize_parent(facts: ManagedStateFacts, *, mode: ParentUseMode, reason: str) -> ParentAuthorization:
    if facts.eligibility.parent_eligible:
        return ParentAuthorization(state_id=facts.state.state_id, mode=mode, reason=reason)
    if mode is ParentUseMode.EXPLICIT_INTERVENTION and facts.attempt_validation.status is ValidationStatus.INVALID:
        if not reason.strip():
            raise DomainInvariantError("explicit invalid-parent authorization requires a reason")
        return ParentAuthorization(state_id=facts.state.state_id, mode=mode, reason=reason)
    raise DomainInvariantError("state is not eligible as a parent for this execution mode")


def validate_selection(selection: ResultSelection, facts: ManagedStateFacts) -> None:
    if selection.run_id != facts.state.run_id or selection.state_id != facts.state.state_id:
        raise DomainInvariantError("selection must reference the same run and state")
    if selection.artifact_id != facts.state.artifact_id:
        raise DomainInvariantError("selection artifact must match the selected state artifact")
    if not facts.eligibility.selection_eligible:
        raise DomainInvariantError("state is not selection-eligible")
    if facts.attempt_validation.status is ValidationStatus.INVALID:
        raise DomainInvariantError("INVALID state can never be selected")

