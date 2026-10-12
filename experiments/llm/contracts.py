"""External, in-process proposal and feedback contracts for LLM experiments."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from optimizer_pipeline.capabilities.intervention import (
    CompiledIntervention,
    InterventionSpec,
)
from optimizer_pipeline.core.domain import FrozenMap, Proposal, frozen_map


@dataclass(frozen=True, slots=True)
class RoundFeedback:
    round_index: int
    proposal_id: str
    accepted: bool
    continue_loop: bool
    summary: str
    evidence: FrozenMap = field(default_factory=FrozenMap)

    def __post_init__(self) -> None:
        if self.round_index < 0:
            raise ValueError("round_index cannot be negative")
        if not self.proposal_id.strip() or not self.summary.strip():
            raise ValueError("proposal_id and feedback summary are required")
        object.__setattr__(self, "evidence", frozen_map(self.evidence))


@dataclass(frozen=True, slots=True)
class QuerySnapshot:
    query_id: str
    run_id: str
    scope_id: str
    parent_state_id: str
    round_index: int
    allowed_primitives: tuple[str, ...]
    context: FrozenMap = field(default_factory=FrozenMap)
    feedback: tuple[RoundFeedback, ...] = ()

    def __post_init__(self) -> None:
        for name in ("query_id", "run_id", "scope_id", "parent_state_id"):
            if not getattr(self, name).strip():
                raise ValueError(f"{name} is required")
        if self.round_index < 0:
            raise ValueError("round_index cannot be negative")
        object.__setattr__(self, "allowed_primitives", tuple(self.allowed_primitives))
        object.__setattr__(self, "feedback", tuple(self.feedback))
        object.__setattr__(self, "context", frozen_map(self.context))
        if not self.allowed_primitives:
            raise ValueError("query snapshot requires an intervention primitive allowlist")
        if len(self.allowed_primitives) != len(set(self.allowed_primitives)):
            raise ValueError("allowed_primitives contains duplicates")
        if any(item.round_index >= self.round_index for item in self.feedback):
            raise ValueError("query snapshot feedback must come from earlier rounds")


@dataclass(frozen=True, slots=True)
class AdapterProposal:
    proposal_id: str
    query_id: str
    parent_state_id: str
    intervention: InterventionSpec
    rationale: str

    def __post_init__(self) -> None:
        for name in ("proposal_id", "query_id", "parent_state_id", "rationale"):
            if not getattr(self, name).strip():
                raise ValueError(f"{name} is required")


@dataclass(frozen=True, slots=True)
class ValidatedProposal:
    adapter_proposal: AdapterProposal
    compiled: CompiledIntervention
    core_proposals: tuple[Proposal, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "core_proposals", tuple(self.core_proposals))
        if not self.core_proposals:
            raise ValueError("validated proposal requires core Proposal facts")
        if self.compiled.spec != self.adapter_proposal.intervention:
            raise ValueError("compiled intervention does not match adapter proposal")
        if len(self.core_proposals) != len(self.compiled.actions):
            raise ValueError("each compiled action requires exactly one core Proposal fact")
        for action, proposal in zip(self.compiled.actions, self.core_proposals, strict=True):
            if proposal.action_id != action.action_id:
                raise ValueError("core Proposal action does not match compiled action")
            if proposal.parent_state_id != self.adapter_proposal.parent_state_id:
                raise ValueError("core Proposal parent does not match adapter proposal")
            if (proposal.run_id, proposal.scope_id) != (action.run_id, action.scope_id):
                raise ValueError("core Proposal and action must share run and scope")


@dataclass(frozen=True, slots=True)
class ProposalValidation:
    accepted: bool
    validated: ValidatedProposal | None = None
    errors: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "errors", tuple(self.errors))
        if self.accepted != (self.validated is not None and not self.errors):
            raise ValueError("proposal validation result is internally inconsistent")


@runtime_checkable
class ProposalAdapter(Protocol):
    async def propose(self, snapshot: QuerySnapshot) -> AdapterProposal: ...


@runtime_checkable
class SnapshotProvider(Protocol):
    async def snapshot(
        self,
        round_index: int,
        feedback: tuple[RoundFeedback, ...],
    ) -> QuerySnapshot: ...


@runtime_checkable
class ProposalValidator(Protocol):
    def validate(
        self,
        snapshot: QuerySnapshot,
        proposal: AdapterProposal,
    ) -> ProposalValidation: ...


@runtime_checkable
class ProposalExecutor(Protocol):
    async def execute(
        self,
        round_index: int,
        proposal: ValidatedProposal,
    ) -> RoundFeedback: ...


@dataclass(frozen=True, slots=True)
class ClosedLoopResult:
    stop_reason: str
    rounds: int
    validated_proposals: tuple[ValidatedProposal, ...]
    feedback: tuple[RoundFeedback, ...]

    def __post_init__(self) -> None:
        if not self.stop_reason.strip() or self.rounds < 0:
            raise ValueError("closed loop requires a stop reason and non-negative round count")
        object.__setattr__(self, "validated_proposals", tuple(self.validated_proposals))
        object.__setattr__(self, "feedback", tuple(self.feedback))
        if self.rounds != len(self.feedback):
            raise ValueError("closed loop round count must match feedback history")
