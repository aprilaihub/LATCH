"""Stable read models derived solely from canonical run facts."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict

from .sqlite import SQLiteRunStore


def load_run_view(run_dir_or_db: str | Path) -> Dict[str, Any]:
    path = Path(run_dir_or_db)
    db_path = path if path.is_file() or path.suffix == ".db" else path / "run.db"
    with SQLiteRunStore(db_path, migrate=False) as store:
        return build_run_view(store)


def build_run_view(store: SQLiteRunStore) -> Dict[str, Any]:
    run = store.get_run()
    if run is None:
        raise ValueError("run.db has no Run fact")
    actions = {item.action_id: item for item in store.actions()}
    proposals = {item.proposal_id: item for item in store.proposals()}
    outcomes = {item.attempt_id: item for item in store.outcomes()}
    states_by_attempt = {
        item.produced_by_attempt_id: item
        for item in store.states()
        if item.produced_by_attempt_id is not None
    }
    artifacts = {item.artifact_id: item for item in store.artifacts()}
    validations = {item.attempt_id: item for item in store.validations() if item.attempt_id}
    eligibility = {item.state_id: item for item in store.eligibility()}
    selected_ids = {item.state_id for item in store.selections()}
    latest_selected_id = selections[-1].state_id if (selections := list(store.selections())) else None
    attempt_rows = []
    for attempt in store.attempts():
        action = actions[attempt.action_id]
        proposal = proposals.get(attempt.proposal_id or "")
        outcome = outcomes.get(attempt.attempt_id)
        state = states_by_attempt.get(attempt.attempt_id)
        artifact = artifacts.get(state.artifact_id) if state else None
        validation = validations.get(attempt.attempt_id)
        eligible = eligibility.get(state.state_id) if state else None
        attempt_rows.append({
            "attempt_id": attempt.attempt_id,
            "scope_id": attempt.scope_id,
            "parent_state_id": attempt.parent_state_id,
            "action_id": action.action_id,
            "action_type": action.action_type,
            "parameters": action.parameters.to_dict(),
            "proposal_id": attempt.proposal_id,
            "source": proposal.source if proposal else None,
            "policy_metadata": proposal.policy_metadata.to_dict() if proposal else {},
            "status": outcome.status.value if outcome else "ORPHANED",
            "output_state_id": state.state_id if state else None,
            "output_uri": artifact.uri if artifact else None,
            "metrics": outcome.metrics.to_dict() if outcome else {},
            "validation_status": validation.status.value if validation else "NOT_RUN",
            "parent_eligible": eligible.parent_eligible if eligible else False,
            "selection_eligible": eligible.selection_eligible if eligible else False,
            "selected": bool(state and state.state_id in selected_ids),
            "selected_latest": bool(state and state.state_id == latest_selected_id),
            "error_type": outcome.error_type if outcome else None,
            "error_message": outcome.error_message if outcome else None,
        })
    latest_selection = selections[-1] if selections else None
    selected_artifact = artifacts.get(latest_selection.artifact_id) if latest_selection else None
    detail_events = [event.detail.to_dict() for event in store.run_events() if event.detail]
    intervention = next(
        (detail.get("payload") for detail in reversed(detail_events) if detail.get("kind") == "intervention_decision"),
        None,
    )
    analysis_sessions = [
        detail.get("payload")
        for detail in detail_events
        if detail.get("kind") == "analysis_session"
    ]
    budget_events = store.budget_events()
    selected_validations = store.validations(subject_id=latest_selected_id) if latest_selected_id else ()
    latest_by_level = {item.level.value: item for item in selected_validations}
    return {
        "schema_version": "managed_run_view.v1",
        "run_id": run.run_id,
        "mode": run.mode,
        "run_status": store.run_events()[-1].status.value if store.run_events() else "CREATED",
        "scopes": [scope.scope_id for scope in store.scopes()],
        "attempts": attempt_rows,
        "attempt_count": len(attempt_rows),
        "orphan_attempt_ids": [row["attempt_id"] for row in attempt_rows if row["status"] == "ORPHANED"],
        "states": [state.state_id for state in store.states()],
        "root_metrics": next((state.metadata.to_dict().get("metrics", {}) for state in store.states() if state.parent_state_id is None), {}),
        "selected_state_id": latest_selection.state_id if latest_selection else None,
        "selected_output_uri": selected_artifact.uri if selected_artifact else None,
        "intervention_decision": intervention,
        "analysis_sessions": analysis_sessions,
        "validation": {
            level: {
                "status": item.status.value,
                "validation_id": item.validation_id,
                "details": item.details.to_dict(),
            }
            for level, item in latest_by_level.items()
        },
        "budget": {
            "reserved": sum(event.kind.value == "RESERVED" for event in budget_events),
            "completed": sum(event.kind.value == "COMPLETED" for event in budget_events),
            "failed": sum(event.kind.value == "FAILED" for event in budget_events),
        },
        "integrity_errors": list(store.integrity_check()),
    }
