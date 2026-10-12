"""Versioned SQLite schema for one canonical optimization run."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Migration:
    version: int
    sql: str


FACT_TABLES = (
    "runs",
    "run_events",
    "scopes",
    "artifacts",
    "actions",
    "proposals",
    "attempts",
    "attempt_events",
    "outcomes",
    "states",
    "validations",
    "state_eligibility",
    "budget_events",
    "selections",
)


_INITIAL_SCHEMA = """
CREATE TABLE runs (
    run_id TEXT PRIMARY KEY,
    singleton INTEGER NOT NULL DEFAULT 1 CHECK (singleton = 1),
    mode TEXT NOT NULL,
    created_at TEXT NOT NULL,
    metadata_json TEXT NOT NULL,
    UNIQUE (singleton)
);

CREATE TABLE run_events (
    event_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(run_id),
    sequence INTEGER NOT NULL CHECK (sequence >= 0),
    status TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    detail_json TEXT NOT NULL,
    UNIQUE (run_id, sequence)
);

CREATE TABLE scopes (
    scope_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(run_id),
    kind TEXT NOT NULL,
    name TEXT NOT NULL,
    parent_scope_id TEXT REFERENCES scopes(scope_id),
    created_at TEXT NOT NULL,
    metadata_json TEXT NOT NULL
);

CREATE TABLE artifacts (
    artifact_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(run_id),
    scope_id TEXT NOT NULL REFERENCES scopes(scope_id),
    kind TEXT NOT NULL,
    uri TEXT NOT NULL,
    sha256 TEXT,
    size_bytes INTEGER CHECK (size_bytes IS NULL OR size_bytes >= 0),
    created_at TEXT NOT NULL,
    metadata_json TEXT NOT NULL
);

