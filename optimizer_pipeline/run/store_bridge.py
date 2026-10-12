"""Run-layer bridge from transition execution to canonical SQLite facts.

This bridge emits no legacy journal or summary artifacts. The temporary
in-memory transition records exist only to keep behavior-preserved search code
running during the hard-cut series; ``run.db`` is the sole persisted truth.
"""

from __future__ import annotations

import copy
from datetime import timedelta
import hashlib
import re
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from optimizer_pipeline.core.domain import (
    ActionSpecification, ArtifactKind, ArtifactReference, Attempt,
    AttemptEventKind, AttemptLifecycleEvent, BudgetEvent, BudgetEventKind,
    DesignState, ExecutionOutcome, ExecutionScope, OutcomeStatus, Proposal,
    ResultSelection, Run, RunLifecycleEvent, RunStatus, ScopeKind,
    ParentUseMode, SelectionKind, StateEligibility, ValidationLevel, ValidationRecord,
    ValidationStatus, utc_now,
)
from optimizer_pipeline.core.store import SQLiteRunStore
from optimizer_pipeline.domain.transition import DcpTransitionRecord


class CanonicalArtifactStore:
    """Workspace locator whose structured persistence is exclusively SQLite."""

    def __init__(self, run_dir: Path):
        self.run_dir = Path(run_dir)
        self.nodes_dir = self.run_dir / "workspaces"
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.nodes_dir.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def safe_label(label: str) -> str:
        cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(label).strip())
        return cleaned.strip("_") or "state"

    def node_dir(self, index: int, label: str) -> Path:
        path = self.nodes_dir / f"attempt_{index:03d}_{self.safe_label(label)}"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def write_json(self, path: Path, data: Any) -> Path:
        return Path(path)

    def write_node_state(self, node: Any) -> Path:
        return Path(node.artifact_dir or self.run_dir)

    def write_pipeline_state(self, state: Any) -> Path:
        return self.run_dir / "run.db"

    def write_state_graph(self, nodes: Any) -> Path:
        return self.run_dir / "run.db"


def initialize_run_store(
    *, run_dir: Path, run_id: str, mode: str, input_dcp: Path, root_metrics: Dict[str, Any]
) -> SQLiteRunStore:
    store = SQLiteRunStore.create(run_dir / "run.db", Run(run_id, mode, metadata={"artifact_contract": "run.db.v1"}))
    root_scope = ExecutionScope("scope-root", run_id, ScopeKind.ROOT, "managed run")
    root_artifact = ArtifactReference(
        "artifact-root", run_id, root_scope.scope_id, ArtifactKind.DCP, str(input_dcp),
        sha256=_sha256(input_dcp) if input_dcp.exists() else None,
        size_bytes=input_dcp.stat().st_size if input_dcp.exists() else None,
        metadata={"role": "baseline"},
    )
    root_state = DesignState("node_000_root", run_id, root_scope.scope_id, root_artifact.artifact_id, metadata={"metrics": root_metrics})
    with store.transaction():
        store.add_run_event(RunLifecycleEvent("run-event-000", run_id, 0, RunStatus.RUNNING))
        store.add_scope(root_scope)
        store.add_artifact(root_artifact)
        store.add_state(root_state)
        store.add_validation(ValidationRecord(
            "validation-root", run_id, root_scope.scope_id, root_state.state_id,
            ValidationLevel.CERTIFICATION, ValidationStatus.NOT_RUN,
            details={"role": "baseline"},
        ))
        store.add_eligibility(StateEligibility(
            "eligibility-root", run_id, root_state.state_id, True, True, "baseline",
        ))
        store.add_selection(ResultSelection(
            "selection-baseline", run_id, root_scope.scope_id, root_state.state_id,
            root_artifact.artifact_id, SelectionKind.BASELINE, "safe baseline",
        ))
    return store


