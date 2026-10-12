#!/usr/bin/env python3
"""Import a legacy JSON/JSONL run into a standalone canonical run.db."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from optimizer_pipeline.core.domain import (
    ActionSpecification, ArtifactKind, ArtifactReference, Attempt,
    AttemptEventKind, AttemptLifecycleEvent, BudgetEvent, BudgetEventKind,
    DesignState, ExecutionOutcome, ExecutionScope, OutcomeStatus, Proposal,
    ResultSelection, Run, RunLifecycleEvent, RunStatus, ScopeKind,
    SelectionKind, StateEligibility, ValidationLevel, ValidationRecord,
    ValidationStatus, utc_now,
)
from optimizer_pipeline.core.store import SQLiteRunStore


SOURCE_FILES = ("pipeline_summary.json", "transition_journal.jsonl", "published_best.json")


def _read_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    value = json.loads(path.read_text())
    return value if isinstance(value, dict) else {}


def _source_digest(source: Path) -> tuple[str, list[str]]:
    digest = hashlib.sha256()
    names = []
    for name in SOURCE_FILES:
        path = source / name
        if not path.is_file():
            continue
        names.append(name)
        digest.update(name.encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    if not names:
        raise ValueError(f"no recognized v1 artifacts in {source}")
    return digest.hexdigest(), names


def _materialized_transitions(path: Path) -> list[dict[str, Any]]:
    latest: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    if not path.is_file():
        return []
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        if not isinstance(row, dict) or not isinstance(row.get("intent"), dict):
            continue
        transition_id = str(row["intent"].get("transition_id") or "")
        if not transition_id:
            continue
        if transition_id not in latest:
            order.append(transition_id)
        latest[transition_id] = row
    return [latest[key] for key in order]


def _validation(value: Any, *, has_output: bool) -> ValidationStatus:
    if not has_output:
        return ValidationStatus.INVALID
    try:
        status = ValidationStatus(str(value or "WARNING").upper())
    except ValueError:
        status = ValidationStatus.WARNING
    return status if status in {
        ValidationStatus.CLEAN, ValidationStatus.WARNING,
        ValidationStatus.FALLBACK_USED, ValidationStatus.INVALID,
        ValidationStatus.FAILED,
    } else ValidationStatus.WARNING


def import_v1_run(source_run_dir: Path, output_db: Path) -> SQLiteRunStore:
    """Create or idempotently reopen an imported database without changing source."""

    source = Path(source_run_dir).resolve()
    output = Path(output_db)
    digest, source_files = _source_digest(source)
    if output.exists():
        store = SQLiteRunStore(output, migrate=False)
        run = store.get_run()
        metadata = run.metadata.to_dict() if run else {}
        if metadata.get("historical_source_digest") != digest:
            store.close()
            raise FileExistsError(f"{output} contains a different imported run")
        return store

    summary = _read_json(source / "pipeline_summary.json")
    transitions = _materialized_transitions(source / "transition_journal.jsonl")
    run_id = f"imported-{digest[:12]}"
    mode = str(summary.get("mode") or "historical_v1_unknown")
    store = SQLiteRunStore.create(output, Run(run_id, mode, metadata={
        "historical_import": True,
        "historical_format": "v1-json-artifacts",
        "historical_source": str(source),
        "historical_source_digest": digest,
        "historical_source_files": source_files,
    }))
    scope_id = "scope-root"
    input_dcp = str(summary.get("input_dcp") or "unknown://input-dcp")
    input_path = Path(input_dcp)
    root_artifact = ArtifactReference(
        "artifact-root", run_id, scope_id, ArtifactKind.DCP, input_dcp,
        size_bytes=input_path.stat().st_size if input_path.is_file() else None,
        metadata={"historical_role": "baseline", "unknown_fields_preserved": True},
    )
    root_state = DesignState(
        "node_000_root", run_id, scope_id, root_artifact.artifact_id,
        metadata={"metrics": summary.get("root_metrics") or {}, "historical_import": True},
    )
    with store.transaction():
        store.add_run_event(RunLifecycleEvent(
            "run-event-000", run_id, 0, RunStatus.RUNNING,
            detail={"kind": "historical_import", "source_digest": digest},
        ))
        store.add_scope(ExecutionScope(scope_id, run_id, ScopeKind.ROOT, "imported v1 run"))
        store.add_artifact(root_artifact)
        store.add_state(root_state)
        store.add_validation(ValidationRecord(
            "certification:node_000_root", run_id, scope_id, root_state.state_id,
            ValidationLevel.CERTIFICATION, ValidationStatus.NOT_RUN,
            details={"reason": "unknown in historical source"},
        ))
        store.add_eligibility(StateEligibility(
            "eligibility-root", run_id, root_state.state_id, True, True,
            "historical baseline",
        ))
        store.add_selection(ResultSelection(
            "selection-baseline", run_id, scope_id, root_state.state_id,
            root_artifact.artifact_id, SelectionKind.BASELINE,
            "historical safe baseline",
        ))

    selected_state = root_state.state_id
    selected_scope = scope_id
    for index, row in enumerate(transitions):
        intent = row.get("intent") or {}
        outcome = row.get("outcome") if isinstance(row.get("outcome"), dict) else None
        handling = row.get("handling") if isinstance(row.get("handling"), dict) else {}
        tid = str(intent.get("transition_id") or f"transition-{index:03d}")
        attempt_id = f"attempt:{tid}"
        action_id = f"action:{tid}"
        proposal_id = f"proposal:{tid}"
        parent_state = str(intent.get("parent_node_id") or "node_000_root")
        action_type = str(intent.get("optimizer_name") or "unknown")
        with store.transaction():
            store.add_action(ActionSpecification(
                action_id, run_id, scope_id, action_type,
                intent.get("optimizer_params") if isinstance(intent.get("optimizer_params"), dict) else {},
            ))
            store.add_proposal(Proposal(
                proposal_id, run_id, scope_id, parent_state, action_id,
                str(intent.get("source") or "historical_unknown"),
                policy_metadata={"historical_intent": intent},
            ))
            store.add_attempt(Attempt(attempt_id, run_id, scope_id, parent_state, action_id, proposal_id))
            store.add_attempt_event(AttemptLifecycleEvent(f"event:{tid}:0", attempt_id, 0, AttemptEventKind.SCHEDULED))
            store.add_attempt_event(AttemptLifecycleEvent(f"event:{tid}:1", attempt_id, 1, AttemptEventKind.STARTED))
            store.add_budget_event(BudgetEvent(f"budget:{tid}:reserved", run_id, scope_id, attempt_id, BudgetEventKind.RESERVED))
        if outcome is None:
            with store.transaction():
                store.add_attempt_event(AttemptLifecycleEvent(
                    f"event:{tid}:2", attempt_id, 2, AttemptEventKind.ORPHANED,
                    detail={"reason": "historical journal has no materialized outcome"},
                ))
                store.add_budget_event(BudgetEvent(f"budget:{tid}:released", run_id, scope_id, attempt_id, BudgetEventKind.RELEASED))
            continue

        output_uri = str(outcome.get("output_dcp") or "")
        has_output = bool(output_uri)
        output_path = Path(output_uri) if has_output else None
        artifact_id = f"artifact:{tid}" if has_output else None
        state_id = str(outcome.get("child_node_id") or tid) if has_output else None
        execution_succeeded = bool(outcome.get("success") and has_output)
        status = _validation(outcome.get("validation_status"), has_output=has_output)
        selection_eligible = bool(status is not ValidationStatus.INVALID and handling.get("updated_incumbent"))
        finished = utc_now()
        with store.transaction():
            if has_output and artifact_id and output_path:
                store.add_artifact(ArtifactReference(
                    artifact_id, run_id, scope_id, ArtifactKind.DCP, output_uri,
                    size_bytes=output_path.stat().st_size if output_path.is_file() else None,
                    metadata={"historical_transition_id": tid},
                ))
            store.add_outcome(ExecutionOutcome(
                f"outcome:{tid}", attempt_id,
                OutcomeStatus.SUCCEEDED if execution_succeeded else OutcomeStatus.FAILED,
                finished, finished,
                output_artifact_id=artifact_id if execution_succeeded else None,
                error_message=outcome.get("rejection_reason"),
                metrics=outcome.get("metrics") if isinstance(outcome.get("metrics"), dict) else {},
            ))
            if has_output and artifact_id and state_id:
                store.add_state(DesignState(
                    state_id, run_id, scope_id, artifact_id, parent_state, attempt_id,
                    metadata={"historical_import": True, "metrics": outcome.get("metrics") or {}},
                ))
            store.add_validation(ValidationRecord(
                f"validation:{tid}", run_id, scope_id, state_id or attempt_id,
                ValidationLevel.ATTEMPT, status, attempt_id=attempt_id,
                details={"historical_status": outcome.get("validation_status")},
            ))
            if state_id:
                store.add_eligibility(StateEligibility(
                    f"eligibility:{tid}", run_id, state_id,
                    status is not ValidationStatus.INVALID, selection_eligible,
                    "inferred from historical validation and incumbent handling",
                ))
                if selection_eligible and artifact_id:
                    store.add_selection(ResultSelection(
                        f"selection:{tid}", run_id, scope_id, state_id, artifact_id,
                        SelectionKind.BEST_VALID, "historical updated_incumbent",
                    ))
                    selected_state, selected_scope = state_id, scope_id
            event = AttemptEventKind.COMPLETED if execution_succeeded else AttemptEventKind.FAILED
            store.add_attempt_event(AttemptLifecycleEvent(f"event:{tid}:2", attempt_id, 2, event))
            budget = BudgetEventKind.COMPLETED if execution_succeeded else BudgetEventKind.FAILED
            store.add_budget_event(BudgetEvent(f"budget:{tid}:done", run_id, scope_id, attempt_id, budget))

    existing_levels = {item.level for item in store.validations(subject_id=selected_state)}
    with store.transaction():
        if ValidationLevel.CERTIFICATION not in existing_levels:
            store.add_validation(ValidationRecord(
                f"certification:{selected_state}", run_id, selected_scope, selected_state,
                ValidationLevel.CERTIFICATION, ValidationStatus.NOT_RUN,
                details={"reason": "unknown in historical source"},
            ))
        if ValidationLevel.FORMAL not in existing_levels:
            store.add_validation(ValidationRecord(
                f"formal:{selected_state}", run_id, selected_scope, selected_state,
                ValidationLevel.FORMAL, ValidationStatus.NOT_RUN,
                details={"reason": "unknown in historical source"},
            ))
        store.add_run_event(RunLifecycleEvent(
            "run-event-001", run_id, 1,
            RunStatus.FAILED if summary.get("success") is False or summary.get("error") else RunStatus.FINALIZED,
            detail={"kind": "historical_import_complete", "attempt_count": len(transitions)},
        ))
    return store


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source_run_dir", type=Path)
    parser.add_argument("output_db", type=Path)
    args = parser.parse_args()
    with import_v1_run(args.source_run_dir, args.output_db) as store:
        run = store.get_run()
        print(f"{run.run_id if run else 'unknown'}: {len(store.attempts())} attempts -> {store.path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
