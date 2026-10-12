from __future__ import annotations

import asyncio

from experiments.llm import (
    AdapterProposal,
    ProposalAdapter,
    QuerySnapshot,
    RoundFeedback,
    TypedProposalValidator,
    run_closed_loop,
)
from optimizer_pipeline.backends import ActionCatalog, ActionDefinition
from optimizer_pipeline.capabilities.intervention import (
    InterventionCatalog,
    InterventionKind,
    InterventionPrimitive,
    InterventionSpec,
    InterventionStep,
)
from optimizer_pipeline.core.domain import Proposal


PRIMITIVES = InterventionCatalog(
    (InterventionPrimitive("flow.phys_opt", "phys_opt"),)
)
BACKEND = ActionCatalog(
    (ActionDefinition("phys_opt", "Existing physical optimization flow", allow_extra_parameters=True),)
)


def intervention(proposal_id: str, *, parent: str = "state-1") -> InterventionSpec:
    return InterventionSpec(
        f"intervention-{proposal_id}",
        InterventionKind.SYNTHESIZED_RECIPE,
        parent,
        (InterventionStep("step-1", "flow.phys_opt", {"directive": "Explore"}),),
        "external_mock_adapter",
    )


class MockSnapshots:
    def __init__(self) -> None:
        self.feedback_lengths: list[int] = []

    async def snapshot(self, round_index, feedback):
        self.feedback_lengths.append(len(feedback))
        return QuerySnapshot(
            f"query-{round_index}",
            "run-1",
            "scope-1",
            "state-1",
            round_index,
            ("flow.phys_opt",),
            {"timing": {"wns": -0.5}},
            feedback,
        )


class MockAdapter:
    def __init__(self) -> None:
        self.seen_feedback_lengths: list[int] = []

    async def propose(self, snapshot):
        self.seen_feedback_lengths.append(len(snapshot.feedback))
        if snapshot.round_index == 0:
            return AdapterProposal(
                "proposal-0",
                "wrong-query",
                snapshot.parent_state_id,
                intervention("0"),
                "Intentionally invalid identity",
            )
        return AdapterProposal(
            f"proposal-{snapshot.round_index}",
            snapshot.query_id,
            snapshot.parent_state_id,
            intervention(str(snapshot.round_index)),
            "Try typed physical optimization after feedback",
        )


class MockExecutor:
    def __init__(self) -> None:
        self.calls = []

    async def execute(self, round_index, proposal):
        self.calls.append(proposal)
        return RoundFeedback(
            round_index,
            proposal.adapter_proposal.proposal_id,
            True,
            round_index < 2,
            "clean but continue" if round_index < 2 else "selection eligible",
            {"selection_eligible": round_index >= 2},
        )


def test_external_adapter_owns_multi_round_feedback_and_rejects_before_execution():
    snapshots = MockSnapshots()
    adapter = MockAdapter()
    executor = MockExecutor()
    validator = TypedProposalValidator(PRIMITIVES, BACKEND)

    result = asyncio.run(
        run_closed_loop(
            adapter=adapter,
            snapshots=snapshots,
            validator=validator,
            executor=executor,
            max_rounds=4,
        )
    )

    assert isinstance(adapter, ProposalAdapter)
    assert result.stop_reason == "executor_stop"
    assert result.rounds == 3
    assert adapter.seen_feedback_lengths == [0, 1, 2]
    assert snapshots.feedback_lengths == [0, 1, 2]
    assert len(executor.calls) == 2
    assert not result.feedback[0].accepted
    assert "query_id" in result.feedback[0].summary
    assert all(
        isinstance(proposal, Proposal)
        for validated in result.validated_proposals
        for proposal in validated.core_proposals
    )
    assert result.validated_proposals[0].core_proposals[0].source == "external_experiment_adapter"


def test_typed_validator_blocks_parent_and_allowlist_violations():
    snapshot = asyncio.run(MockSnapshots().snapshot(0, ()))
    validator = TypedProposalValidator(PRIMITIVES, BACKEND)
    wrong_parent = AdapterProposal(
        "proposal-wrong-parent",
        snapshot.query_id,
        "state-other",
        intervention("wrong", parent="state-other"),
        "wrong parent",
    )
    validation = validator.validate(snapshot, wrong_parent)
    assert not validation.accepted
    assert any("parent_state_id" in error for error in validation.errors)

    outside = AdapterProposal(
        "proposal-outside",
        snapshot.query_id,
        snapshot.parent_state_id,
        InterventionSpec(
            "intervention-outside",
            InterventionKind.SYNTHESIZED_RECIPE,
            snapshot.parent_state_id,
            (InterventionStep("step-1", "manual.route_design"),),
            "external_mock_adapter",
        ),
        "outside allowlist",
    )
    validation = validator.validate(snapshot, outside)
    assert not validation.accepted
    assert any("outside snapshot allowlist" in error for error in validation.errors)


def test_query_snapshot_and_feedback_are_deeply_immutable():
    context = {"nested": {"rows": [1, 2]}}
    snapshot = QuerySnapshot(
        "query-1", "run-1", "scope-1", "state-1", 0,
        ("flow.phys_opt",), context,
    )
    context["nested"]["rows"].append(3)
    assert snapshot.context.to_dict() == {"nested": {"rows": [1, 2]}}