class DatabaseTransitionSink:
    def __init__(self, store: SQLiteRunStore, *, scope_id: str = "scope-root") -> None:
        self.store = store
        self.scope_id = scope_id
        self._records: Dict[str, DcpTransitionRecord] = {}
        self._order: List[str] = []

    @property
    def path(self) -> Path:
        return self.store.path

    def append(self, record: DcpTransitionRecord, *, refresh_exports: bool = True) -> Path:
        transition_id = record.intent.transition_id
        if transition_id not in self._records:
            self._order.append(transition_id)
        self._records[transition_id] = copy.deepcopy(record)
        if record.outcome is None:
            self._record_scheduled(record)
        else:
            self._record_completed(record)
        return self.store.path

    def append_many(self, records: Iterable[DcpTransitionRecord], **kwargs: Any) -> int:
        count = 0
        prefix = str(kwargs.get("transition_id_prefix") or "")
        source_prefix = str(kwargs.get("source_prefix") or "")
        rewrites = dict(kwargs.get("node_id_rewrites") or {})
        metadata = dict(kwargs.get("metadata") or {})
        for original in records:
            record = copy.deepcopy(original)
            if prefix:
                record.intent.transition_id = f"{prefix}:{record.intent.transition_id}"
            if source_prefix:
                record.intent.source = f"{source_prefix}:{record.intent.source}"
            record.intent.parent_node_id = rewrites.get(record.intent.parent_node_id, record.intent.parent_node_id)
            record.intent.target_node_id = rewrites.get(record.intent.target_node_id, record.intent.target_node_id)
            record.intent.metadata.update(metadata)
            self.append(record, refresh_exports=False)
            count += 1
        return count

    def refresh_policy_exports(self) -> List[Dict[str, Any]]:
        return [record.to_policy_candidate_row() for record in self.materialized_records()]

    def load_all(self) -> List[DcpTransitionRecord]:
        return [copy.deepcopy(self._records[key]) for key in self._order]

    def materialized_records(self) -> List[DcpTransitionRecord]:
        return [record for record in self.load_all() if record.outcome is not None]

    def progress_summary(self) -> Dict[str, Any]:
        records = self.load_all()
        return {
            "scheduled": len(records),
            "completed": sum(record.outcome is not None for record in records),
            "canonical_store": str(self.store.path),
        }

    def _record_scheduled(self, record: DcpTransitionRecord) -> None:
        intent = record.intent
        run_id = str(intent.run_id or self.store.get_run().run_id)
        tid = intent.transition_id
        action_id, proposal_id, attempt_id = f"action:{tid}", f"proposal:{tid}", f"attempt:{tid}"
        parent_facts = self.store.eligibility(state_id=str(intent.parent_node_id))
        parent_eligible = not parent_facts or parent_facts[-1].parent_eligible
        intervention = str(intent.source).startswith("rescue_")
        parent_mode = ParentUseMode.EXPLICIT_INTERVENTION if intervention and not parent_eligible else ParentUseMode.NORMAL
        with self.store.transaction():
            self.store.add_action(ActionSpecification(action_id, run_id, self.scope_id, intent.optimizer_name, intent.optimizer_params))
            self.store.add_proposal(Proposal(
                proposal_id, run_id, self.scope_id, str(intent.parent_node_id), action_id,
                intent.source, policy_metadata={"rank": intent.candidate_rank, "priority": intent.priority, **intent.metadata},
            ))
            self.store.add_attempt(Attempt(
                attempt_id, run_id, self.scope_id, str(intent.parent_node_id), action_id,
                proposal_id, parent_use_mode=parent_mode,
            ))
            self.store.add_attempt_event(AttemptLifecycleEvent(f"event:{tid}:0", attempt_id, 0, AttemptEventKind.SCHEDULED))
            self.store.add_attempt_event(AttemptLifecycleEvent(f"event:{tid}:1", attempt_id, 1, AttemptEventKind.STARTED))
            self.store.add_budget_event(BudgetEvent(f"budget:{tid}:reserved", run_id, self.scope_id, attempt_id, BudgetEventKind.RESERVED))

    def _record_completed(self, record: DcpTransitionRecord) -> None:
        intent, outcome = record.intent, record.outcome
        assert outcome is not None
        run_id = str(intent.run_id or self.store.get_run().run_id)
        tid, attempt_id = intent.transition_id, f"attempt:{intent.transition_id}"
        output = Path(outcome.output_dcp) if outcome.output_dcp else None
        has_artifact = bool(output and output.exists())
        artifact_id = f"artifact:{tid}" if has_artifact else None
        state_id = str(outcome.child_node_id or tid) if has_artifact else None
        succeeded = bool(outcome.success and has_artifact)
        status = OutcomeStatus.SUCCEEDED if succeeded else OutcomeStatus.FAILED
        validation_status = _validation_status(outcome.validation_status, has_artifact)
        parent_eligible = validation_status is not ValidationStatus.INVALID
        publish_eligible = outcome.metrics.get("publish_eligible")
        selection_eligible = bool(parent_eligible and publish_eligible is not False)
        should_select = bool(
            selection_eligible
            and record.handling
            and record.handling.updated_incumbent
            and self._improves_current_selection(outcome.metrics)
        )
        with self.store.transaction():
            if has_artifact and artifact_id and state_id:
                self.store.add_artifact(ArtifactReference(
                    artifact_id, run_id, self.scope_id, ArtifactKind.DCP, str(output),
                    sha256=_sha256(output), size_bytes=output.stat().st_size,
                    metadata={"attempt_id": attempt_id},
                ))
            finished_at = utc_now()
            runtime_seconds = float(outcome.runtime_seconds or 0.0)
            started_at = finished_at - timedelta(seconds=max(0.0, runtime_seconds))
            self.store.add_outcome(ExecutionOutcome(
                f"outcome:{tid}", attempt_id, status,
                started_at, finished_at,
                output_artifact_id=artifact_id if succeeded else None,
                error_message=outcome.rejection_reason,
                metrics=outcome.metrics,
            ))
            if has_artifact and artifact_id and state_id:
                self.store.add_state(DesignState(
                    state_id, run_id, self.scope_id, artifact_id,
                    parent_state_id=str(intent.parent_node_id), produced_by_attempt_id=attempt_id,
                    metadata={"flow": intent.optimizer_name, "metrics": outcome.metrics},
                ))
                self.store.add_eligibility(StateEligibility(
                    f"eligibility:{tid}", run_id, state_id, parent_eligible,
                    selection_eligible, "attempt validation and selection handling",
                ))
                if should_select:
                    self.store.add_selection(ResultSelection(
                        f"selection:{tid}", run_id, self.scope_id, state_id, artifact_id,
                        SelectionKind.BEST_VALID, "accepted better valid result",
                    ))
            self.store.add_validation(ValidationRecord(
                f"validation:{tid}", run_id, self.scope_id, state_id or attempt_id,
                ValidationLevel.ATTEMPT, validation_status, attempt_id=attempt_id,
                details={"rejection_reason": outcome.rejection_reason},
            ))
            event_kind = AttemptEventKind.COMPLETED if succeeded else AttemptEventKind.FAILED
            self.store.add_attempt_event(AttemptLifecycleEvent(f"event:{tid}:2", attempt_id, 2, event_kind))
            budget_kind = (
                BudgetEventKind.COMPLETED
                if succeeded and validation_status is not ValidationStatus.INVALID
                else BudgetEventKind.FAILED
            )
            self.store.add_budget_event(BudgetEvent(f"budget:{tid}:done", run_id, self.scope_id, attempt_id, budget_kind))

    def _improves_current_selection(self, metrics: Dict[str, Any]) -> bool:
        candidate = _qor_total(metrics)
        selections = self.store.selections()
        if not selections:
            return True
        states = {state.state_id: state for state in self.store.states()}
        current_state = states.get(selections[-1].state_id)
        current_metrics = current_state.metadata.to_dict().get("metrics", {}) if current_state else {}
        current = _qor_total(current_metrics)
        if candidate is None:
            return True
        return candidate > (0.0 if current is None else current)


def _validation_status(value: Optional[str], has_artifact: bool) -> ValidationStatus:
    if not has_artifact:
        return ValidationStatus.INVALID
    try:
        return ValidationStatus(str(value or "INVALID").upper())
    except ValueError:
        return ValidationStatus.WARNING


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _qor_total(metrics: Dict[str, Any]) -> Optional[float]:
    score = metrics.get("qor_score") if isinstance(metrics, dict) else None
    value = score.get("total_score") if isinstance(score, dict) else None
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None
