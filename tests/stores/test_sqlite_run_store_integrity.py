from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import pytest

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
    utc_now,
)
from optimizer_pipeline.core.store import (
    CURRENT_SCHEMA_VERSION,
    FACT_TABLES,
    SQLiteRunStore,
    SchemaVersionError,
    current_schema_version,
    migrate_schema,
)


def seed_run(path):
    run = Run("run-1", "mock", metadata={"owner": "test"})
    store = SQLiteRunStore.create(path, run)
    root_scope = ExecutionScope("scope-root", run.run_id, ScopeKind.ROOT, "root")
    store.add_scope(root_scope)
    return store, run, root_scope


def test_complete_run_fact_graph_round_trips_without_summary_json(tmp_path):
    store, run, root_scope = seed_run(tmp_path / "run.db")
    now = utc_now()
    run_events = (
        RunLifecycleEvent("run-event-0", run.run_id, 0, RunStatus.CREATED),
        RunLifecycleEvent("run-event-1", run.run_id, 1, RunStatus.RUNNING),
        RunLifecycleEvent("run-event-2", run.run_id, 2, RunStatus.FINALIZED),
    )
    child_scope = ExecutionScope(
        "scope-frontier", run.run_id, ScopeKind.FRONTIER, "frontier-0", root_scope.scope_id,
    )
    root_artifact = ArtifactReference(
        "artifact-root", run.run_id, root_scope.scope_id, ArtifactKind.DCP,
        "artifacts/root.dcp", sha256="a" * 64, size_bytes=100,
    )
    root_state = DesignState(
        "state-root", run.run_id, root_scope.scope_id, root_artifact.artifact_id,
        metadata={"baseline": True},
    )
    action = ActionSpecification(
        "action-1", run.run_id, child_scope.scope_id, "phys_opt",
        {"directive": "Explore"},
    )
    proposal = Proposal(
        "proposal-1", run.run_id, child_scope.scope_id, root_state.state_id,
        action.action_id, "deterministic", policy_metadata={"rank": 1},
    )
    attempt = Attempt(
        "attempt-1", run.run_id, child_scope.scope_id, root_state.state_id,
        action.action_id, proposal.proposal_id,
    )
    attempt_events = (
        AttemptLifecycleEvent("attempt-event-0", attempt.attempt_id, 0, AttemptEventKind.SCHEDULED),
        AttemptLifecycleEvent("attempt-event-1", attempt.attempt_id, 1, AttemptEventKind.STARTED),
        AttemptLifecycleEvent("attempt-event-2", attempt.attempt_id, 2, AttemptEventKind.COMPLETED),
    )
    output_artifact = ArtifactReference(
        "artifact-output", run.run_id, child_scope.scope_id, ArtifactKind.DCP,
        "artifacts/output.dcp", size_bytes=120,
    )
    outcome = ExecutionOutcome(
        "outcome-1", attempt.attempt_id, OutcomeStatus.SUCCEEDED,
        now, now + timedelta(seconds=2), output_artifact.artifact_id,
        metrics={"wns_ns": -0.75},
    )
    output_state = DesignState(
        "state-output", run.run_id, child_scope.scope_id, output_artifact.artifact_id,
        root_state.state_id, attempt.attempt_id,
    )
    validation = ValidationRecord(
        "validation-1", run.run_id, child_scope.scope_id, output_state.state_id,
        ValidationLevel.ATTEMPT, ValidationStatus.CLEAN, attempt.attempt_id,
        details={"validator": "mock"},
    )
    eligibility = StateEligibility(
        "eligibility-1", run.run_id, output_state.state_id, True, True, "clean attempt",
    )
    budget = BudgetEvent(
        "budget-1", run.run_id, child_scope.scope_id, attempt.attempt_id,
        BudgetEventKind.COMPLETED,
    )
    selection = ResultSelection(
        "selection-1", run.run_id, child_scope.scope_id, output_state.state_id,
        output_artifact.artifact_id, SelectionKind.BEST_VALID, "best valid QoR",
    )

    with store.transaction():
        for fact in run_events:
            store.add_run_event(fact)
        store.add_scope(child_scope)
        store.add_artifact(root_artifact)
        store.add_state(root_state)
        store.add_action(action)
        store.add_proposal(proposal)
        store.add_attempt(attempt)
        for fact in attempt_events:
            store.add_attempt_event(fact)
        store.add_artifact(output_artifact)
        store.add_outcome(outcome)
        store.add_state(output_state)
        store.add_validation(validation)
        store.add_eligibility(eligibility)
        store.add_budget_event(budget)
        store.add_selection(selection)

    assert store.get_run() == run
    assert store.run_events() == run_events
    assert store.scopes() == (root_scope, child_scope)
    assert store.artifacts() == (root_artifact, output_artifact)
    assert store.actions() == (action,)
    assert store.proposals() == (proposal,)
    assert store.attempts() == (attempt,)
    assert store.attempt_events(attempt_id=attempt.attempt_id) == attempt_events
    assert store.outcomes() == (outcome,)
    assert store.states() == (root_state, output_state)
    assert store.validations(subject_id=output_state.state_id) == (validation,)
    assert store.eligibility(state_id=output_state.state_id) == (eligibility,)
    assert store.budget_events(scope_id=child_scope.scope_id) == (budget,)
    assert store.selections(scope_id=child_scope.scope_id) == (selection,)
    assert store.integrity_check() == ()
    store.close()


