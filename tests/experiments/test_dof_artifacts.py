from __future__ import annotations

import hashlib
from pathlib import Path
import stat

import pytest

from optimizer_pipeline.core.domain import (
    ActionSpecification,
    ArtifactKind,
    ArtifactReference,
    Attempt,
    AttemptEventKind,
    AttemptLifecycleEvent,
    DesignState,
    ExecutionOutcome,
    ExecutionScope,
    OutcomeStatus,
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
from optimizer_pipeline.core.store import CURRENT_SCHEMA_VERSION, SQLiteRunStore
from optimizer_pipeline.dof.artifacts import (
    ARTIFACT_CONTRACT,
    ARTIFACT_LIFECYCLE_EVENT_SCHEMA,
    ARTIFACT_TRANSACTION_VERSION,
    RESUME_POLICY,
    ArtifactFailurePoint,
    ArtifactPhase,
    ArtifactTransaction,
    ArtifactTransactionError,
    ArtifactValidation,
    InjectedArtifactCrash,
    RootPreservationGuard,
    run_evidence_finalizer,
    verify_artifact_lifecycle,
)
from optimizer_pipeline.dof.evidence import (
    DEFAULT_REQUIRED_EVIDENCE_ROLES,
    ImmutableEvidenceWriter,
    register_evidence_and_finalize,
)


RUN_ID = "run-artifact-test"
SCOPE_ID = "scope-root"
ROOT_STATE_ID = "state-root"
ATTEMPT_ID = "attempt-001"


def _seed(tmp_path: Path) -> tuple[Path, SQLiteRunStore]:
    run_dir = tmp_path / "run"
    run_dir.mkdir(parents=True)
    root_path = run_dir / "root.dcp"
    root_path.write_bytes(b"immutable root\n")
    store = SQLiteRunStore.create(run_dir / "run.db", Run(RUN_ID, "dof_fake"))
    scope = ExecutionScope(SCOPE_ID, RUN_ID, ScopeKind.ROOT, "test root")
    root_artifact = ArtifactReference(
        "artifact-root",
        RUN_ID,
        SCOPE_ID,
        ArtifactKind.DCP,
        "root.dcp",
        sha256=hashlib.sha256(root_path.read_bytes()).hexdigest(),
        size_bytes=root_path.stat().st_size,
        metadata={"role": "baseline"},
    )
    root_state = DesignState(ROOT_STATE_ID, RUN_ID, SCOPE_ID, "artifact-root")
    action = ActionSpecification("action-001", RUN_ID, SCOPE_ID, "fake.action")
    attempt = Attempt(
        ATTEMPT_ID,
        RUN_ID,
        SCOPE_ID,
        ROOT_STATE_ID,
        action.action_id,
    )
    with store.transaction():
        store.add_run_event(
            RunLifecycleEvent("run-event-000", RUN_ID, 0, RunStatus.RUNNING)
        )
        store.add_scope(scope)
        store.add_artifact(root_artifact)
        store.add_state(root_state)
        store.add_eligibility(
            StateEligibility(
                "eligibility-root", RUN_ID, ROOT_STATE_ID, True, True, "baseline"
            )
        )
        store.add_selection(
            ResultSelection(
                "selection-root",
                RUN_ID,
                SCOPE_ID,
                ROOT_STATE_ID,
                "artifact-root",
                SelectionKind.BASELINE,
                "baseline",
            )
        )
        store.add_action(action)
        store.add_attempt(attempt)
        store.add_attempt_event(
            AttemptLifecycleEvent(
                "attempt-event-000", ATTEMPT_ID, 0, AttemptEventKind.SCHEDULED
            )
        )
        store.add_attempt_event(
            AttemptLifecycleEvent(
                "attempt-event-001", ATTEMPT_ID, 1, AttemptEventKind.STARTED
            )
        )
    return run_dir, store


def _producer(payload: bytes = b"validated child dcp\n"):
    def produce(path: Path) -> None:
        path.write_bytes(payload)

    return produce


def _clean_validator(path: Path, sha256: str, size_bytes: int) -> ArtifactValidation:
    assert hashlib.sha256(path.read_bytes()).hexdigest() == sha256
    assert path.stat().st_size == size_bytes
    return ArtifactValidation(
        ValidationStatus.CLEAN,
        "fake-validator.v1",
        {"route_clean": True},
    )


def _invalid_validator(path: Path, sha256: str, size_bytes: int) -> ArtifactValidation:
    return ArtifactValidation(
        ValidationStatus.INVALID,
        "fake-validator.v1",
        {"reason": "injected invalid artifact"},
    )


def _transaction(
    store: SQLiteRunStore,
    run_dir: Path,
    *,
    attempt_id: str = ATTEMPT_ID,
    state_id: str = "state-child",
) -> ArtifactTransaction:
    return ArtifactTransaction(
        store=store,
        run_dir=run_dir,
        run_id=RUN_ID,
        scope_id=SCOPE_ID,
        attempt_id=attempt_id,
        parent_state_id=ROOT_STATE_ID,
        state_id=state_id,
    )


def _append_manual_contract_phases(
    store: SQLiteRunStore,
    phases: tuple[ArtifactPhase, ...],
) -> None:
    """Append an intentionally unchecked phase sequence for verifier tests."""

    existing = store.attempt_events(attempt_id=ATTEMPT_ID)
    sequence = len(existing)
    with store.transaction():
        for offset, phase in enumerate(phases):
            detail: dict[str, object] = {
                "schema_version": ARTIFACT_LIFECYCLE_EVENT_SCHEMA,
                "transaction_version": ARTIFACT_TRANSACTION_VERSION,
                "phase": phase.value,
                "resume_policy": RESUME_POLICY,
            }
            kind = AttemptEventKind.STARTED
            if phase is ArtifactPhase.VALIDATED:
                detail["validation_status"] = ValidationStatus.CLEAN.value
            elif phase is ArtifactPhase.CANONICAL_FACTS_RECORDED:
                detail["selection_eligible"] = True
            elif phase is ArtifactPhase.SELECTED:
                kind = AttemptEventKind.COMPLETED
            elif phase is ArtifactPhase.INVALID_QUARANTINED:
                kind = AttemptEventKind.FAILED
                detail["validation_status"] = ValidationStatus.INVALID.value
            store.add_attempt_event(
                AttemptLifecycleEvent(
                    f"adversarial-artifact-event-{offset:03d}",
                    ATTEMPT_ID,
                    sequence + offset,
                    kind,
                    detail=detail,
                )
            )


def _insert_manual_contract_success(
    store: SQLiteRunStore,
    run_dir: Path,
    *,
    validation_subject: str | None,
) -> None:
    """Insert a schema-valid graph that bypasses the semantic writer guards."""

    payload = b"manually inserted contract child\n"
    digest = hashlib.sha256(payload).hexdigest()
    relative = f"artifacts/sha256/{digest}.dcp"
    output = run_dir / relative
    output.parent.mkdir(parents=True)
    output.write_bytes(payload)
    output.chmod(0o444)
    transaction = _transaction(store, run_dir)
    artifact_id = transaction._artifact_id(digest)
    artifact = ArtifactReference(
        artifact_id,
        RUN_ID,
        SCOPE_ID,
        ArtifactKind.DCP,
        relative,
        sha256=digest,
        size_bytes=len(payload),
        metadata={
            "contract": ARTIFACT_CONTRACT,
            "transaction_version": ARTIFACT_TRANSACTION_VERSION,
            "attempt_id": ATTEMPT_ID,
            "validated": True,
            "validation_status": ValidationStatus.CLEAN.value,
            "validator_version": "manual-test-validator.v1",
            "immutable": True,
        },
    )
    state = DesignState(
        "state-child",
        RUN_ID,
        SCOPE_ID,
        artifact.artifact_id,
        ROOT_STATE_ID,
        ATTEMPT_ID,
    )
    now = utc_now()
    with store.transaction():
        store.add_artifact(artifact)
        store.add_outcome(
            ExecutionOutcome(
                "outcome-manual-contract",
                ATTEMPT_ID,
                OutcomeStatus.SUCCEEDED,
                now,
                now,
                output_artifact_id=artifact.artifact_id,
            )
        )
        store.add_state(state)
        if validation_subject is not None:
            store.add_validation(
                ValidationRecord(
                    "validation-manual-contract",
                    RUN_ID,
                    SCOPE_ID,
                    validation_subject,
                    ValidationLevel.ATTEMPT,
                    ValidationStatus.CLEAN,
                    ATTEMPT_ID,
                )
            )
        store.add_eligibility(
            StateEligibility(
                "eligibility-manual-contract",
                RUN_ID,
                state.state_id,
                True,
                True,
                "unsafe manual insertion",
            )
        )
        store.add_selection(
            ResultSelection(
                "selection-manual-contract",
                RUN_ID,
                SCOPE_ID,
                state.state_id,
                artifact.artifact_id,
                SelectionKind.BEST_VALID,
                "unsafe manual insertion",
            )
        )
        store.add_attempt_event(
            AttemptLifecycleEvent(
                "attempt-event-manual-contract",
                ATTEMPT_ID,
                2,
                AttemptEventKind.COMPLETED,
                detail={
                    "schema_version": ARTIFACT_LIFECYCLE_EVENT_SCHEMA,
                    "transaction_version": ARTIFACT_TRANSACTION_VERSION,
                    "phase": ArtifactPhase.SELECTED.value,
                    "resume_policy": RESUME_POLICY,
                },
            )
        )


def test_valid_child_is_content_addressed_read_only_and_selected_only_after_validation(
    tmp_path: Path,
) -> None:
    run_dir, store = _seed(tmp_path)
    result = _transaction(store, run_dir).execute(
        producer=_producer(),
        validator=_clean_validator,
        metrics={"wns_ns": 0.1},
        selection_eligible=True,
    )

    assert store.schema_version == CURRENT_SCHEMA_VERSION == 2
    assert result.endpoint == "VALID_ARTIFACT"
    assert result.selected and result.valid
    assert result.immutable_uri == f"artifacts/sha256/{result.sha256}.dcp"
    published = run_dir / result.immutable_uri
    assert published.read_bytes() == b"validated child dcp\n"
    assert stat.S_IMODE(published.stat().st_mode) & 0o222 == 0
    assert not any((run_dir / ".staging").glob("*"))
    assert store.outcomes()[0].output_artifact_id == result.artifact_id
    child = next(state for state in store.states() if state.state_id == "state-child")
    assert child.artifact_id == result.artifact_id
    validation = store.validations(subject_id="state-child")[0]
    assert validation.status is ValidationStatus.CLEAN
    assert store.selections()[-1].state_id == "state-child"
    verification = verify_artifact_lifecycle(store, run_dir=run_dir)
    assert verification.ok, verification.errors
    assert verification.checked_artifact_ids == (result.artifact_id,)
    store.close()


def test_invalid_child_is_quarantined_without_artifact_state_or_selection(
    tmp_path: Path,
) -> None:
    run_dir, store = _seed(tmp_path)
    result = _transaction(store, run_dir).execute(
        producer=_producer(b"invalid child\n"),
        validator=_invalid_validator,
        selection_eligible=True,
    )

    assert result.endpoint == "INVALID_ARTIFACT"
    assert not result.valid and not result.selected
    assert result.artifact_id is None and result.state_id is None
    assert result.temporary_uri is not None
    assert result.temporary_uri.startswith(".quarantine/")
    assert (run_dir / result.temporary_uri).is_file()
    assert [artifact.artifact_id for artifact in store.artifacts()] == ["artifact-root"]
    assert [state.state_id for state in store.states()] == [ROOT_STATE_ID]
    assert [selection.state_id for selection in store.selections()] == [ROOT_STATE_ID]
    outcome = store.outcomes()[0]
    assert outcome.status.value == "FAILED"
    assert outcome.output_artifact_id is None
    assert store.validations(subject_id=ATTEMPT_ID)[0].status is ValidationStatus.INVALID
    contract_phases = [
        event.detail.to_dict()["phase"]
        for event in store.attempt_events(attempt_id=ATTEMPT_ID)
        if event.detail.to_dict().get("transaction_version")
        == ARTIFACT_TRANSACTION_VERSION
    ]
    assert contract_phases == [
        ArtifactPhase.BACKEND_COMPLETE.value,
        ArtifactPhase.TEMPORARY_WRITTEN.value,
        ArtifactPhase.VALIDATED.value,
        ArtifactPhase.INVALID_QUARANTINED.value,
    ]
    verification = verify_artifact_lifecycle(store, run_dir=run_dir)
    assert verification.ok, verification.errors
    store.close()


def test_quarantine_rename_before_sqlite_terminal_is_a_safe_incomplete_prefix(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_dir, store = _seed(tmp_path)

    def interrupt_before_invalid_facts(self: ArtifactTransaction, **kwargs) -> None:
        raise RuntimeError("injected after quarantine rename")

    monkeypatch.setattr(
        ArtifactTransaction,
        "_record_invalid_outcome",
        interrupt_before_invalid_facts,
    )
    with pytest.raises(RuntimeError, match="after quarantine rename"):
        _transaction(store, run_dir).execute(
            producer=_producer(b"invalid orphan\n"),
            validator=_invalid_validator,
            selection_eligible=True,
        )

    quarantined = list((run_dir / ".quarantine").glob("*.invalid"))
    assert len(quarantined) == 1
    assert stat.S_IMODE(quarantined[0].stat().st_mode) & 0o222 == 0
    contract_phases = [
        event.detail.to_dict()["phase"]
        for event in store.attempt_events(attempt_id=ATTEMPT_ID)
        if event.detail.to_dict().get("transaction_version")
        == ARTIFACT_TRANSACTION_VERSION
    ]
    assert contract_phases == [
        ArtifactPhase.BACKEND_COMPLETE.value,
        ArtifactPhase.TEMPORARY_WRITTEN.value,
        ArtifactPhase.VALIDATED.value,
    ]
    verification = verify_artifact_lifecycle(store, run_dir=run_dir)
    assert verification.safe, verification.errors
    assert verification.incomplete_attempt_ids == (ATTEMPT_ID,)
    store.close()


@pytest.mark.parametrize(
    "phases,expected_error",
    [
        (
            (
                ArtifactPhase.BACKEND_COMPLETE,
                ArtifactPhase.VALIDATED,
            ),
            "is not a valid artifact phase prefix",
        ),
        (
            (
                ArtifactPhase.BACKEND_COMPLETE,
                ArtifactPhase.TEMPORARY_WRITTEN,
                ArtifactPhase.TEMPORARY_WRITTEN,
            ),
            "repeats artifact phase TEMPORARY_WRITTEN",
        ),
        (
            (
                ArtifactPhase.BACKEND_COMPLETE,
                ArtifactPhase.VALIDATED,
                ArtifactPhase.TEMPORARY_WRITTEN,
            ),
            "is not a valid artifact phase prefix",
        ),
        (
            (
                ArtifactPhase.BACKEND_COMPLETE,
                ArtifactPhase.TEMPORARY_WRITTEN,
                ArtifactPhase.VALIDATED,
                ArtifactPhase.SELECTED,
            ),
            "is not a valid artifact phase prefix",
        ),
    ],
    ids=("missing", "duplicate", "out-of-order", "illegal-terminal-path"),
)
def test_semantic_verifier_rejects_malformed_artifact_phase_paths(
    tmp_path: Path,
    phases: tuple[ArtifactPhase, ...],
    expected_error: str,
) -> None:
    run_dir, store = _seed(tmp_path)
    _append_manual_contract_phases(store, phases)

    assert store.integrity_check() == ()
    verification = verify_artifact_lifecycle(store, run_dir=run_dir)
    assert not verification.safe
    assert any(expected_error in error for error in verification.errors)
    store.close()


def test_valid_nonselected_child_has_a_terminal_attempt_and_safe_complete_graph(
    tmp_path: Path,
) -> None:
    run_dir, store = _seed(tmp_path)
    result = _transaction(store, run_dir).execute(
        producer=_producer(),
        validator=_clean_validator,
        selection_eligible=False,
    )

    assert result.valid and not result.selected
    assert store.attempt_events(attempt_id=ATTEMPT_ID)[-1].kind is AttemptEventKind.COMPLETED
    assert [selection.state_id for selection in store.selections()] == [ROOT_STATE_ID]
    verification = verify_artifact_lifecycle(store, run_dir=run_dir)
    assert verification.ok, verification.errors
    store.close()


@pytest.mark.parametrize(
    "failure_point,registered,canonical",
    [
        (ArtifactFailurePoint.AFTER_BACKEND_BEFORE_ARTIFACT, False, False),
        (ArtifactFailurePoint.AFTER_TEMP_WRITE_BEFORE_VALIDATION, False, False),
        (ArtifactFailurePoint.AFTER_VALIDATION_BEFORE_REGISTRATION, False, False),
        (ArtifactFailurePoint.AFTER_REGISTRATION_BEFORE_CANONICAL_FACTS, True, False),
        (ArtifactFailurePoint.AFTER_CANONICAL_OUTCOME_BEFORE_SELECTION, True, True),
        (ArtifactFailurePoint.AFTER_SELECTION_INTENT_BEFORE_TERMINAL, True, True),
    ],
)
def test_crash_points_preserve_schema_integrity_and_never_select_partial_child(
    tmp_path: Path,
    failure_point: ArtifactFailurePoint,
    registered: bool,
    canonical: bool,
) -> None:
    run_dir, store = _seed(tmp_path / failure_point.value)
    with pytest.raises(InjectedArtifactCrash) as raised:
        _transaction(store, run_dir).execute(
            producer=_producer(),
            validator=_clean_validator,
            selection_eligible=True,
            fail_at=failure_point,
        )

    assert raised.value.failure_point is failure_point
    assert store.integrity_check() == ()
    contract_artifacts = [
        item
        for item in store.artifacts()
        if item.metadata.to_dict().get("contract") == ARTIFACT_CONTRACT
    ]
    assert bool(contract_artifacts) is registered
    assert bool(store.outcomes()) is canonical
    assert ("state-child" in {state.state_id for state in store.states()}) is canonical
    assert [selection.state_id for selection in store.selections()] == [ROOT_STATE_ID]
    if failure_point is ArtifactFailurePoint.AFTER_VALIDATION_BEFORE_REGISTRATION:
        # The critical filesystem/SQLite boundary is exercised after the
        # immutable rename and before ArtifactReference insertion.
        orphans = list((run_dir / "artifacts" / "sha256").glob("*.dcp"))
        assert len(orphans) == 1
        assert stat.S_IMODE(orphans[0].stat().st_mode) & 0o222 == 0
        assert contract_artifacts == []
    verification = verify_artifact_lifecycle(store, run_dir=run_dir)
    assert verification.safe, verification.errors
    assert ATTEMPT_ID in verification.incomplete_attempt_ids
    store.close()


def test_evidence_finalization_failpoint_never_calls_finalizer() -> None:
    calls: list[str] = []

    with pytest.raises(InjectedArtifactCrash) as raised:
        run_evidence_finalizer(
            lambda: calls.append("called"),
            fail_at=ArtifactFailurePoint.DURING_EVIDENCE_FINALIZATION,
        )

    assert raised.value.endpoint == "ARTIFACT_TRANSACTION_INTERRUPTED"
    assert calls == []


def test_real_evidence_writer_and_schema_v2_registration_roll_back_mid_finalization(
    tmp_path: Path,
) -> None:
    run_dir, store = _seed(tmp_path)
    spec_hash = hashlib.sha256(b"artifact crash evidence spec").hexdigest()
    writer = ImmutableEvidenceWriter(run_dir, RUN_ID, spec_hash)
    sidecars = []
    for role in DEFAULT_REQUIRED_EVIDENCE_ROLES:
        if role == "decision_event_trace":
            sidecars.append(
                writer.write_jsonl(
                    "evidence/decision_event_trace.jsonl",
                    ({"record_type": "CRASH_INJECTION"},),
                )
            )
        elif role == "result_summary":
            sidecars.append(
                writer.write_json(
                    "evidence/result_summary.json",
                    {"endpoint": "EVIDENCE_INTEGRITY_FAILURE"},
                    role=role,
                    schema_version="dof.result-summary.v1",
                    kind=ArtifactKind.REPORT,
                )
            )
        else:
            sidecars.append(
                writer.write_json(
                    f"evidence/{role}.json",
                    {"schema_version": f"{role}.v1"},
                    role=role,
                    schema_version=f"{role}.v1",
                )
            )

    original_add_run_event = store.add_run_event

    def fail_on_terminal(event):
        if event.status is RunStatus.FINALIZED:
            raise RuntimeError("injected during evidence finalization")
        return original_add_run_event(event)

    store.add_run_event = fail_on_terminal  # type: ignore[method-assign]
    with pytest.raises(RuntimeError, match="injected during evidence finalization"):
        register_evidence_and_finalize(
            store,
            run_dir=run_dir,
            sidecars=sidecars,
            run_id=RUN_ID,
            spec_hash=spec_hash,
        )

    # Every evidence artifact insert and the registration event shared the
    # failed SQLite transaction; sealed files remain auditable and untouched.
    assert [artifact.artifact_id for artifact in store.artifacts()] == ["artifact-root"]
    assert [event.event_id for event in store.run_events()] == ["run-event-000"]
    assert store.integrity_check() == ()
    assert all(stat.S_IMODE((run_dir / sidecar.uri).stat().st_mode) & 0o222 == 0 for sidecar in sidecars)
    store.close()


def test_semantic_verifier_detects_schema_permitted_selection_without_validation(
    tmp_path: Path,
) -> None:
    run_dir, store = _seed(tmp_path)
    _insert_manual_contract_success(
        store,
        run_dir,
        validation_subject=None,
    )

    assert store.integrity_check() == ()
    verification = verify_artifact_lifecycle(store, run_dir=run_dir)
    assert not verification.safe
    assert any("without exactly one validation" in error for error in verification.errors)
    store.close()


def test_validator_cannot_rewrite_fingerprinted_child_and_return_clean(
    tmp_path: Path,
) -> None:
    run_dir, store = _seed(tmp_path)

    def mutating_validator(
        path: Path, sha256: str, size_bytes: int
    ) -> ArtifactValidation:
        path.write_bytes(b"mutated after initial fingerprint\n")
        return ArtifactValidation(
            ValidationStatus.CLEAN,
            "mutating-validator.v1",
            {"claimed_clean": True},
        )

    result = _transaction(store, run_dir).execute(
        producer=_producer(),
        validator=mutating_validator,
        selection_eligible=True,
    )

    assert result.endpoint == "EVIDENCE_INTEGRITY_FAILURE"
    assert not result.valid and not result.selected
    assert result.validation_status == ValidationStatus.FAILED.value
    assert result.temporary_uri is not None
    assert (run_dir / result.temporary_uri).read_bytes() == (
        b"mutated after initial fingerprint\n"
    )
    assert [artifact.artifact_id for artifact in store.artifacts()] == ["artifact-root"]
    assert [state.state_id for state in store.states()] == [ROOT_STATE_ID]
    assert [selection.state_id for selection in store.selections()] == [ROOT_STATE_ID]
    assert store.outcomes()[0].error_type == "EVIDENCE_INTEGRITY_FAILURE"
    verification = verify_artifact_lifecycle(store, run_dir=run_dir)
    assert verification.ok, verification.errors
    store.close()


def test_post_publish_refingerprint_blocks_changed_immutable_before_db_facts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_dir, store = _seed(tmp_path)
    original = ArtifactTransaction._publish_immutable

    def publish_then_corrupt(
        self: ArtifactTransaction,
        temporary: Path,
        digest: str,
        size_bytes: int,
    ) -> Path:
        target = original(self, temporary, digest, size_bytes)
        target.chmod(0o644)
        target.write_bytes(b"changed after immutable rename\n")
        target.chmod(0o444)
        return target

    monkeypatch.setattr(
        ArtifactTransaction,
        "_publish_immutable",
        publish_then_corrupt,
    )
    with pytest.raises(
        ArtifactTransactionError,
        match="immutable_artifact_changed_before_registration",
    ) as raised:
        _transaction(store, run_dir).execute(
            producer=_producer(),
            validator=_clean_validator,
            selection_eligible=True,
        )

    assert raised.value.endpoint == "EVIDENCE_INTEGRITY_FAILURE"
    assert [artifact.artifact_id for artifact in store.artifacts()] == ["artifact-root"]
    assert store.outcomes() == ()
    assert [state.state_id for state in store.states()] == [ROOT_STATE_ID]
    assert [selection.state_id for selection in store.selections()] == [ROOT_STATE_ID]
    verification = verify_artifact_lifecycle(store, run_dir=run_dir)
    assert verification.safe, verification.errors
    assert verification.incomplete_attempt_ids == (ATTEMPT_ID,)
    store.close()


def test_validator_cannot_replace_candidate_with_symlink(tmp_path: Path) -> None:
    run_dir, store = _seed(tmp_path)

    def symlink_validator(
        path: Path, sha256: str, size_bytes: int
    ) -> ArtifactValidation:
        replacement = path.parent / "replacement.dcp"
        replacement.write_bytes(path.read_bytes())
        path.unlink()
        path.symlink_to(replacement.name)
        return ArtifactValidation(
            ValidationStatus.CLEAN,
            "symlink-validator.v1",
            {"claimed_clean": True},
        )

    with pytest.raises(
        ArtifactTransactionError,
        match="validator_replaced_artifact_with_non_regular_file",
    ) as raised:
        _transaction(store, run_dir).execute(
            producer=_producer(),
            validator=symlink_validator,
            selection_eligible=True,
        )

    assert raised.value.endpoint == "EVIDENCE_INTEGRITY_FAILURE"
    assert store.outcomes() == ()
    assert [selection.state_id for selection in store.selections()] == [ROOT_STATE_ID]
    verification = verify_artifact_lifecycle(store, run_dir=run_dir)
    assert verification.safe, verification.errors
    assert verification.incomplete_attempt_ids == (ATTEMPT_ID,)
    store.close()


def test_semantic_verifier_rejects_validation_for_unrelated_subject(
    tmp_path: Path,
) -> None:
    run_dir, store = _seed(tmp_path)
    _insert_manual_contract_success(
        store,
        run_dir,
        validation_subject="totally-unrelated-subject",
    )

    assert store.integrity_check() == ()
    verification = verify_artifact_lifecycle(store, run_dir=run_dir)
    assert not verification.safe
    assert any(
        "validation subject/run/scope mismatch" in error
        for error in verification.errors
    )
    store.close()


def test_semantic_verifier_rejects_duplicate_attempt_validation(
    tmp_path: Path,
) -> None:
    run_dir, store = _seed(tmp_path)
    _transaction(store, run_dir).execute(
        producer=_producer(),
        validator=_clean_validator,
        selection_eligible=False,
    )
    with store.transaction():
        store.add_validation(
            ValidationRecord(
                "validation-duplicate",
                RUN_ID,
                SCOPE_ID,
                "state-child",
                ValidationLevel.ATTEMPT,
                ValidationStatus.CLEAN,
                ATTEMPT_ID,
                details={"duplicate": True},
            )
        )

    verification = verify_artifact_lifecycle(store, run_dir=run_dir)
    assert not verification.safe
    assert any("multiple attempt validations" in error for error in verification.errors)
    store.close()


def test_additive_verifier_does_not_apply_contract_rules_to_legacy_success(
    tmp_path: Path,
) -> None:
    run_dir, store = _seed(tmp_path)
    payload = b"legacy managed child\n"
    child_path = run_dir / "legacy-child.dcp"
    child_path.write_bytes(payload)
    digest = hashlib.sha256(payload).hexdigest()
    artifact = ArtifactReference(
        "artifact-legacy-child",
        RUN_ID,
        SCOPE_ID,
        ArtifactKind.DCP,
        "legacy-child.dcp",
        sha256=digest,
        size_bytes=len(payload),
        metadata={"legacy_contract": "managed-run.v1"},
    )
    state = DesignState(
        "state-legacy-child",
        RUN_ID,
        SCOPE_ID,
        artifact.artifact_id,
        ROOT_STATE_ID,
        ATTEMPT_ID,
    )
    now = utc_now()
    with store.transaction():
        store.add_artifact(artifact)
        store.add_outcome(
            ExecutionOutcome(
                "outcome-legacy",
                ATTEMPT_ID,
                OutcomeStatus.SUCCEEDED,
                now,
                now,
                output_artifact_id=artifact.artifact_id,
            )
        )
        store.add_state(state)
        store.add_validation(
            ValidationRecord(
                "validation-legacy",
                RUN_ID,
                SCOPE_ID,
                state.state_id,
                ValidationLevel.ATTEMPT,
                ValidationStatus.CLEAN,
                ATTEMPT_ID,
            )
        )
        store.add_eligibility(
            StateEligibility(
                "eligibility-legacy",
                RUN_ID,
                state.state_id,
                True,
                False,
                "legacy child",
            )
        )
        store.add_attempt_event(
            AttemptLifecycleEvent(
                "attempt-event-legacy-complete",
                ATTEMPT_ID,
                2,
                AttemptEventKind.COMPLETED,
            )
        )

    verification = verify_artifact_lifecycle(store, run_dir=run_dir)
    assert verification.ok, verification.errors
    assert verification.incomplete_attempt_ids == ()
    assert verification.checked_artifact_ids == ()
    store.close()


@pytest.mark.parametrize(
    "linked_parent,validator",
    [
        (".staging", _clean_validator),
        (".quarantine", _invalid_validator),
        ("artifacts", _clean_validator),
    ],
)
def test_artifact_workspace_rejects_parent_symlink_escape_before_replace(
    tmp_path: Path,
    linked_parent: str,
    validator,
) -> None:
    run_dir, store = _seed(tmp_path / "case")
    outside = tmp_path / f"outside-{linked_parent.lstrip('.')}"
    outside.mkdir()
    (run_dir / linked_parent).symlink_to(outside, target_is_directory=True)

    with pytest.raises(ArtifactTransactionError, match="escapes the run directory"):
        _transaction(store, run_dir).execute(
            producer=_producer(),
            validator=validator,
            selection_eligible=True,
        )

    assert list(outside.iterdir()) == []
    assert [selection.state_id for selection in store.selections()] == [ROOT_STATE_ID]
    store.close()


def test_artifact_workspace_rejects_internal_parent_symlink_alias(
    tmp_path: Path,
) -> None:
    run_dir, store = _seed(tmp_path)
    aliased = run_dir / "aliased-artifacts"
    aliased.mkdir()
    (run_dir / "artifacts").symlink_to(aliased, target_is_directory=True)

    with pytest.raises(ArtifactTransactionError, match="symlinked parent"):
        _transaction(store, run_dir).execute(
            producer=_producer(),
            validator=_clean_validator,
            selection_eligible=True,
        )

    assert list(aliased.iterdir()) == []
    assert [selection.state_id for selection in store.selections()] == [ROOT_STATE_ID]
    store.close()


def test_sanitization_collisions_cannot_alias_paths_or_generated_fact_ids(
    tmp_path: Path,
) -> None:
    run_dir, store = _seed(tmp_path)
    colliding_attempt_ids = ("attempt/a", "attempt?a")
    with store.transaction():
        for index, attempt_id in enumerate(colliding_attempt_ids, start=10):
            store.add_attempt(
                Attempt(
                    attempt_id,
                    RUN_ID,
                    SCOPE_ID,
                    ROOT_STATE_ID,
                    "action-001",
                )
            )
            store.add_attempt_event(
                AttemptLifecycleEvent(
                    f"collision-event-{index}-scheduled",
                    attempt_id,
                    0,
                    AttemptEventKind.SCHEDULED,
                )
            )
            store.add_attempt_event(
                AttemptLifecycleEvent(
                    f"collision-event-{index}-started",
                    attempt_id,
                    1,
                    AttemptEventKind.STARTED,
                )
            )

    results = [
        _transaction(
            store,
            run_dir,
            attempt_id=attempt_id,
            state_id=f"state-collision-{index}",
        ).execute(
            producer=_producer(b"invalid collision candidate\n"),
            validator=_invalid_validator,
            selection_eligible=True,
        )
        for index, attempt_id in enumerate(colliding_attempt_ids)
    ]

    assert len({result.temporary_uri for result in results}) == 2
    assert len({outcome.outcome_id for outcome in store.outcomes()}) == 2
    assert len({item.validation_id for item in store.validations()}) == 2
    verification = verify_artifact_lifecycle(store, run_dir=run_dir)
    assert verification.ok, verification.errors
    store.close()


def test_root_preservation_guard_detects_input_mutation(tmp_path: Path) -> None:
    root = tmp_path / "input.dcp"
    root.write_bytes(b"root before\n")
    guard = RootPreservationGuard(root)
    assert guard.verify_unchanged() == guard.before
    root.write_bytes(b"root after\n")
    with pytest.raises(Exception, match="root_artifact_changed"):
        guard.verify_unchanged()