CREATE TABLE actions (
    action_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(run_id),
    scope_id TEXT NOT NULL REFERENCES scopes(scope_id),
    action_type TEXT NOT NULL,
    parameters_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE states (
    state_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(run_id),
    scope_id TEXT NOT NULL REFERENCES scopes(scope_id),
    artifact_id TEXT NOT NULL REFERENCES artifacts(artifact_id),
    parent_state_id TEXT REFERENCES states(state_id),
    produced_by_attempt_id TEXT REFERENCES attempts(attempt_id),
    managed INTEGER NOT NULL CHECK (managed IN (0, 1)),
    created_at TEXT NOT NULL,
    metadata_json TEXT NOT NULL,
    CHECK ((parent_state_id IS NULL) = (produced_by_attempt_id IS NULL))
);

CREATE TABLE proposals (
    proposal_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(run_id),
    scope_id TEXT NOT NULL REFERENCES scopes(scope_id),
    parent_state_id TEXT NOT NULL REFERENCES states(state_id),
    action_id TEXT NOT NULL REFERENCES actions(action_id),
    source TEXT NOT NULL,
    proposed_at TEXT NOT NULL,
    policy_metadata_json TEXT NOT NULL
);

CREATE TABLE attempts (
    attempt_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(run_id),
    scope_id TEXT NOT NULL REFERENCES scopes(scope_id),
    parent_state_id TEXT NOT NULL REFERENCES states(state_id),
    action_id TEXT NOT NULL REFERENCES actions(action_id),
    proposal_id TEXT REFERENCES proposals(proposal_id),
    parent_use_mode TEXT NOT NULL,
    scheduled_at TEXT NOT NULL
);

CREATE TABLE attempt_events (
    event_id TEXT PRIMARY KEY,
    attempt_id TEXT NOT NULL REFERENCES attempts(attempt_id),
    sequence INTEGER NOT NULL CHECK (sequence >= 0),
    kind TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    detail_json TEXT NOT NULL,
    UNIQUE (attempt_id, sequence)
);

CREATE TABLE outcomes (
    outcome_id TEXT PRIMARY KEY,
    attempt_id TEXT NOT NULL UNIQUE REFERENCES attempts(attempt_id),
    status TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT NOT NULL,
    output_artifact_id TEXT REFERENCES artifacts(artifact_id),
    error_type TEXT,
    error_message TEXT,
    metrics_json TEXT NOT NULL
);

CREATE TABLE validations (
    validation_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(run_id),
    scope_id TEXT NOT NULL REFERENCES scopes(scope_id),
    subject_id TEXT NOT NULL,
    level TEXT NOT NULL CHECK (level IN ('ATTEMPT', 'CERTIFICATION', 'FORMAL')),
    status TEXT NOT NULL,
    attempt_id TEXT REFERENCES attempts(attempt_id),
    checked_at TEXT NOT NULL,
    details_json TEXT NOT NULL,
    CHECK (
        (level = 'ATTEMPT' AND attempt_id IS NOT NULL AND status IN ('CLEAN', 'WARNING', 'FALLBACK_USED', 'INVALID', 'FAILED'))
        OR (level IN ('CERTIFICATION', 'FORMAL') AND status IN ('NOT_RUN', 'PASSED', 'FAILED'))
    )
);

CREATE TABLE state_eligibility (
    eligibility_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(run_id),
    state_id TEXT NOT NULL UNIQUE REFERENCES states(state_id),
    parent_eligible INTEGER NOT NULL CHECK (parent_eligible IN (0, 1)),
    selection_eligible INTEGER NOT NULL CHECK (selection_eligible IN (0, 1)),
    reason TEXT NOT NULL,
    recorded_at TEXT NOT NULL
);

CREATE TABLE budget_events (
    budget_event_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(run_id),
    scope_id TEXT NOT NULL REFERENCES scopes(scope_id),
    attempt_id TEXT NOT NULL REFERENCES attempts(attempt_id),
    kind TEXT NOT NULL,
    units INTEGER NOT NULL CHECK (units > 0),
    occurred_at TEXT NOT NULL
);

CREATE TABLE selections (
    selection_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(run_id),
    scope_id TEXT NOT NULL REFERENCES scopes(scope_id),
    state_id TEXT NOT NULL REFERENCES states(state_id),
    artifact_id TEXT NOT NULL REFERENCES artifacts(artifact_id),
    kind TEXT NOT NULL,
    reason TEXT NOT NULL,
    selected_at TEXT NOT NULL
);
"""


def _append_only_sql() -> str:
    statements: list[str] = []
    for table in FACT_TABLES:
        statements.extend(
            (
                f"CREATE TRIGGER {table}_reject_update BEFORE UPDATE ON {table} "
                f"BEGIN SELECT RAISE(ABORT, '{table} is append-only'); END;",
                f"CREATE TRIGGER {table}_reject_delete BEFORE DELETE ON {table} "
                f"BEGIN SELECT RAISE(ABORT, '{table} is append-only'); END;",
            )
        )
    return "\n".join(statements)


_INDEXES_AND_GUARDS = """
CREATE INDEX idx_scopes_run ON scopes(run_id, created_at);
CREATE INDEX idx_states_run_scope ON states(run_id, scope_id, created_at);
CREATE INDEX idx_states_parent ON states(parent_state_id);
CREATE INDEX idx_attempts_run_scope ON attempts(run_id, scope_id, scheduled_at);
CREATE INDEX idx_attempt_events_attempt ON attempt_events(attempt_id, sequence);
CREATE INDEX idx_validations_subject ON validations(run_id, subject_id, checked_at);
CREATE INDEX idx_eligibility_state ON state_eligibility(run_id, state_id, recorded_at);
CREATE INDEX idx_budget_scope ON budget_events(run_id, scope_id, occurred_at);
CREATE INDEX idx_selections_scope ON selections(run_id, scope_id, selected_at);

CREATE TRIGGER eligibility_rejects_invalid_state
BEFORE INSERT ON state_eligibility
WHEN (NEW.parent_eligible = 1 OR NEW.selection_eligible = 1)
 AND EXISTS (
    SELECT 1 FROM validations
    WHERE subject_id = NEW.state_id AND level = 'ATTEMPT' AND status = 'INVALID'
 )
BEGIN
    SELECT RAISE(ABORT, 'INVALID state cannot be eligible');
END;

CREATE TRIGGER invalid_validation_rejects_eligible_state
BEFORE INSERT ON validations
WHEN NEW.level = 'ATTEMPT' AND NEW.status = 'INVALID'
 AND EXISTS (
    SELECT 1 FROM state_eligibility
    WHERE state_id = NEW.subject_id
      AND (parent_eligible = 1 OR selection_eligible = 1)
 )
BEGIN
    SELECT RAISE(ABORT, 'INVALID state cannot be eligible');
END;

CREATE TRIGGER selection_requires_eligible_matching_state
BEFORE INSERT ON selections
WHEN NOT EXISTS (
    SELECT 1
    FROM states
    JOIN state_eligibility USING (state_id)
    WHERE states.state_id = NEW.state_id
      AND states.artifact_id = NEW.artifact_id
      AND state_eligibility.selection_eligible = 1
 )
BEGIN
    SELECT RAISE(ABORT, 'selection requires an eligible state with matching artifact');
END;
""" + _append_only_sql()


MIGRATIONS = (
    Migration(1, _INITIAL_SCHEMA),
    Migration(2, _INDEXES_AND_GUARDS),
)
CURRENT_SCHEMA_VERSION = MIGRATIONS[-1].version


class SchemaVersionError(RuntimeError):
    pass


def current_schema_version(connection: sqlite3.Connection) -> int:
    row = connection.execute("PRAGMA user_version").fetchone()
    return int(row[0])


def migrate_schema(
    connection: sqlite3.Connection,
    *,
    target_version: int = CURRENT_SCHEMA_VERSION,
) -> int:
    """Apply ordered migrations atomically and reject unknown schema versions."""

    connection.execute("PRAGMA foreign_keys = ON")
    current = current_schema_version(connection)
    if current > CURRENT_SCHEMA_VERSION:
        raise SchemaVersionError(
            f"run.db schema {current} is newer than supported {CURRENT_SCHEMA_VERSION}"
        )
    if target_version < current or target_version > CURRENT_SCHEMA_VERSION:
        raise SchemaVersionError(
            f"cannot migrate schema {current} to unsupported target {target_version}"
        )

    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version INTEGER PRIMARY KEY,
            applied_at TEXT NOT NULL
        )
        """
    )
    for migration in MIGRATIONS:
        if not current < migration.version <= target_version:
            continue
        try:
            connection.executescript(
                "BEGIN IMMEDIATE;\n"
                + migration.sql
                + "\nINSERT INTO schema_migrations(version, applied_at) "
                + f"VALUES ({migration.version}, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'));\n"
                + f"PRAGMA user_version = {migration.version};\n"
                + "COMMIT;"
            )
        except Exception:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
        current = migration.version
    return current
