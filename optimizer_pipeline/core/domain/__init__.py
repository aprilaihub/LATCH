"""Stable immutable facts and invariants for managed experiments."""

from .enums import (
    ArtifactKind,
    AttemptEventKind,
    BudgetEventKind,
    OutcomeStatus,
    ParentUseMode,
    RunStatus,
    ScopeKind,
    SelectionKind,
    ValidationLevel,
    ValidationStatus,
)
from .facts import (
    ActionSpecification,
    ArtifactReference,
    Attempt,
    AttemptLifecycleEvent,
    BudgetEvent,
    DesignState,
    DomainInvariantError,
    ExecutionOutcome,
    ExecutionScope,
    Proposal,
    ResultSelection,
    Run,
    RunLifecycleEvent,
    StateEligibility,
    ValidationRecord,
    utc_now,
)
from .invariants import ManagedStateFacts, ParentAuthorization, authorize_parent, validate_selection
from .value import FrozenMap, frozen_map

__all__ = [name for name in globals() if not name.startswith("_")]

