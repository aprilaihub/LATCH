"""Stdlib SQLite implementation of the per-run canonical fact store."""

from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Iterator, Sequence

from optimizer_pipeline.core.domain import (
    ActionSpecification,
    ArtifactKind,
    ArtifactReference,
    Attempt,
    AttemptEventKind,
    AttemptLifecycleEvent,
    BudgetEvent,
    BudgetEventKind,
    DesignState,
    ExecutionOutcome,
    ExecutionScope,
    OutcomeStatus,
    ParentUseMode,
    Proposal,
    ResultSelection,
    Run,
    RunLifecycleEvent,
    RunStatus,
    ScopeKind,
    SelectionKind,
    StateEligibility,
    ValidationLevel,
    ValidationRecord,
    ValidationStatus,
)

from .schema import CURRENT_SCHEMA_VERSION, current_schema_version, migrate_schema


def _time(value: datetime) -> str:
    return value.isoformat()


def _json(value: object) -> str:
    return json.dumps(value.to_dict(), sort_keys=True, separators=(",", ":"))  # type: ignore[attr-defined]


def _decode_json(value: str) -> dict[str, object]:
    decoded = json.loads(value)
    if not isinstance(decoded, dict):
        raise ValueError("stored metadata must be a JSON object")
    return decoded


class SQLiteRunStore:
    """One connection to one append-only ``run.db``.

    Methods outside :meth:`transaction` are individually atomic. A transaction
    holds the connection lock for its full duration and supports nested
    savepoints. Separate store instances coordinate through SQLite WAL and the
    configured busy timeout.
    """

    def __init__(
        self,
        path: str | Path,
        *,
        migrate: bool = True,
        timeout_seconds: float = 30.0,
    ) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(
            self.path,
            timeout=timeout_seconds,
            isolation_level=None,
            check_same_thread=False,
        )
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA foreign_keys = ON")
        self._connection.execute(f"PRAGMA busy_timeout = {int(timeout_seconds * 1000)}")
        self._connection.execute("PRAGMA journal_mode = WAL")
        self._connection.execute("PRAGMA synchronous = FULL")
        self._lock = threading.RLock()
        self._transaction_depth = 0
        if migrate:
            migrate_schema(self._connection)
        elif current_schema_version(self._connection) != CURRENT_SCHEMA_VERSION:
            raise RuntimeError("run.db schema is not current and migration was disabled")

    @classmethod
    def create(cls, path: str | Path, run: Run) -> "SQLiteRunStore":
        store = cls(path)
        try:
            store.add_run(run)
        except Exception:
            store.close()
            raise
        return store

    @property
    def schema_version(self) -> int:
        return current_schema_version(self._connection)

    @property
    def connection(self) -> sqlite3.Connection:
        """Low-level read access for diagnostics and standalone migration tools."""

        return self._connection

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def __enter__(self) -> "SQLiteRunStore":
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.close()

    @contextmanager
    def transaction(self) -> Iterator["SQLiteRunStore"]:
        with self._lock:
            depth = self._transaction_depth
            savepoint = f"run_store_{depth}"
            if depth == 0:
                self._connection.execute("BEGIN IMMEDIATE")
            else:
                self._connection.execute(f"SAVEPOINT {savepoint}")
            self._transaction_depth += 1
            try:
                yield self
            except BaseException:
                self._transaction_depth -= 1
                if depth == 0:
                    self._connection.execute("ROLLBACK")
                else:
                    self._connection.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
                    self._connection.execute(f"RELEASE SAVEPOINT {savepoint}")
                raise
            else:
                self._transaction_depth -= 1
                if depth == 0:
                    self._connection.execute("COMMIT")
                else:
                    self._connection.execute(f"RELEASE SAVEPOINT {savepoint}")

    def _insert(self, sql: str, values: Sequence[object]) -> None:
        with self._lock:
            self._connection.execute(sql, values)

    def _rows(self, sql: str, values: Sequence[object] = ()) -> tuple[sqlite3.Row, ...]:
        with self._lock:
            return tuple(self._connection.execute(sql, values).fetchall())

    def add_run(self, fact: Run) -> None:
        self._insert(
            "INSERT INTO runs(run_id, mode, created_at, metadata_json) VALUES (?, ?, ?, ?)",
            (fact.run_id, fact.mode, _time(fact.created_at), _json(fact.metadata)),
        )

    def add_run_event(self, fact: RunLifecycleEvent) -> None:
        self._insert(
            "INSERT INTO run_events(event_id, run_id, sequence, status, occurred_at, detail_json) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                fact.event_id,
                fact.run_id,
                fact.sequence,
                fact.status.value,
                _time(fact.occurred_at),
                _json(fact.detail),
            ),
        )

    def add_scope(self, fact: ExecutionScope) -> None:
        self._insert(
            "INSERT INTO scopes(scope_id, run_id, kind, name, parent_scope_id, created_at, metadata_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                fact.scope_id,
                fact.run_id,
                fact.kind.value,
                fact.name,
                fact.parent_scope_id,
                _time(fact.created_at),
                _json(fact.metadata),
            ),
        )

    def add_artifact(self, fact: ArtifactReference) -> None:
        self._insert(
            "INSERT INTO artifacts(artifact_id, run_id, scope_id, kind, uri, sha256, size_bytes, created_at, metadata_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                fact.artifact_id,
                fact.run_id,
                fact.scope_id,
                fact.kind.value,
                fact.uri,
                fact.sha256,
                fact.size_bytes,
                _time(fact.created_at),
                _json(fact.metadata),
            ),
        )

    def add_action(self, fact: ActionSpecification) -> None:
        self._insert(
            "INSERT INTO actions(action_id, run_id, scope_id, action_type, parameters_json, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                fact.action_id,
                fact.run_id,
                fact.scope_id,
                fact.action_type,
                _json(fact.parameters),
                _time(fact.created_at),
            ),
        )

    def add_proposal(self, fact: Proposal) -> None:
        self._insert(
            "INSERT INTO proposals(proposal_id, run_id, scope_id, parent_state_id, action_id, source, proposed_at, policy_metadata_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                fact.proposal_id,
                fact.run_id,
                fact.scope_id,
                fact.parent_state_id,
                fact.action_id,
                fact.source,
                _time(fact.proposed_at),
                _json(fact.policy_metadata),
            ),
        )

    def add_attempt(self, fact: Attempt) -> None:
        self._insert(
            "INSERT INTO attempts(attempt_id, run_id, scope_id, parent_state_id, action_id, proposal_id, parent_use_mode, scheduled_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                fact.attempt_id,
                fact.run_id,
                fact.scope_id,
                fact.parent_state_id,
                fact.action_id,
                fact.proposal_id,
                fact.parent_use_mode.value,
                _time(fact.scheduled_at),
            ),
        )

    def add_attempt_event(self, fact: AttemptLifecycleEvent) -> None:
        self._insert(
            "INSERT INTO attempt_events(event_id, attempt_id, sequence, kind, occurred_at, detail_json) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                fact.event_id,
                fact.attempt_id,
                fact.sequence,
                fact.kind.value,
                _time(fact.occurred_at),
                _json(fact.detail),
            ),
        )

    def add_outcome(self, fact: ExecutionOutcome) -> None:
        self._insert(
            "INSERT INTO outcomes(outcome_id, attempt_id, status, started_at, finished_at, output_artifact_id, error_type, error_message, metrics_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                fact.outcome_id,
                fact.attempt_id,
                fact.status.value,
                _time(fact.started_at),
                _time(fact.finished_at),
                fact.output_artifact_id,
                fact.error_type,
                fact.error_message,
                _json(fact.metrics),
            ),
        )

    def add_state(self, fact: DesignState) -> None:
        self._insert(
            "INSERT INTO states(state_id, run_id, scope_id, artifact_id, parent_state_id, produced_by_attempt_id, managed, created_at, metadata_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                fact.state_id,
                fact.run_id,
                fact.scope_id,
                fact.artifact_id,
                fact.parent_state_id,
                fact.produced_by_attempt_id,
                int(fact.managed),
                _time(fact.created_at),
                _json(fact.metadata),
            ),
        )

    def add_validation(self, fact: ValidationRecord) -> None:
        self._insert(
            "INSERT INTO validations(validation_id, run_id, scope_id, subject_id, level, status, attempt_id, checked_at, details_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                fact.validation_id,
                fact.run_id,
                fact.scope_id,
                fact.subject_id,
                fact.level.value,
                fact.status.value,
                fact.attempt_id,
                _time(fact.checked_at),
                _json(fact.details),
            ),
        )

    def add_eligibility(self, fact: StateEligibility) -> None:
        self._insert(
            "INSERT INTO state_eligibility(eligibility_id, run_id, state_id, parent_eligible, selection_eligible, reason, recorded_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                fact.eligibility_id,
                fact.run_id,
                fact.state_id,
                int(fact.parent_eligible),
                int(fact.selection_eligible),
                fact.reason,
                _time(fact.recorded_at),
            ),
        )

    def add_budget_event(self, fact: BudgetEvent) -> None:
        self._insert(
            "INSERT INTO budget_events(budget_event_id, run_id, scope_id, attempt_id, kind, units, occurred_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                fact.budget_event_id,
                fact.run_id,
                fact.scope_id,
                fact.attempt_id,
                fact.kind.value,
                fact.units,
                _time(fact.occurred_at),
            ),
        )

    def add_selection(self, fact: ResultSelection) -> None:
        self._insert(
            "INSERT INTO selections(selection_id, run_id, scope_id, state_id, artifact_id, kind, reason, selected_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                fact.selection_id,
                fact.run_id,
                fact.scope_id,
                fact.state_id,
                fact.artifact_id,
                fact.kind.value,
                fact.reason,
                _time(fact.selected_at),
            ),
        )

    def get_run(self) -> Run | None:
        rows = self._rows("SELECT * FROM runs")
        if not rows:
            return None
        row = rows[0]
        return Run(row["run_id"], row["mode"], datetime.fromisoformat(row["created_at"]), _decode_json(row["metadata_json"]))

    def run_events(self) -> tuple[RunLifecycleEvent, ...]:
        return tuple(
            RunLifecycleEvent(
                row["event_id"], row["run_id"], row["sequence"], RunStatus(row["status"]),
                datetime.fromisoformat(row["occurred_at"]), _decode_json(row["detail_json"]),
            )
            for row in self._rows("SELECT * FROM run_events ORDER BY sequence")
        )

    def scopes(self) -> tuple[ExecutionScope, ...]:
        return tuple(
            ExecutionScope(
                row["scope_id"], row["run_id"], ScopeKind(row["kind"]), row["name"],
                row["parent_scope_id"], datetime.fromisoformat(row["created_at"]),
                _decode_json(row["metadata_json"]),
            )
            for row in self._rows("SELECT * FROM scopes ORDER BY created_at, scope_id")
        )

    def artifacts(self, *, scope_id: str | None = None) -> tuple[ArtifactReference, ...]:
        sql, values = self._scope_query("artifacts", scope_id, "created_at, artifact_id")
        return tuple(
            ArtifactReference(
                row["artifact_id"], row["run_id"], row["scope_id"], ArtifactKind(row["kind"]),
                row["uri"], row["sha256"], row["size_bytes"],
                datetime.fromisoformat(row["created_at"]), _decode_json(row["metadata_json"]),
            )
            for row in self._rows(sql, values)
        )

    def actions(self, *, scope_id: str | None = None) -> tuple[ActionSpecification, ...]:
        sql, values = self._scope_query("actions", scope_id, "created_at, action_id")
        return tuple(
            ActionSpecification(
                row["action_id"], row["run_id"], row["scope_id"], row["action_type"],
                _decode_json(row["parameters_json"]), datetime.fromisoformat(row["created_at"]),
            )
            for row in self._rows(sql, values)
        )

    def proposals(self, *, scope_id: str | None = None) -> tuple[Proposal, ...]:
        sql, values = self._scope_query("proposals", scope_id, "proposed_at, proposal_id")
        return tuple(
            Proposal(
                row["proposal_id"], row["run_id"], row["scope_id"], row["parent_state_id"],
                row["action_id"], row["source"], datetime.fromisoformat(row["proposed_at"]),
                _decode_json(row["policy_metadata_json"]),
            )
            for row in self._rows(sql, values)
        )

    def attempts(self, *, scope_id: str | None = None) -> tuple[Attempt, ...]:
        sql, values = self._scope_query("attempts", scope_id, "scheduled_at, attempt_id")
        return tuple(
            Attempt(
                row["attempt_id"], row["run_id"], row["scope_id"], row["parent_state_id"],
                row["action_id"], row["proposal_id"], ParentUseMode(row["parent_use_mode"]),
                datetime.fromisoformat(row["scheduled_at"]),
            )
            for row in self._rows(sql, values)
        )

    def attempt_events(self, *, attempt_id: str | None = None) -> tuple[AttemptLifecycleEvent, ...]:
        if attempt_id is None:
            rows = self._rows("SELECT * FROM attempt_events ORDER BY occurred_at, event_id")
        else:
            rows = self._rows(
                "SELECT * FROM attempt_events WHERE attempt_id = ? ORDER BY sequence",
                (attempt_id,),
            )
        return tuple(
            AttemptLifecycleEvent(
                row["event_id"], row["attempt_id"], row["sequence"],
                AttemptEventKind(row["kind"]), datetime.fromisoformat(row["occurred_at"]),
                _decode_json(row["detail_json"]),
            )
            for row in rows
        )

    def outcomes(self) -> tuple[ExecutionOutcome, ...]:
        return tuple(
            ExecutionOutcome(
                row["outcome_id"], row["attempt_id"], OutcomeStatus(row["status"]),
                datetime.fromisoformat(row["started_at"]), datetime.fromisoformat(row["finished_at"]),
                row["output_artifact_id"], row["error_type"], row["error_message"],
                _decode_json(row["metrics_json"]),
            )
            for row in self._rows("SELECT * FROM outcomes ORDER BY finished_at, outcome_id")
        )

    def states(self, *, scope_id: str | None = None) -> tuple[DesignState, ...]:
        sql, values = self._scope_query("states", scope_id, "created_at, state_id")
        return tuple(
            DesignState(
                row["state_id"], row["run_id"], row["scope_id"], row["artifact_id"],
                row["parent_state_id"], row["produced_by_attempt_id"], bool(row["managed"]),
                datetime.fromisoformat(row["created_at"]), _decode_json(row["metadata_json"]),
            )
            for row in self._rows(sql, values)
        )

    def validations(self, *, subject_id: str | None = None) -> tuple[ValidationRecord, ...]:
        if subject_id is None:
            rows = self._rows("SELECT * FROM validations ORDER BY checked_at, validation_id")
        else:
            rows = self._rows(
                "SELECT * FROM validations WHERE subject_id = ? ORDER BY checked_at, validation_id",
                (subject_id,),
            )
        return tuple(
            ValidationRecord(
                row["validation_id"], row["run_id"], row["scope_id"], row["subject_id"],
                ValidationLevel(row["level"]), ValidationStatus(row["status"]), row["attempt_id"],
                datetime.fromisoformat(row["checked_at"]), _decode_json(row["details_json"]),
            )
            for row in rows
        )

    def eligibility(self, *, state_id: str | None = None) -> tuple[StateEligibility, ...]:
        if state_id is None:
            rows = self._rows("SELECT * FROM state_eligibility ORDER BY recorded_at, eligibility_id")
        else:
            rows = self._rows(
                "SELECT * FROM state_eligibility WHERE state_id = ? ORDER BY recorded_at, eligibility_id",
                (state_id,),
            )
        return tuple(
            StateEligibility(
                row["eligibility_id"], row["run_id"], row["state_id"],
                bool(row["parent_eligible"]), bool(row["selection_eligible"]), row["reason"],
                datetime.fromisoformat(row["recorded_at"]),
            )
            for row in rows
        )

    def budget_events(self, *, scope_id: str | None = None) -> tuple[BudgetEvent, ...]:
        sql, values = self._scope_query("budget_events", scope_id, "occurred_at, budget_event_id")
        return tuple(
            BudgetEvent(
                row["budget_event_id"], row["run_id"], row["scope_id"], row["attempt_id"],
                BudgetEventKind(row["kind"]), row["units"], datetime.fromisoformat(row["occurred_at"]),
            )
            for row in self._rows(sql, values)
        )

    def selections(self, *, scope_id: str | None = None) -> tuple[ResultSelection, ...]:
        sql, values = self._scope_query("selections", scope_id, "selected_at, selection_id")
        return tuple(
            ResultSelection(
                row["selection_id"], row["run_id"], row["scope_id"], row["state_id"],
                row["artifact_id"], SelectionKind(row["kind"]), row["reason"],
                datetime.fromisoformat(row["selected_at"]),
            )
            for row in self._rows(sql, values)
        )

    def integrity_check(self) -> tuple[str, ...]:
        with self._lock:
            quick = tuple(str(row[0]) for row in self._connection.execute("PRAGMA quick_check"))
            foreign = tuple(
                f"foreign-key violation: {tuple(row)}"
                for row in self._connection.execute("PRAGMA foreign_key_check")
            )
        return tuple(item for item in quick if item != "ok") + foreign

    @staticmethod
    def _scope_query(table: str, scope_id: str | None, ordering: str) -> tuple[str, tuple[object, ...]]:
        if scope_id is None:
            return f"SELECT * FROM {table} ORDER BY {ordering}", ()
        return f"SELECT * FROM {table} WHERE scope_id = ? ORDER BY {ordering}", (scope_id,)