def test_transaction_rolls_back_all_facts_after_crash(tmp_path):
    store, run, root = seed_run(tmp_path / "run.db")
    with pytest.raises(RuntimeError, match="simulated crash"):
        with store.transaction():
            store.add_scope(
                ExecutionScope("scope-lost", run.run_id, ScopeKind.FIXED, "lost", root.scope_id)
            )
            raise RuntimeError("simulated crash")
    assert [scope.scope_id for scope in store.scopes()] == [root.scope_id]


def test_nested_transaction_uses_savepoint_without_aborting_outer_batch(tmp_path):
    store, run, root = seed_run(tmp_path / "run.db")
    with store.transaction():
        store.add_scope(ExecutionScope("scope-a", run.run_id, ScopeKind.FIXED, "a", root.scope_id))
        with pytest.raises(ValueError):
            with store.transaction():
                store.add_scope(
                    ExecutionScope("scope-rolled-back", run.run_id, ScopeKind.FIXED, "b", root.scope_id)
                )
                raise ValueError("rollback savepoint")
        store.add_scope(ExecutionScope("scope-c", run.run_id, ScopeKind.FIXED, "c", root.scope_id))
    assert {scope.scope_id for scope in store.scopes()} == {
        root.scope_id,
        "scope-a",
        "scope-c",
    }


def test_fact_tables_are_append_only_and_database_allows_only_one_run(tmp_path):
    store, _, _ = seed_run(tmp_path / "run.db")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        store.connection.execute("UPDATE runs SET mode = 'changed'")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        store.connection.execute("DELETE FROM scopes")
    with pytest.raises(sqlite3.IntegrityError):
        store.add_run(Run("run-2", "mock"))

    trigger_names = {
        row[0]
        for row in store.connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'trigger'"
        )
    }
    for table in FACT_TABLES:
        assert f"{table}_reject_update" in trigger_names
        assert f"{table}_reject_delete" in trigger_names


def test_lifecycle_sequences_cannot_be_overwritten_or_reused(tmp_path):
    store, run, _ = seed_run(tmp_path / "run.db")
    event = RunLifecycleEvent("event-1", run.run_id, 0, RunStatus.CREATED)
    store.add_run_event(event)
    with pytest.raises(sqlite3.IntegrityError, match="UNIQUE"):
        store.add_run_event(RunLifecycleEvent("event-2", run.run_id, 0, RunStatus.RUNNING))
    assert store.run_events() == (event,)


