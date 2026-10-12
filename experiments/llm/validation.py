"""Typed proposal validation before an experiment can reach execution."""

from __future__ import annotations

from optimizer_pipeline.backends import ActionCatalog
from optimizer_pipeline.capabilities.intervention import InterventionCatalog
from optimizer_pipeline.core.domain import Proposal

from .contracts import (
    AdapterProposal,
    ProposalValidation,
    QuerySnapshot,
    ValidatedProposal,
)


class TypedProposalValidator:
    def __init__(
        self,
        intervention_catalog: InterventionCatalog,
        backend_catalog: ActionCatalog,
    ) -> None:
        self.intervention_catalog = intervention_catalog
        self.backend_catalog = backend_catalog

    def validate(
        self,
        snapshot: QuerySnapshot,
        proposal: AdapterProposal,
    ) -> ProposalValidation:
        errors: list[str] = []
        if proposal.query_id != snapshot.query_id:
            errors.append("proposal query_id does not match snapshot")
        if proposal.parent_state_id != snapshot.parent_state_id:
            errors.append("proposal parent_state_id does not match snapshot")
        if proposal.intervention.parent_state_id != snapshot.parent_state_id:
            errors.append("intervention parent_state_id does not match snapshot")
        used = {step.primitive for step in proposal.intervention.steps}
        outside_allowlist = sorted(used - set(snapshot.allowed_primitives))
        if outside_allowlist:
            errors.append(
                "proposal uses primitives outside snapshot allowlist: "
                + ", ".join(outside_allowlist)
            )
        if errors:
            return ProposalValidation(False, errors=tuple(errors))

        try:
            compiled = self.intervention_catalog.compile(
                proposal.intervention,
                run_id=snapshot.run_id,
                scope_id=snapshot.scope_id,
                backend_catalog=self.backend_catalog,
                action_id=lambda index, step: f"{proposal.proposal_id}:action:{index}",
            )
        except (TypeError, ValueError) as exc:
            return ProposalValidation(False, errors=(f"typed proposal rejected: {exc}",))

        core_proposals = tuple(
            Proposal(
                proposal_id=f"{proposal.proposal_id}:{index}",
                run_id=snapshot.run_id,
                scope_id=snapshot.scope_id,
                parent_state_id=snapshot.parent_state_id,
                action_id=action.action_id,
                source="external_experiment_adapter",
                policy_metadata={"rationale": proposal.rationale},
            )
            for index, action in enumerate(compiled.actions)
        )
        return ProposalValidation(
            True,
            ValidatedProposal(proposal, compiled, core_proposals),
        )
