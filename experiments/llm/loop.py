"""Closed-loop orchestration owned outside the managed runtime core."""

from __future__ import annotations

from .contracts import (
    ClosedLoopResult,
    ProposalAdapter,
    ProposalExecutor,
    ProposalValidator,
    RoundFeedback,
    SnapshotProvider,
    ValidatedProposal,
)


async def run_closed_loop(
    *,
    adapter: ProposalAdapter,
    snapshots: SnapshotProvider,
    validator: ProposalValidator,
    executor: ProposalExecutor,
    max_rounds: int,
) -> ClosedLoopResult:
    if max_rounds <= 0:
        raise ValueError("max_rounds must be positive")
    feedback: list[RoundFeedback] = []
    validated: list[ValidatedProposal] = []
    stop_reason = "max_rounds"

    for round_index in range(max_rounds):
        snapshot = await snapshots.snapshot(round_index, tuple(feedback))
        if snapshot.round_index != round_index:
            raise ValueError("snapshot provider returned the wrong round")
        proposal = await adapter.propose(snapshot)
        validation = validator.validate(snapshot, proposal)
        if not validation.accepted:
            rejection = RoundFeedback(
                round_index,
                proposal.proposal_id,
                False,
                True,
                "; ".join(validation.errors),
                {"validation_errors": list(validation.errors)},
            )
            feedback.append(rejection)
            continue

        assert validation.validated is not None
        validated.append(validation.validated)
        result = await executor.execute(round_index, validation.validated)
        if result.round_index != round_index:
            raise ValueError("proposal executor returned feedback for the wrong round")
        if result.proposal_id != proposal.proposal_id:
            raise ValueError("proposal executor returned feedback for another proposal")
        feedback.append(result)
        if not result.continue_loop:
            stop_reason = "executor_stop"
            break

    return ClosedLoopResult(
        stop_reason,
        len(feedback),
        tuple(validated),
        tuple(feedback),
    )