def test_foreign_keys_reject_orphan_facts(tmp_path):
    store = SQLiteRunStore.create(tmp_path / "run.db", Run("run-1", "mock"))
    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
        store.add_scope(ExecutionScope("child", "run-1", ScopeKind.FIXED, "child", "missing"))


def test_database_rejects_invalid_state_eligibility_and_unsafe_selection(tmp_path):
    store, run, scope = seed_run(tmp_path / "run.db")
    root_artifact = ArtifactReference(
        "artifact-root", run.run_id, scope.scope_id, ArtifactKind.DCP, "root.dcp",
    )
    root_state = DesignState(
        "state-root", run.run_id, scope.scope_id, root_artifact.artifact_id,
    )
    action = ActionSpecification(
        "action-1", run.run_id, scope.scope_id, "mock_action",
    )
    attempt = Attempt(
        "attempt-1", run.run_id, scope.scope_id, root_state.state_id, action.action_id,
    )
    invalid_artifact = ArtifactReference(
        "artifact-invalid", run.run_id, scope.scope_id, ArtifactKind.DCP, "invalid.dcp",
    )
    invalid_state = DesignState(
        "state-invalid", run.run_id, scope.scope_id, invalid_artifact.artifact_id,
        root_state.state_id, attempt.attempt_id,
    )
    invalid_validation = ValidationRecord(
        "validation-invalid", run.run_id, scope.scope_id, invalid_state.state_id,
        ValidationLevel.ATTEMPT, ValidationStatus.INVALID, attempt.attempt_id,
    )
    with store.transaction():
        store.add_artifact(root_artifact)
        store.add_state(root_state)
        store.add_action(action)
        store.add_attempt(attempt)
        store.add_artifact(invalid_artifact)
        store.add_state(invalid_state)
        store.add_validation(invalid_validation)

    with pytest.raises(sqlite3.IntegrityError, match="INVALID state"):
        store.add_eligibility(
            StateEligibility(
                "eligibility-invalid", run.run_id, invalid_state.state_id,
                True, True, "must be rejected",
            )
        )

    store.add_eligibility(
        StateEligibility(
            "eligibility-safe", run.run_id, invalid_state.state_id,
            False, False, "canonical invalid scratch",
        )
    )
    with pytest.raises(sqlite3.IntegrityError, match="eligible state"):
        store.add_selection(
            ResultSelection(
                "selection-invalid", run.run_id, scope.scope_id, invalid_state.state_id,
                invalid_artifact.artifact_id, SelectionKind.EXPLICIT, "must be rejected",
            )
        )


def test_database_rejects_invalid_validation_after_state_was_marked_eligible(tmp_path):
    store, run, scope = seed_run(tmp_path / "run.db")
    root_artifact = ArtifactReference(
        "artifact-root", run.run_id, scope.scope_id, ArtifactKind.DCP, "root.dcp",
    )
    root_state = DesignState("state-root", run.run_id, scope.scope_id, root_artifact.artifact_id)
    action = ActionSpecification("action-1", run.run_id, scope.scope_id, "mock_action")
    attempt = Attempt(
        "attempt-1", run.run_id, scope.scope_id, root_state.state_id, action.action_id,
    )
    output_artifact = ArtifactReference(
        "artifact-output", run.run_id, scope.scope_id, ArtifactKind.DCP, "output.dcp",
    )
    output_state = DesignState(
        "state-output", run.run_id, scope.scope_id, output_artifact.artifact_id,
        root_state.state_id, attempt.attempt_id,
    )
    with store.transaction():
        store.add_artifact(root_artifact)
        store.add_state(root_state)
        store.add_action(action)
        store.add_attempt(attempt)
        store.add_artifact(output_artifact)
        store.add_state(output_state)
        store.add_eligibility(
            StateEligibility(
                "eligibility-1", run.run_id, output_state.state_id,
                True, True, "temporarily asserted by test",
            )
        )

    with pytest.raises(sqlite3.IntegrityError, match="INVALID state"):
        store.add_validation(
            ValidationRecord(
                "validation-invalid", run.run_id, scope.scope_id, output_state.state_id,
                ValidationLevel.ATTEMPT, ValidationStatus.INVALID, attempt.attempt_id,
            )
        )


