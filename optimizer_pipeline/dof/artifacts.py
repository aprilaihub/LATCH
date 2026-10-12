"""Crash-safe artifact transactions for the DoF experiment path.

This module is intentionally additive to the canonical schema-v2 store.  The
filesystem and SQLite cannot share one atomic transaction, so the protocol
makes every boundary explicit and safe to inspect after interruption:

``temporary -> validated -> immutable content address -> canonical facts -> selection``

An interrupted run is never resumed in place.  A caller may preserve it for
audit and start a new run/repeat, but must not rewrite an existing immutable
artifact or pretend that a partially completed lifecycle was successful.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
import hashlib
import os
from pathlib import Path
import re
import stat
from typing import Any, Protocol

from optimizer_pipeline.core.domain import (
    ArtifactKind,
    ArtifactReference,
    AttemptEventKind,
    AttemptLifecycleEvent,
    DesignState,
    DomainInvariantError,
    ExecutionOutcome,
    ManagedStateFacts,
    OutcomeStatus,
    ResultSelection,
    SelectionKind,
    StateEligibility,
    ValidationLevel,
    ValidationRecord,
    ValidationStatus,
    utc_now,
    validate_selection,
)
from optimizer_pipeline.core.store import CURRENT_SCHEMA_VERSION, SQLiteRunStore


ARTIFACT_TRANSACTION_VERSION = "dof.artifact-transaction.v1"
ARTIFACT_CONTRACT = "dof.content-addressed-dcp.v1"
ARTIFACT_LIFECYCLE_EVENT_SCHEMA = "dof.artifact-lifecycle-event.v1"
RESUME_POLICY = "new-run-or-repeat-only.v1"

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_SAFE_ID_RE = re.compile(r"[^A-Za-z0-9_.:-]+")
_ACCEPTED_VALIDATION_STATUSES = frozenset(
    {
        ValidationStatus.CLEAN,
        ValidationStatus.WARNING,
        ValidationStatus.FALLBACK_USED,
    }
)


class ArtifactPhase(str, Enum):
    BACKEND_COMPLETE = "BACKEND_COMPLETE"
    TEMPORARY_WRITTEN = "TEMPORARY_WRITTEN"
    VALIDATED = "VALIDATED"
    IMMUTABLE_REGISTERED = "IMMUTABLE_REGISTERED"
    CANONICAL_FACTS_RECORDED = "CANONICAL_FACTS_RECORDED"
    SELECTION_INTENT_RECORDED = "SELECTION_INTENT_RECORDED"
    SELECTED = "SELECTED"
    INVALID_QUARANTINED = "INVALID_QUARANTINED"


_SELECTED_ARTIFACT_PHASE_PATH = (
    ArtifactPhase.BACKEND_COMPLETE,
    ArtifactPhase.TEMPORARY_WRITTEN,
    ArtifactPhase.VALIDATED,
    ArtifactPhase.IMMUTABLE_REGISTERED,
    ArtifactPhase.CANONICAL_FACTS_RECORDED,
    ArtifactPhase.SELECTION_INTENT_RECORDED,
    ArtifactPhase.SELECTED,
)
_NONSELECTED_ARTIFACT_PHASE_PATH = _SELECTED_ARTIFACT_PHASE_PATH[:5]
_INVALID_ARTIFACT_PHASE_PATH = (
    ArtifactPhase.BACKEND_COMPLETE,
    ArtifactPhase.TEMPORARY_WRITTEN,
    ArtifactPhase.VALIDATED,
    ArtifactPhase.INVALID_QUARANTINED,
)
_ARTIFACT_PHASE_PATHS = (
    _SELECTED_ARTIFACT_PHASE_PATH,
    _NONSELECTED_ARTIFACT_PHASE_PATH,
    _INVALID_ARTIFACT_PHASE_PATH,
)


class ArtifactFailurePoint(str, Enum):
    AFTER_BACKEND_BEFORE_ARTIFACT = "after_backend_before_artifact"
    AFTER_TEMP_WRITE_BEFORE_VALIDATION = "after_temp_write_before_validation"
    AFTER_VALIDATION_BEFORE_REGISTRATION = "after_validation_before_registration"
    AFTER_REGISTRATION_BEFORE_CANONICAL_FACTS = (
        "after_registration_before_canonical_facts"
    )
    AFTER_CANONICAL_OUTCOME_BEFORE_SELECTION = (
        "after_canonical_outcome_before_selection"
    )
    AFTER_SELECTION_INTENT_BEFORE_TERMINAL = (
        "after_selection_intent_before_terminal"
    )
    DURING_EVIDENCE_FINALIZATION = "during_evidence_finalization"


class ArtifactTransactionError(RuntimeError):
    """Typed non-success raised by an interrupted artifact transaction."""

    def __init__(
        self,
        reason: str,
        *,
        endpoint: str = "ARTIFACT_TRANSACTION_FAILURE",
        phase: ArtifactPhase | None = None,
        failure_point: ArtifactFailurePoint | None = None,
    ) -> None:
        self.reason = str(reason)
        self.endpoint = str(endpoint)
        self.phase = phase
        self.failure_point = failure_point
        super().__init__(self.reason)


class InjectedArtifactCrash(ArtifactTransactionError):
    """Deterministic test-only interruption at one documented boundary."""

    def __init__(
        self,
        failure_point: ArtifactFailurePoint,
        *,
        phase: ArtifactPhase | None,
    ) -> None:
        super().__init__(
            f"injected_artifact_crash:{failure_point.value}",
            endpoint="ARTIFACT_TRANSACTION_INTERRUPTED",
            phase=phase,
            failure_point=failure_point,
        )


@dataclass(frozen=True, slots=True)
class RootFingerprint:
    sha256: str
    size_bytes: int


@dataclass(frozen=True, slots=True)
class ArtifactValidation:
    status: ValidationStatus
    validator_version: str
    details: Mapping[str, Any]

    def __post_init__(self) -> None:
        if self.status not in {
            ValidationStatus.CLEAN,
            ValidationStatus.WARNING,
            ValidationStatus.FALLBACK_USED,
            ValidationStatus.INVALID,
            ValidationStatus.FAILED,
        }:
            raise ValueError("artifact validation must be an Attempt status")
        if not isinstance(self.validator_version, str) or not self.validator_version.strip():
            raise ValueError("validator_version must be a non-empty string")
        object.__setattr__(self, "details", dict(self.details))

    @property
    def valid(self) -> bool:
        return self.status in _ACCEPTED_VALIDATION_STATUSES


class ArtifactValidator(Protocol):
    def __call__(self, path: Path, sha256: str, size_bytes: int) -> ArtifactValidation: ...


class ArtifactProducer(Protocol):
    def __call__(self, temporary_path: Path) -> object: ...


@dataclass(frozen=True, slots=True)
class ArtifactTransactionResult:
    endpoint: str
    phase: ArtifactPhase
    valid: bool
    selected: bool
    artifact_id: str | None
    state_id: str | None
    immutable_uri: str | None
    temporary_uri: str | None
    sha256: str | None
    size_bytes: int | None
    validation_status: str
    resume_policy: str = RESUME_POLICY


@dataclass(frozen=True, slots=True)
class ArtifactLifecycleVerification:
    errors: tuple[str, ...]
    incomplete_attempt_ids: tuple[str, ...]
    checked_artifact_ids: tuple[str, ...]

    @property
    def ok(self) -> bool:
        return not self.errors and not self.incomplete_attempt_ids

    @property
    def safe(self) -> bool:
        """Whether no unsafe publication/selection was found.

        An interrupted run can be safe but incomplete.  This distinction is
        useful in crash-injection tests and avoids calling an orphan a valid
        completed result.
        """

        return not self.errors


def fingerprint_file(path: str | Path) -> RootFingerprint:
    source = Path(path)
    if source.is_symlink() or not source.is_file():
        raise ArtifactTransactionError(
            "root_or_artifact_must_be_a_regular_non_symlink_file",
            endpoint="INVALID_ARTIFACT",
        )
    digest, size = _hash_and_size(source)
    return RootFingerprint(digest, size)


class RootPreservationGuard:
    """Record and later prove that the experiment input did not change."""

    def __init__(self, root_path: str | Path) -> None:
        # ``absolute`` keeps a final symlink visible to ``fingerprint_file``;
        # resolving first would accidentally turn a symlink into an accepted
        # regular target.
        self.root_path = Path(root_path).absolute()
        self.before = fingerprint_file(self.root_path)

    def verify_unchanged(self) -> RootFingerprint:
        after = fingerprint_file(self.root_path)
        if after != self.before:
            raise ArtifactTransactionError(
                "root_artifact_changed_during_run",
                endpoint="ROOT_ARTIFACT_MUTATION",
            )
        return after


class ArtifactTransaction:
    """Publish one validated child and append its schema-v2 fact lifecycle."""

    def __init__(
        self,
        *,
        store: SQLiteRunStore,
        run_dir: str | Path,
        run_id: str,
        scope_id: str,
        attempt_id: str,
        parent_state_id: str,
        state_id: str,
        started_at: datetime | None = None,
        now: Callable[[], datetime] = utc_now,
    ) -> None:
        if store.schema_version != CURRENT_SCHEMA_VERSION:
            raise ArtifactTransactionError(
                f"run.db schema must remain {CURRENT_SCHEMA_VERSION}",
                endpoint="EVIDENCE_INTEGRITY_FAILURE",
            )
        self.store = store
        self.run_dir = Path(run_dir).resolve()
        self.run_id = _required("run_id", run_id)
        self.scope_id = _required("scope_id", scope_id)
        self.attempt_id = _required("attempt_id", attempt_id)
        self.parent_state_id = _required("parent_state_id", parent_state_id)
        self.state_id = _required("state_id", state_id)
        self.started_at = started_at or now()
        self.now = now
        self._phase: ArtifactPhase | None = None
        self._validate_canonical_links()

    def execute(
        self,
        *,
        producer: ArtifactProducer,
        validator: ArtifactValidator,
        metrics: Mapping[str, Any] | None = None,
        selection_eligible: bool = False,
        selection_reason: str = "validated eligible DoF successor",
        fail_at: ArtifactFailurePoint | None = None,
    ) -> ArtifactTransactionResult:
        """Run the transaction, raising only for interruption/contract faults.

        An ordinary invalid artifact is preserved in a quarantine directory,
        recorded as a failed canonical outcome, and returned as a typed result.
        It is never registered as a DCP artifact, state, eligibility or
        selection.
        """

        self._phase = ArtifactPhase.BACKEND_COMPLETE
        self._append_phase(self._phase)
        self._inject(fail_at, ArtifactFailurePoint.AFTER_BACKEND_BEFORE_ARTIFACT)

        temporary = self._temporary_path()
        if temporary.exists() or temporary.is_symlink():
            raise ArtifactTransactionError(
                "temporary_artifact_already_exists_resume_forbidden",
                phase=self._phase,
            )
        producer(temporary)
        if temporary.is_symlink() or not temporary.is_file():
            raise ArtifactTransactionError(
                "backend_completed_without_regular_temporary_artifact",
                endpoint="INVALID_ARTIFACT",
                phase=self._phase,
            )
        _fsync_file(temporary)
        digest, size_bytes = _hash_and_size(temporary)
        self._phase = ArtifactPhase.TEMPORARY_WRITTEN
        self._append_phase(
            self._phase,
            sha256=digest,
            size_bytes=size_bytes,
            temporary_uri=self._relative_uri(temporary),
        )
        self._inject(
            fail_at, ArtifactFailurePoint.AFTER_TEMP_WRITE_BEFORE_VALIDATION
        )

        validation = validator(temporary, digest, size_bytes)
        if not isinstance(validation, ArtifactValidation):
            raise ArtifactTransactionError(
                "validator_returned_wrong_contract",
                endpoint="EVIDENCE_INTEGRITY_FAILURE",
                phase=self._phase,
            )
        if temporary.is_symlink() or not temporary.is_file():
            raise ArtifactTransactionError(
                "validator_replaced_artifact_with_non_regular_file",
                endpoint="EVIDENCE_INTEGRITY_FAILURE",
                phase=self._phase,
            )
        validated_digest, validated_size = _hash_and_size(temporary)
        if (validated_digest, validated_size) != (digest, size_bytes):
            failed_validation = ArtifactValidation(
                ValidationStatus.FAILED,
                validation.validator_version,
                {
                    **dict(validation.details),
                    "reason": "artifact_changed_during_validation",
                    "validator_reported_status": validation.status.value,
                    "pre_validation_sha256": digest,
                    "pre_validation_size_bytes": size_bytes,
                    "post_validation_sha256": validated_digest,
                    "post_validation_size_bytes": validated_size,
                },
            )
            self._phase = ArtifactPhase.VALIDATED
            self._append_phase(
                self._phase,
                sha256=validated_digest,
                size_bytes=validated_size,
                validation_status=failed_validation.status.value,
                validator_version=failed_validation.validator_version,
                integrity_failure="artifact_changed_during_validation",
            )
            quarantine = self._quarantine(temporary, validated_digest)
            self._record_invalid_outcome(
                validation=failed_validation,
                digest=validated_digest,
                size_bytes=validated_size,
                metrics=metrics,
                quarantine_uri=self._relative_uri(quarantine),
                endpoint="EVIDENCE_INTEGRITY_FAILURE",
            )
            self._phase = ArtifactPhase.INVALID_QUARANTINED
            return ArtifactTransactionResult(
                endpoint="EVIDENCE_INTEGRITY_FAILURE",
                phase=self._phase,
                valid=False,
                selected=False,
                artifact_id=None,
                state_id=None,
                immutable_uri=None,
                temporary_uri=self._relative_uri(quarantine),
                sha256=validated_digest,
                size_bytes=validated_size,
                validation_status=failed_validation.status.value,
            )
        self._phase = ArtifactPhase.VALIDATED
        self._append_phase(
            self._phase,
            sha256=digest,
            size_bytes=size_bytes,
            validation_status=validation.status.value,
            validator_version=validation.validator_version,
        )
        if not validation.valid:
            quarantine = self._quarantine(temporary, digest)
            self._record_invalid_outcome(
                validation=validation,
                digest=digest,
                size_bytes=size_bytes,
                metrics=metrics,
                quarantine_uri=self._relative_uri(quarantine),
                endpoint="INVALID_ARTIFACT",
            )
            self._phase = ArtifactPhase.INVALID_QUARANTINED
            return ArtifactTransactionResult(
                endpoint="INVALID_ARTIFACT",
                phase=self._phase,
                valid=False,
                selected=False,
                artifact_id=None,
                state_id=None,
                immutable_uri=None,
                temporary_uri=self._relative_uri(quarantine),
                sha256=digest,
                size_bytes=size_bytes,
                validation_status=validation.status.value,
            )

        immutable = self._publish_immutable(temporary, digest, size_bytes)
        if immutable.is_symlink() or not immutable.is_file():
            raise ArtifactTransactionError(
                "published_artifact_is_not_a_regular_file",
                endpoint="EVIDENCE_INTEGRITY_FAILURE",
                phase=self._phase,
            )
        published_digest, published_size = _hash_and_size(immutable)
        if (published_digest, published_size) != (digest, size_bytes):
            raise ArtifactTransactionError(
                "immutable_artifact_changed_before_registration",
                endpoint="EVIDENCE_INTEGRITY_FAILURE",
                phase=self._phase,
            )
        immutable_uri = self._relative_uri(immutable)
        # This is the filesystem/SQLite crash boundary: the immutable rename
        # is durable, but no canonical ArtifactReference exists yet.  A crash
        # here leaves a harmless content-addressed orphan and an incomplete
        # attempt; a new run/repeat may reuse the bytes but never resumes the
        # interrupted attempt in place.
        self._inject(
            fail_at, ArtifactFailurePoint.AFTER_VALIDATION_BEFORE_REGISTRATION
        )
        artifact_id = self._artifact_id(digest)
        artifact = ArtifactReference(
            artifact_id,
            self.run_id,
            self.scope_id,
            ArtifactKind.DCP,
            immutable_uri,
            sha256=digest,
            size_bytes=size_bytes,
            created_at=self.now(),
            metadata={
                "contract": ARTIFACT_CONTRACT,
                "transaction_version": ARTIFACT_TRANSACTION_VERSION,
                "attempt_id": self.attempt_id,
                "validated": True,
                "validation_status": validation.status.value,
                "validator_version": validation.validator_version,
                "immutable": True,
            },
        )
        with self.store.transaction():
            self.store.add_artifact(artifact)
            self._append_phase_in_transaction(
                ArtifactPhase.IMMUTABLE_REGISTERED,
                artifact_id=artifact_id,
                immutable_uri=immutable_uri,
                sha256=digest,
                size_bytes=size_bytes,
            )
        self._phase = ArtifactPhase.IMMUTABLE_REGISTERED
        self._inject(
            fail_at, ArtifactFailurePoint.AFTER_REGISTRATION_BEFORE_CANONICAL_FACTS
        )

        finished_at = self.now()
        canonical_metrics = {
            **dict(metrics or {}),
            "artifact_sha256": digest,
            "artifact_size_bytes": size_bytes,
            "artifact_transaction_version": ARTIFACT_TRANSACTION_VERSION,
        }
        with self.store.transaction():
            self.store.add_outcome(
                ExecutionOutcome(
                    self._outcome_id(),
                    self.attempt_id,
                    OutcomeStatus.SUCCEEDED,
                    self.started_at,
                    finished_at,
                    output_artifact_id=artifact_id,
                    metrics=canonical_metrics,
                )
            )
            self.store.add_state(
                DesignState(
                    self.state_id,
                    self.run_id,
                    self.scope_id,
                    artifact_id,
                    self.parent_state_id,
                    self.attempt_id,
                    created_at=finished_at,
                    metadata={
                        "artifact_sha256": digest,
                        "artifact_transaction_version": ARTIFACT_TRANSACTION_VERSION,
                        "metrics": canonical_metrics,
                    },
                )
            )
            self.store.add_validation(
                ValidationRecord(
                    self._validation_id(),
                    self.run_id,
                    self.scope_id,
                    self.state_id,
                    ValidationLevel.ATTEMPT,
                    validation.status,
                    self.attempt_id,
                    checked_at=finished_at,
                    details={
                        **dict(validation.details),
                        "artifact_sha256": digest,
                        "artifact_size_bytes": size_bytes,
                        "validator_version": validation.validator_version,
                    },
                )
            )
            self.store.add_eligibility(
                StateEligibility(
                    self._eligibility_id(),
                    self.run_id,
                    self.state_id,
                    True,
                    bool(selection_eligible),
                    (
                        selection_reason
                        if selection_eligible
                        else "validated successor is not selection eligible"
                    ),
                    recorded_at=finished_at,
                )
            )
            self._append_phase_in_transaction(
                ArtifactPhase.CANONICAL_FACTS_RECORDED,
                artifact_id=artifact_id,
                state_id=self.state_id,
                validation_status=validation.status.value,
                selection_eligible=bool(selection_eligible),
            )
        self._phase = ArtifactPhase.CANONICAL_FACTS_RECORDED
        self._inject(
            fail_at, ArtifactFailurePoint.AFTER_CANONICAL_OUTCOME_BEFORE_SELECTION
        )

        selected = False
        if selection_eligible:
            # Intent is a durable fact separate from the selection.  A crash at
            # this point leaves a complete valid state but the baseline remains
            # selected, which is safe and unambiguous.
            self._append_phase(
                ArtifactPhase.SELECTION_INTENT_RECORDED,
                artifact_id=artifact_id,
                state_id=self.state_id,
                reason=selection_reason,
            )
            self._phase = ArtifactPhase.SELECTION_INTENT_RECORDED
            self._inject(
                fail_at,
                ArtifactFailurePoint.AFTER_SELECTION_INTENT_BEFORE_TERMINAL,
            )
            with self.store.transaction():
                self.store.add_selection(
                    ResultSelection(
                        self._selection_id(),
                        self.run_id,
                        self.scope_id,
                        self.state_id,
                        artifact_id,
                        SelectionKind.BEST_VALID,
                        selection_reason,
                        selected_at=self.now(),
                    )
                )
                self._append_phase_in_transaction(
                    ArtifactPhase.SELECTED,
                    artifact_id=artifact_id,
                    state_id=self.state_id,
                )
            selected = True
            self._phase = ArtifactPhase.SELECTED

        return ArtifactTransactionResult(
            endpoint="VALID_ARTIFACT",
            phase=self._phase,
            valid=True,
            selected=selected,
            artifact_id=artifact_id,
            state_id=self.state_id,
            immutable_uri=immutable_uri,
            temporary_uri=None,
            sha256=digest,
            size_bytes=size_bytes,
            validation_status=validation.status.value,
        )

    def _validate_canonical_links(self) -> None:
        run = self.store.get_run()
        if run is None or run.run_id != self.run_id:
            raise ArtifactTransactionError(
                "artifact transaction run_id mismatch",
                endpoint="EVIDENCE_INTEGRITY_FAILURE",
            )
        scopes = {scope.scope_id: scope for scope in self.store.scopes()}
        if self.scope_id not in scopes or scopes[self.scope_id].run_id != self.run_id:
            raise ArtifactTransactionError(
                "artifact transaction scope mismatch",
                endpoint="EVIDENCE_INTEGRITY_FAILURE",
            )
        attempts = {attempt.attempt_id: attempt for attempt in self.store.attempts()}
        attempt = attempts.get(self.attempt_id)
        if attempt is None:
            raise ArtifactTransactionError(
                "artifact transaction attempt does not exist",
                endpoint="EVIDENCE_INTEGRITY_FAILURE",
            )
        if (
            attempt.run_id != self.run_id
            or attempt.scope_id != self.scope_id
            or attempt.parent_state_id != self.parent_state_id
        ):
            raise ArtifactTransactionError(
                "artifact transaction attempt binding mismatch",
                endpoint="EVIDENCE_INTEGRITY_FAILURE",
            )
        if self.state_id in {state.state_id for state in self.store.states()}:
            raise ArtifactTransactionError("state_id already exists; resume is forbidden")
        if any(outcome.attempt_id == self.attempt_id for outcome in self.store.outcomes()):
            raise ArtifactTransactionError("attempt already has an outcome; resume is forbidden")

    def _temporary_path(self) -> Path:
        label = _identity_token(self.attempt_id)
        return self._prepare_target(f".staging/{label}.dcp.part")

    def _quarantine(self, temporary: Path, digest: str) -> Path:
        target = self._prepare_target(
            ".quarantine/"
            f"{_identity_token(self.attempt_id)}-{digest}.invalid"
        )
        if target.exists() or target.is_symlink():
            raise ArtifactTransactionError("quarantine target already exists")
        temporary = self._reconfine(temporary)
        target = self._reconfine(target)
        os.replace(temporary, target)
        target.chmod(stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)
        _fsync_file(target)
        _fsync_directory(target.parent)
        # Do not append INVALID_QUARANTINED here.  The rename is durable, but
        # the failed outcome, validation and terminal lifecycle event must be
        # one SQLite transaction in ``_record_invalid_outcome``.  A crash at
        # this filesystem/SQLite boundary therefore leaves a read-only orphan
        # and the valid prefix ending in VALIDATED, never a forged terminal or
        # two INVALID_QUARANTINED events.
        return target

    def _publish_immutable(
        self, temporary: Path, digest: str, size_bytes: int
    ) -> Path:
        target = self._prepare_target(f"artifacts/sha256/{digest}.dcp")
        if target.is_symlink():
            raise ArtifactTransactionError("immutable artifact target is a symlink")
        if target.exists():
            existing_digest, existing_size = _hash_and_size(target)
            if (existing_digest, existing_size) != (digest, size_bytes):
                raise ArtifactTransactionError("content-address collision")
            temporary.unlink()
        else:
            temporary = self._reconfine(temporary)
            target = self._reconfine(target)
            os.replace(temporary, target)
        target.chmod(stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)
        _fsync_file(target)
        _fsync_directory(target.parent)
        return target

    def _record_invalid_outcome(
        self,
        *,
        validation: ArtifactValidation,
        digest: str,
        size_bytes: int,
        metrics: Mapping[str, Any] | None,
        quarantine_uri: str,
        endpoint: str,
    ) -> None:
        finished_at = self.now()
        with self.store.transaction():
            self.store.add_outcome(
                ExecutionOutcome(
                    self._outcome_id(),
                    self.attempt_id,
                    OutcomeStatus.FAILED,
                    self.started_at,
                    finished_at,
                    output_artifact_id=None,
                    error_type=endpoint,
                    error_message=(
                        "artifact changed during validation"
                        if endpoint == "EVIDENCE_INTEGRITY_FAILURE"
                        else "artifact validation rejected the temporary child"
                    ),
                    metrics={
                        **dict(metrics or {}),
                        "candidate_sha256": digest,
                        "candidate_size_bytes": size_bytes,
                        "quarantine_uri": quarantine_uri,
                    },
                )
            )
            self.store.add_validation(
                ValidationRecord(
                    self._validation_id(),
                    self.run_id,
                    self.scope_id,
                    self.attempt_id,
                    ValidationLevel.ATTEMPT,
                    validation.status,
                    self.attempt_id,
                    checked_at=finished_at,
                    details={
                        **dict(validation.details),
                        "candidate_sha256": digest,
                        "candidate_size_bytes": size_bytes,
                        "quarantine_uri": quarantine_uri,
                        "validator_version": validation.validator_version,
                    },
                )
            )
            self._append_phase_in_transaction(
                ArtifactPhase.INVALID_QUARANTINED,
                sha256=digest,
                size_bytes=size_bytes,
                quarantine_uri=quarantine_uri,
                validation_status=validation.status.value,
            )

    def _append_phase(self, phase: ArtifactPhase, **detail: Any) -> None:
        with self.store.transaction():
            self._append_phase_in_transaction(phase, **detail)

    def _append_phase_in_transaction(
        self, phase: ArtifactPhase, **detail: Any
    ) -> None:
        existing = self.store.attempt_events(attempt_id=self.attempt_id)
        sequence = len(existing)
        if [item.sequence for item in existing] != list(range(sequence)):
            raise ArtifactTransactionError(
                "attempt lifecycle sequence is not contiguous",
                endpoint="EVIDENCE_INTEGRITY_FAILURE",
                phase=self._phase,
            )
        event_kind = AttemptEventKind.STARTED
        if phase is ArtifactPhase.SELECTED:
            event_kind = AttemptEventKind.COMPLETED
        elif phase is ArtifactPhase.INVALID_QUARANTINED and "validation_status" in detail:
            event_kind = AttemptEventKind.FAILED
        elif (
            phase is ArtifactPhase.CANONICAL_FACTS_RECORDED
            and detail.get("selection_eligible") is False
        ):
            event_kind = AttemptEventKind.COMPLETED
        self.store.add_attempt_event(
            AttemptLifecycleEvent(
                f"{self.attempt_id}:artifact:{sequence:03d}",
                self.attempt_id,
                sequence,
                event_kind,
                occurred_at=self.now(),
                detail={
                    "schema_version": ARTIFACT_LIFECYCLE_EVENT_SCHEMA,
                    "transaction_version": ARTIFACT_TRANSACTION_VERSION,
                    "phase": phase.value,
                    "resume_policy": RESUME_POLICY,
                    **detail,
                },
            )
        )

    def _inject(
        self,
        configured: ArtifactFailurePoint | None,
        point: ArtifactFailurePoint,
    ) -> None:
        if configured is point:
            raise InjectedArtifactCrash(point, phase=self._phase)

    def _relative_uri(self, path: Path) -> str:
        resolved = path.resolve()
        try:
            return resolved.relative_to(self.run_dir).as_posix()
        except ValueError as exc:
            raise ArtifactTransactionError("artifact path escaped run workspace") from exc

    def _prepare_target(self, uri: str) -> Path:
        """Create and re-check one confined parent before any filesystem write."""

        target = _confined(self.run_dir, uri)
        target.parent.mkdir(parents=True, exist_ok=True)
        return _confined(self.run_dir, uri)

    def _reconfine(self, path: Path) -> Path:
        """Re-resolve a lexical run-relative path immediately before rename."""

        try:
            uri = path.absolute().relative_to(self.run_dir).as_posix()
        except ValueError as exc:
            raise ArtifactTransactionError("artifact path escaped run workspace") from exc
        return _confined(self.run_dir, uri)

    def _artifact_id(self, digest: str) -> str:
        return f"artifact:{_identity_token(self.attempt_id)}:{digest}"

    def _outcome_id(self) -> str:
        return f"outcome:{_identity_token(self.attempt_id)}"

    def _validation_id(self) -> str:
        return f"validation:{_identity_token(self.attempt_id)}"

    def _eligibility_id(self) -> str:
        return f"eligibility:{_identity_token(self.attempt_id)}"

    def _selection_id(self) -> str:
        return f"selection:{_identity_token(self.attempt_id)}"


def verify_artifact_lifecycle(
    store: SQLiteRunStore,
    *,
    run_dir: str | Path,
) -> ArtifactLifecycleVerification:
    """Verify only facts owned by the additive artifact contract.

    Schema v2 intentionally permits legacy attempts and more than one
    validation/state row per attempt.  Contract ownership is therefore derived
    from the versioned lifecycle marker or a contract artifact, and every
    owned relation is then checked for exact cardinality and binding.  Legacy
    attempts remain the responsibility of their original verifier.
    """

    root = Path(run_dir).resolve()
    errors: list[str] = list(store.integrity_check())
    checked: list[str] = []
    attempts = {item.attempt_id: item for item in store.attempts()}
    artifacts = {item.artifact_id: item for item in store.artifacts()}
    states = {item.state_id: item for item in store.states()}

    outcomes_by_attempt: dict[str, list[ExecutionOutcome]] = defaultdict(list)
    for outcome in store.outcomes():
        outcomes_by_attempt[outcome.attempt_id].append(outcome)
    states_by_attempt: dict[str, list[DesignState]] = defaultdict(list)
    for state in states.values():
        if state.produced_by_attempt_id is not None:
            states_by_attempt[state.produced_by_attempt_id].append(state)
    validations_by_attempt: dict[str, list[ValidationRecord]] = defaultdict(list)
    for validation in store.validations():
        if (
            validation.level is ValidationLevel.ATTEMPT
            and validation.attempt_id is not None
        ):
            validations_by_attempt[validation.attempt_id].append(validation)
    eligibility_by_state: dict[str, list[StateEligibility]] = defaultdict(list)
    for fact in store.eligibility():
        eligibility_by_state[fact.state_id].append(fact)
    selections_by_state: dict[str, list[ResultSelection]] = defaultdict(list)
    selections = store.selections()
    for selection in selections:
        selections_by_state[selection.state_id].append(selection)

    terminal_attempt_kinds = {
        AttemptEventKind.COMPLETED,
        AttemptEventKind.FAILED,
        AttemptEventKind.CANCELLED,
    }
    events_by_attempt = {
        attempt_id: store.attempt_events(attempt_id=attempt_id)
        for attempt_id in attempts
    }
    contract_events_by_attempt: dict[str, list[AttemptLifecycleEvent]] = defaultdict(
        list
    )
    owned_attempt_ids: set[str] = set()
    for attempt_id, events in events_by_attempt.items():
        for event in events:
            detail = event.detail.to_dict()
            if detail.get("transaction_version") != ARTIFACT_TRANSACTION_VERSION:
                continue
            contract_events_by_attempt[attempt_id].append(event)
            owned_attempt_ids.add(attempt_id)
            if detail.get("schema_version") != ARTIFACT_LIFECYCLE_EVENT_SCHEMA:
                errors.append(
                    f"attempt {attempt_id} has wrong artifact lifecycle schema"
                )
            if detail.get("resume_policy") != RESUME_POLICY:
                errors.append(f"attempt {attempt_id} has wrong artifact resume policy")
            try:
                ArtifactPhase(detail.get("phase"))
            except (TypeError, ValueError):
                errors.append(f"attempt {attempt_id} has unknown artifact phase")

    contract_artifacts = {
        artifact_id: artifact
        for artifact_id, artifact in artifacts.items()
        if artifact.metadata.to_dict().get("contract") == ARTIFACT_CONTRACT
    }
    contract_artifacts_by_attempt: dict[str, list[ArtifactReference]] = defaultdict(
        list
    )
    for artifact_id, artifact in contract_artifacts.items():
        checked.append(artifact_id)
        metadata = artifact.metadata.to_dict()
        attempt_id = metadata.get("attempt_id")
        if not isinstance(attempt_id, str) or not attempt_id:
            errors.append(f"artifact {artifact_id} has no owning attempt_id")
        else:
            owned_attempt_ids.add(attempt_id)
            contract_artifacts_by_attempt[attempt_id].append(artifact)
            if attempt_id not in attempts:
                errors.append(f"artifact {artifact_id} names an unknown attempt")
        if metadata.get("transaction_version") != ARTIFACT_TRANSACTION_VERSION:
            errors.append(f"artifact {artifact_id} has wrong transaction version")
        if metadata.get("validated") is not True or metadata.get("immutable") is not True:
            errors.append(f"artifact {artifact_id} is not validated immutable content")
        if metadata.get("validation_status") not in {
            status.value for status in _ACCEPTED_VALIDATION_STATUSES
        }:
            errors.append(f"artifact {artifact_id} has non-accepted validation metadata")
        if not isinstance(metadata.get("validator_version"), str) or not metadata.get(
            "validator_version"
        ):
            errors.append(f"artifact {artifact_id} has no validator version")
        if artifact.kind is not ArtifactKind.DCP:
            errors.append(f"artifact {artifact_id} is not a DCP")
        if artifact.sha256 is None or not _SHA256_RE.fullmatch(artifact.sha256):
            errors.append(f"artifact {artifact_id} has no canonical sha256")
            continue
        if isinstance(attempt_id, str) and attempt_id:
            expected_id = f"artifact:{_identity_token(attempt_id)}:{artifact.sha256}"
            if artifact.artifact_id != expected_id:
                errors.append(f"artifact {artifact_id} has a non-canonical artifact_id")
        expected_uri = f"artifacts/sha256/{artifact.sha256}.dcp"
        if artifact.uri != expected_uri:
            errors.append(f"artifact {artifact_id} has non-content-addressed uri")
        try:
            path = _confined(root, artifact.uri)
        except ArtifactTransactionError as exc:
            errors.append(f"artifact {artifact_id}: {exc.reason}")
            continue
        if path.is_symlink() or not path.is_file():
            errors.append(f"artifact {artifact_id} is missing or a symlink")
            continue
        mode = stat.S_IMODE(path.stat().st_mode)
        if mode & 0o222:
            errors.append(f"artifact {artifact_id} is writable")
        digest, size = _hash_and_size(path)
        if digest != artifact.sha256 or size != artifact.size_bytes:
            errors.append(f"artifact {artifact_id} content hash/size mismatch")

    # A state/outcome/selection referencing a contract artifact is also owned,
    # even if malformed metadata tried to hide its attempt identifier.
    for state in states.values():
        if state.artifact_id in contract_artifacts:
            if state.produced_by_attempt_id is None:
                errors.append(f"contract state {state.state_id} has no producing attempt")
            else:
                owned_attempt_ids.add(state.produced_by_attempt_id)
    for attempt_id, rows in outcomes_by_attempt.items():
        if any(row.output_artifact_id in contract_artifacts for row in rows):
            owned_attempt_ids.add(attempt_id)
    for selection in selections:
        if selection.artifact_id not in contract_artifacts:
            continue
        state = states.get(selection.state_id)
        if state is None or state.artifact_id != selection.artifact_id:
            errors.append(f"selection {selection.selection_id} has broken state/artifact")
        elif state.produced_by_attempt_id is None:
            errors.append(f"selection {selection.selection_id} has no producing attempt")
        else:
            owned_attempt_ids.add(state.produced_by_attempt_id)

    incomplete: list[str] = []
    for attempt_id in sorted(owned_attempt_ids):
        attempt = attempts.get(attempt_id)
        if attempt is None:
            continue
        artifacts_for_attempt = contract_artifacts_by_attempt.get(attempt_id, [])
        outcome_rows = outcomes_by_attempt.get(attempt_id, [])
        state_rows = states_by_attempt.get(attempt_id, [])
        validation_rows = validations_by_attempt.get(attempt_id, [])
        contract_events = contract_events_by_attempt.get(attempt_id, [])
        lifecycle_errors, lifecycle_complete = _verify_artifact_phase_path(
            attempt_id,
            events_by_attempt.get(attempt_id, ()),
            contract_events,
        )
        errors.extend(lifecycle_errors)
        terminal_events = [
            event
            for event in events_by_attempt.get(attempt_id, ())
            if event.kind in terminal_attempt_kinds
        ]

        if not contract_events:
            errors.append(f"attempt {attempt_id} has no artifact lifecycle evidence")
        if len(artifacts_for_attempt) > 1:
            errors.append(f"attempt {attempt_id} has multiple contract artifacts")
        if len(outcome_rows) > 1:
            errors.append(f"attempt {attempt_id} has multiple outcomes")
        if len(state_rows) > 1:
            errors.append(f"attempt {attempt_id} has multiple produced states")
        if len(validation_rows) > 1:
            errors.append(f"attempt {attempt_id} has multiple attempt validations")
        if len(terminal_events) > 1:
            errors.append(f"attempt {attempt_id} has multiple terminal events")
        if (
            len(outcome_rows) != 1
            or len(terminal_events) != 1
            or not lifecycle_complete
        ):
            incomplete.append(attempt_id)

        for artifact in artifacts_for_attempt:
            if artifact.run_id != attempt.run_id or artifact.scope_id != attempt.scope_id:
                errors.append(f"attempt {attempt_id} artifact has wrong run/scope")

        outcome = outcome_rows[0] if len(outcome_rows) == 1 else None
        state = state_rows[0] if len(state_rows) == 1 else None
        validation = validation_rows[0] if len(validation_rows) == 1 else None
        terminal_event = terminal_events[0] if len(terminal_events) == 1 else None

        if outcome is None:
            # Registration may be durable before a crash, but canonical
            # state/validation/eligibility are one later SQLite transaction.
            if state_rows or validation_rows:
                errors.append(
                    f"attempt {attempt_id} has canonical facts without an outcome"
                )
            if terminal_events:
                errors.append(f"attempt {attempt_id} is terminal without an outcome")
            continue

        if outcome.status is OutcomeStatus.SUCCEEDED:
            if len(artifacts_for_attempt) != 1:
                errors.append(
                    f"attempt {attempt_id} succeeded without exactly one contract artifact"
                )
            artifact = artifacts_for_attempt[0] if len(artifacts_for_attempt) == 1 else None
            if artifact is None or outcome.output_artifact_id != artifact.artifact_id:
                errors.append(f"attempt {attempt_id} outcome has wrong artifact")
            elif (
                outcome.metrics.to_dict().get("artifact_sha256") != artifact.sha256
                or outcome.metrics.to_dict().get("artifact_size_bytes")
                != artifact.size_bytes
                or outcome.metrics.to_dict().get("artifact_transaction_version")
                != ARTIFACT_TRANSACTION_VERSION
            ):
                errors.append(f"attempt {attempt_id} outcome artifact metrics mismatch")
            if state is None:
                errors.append(f"attempt {attempt_id} succeeded without exactly one state")
                continue
            if (
                state.run_id != attempt.run_id
                or state.scope_id != attempt.scope_id
                or state.parent_state_id != attempt.parent_state_id
                or state.artifact_id != outcome.output_artifact_id
            ):
                errors.append(f"attempt {attempt_id} state binding mismatch")
            if validation is None:
                errors.append(
                    f"attempt {attempt_id} succeeded without exactly one validation"
                )
                continue
            if (
                validation.run_id != attempt.run_id
                or validation.scope_id != attempt.scope_id
                or validation.subject_id != state.state_id
            ):
                errors.append(f"attempt {attempt_id} validation subject/run/scope mismatch")
            if validation.status not in _ACCEPTED_VALIDATION_STATUSES:
                errors.append(f"attempt {attempt_id} succeeded without accepted validation")
            if artifact is not None:
                artifact_metadata = artifact.metadata.to_dict()
                validation_details = validation.details.to_dict()
                if (
                    artifact_metadata.get("validation_status")
                    != validation.status.value
                    or validation_details.get("artifact_sha256") != artifact.sha256
                    or validation_details.get("artifact_size_bytes")
                    != artifact.size_bytes
                    or validation_details.get("validator_version")
                    != artifact_metadata.get("validator_version")
                ):
                    errors.append(
                        f"attempt {attempt_id} artifact/validation evidence mismatch"
                    )
                state_metadata = state.metadata.to_dict()
                if (
                    state_metadata.get("artifact_sha256") != artifact.sha256
                    or state_metadata.get("artifact_transaction_version")
                    != ARTIFACT_TRANSACTION_VERSION
                ):
                    errors.append(f"attempt {attempt_id} state artifact metadata mismatch")
            facts = eligibility_by_state.get(state.state_id, [])
            if len(facts) != 1:
                errors.append(
                    f"state {state.state_id} lacks exactly one eligibility fact"
                )
                continue
            fact = facts[0]
            if fact.run_id != attempt.run_id or not fact.parent_eligible:
                errors.append(f"state {state.state_id} eligibility binding mismatch")
            state_selections = selections_by_state.get(state.state_id, [])
            if len(state_selections) > 1:
                errors.append(f"state {state.state_id} has multiple selections")
            if not fact.selection_eligible and state_selections:
                errors.append(f"state {state.state_id} was selected while ineligible")
            if terminal_event is not None:
                if terminal_event.kind is not AttemptEventKind.COMPLETED:
                    errors.append(f"attempt {attempt_id} success has wrong terminal kind")
                terminal_phase = terminal_event.detail.to_dict().get("phase")
                expected_phase = (
                    ArtifactPhase.SELECTED.value
                    if fact.selection_eligible
                    else ArtifactPhase.CANONICAL_FACTS_RECORDED.value
                )
                if terminal_phase != expected_phase:
                    errors.append(f"attempt {attempt_id} success has wrong terminal phase")
                if fact.selection_eligible and len(state_selections) != 1:
                    errors.append(
                        f"state {state.state_id} completed without required selection"
                    )
            if state_selections and terminal_event is None:
                errors.append(f"state {state.state_id} selected without terminal event")
            try:
                managed = ManagedStateFacts(state, validation, fact)
                for selection in state_selections:
                    if (
                        selection.run_id != attempt.run_id
                        or selection.scope_id != attempt.scope_id
                        or selection.kind is not SelectionKind.BEST_VALID
                    ):
                        errors.append(
                            f"selection {selection.selection_id} run/scope/kind mismatch"
                        )
                    validate_selection(selection, managed)
            except DomainInvariantError as exc:
                errors.append(f"attempt {attempt_id} managed fact mismatch: {exc}")
        elif outcome.status is OutcomeStatus.FAILED:
            if outcome.output_artifact_id is not None:
                errors.append(f"attempt {attempt_id} failed with an output artifact")
            if artifacts_for_attempt or state_rows:
                errors.append(f"attempt {attempt_id} failed after publishing canonical output")
            if validation is None:
                errors.append(f"attempt {attempt_id} failed without exactly one validation")
            elif (
                validation.run_id != attempt.run_id
                or validation.scope_id != attempt.scope_id
                or validation.subject_id != attempt_id
                or validation.status not in {ValidationStatus.INVALID, ValidationStatus.FAILED}
            ):
                errors.append(f"attempt {attempt_id} failed validation binding mismatch")
            if outcome.error_type not in {
                "INVALID_ARTIFACT",
                "EVIDENCE_INTEGRITY_FAILURE",
            }:
                errors.append(f"attempt {attempt_id} failure has wrong endpoint")
            failure_metrics = outcome.metrics.to_dict()
            quarantine_uri = failure_metrics.get("quarantine_uri")
            candidate_sha256 = failure_metrics.get("candidate_sha256")
            candidate_size = failure_metrics.get("candidate_size_bytes")
            if (
                not isinstance(quarantine_uri, str)
                or not quarantine_uri.startswith(".quarantine/")
                or not isinstance(candidate_sha256, str)
                or not _SHA256_RE.fullmatch(candidate_sha256)
                or not isinstance(candidate_size, int)
                or isinstance(candidate_size, bool)
                or candidate_size < 0
            ):
                errors.append(f"attempt {attempt_id} has incomplete quarantine evidence")
            else:
                try:
                    quarantine = _confined(root, quarantine_uri)
                except ArtifactTransactionError as exc:
                    errors.append(f"attempt {attempt_id} quarantine: {exc.reason}")
                else:
                    if quarantine.is_symlink() or not quarantine.is_file():
                        errors.append(f"attempt {attempt_id} quarantine is missing or a symlink")
                    else:
                        mode = stat.S_IMODE(quarantine.stat().st_mode)
                        if mode & 0o222:
                            errors.append(f"attempt {attempt_id} quarantine is writable")
                        digest, size = _hash_and_size(quarantine)
                        if (digest, size) != (candidate_sha256, candidate_size):
                            errors.append(
                                f"attempt {attempt_id} quarantine hash/size mismatch"
                            )
            if terminal_event is not None:
                if terminal_event.kind is not AttemptEventKind.FAILED:
                    errors.append(f"attempt {attempt_id} failure has wrong terminal kind")
                if (
                    terminal_event.detail.to_dict().get("phase")
                    != ArtifactPhase.INVALID_QUARANTINED.value
                ):
                    errors.append(f"attempt {attempt_id} failure has wrong terminal phase")
        else:
            errors.append(
                f"attempt {attempt_id} has unsupported artifact outcome {outcome.status.value}"
            )

    terminal_run = any(
        event.status.value in {"FINALIZED", "FAILED", "ABORTED"}
        for event in store.run_events()
    )
    if terminal_run and incomplete:
        errors.append(
            "terminal run has contract attempts without outcomes/terminal events: "
            + ",".join(incomplete)
        )
    return ArtifactLifecycleVerification(
        tuple(errors), tuple(sorted(set(incomplete))), tuple(sorted(checked))
    )


def _verify_artifact_phase_path(
    attempt_id: str,
    all_events: tuple[AttemptLifecycleEvent, ...] | list[AttemptLifecycleEvent],
    contract_events: tuple[AttemptLifecycleEvent, ...]
    | list[AttemptLifecycleEvent],
) -> tuple[tuple[str, ...], bool]:
    """Validate one contract lifecycle as a complete path or safe prefix.

    Contract events are an append-only suffix of the attempt lifecycle.  A
    crash may leave any *prefix* of one of the three documented terminal paths,
    but skipping, repeating, reordering or continuing after a terminal phase is
    evidence corruption rather than an interrupted transaction.
    """

    if not contract_events:
        return (), False

    errors: list[str] = []
    ordered_events = sorted(all_events, key=lambda item: item.sequence)
    ordered_contract = sorted(contract_events, key=lambda item: item.sequence)
    all_sequences = [event.sequence for event in ordered_events]
    if all_sequences != list(range(len(ordered_events))):
        errors.append(f"attempt {attempt_id} event sequence is not contiguous")

    contract_sequences = [event.sequence for event in ordered_contract]
    first_contract_sequence = contract_sequences[0]
    if contract_sequences != list(
        range(first_contract_sequence, first_contract_sequence + len(ordered_contract))
    ):
        errors.append(
            f"attempt {attempt_id} artifact lifecycle sequence is not contiguous"
        )
    contract_event_ids = {event.event_id for event in ordered_contract}
    if any(
        event.sequence >= first_contract_sequence
        and event.event_id not in contract_event_ids
        for event in ordered_events
    ):
        errors.append(
            f"attempt {attempt_id} has non-contract events interleaved with "
            "artifact lifecycle"
        )

    phases: list[ArtifactPhase] = []
    phase_events: dict[ArtifactPhase, AttemptLifecycleEvent] = {}
    unknown_phase = False
    for event in ordered_contract:
        detail = event.detail.to_dict()
        try:
            phase = ArtifactPhase(detail.get("phase"))
        except (TypeError, ValueError):
            unknown_phase = True
            continue
        phases.append(phase)
        if phase in phase_events:
            errors.append(
                f"attempt {attempt_id} repeats artifact phase {phase.value}"
            )
        else:
            phase_events[phase] = event

    if unknown_phase:
        return tuple(errors), False

    actual_path = tuple(phases)
    matching_paths = [
        path
        for path in _ARTIFACT_PHASE_PATHS
        if len(actual_path) <= len(path) and path[: len(actual_path)] == actual_path
    ]
    if not matching_paths:
        rendered = " -> ".join(phase.value for phase in actual_path)
        errors.append(
            f"attempt {attempt_id} is not a valid artifact phase prefix: {rendered}"
        )

    terminal_kinds = {
        AttemptEventKind.COMPLETED,
        AttemptEventKind.FAILED,
        AttemptEventKind.CANCELLED,
    }
    canonical_eligible: bool | None = None
    for index, (phase, event) in enumerate(zip(phases, ordered_contract, strict=True)):
        detail = event.detail.to_dict()
        expected_kind = AttemptEventKind.STARTED
        if phase is ArtifactPhase.CANONICAL_FACTS_RECORDED:
            candidate = detail.get("selection_eligible")
            if not isinstance(candidate, bool):
                errors.append(
                    f"attempt {attempt_id} canonical phase has no boolean "
                    "selection_eligible"
                )
            else:
                canonical_eligible = candidate
                if not candidate:
                    expected_kind = AttemptEventKind.COMPLETED
        elif phase is ArtifactPhase.SELECTED:
            expected_kind = AttemptEventKind.COMPLETED
        elif phase is ArtifactPhase.INVALID_QUARANTINED:
            expected_kind = AttemptEventKind.FAILED

        if event.kind is not expected_kind:
            errors.append(
                f"attempt {attempt_id} phase {phase.value} has event kind "
                f"{event.kind.value}, expected {expected_kind.value}"
            )
        if event.kind in terminal_kinds and index != len(ordered_contract) - 1:
            errors.append(
                f"attempt {attempt_id} artifact terminal phase is followed by events"
            )

    validated_event = phase_events.get(ArtifactPhase.VALIDATED)
    validated_status = (
        validated_event.detail.to_dict().get("validation_status")
        if validated_event is not None
        else None
    )
    if validated_event is not None and validated_status not in {
        status.value
        for status in (
            *_ACCEPTED_VALIDATION_STATUSES,
            ValidationStatus.INVALID,
            ValidationStatus.FAILED,
        )
    }:
        errors.append(f"attempt {attempt_id} validated phase has invalid status")
    if ArtifactPhase.IMMUTABLE_REGISTERED in phase_events and validated_status not in {
        status.value for status in _ACCEPTED_VALIDATION_STATUSES
    }:
        errors.append(
            f"attempt {attempt_id} registered an artifact after rejected validation"
        )
    invalid_event = phase_events.get(ArtifactPhase.INVALID_QUARANTINED)
    if invalid_event is not None:
        invalid_status = invalid_event.detail.to_dict().get("validation_status")
        if invalid_status not in {
            ValidationStatus.INVALID.value,
            ValidationStatus.FAILED.value,
        }:
            errors.append(
                f"attempt {attempt_id} invalid terminal has wrong validation status"
            )
        if validated_status != invalid_status:
            errors.append(
                f"attempt {attempt_id} validated and quarantine statuses disagree"
            )

    if actual_path == _INVALID_ARTIFACT_PHASE_PATH:
        final_kind_ok = ordered_contract[-1].kind is AttemptEventKind.FAILED
        return tuple(errors), final_kind_ok and not errors
    if actual_path == _SELECTED_ARTIFACT_PHASE_PATH:
        final_kind_ok = ordered_contract[-1].kind is AttemptEventKind.COMPLETED
        return (
            tuple(errors),
            canonical_eligible is True and final_kind_ok and not errors,
        )
    if actual_path == _NONSELECTED_ARTIFACT_PHASE_PATH:
        final_kind_ok = ordered_contract[-1].kind is AttemptEventKind.COMPLETED
        complete = canonical_eligible is False and final_kind_ok and not errors
        return tuple(errors), complete
    return tuple(errors), False


def run_evidence_finalizer(
    finalizer: Callable[[], Any],
    *,
    fail_at: ArtifactFailurePoint | None = None,
) -> Any:
    """Expose the final crash point without changing the evidence writer API."""

    if fail_at is ArtifactFailurePoint.DURING_EVIDENCE_FINALIZATION:
        raise InjectedArtifactCrash(
            ArtifactFailurePoint.DURING_EVIDENCE_FINALIZATION,
            phase=ArtifactPhase.SELECTION_INTENT_RECORDED,
        )
    return finalizer()


def _required(name: str, value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value


def _identity_token(value: str) -> str:
    """Return a readable token whose identity never relies on sanitization."""

    cleaned = _SAFE_ID_RE.sub("_", value).strip("_") or "attempt"
    readable = cleaned[:48]
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()
    return f"{readable}-{digest}"


def _hash_and_size(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def _fsync_file(path: Path) -> None:
    with path.open("rb") as handle:
        os.fsync(handle.fileno())


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    descriptor = os.open(path, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _confined(root: Path, uri: str) -> Path:
    if not isinstance(uri, str) or not uri or "\\" in uri or "\x00" in uri:
        raise ArtifactTransactionError("artifact uri is not a safe relative URI")
    pure = Path(uri)
    if pure.is_absolute() or any(part in {"", ".", ".."} for part in pure.parts):
        raise ArtifactTransactionError("artifact uri is not a safe relative URI")
    lexical = (root / pure).absolute()
    candidate = lexical.resolve(strict=False)
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise ArtifactTransactionError("artifact uri escapes the run directory") from exc
    current = root
    for part in pure.parts[:-1]:
        current = current / part
        if current.is_symlink():
            raise ArtifactTransactionError(
                "artifact uri has a symlinked parent in the run directory"
            )
    # Return the lexical path so callers can still detect a symlink at the
    # final component; returning ``resolve`` would erase that evidence.
    return lexical