def test_selection_artifact_must_match_the_eligible_state(tmp_path):
    store, run, scope = seed_run(tmp_path / "run.db")
    state_artifact = ArtifactReference(
        "artifact-state", run.run_id, scope.scope_id, ArtifactKind.DCP, "state.dcp",
    )
    other_artifact = ArtifactReference(
        "artifact-other", run.run_id, scope.scope_id, ArtifactKind.DCP, "other.dcp",
    )
    state = DesignState("state-1", run.run_id, scope.scope_id, state_artifact.artifact_id)
    with store.transaction():
        store.add_artifact(state_artifact)
        store.add_artifact(other_artifact)
        store.add_state(state)
        store.add_eligibility(
            StateEligibility("eligibility-1", run.run_id, state.state_id, True, True, "baseline")
        )

    with pytest.raises(sqlite3.IntegrityError, match="matching artifact"):
        store.add_selection(
            ResultSelection(
                "selection-wrong", run.run_id, scope.scope_id, state.state_id,
                other_artifact.artifact_id, SelectionKind.BASELINE, "wrong artifact",
            )
        )
    expected = ResultSelection(
        "selection-safe", run.run_id, scope.scope_id, state.state_id,
        state_artifact.artifact_id, SelectionKind.BASELINE, "safe baseline",
    )
    store.add_selection(expected)
    assert store.selections() == (expected,)


def test_schema_migrates_existing_database_and_rejects_future_version(tmp_path):
    path = tmp_path / "run.db"
    connection = sqlite3.connect(path, isolation_level=None)
    assert migrate_schema(connection, target_version=1) == 1
    connection.execute(
        "INSERT INTO runs(run_id, mode, created_at, metadata_json) VALUES (?, ?, ?, ?)",
        ("run-1", "mock", utc_now().isoformat(), "{}"),
    )
    connection.close()

    with SQLiteRunStore(path) as migrated:
        assert migrated.schema_version == CURRENT_SCHEMA_VERSION
        assert migrated.get_run().run_id == "run-1"
        versions = tuple(
            row[0]
            for row in migrated.connection.execute(
                "SELECT version FROM schema_migrations ORDER BY version"
            )
        )
        assert versions == (1, 2)

    future = tmp_path / "future.db"
    connection = sqlite3.connect(future, isolation_level=None)
    connection.execute("PRAGMA user_version = 999")
    assert current_schema_version(connection) == 999
    connection.close()
    with pytest.raises(SchemaVersionError, match="newer than supported"):
        SQLiteRunStore(future)


def test_failed_schema_migration_rolls_back_version_and_partial_objects(tmp_path):
    path = tmp_path / "migration-crash.db"
    connection = sqlite3.connect(path, isolation_level=None)
    assert migrate_schema(connection, target_version=1) == 1
    connection.execute("CREATE INDEX idx_scopes_run ON scopes(run_id)")

    with pytest.raises(sqlite3.OperationalError, match="already exists"):
        migrate_schema(connection)

    assert current_schema_version(connection) == 1
    assert tuple(
        row[0]
        for row in connection.execute(
            "SELECT version FROM schema_migrations ORDER BY version"
        )
    ) == (1,)
    assert connection.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type = 'trigger'"
    ).fetchone()[0] == 0
    connection.close()


def test_concurrent_connections_append_independent_scope_facts(tmp_path):
    path = tmp_path / "run.db"
    store, run, root = seed_run(path)
    store.close()

    def append_scope(index: int) -> None:
        with SQLiteRunStore(path) as worker:
            worker.add_scope(
                ExecutionScope(
                    f"scope-{index}", run.run_id, ScopeKind.FRONTIER,
                    f"frontier-{index}", root.scope_id,
                )
            )

    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(append_scope, range(12)))

    with SQLiteRunStore(path) as reopened:
        assert len(reopened.scopes()) == 13
        assert reopened.integrity_check() == ()
