"""Write-once sidecar evidence linked to the canonical schema-v2 run store.

The common DoF experiment path needs richer policy and internal-call evidence
than the existing execution fact schema represents.  This module deliberately
keeps that evidence in immutable, hash-addressed sidecars while using the
existing ``artifacts`` and ``run_events`` tables as the link to ``run.db``.

It does not alter the store schema or the legacy run finalizer.  A caller writes
all sidecars, closes the event trace, and then calls
``register_evidence_and_finalize`` exactly once.  Registration and the terminal
run event are committed in one SQLite transaction, with the registration event
immediately before the terminal event.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
from typing import Any

from optimizer_pipeline.core.domain import (
    ArtifactKind,
    ArtifactReference,
    RunLifecycleEvent,
    RunStatus,
)
from optimizer_pipeline.core.store import CURRENT_SCHEMA_VERSION, SQLiteRunStore

from .provenance import (
    REPOSITORY_PROVENANCE_SCHEMA_VERSION,
    repository_constraint_mismatches,
    validate_repository_observation_payload,
)


EVIDENCE_ARTIFACT_CONTRACT = "dof.immutable-sidecar.v1"
EVIDENCE_REGISTRATION_SCHEMA = "dof.evidence-registration.v1"
EVIDENCE_REGISTRATION_KIND = "dof_evidence_registration"
TERMINAL_DETAIL_KIND = "dof_run_terminal"
JSON_CONTENT_TYPE = "application/json"
JSONL_CONTENT_TYPE = "application/x-ndjson"

DEFAULT_REQUIRED_EVIDENCE_ROLES = (
    "experiment_manifest",
    "capability_manifest",
    "catalogue_manifest",
    "mask_manifest",
    "prompt_model_manifest",
    "tool_environment_manifest",
    "decision_event_trace",
    "result_summary",
)

_TERMINAL_STATUSES = frozenset(
    {RunStatus.FINALIZED, RunStatus.FAILED, RunStatus.ABORTED}
)
_ROLE_RE = re.compile(r"^[a-z][a-z0-9_.-]*$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_EXPECTED_ROLE_KINDS = {
    **{
        role: ArtifactKind.MANIFEST
        for role in DEFAULT_REQUIRED_EVIDENCE_ROLES
        if role not in {"decision_event_trace", "result_summary"}
    },
    "decision_event_trace": ArtifactKind.LOG,
    "result_summary": ArtifactKind.REPORT,
    "repository_provenance": ArtifactKind.MANIFEST,
}


class EvidenceError(RuntimeError):
    """Raised when immutable evidence cannot be safely written or linked."""


@dataclass(frozen=True, slots=True)
class ImmutableSidecar:
    """Closed, hashed sidecar ready for registration in ``run.db``."""

    role: str
    kind: ArtifactKind
    uri: str
    sha256: str
    size_bytes: int
    schema_version: str
    content_type: str
    run_id: str
    spec_hash: str
    is_event_trace: bool = False

    @property
    def hash(self) -> str:
        """Content hash alias used by manifest-oriented callers."""

        return self.sha256

    @property
    def artifact_id(self) -> str:
        return f"evidence:{self.role}:{self.sha256[:20]}"


@dataclass(frozen=True, slots=True)
class EvidenceRegistration:
    artifacts: tuple[ArtifactReference, ...]
    registration_event: RunLifecycleEvent
    terminal_event: RunLifecycleEvent
    evidence_set_hash: str


@dataclass(frozen=True, slots=True)
class EvidenceVerification:
    errors: tuple[str, ...]
    artifact_ids: tuple[str, ...]
    roles: tuple[str, ...]

    @property
    def ok(self) -> bool:
        return not self.errors

    def raise_for_errors(self) -> None:
        if self.errors:
            raise EvidenceError("; ".join(self.errors))


def canonical_json_bytes(value: Any, *, newline: bool = True) -> bytes:
    """Return stable UTF-8 JSON bytes and reject NaN/Infinity values."""

    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return encoded + (b"\n" if newline else b"")


class ImmutableEvidenceWriter:
    """Create evidence files once beneath a single run directory."""

    def __init__(self, run_dir: str | Path, run_id: str, spec_hash: str) -> None:
        if not isinstance(run_id, str) or not run_id.strip():
            raise ValueError("run_id must be a non-empty string")
        _require_sha256("spec_hash", spec_hash)
        self.run_dir = Path(run_dir).resolve()
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.run_id = run_id
        self.spec_hash = spec_hash

    def write_json(
        self,
        uri: str,
        payload: Any,
        *,
        role: str,
        schema_version: str,
        kind: ArtifactKind = ArtifactKind.MANIFEST,
    ) -> ImmutableSidecar:
        """Write one canonical JSON MANIFEST or REPORT sidecar."""

        _require_role(role)
        _require_schema_version(schema_version)
        if kind not in {ArtifactKind.MANIFEST, ArtifactKind.REPORT}:
            raise ValueError("JSON evidence kind must be MANIFEST or REPORT")
        data = canonical_json_bytes(payload)
        path, normalized_uri = self._new_target(uri)
        with path.open("xb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        return self._seal(
            path,
            normalized_uri,
            role=role,
            kind=kind,
            schema_version=schema_version,
            content_type=JSON_CONTENT_TYPE,
            is_event_trace=False,
        )

    def open_trace(
        self,
        uri: str,
        *,
        role: str = "decision_event_trace",
        schema_version: str = "dof.decision-event.v1",
    ) -> "ImmutableJsonlTraceWriter":
        """Open a new JSONL trace; the returned handle only supports append."""

        _require_role(role)
        _require_schema_version(schema_version)
        path, normalized_uri = self._new_target(uri)
        return ImmutableJsonlTraceWriter(
            owner=self,
            path=path,
            uri=normalized_uri,
            role=role,
            schema_version=schema_version,
        )

    def write_jsonl(
        self,
        uri: str,
        rows: Iterable[Mapping[str, Any]],
        *,
        role: str = "decision_event_trace",
        schema_version: str = "dof.decision-event.v1",
    ) -> ImmutableSidecar:
        """Convenience wrapper for a complete append-only trace."""

        trace = self.open_trace(
            uri,
            role=role,
            schema_version=schema_version,
        )
        try:
            for row in rows:
                trace.append(row)
        finally:
            sidecar = trace.close()
        return sidecar

    def _new_target(self, uri: str) -> tuple[Path, str]:
        path, normalized_uri = _confined_path(self.run_dir, uri)
        path.parent.mkdir(parents=True, exist_ok=True)
        # Resolve again after directory creation to catch a pre-existing symlink
        # in any parent component.
        path, normalized_uri = _confined_path(self.run_dir, normalized_uri)
        if path.exists() or path.is_symlink():
            raise FileExistsError(f"immutable evidence already exists: {normalized_uri}")
        return path, normalized_uri

    def _seal(
        self,
        path: Path,
        uri: str,
        *,
        role: str,
        kind: ArtifactKind,
        schema_version: str,
        content_type: str,
        is_event_trace: bool,
    ) -> ImmutableSidecar:
        digest, size_bytes = _hash_and_size(path)
        # Rich sidecars may contain exact provider/context evidence.  Seal them
        # owner-readable only on shared experiment hosts; portability tooling
        # can still copy/archive them as the owning user.
        path.chmod(stat.S_IRUSR)
        return ImmutableSidecar(
            role=role,
            kind=kind,
            uri=uri,
            sha256=digest,
            size_bytes=size_bytes,
            schema_version=schema_version,
            content_type=content_type,
            run_id=self.run_id,
            spec_hash=self.spec_hash,
            is_event_trace=is_event_trace,
        )


class ImmutableJsonlTraceWriter:
    """A single-use JSONL handle with generated identity and sequence fields."""

    def __init__(
        self,
        *,
        owner: ImmutableEvidenceWriter,
        path: Path,
        uri: str,
        role: str,
        schema_version: str,
    ) -> None:
        self._owner = owner
        self._path = path
        self._uri = uri
        self._role = role
        self._schema_version = schema_version
        self._handle = path.open("xb")
        self._next_sequence = 0
        self._sidecar: ImmutableSidecar | None = None

    @property
    def closed(self) -> bool:
        return self._sidecar is not None

    @property
    def sidecar(self) -> ImmutableSidecar:
        if self._sidecar is None:
            raise EvidenceError("event trace has not been closed")
        return self._sidecar

    def append(self, payload: Mapping[str, Any]) -> int:
        if self.closed:
            raise EvidenceError("cannot append to a closed event trace")
        if not isinstance(payload, Mapping):
            raise TypeError("event trace rows must be mappings")
        row = dict(payload)
        expected = {
            "sequence": self._next_sequence,
            "run_id": self._owner.run_id,
            "spec_hash": self._owner.spec_hash,
        }
        for key, value in expected.items():
            if key in row and row[key] != value:
                raise EvidenceError(
                    f"trace row {key} must be {value!r}, got {row[key]!r}"
                )
            row[key] = value
        data = canonical_json_bytes(row)
        self._handle.write(data)
        self._handle.flush()
        self._next_sequence += 1
        return self._next_sequence - 1

    def close(self) -> ImmutableSidecar:
        if self._sidecar is not None:
            return self._sidecar
        self._handle.flush()
        os.fsync(self._handle.fileno())
        self._handle.close()
        self._sidecar = self._owner._seal(
            self._path,
            self._uri,
            role=self._role,
            kind=ArtifactKind.LOG,
            schema_version=self._schema_version,
            content_type=JSONL_CONTENT_TYPE,
            is_event_trace=True,
        )
        return self._sidecar

    def __enter__(self) -> "ImmutableJsonlTraceWriter":
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.close()


def register_evidence_and_finalize(
    store: SQLiteRunStore,
    *,
    run_dir: str | Path,
    sidecars: Sequence[ImmutableSidecar],
    run_id: str,
    spec_hash: str,
    terminal_status: RunStatus = RunStatus.FINALIZED,
    terminal_detail: Mapping[str, Any] | None = None,
    scope_id: str = "scope-root",
    required_roles: Iterable[str] = DEFAULT_REQUIRED_EVIDENCE_ROLES,
) -> EvidenceRegistration:
    """Register a complete evidence set and append the terminal event atomically."""

    _require_sha256("spec_hash", spec_hash)
    if terminal_status not in _TERMINAL_STATUSES:
        raise ValueError("terminal_status must be FINALIZED, FAILED, or ABORTED")
    if store.schema_version != CURRENT_SCHEMA_VERSION:
        raise EvidenceError(
            f"run.db schema must be {CURRENT_SCHEMA_VERSION}, got {store.schema_version}"
        )
    run = store.get_run()
    if run is None or run.run_id != run_id:
        raise EvidenceError("run_id does not match the canonical Run fact")
    scopes = {scope.scope_id: scope for scope in store.scopes()}
    scope = scopes.get(scope_id)
    if scope is None or scope.run_id != run_id:
        raise EvidenceError(f"scope {scope_id!r} does not belong to run {run_id!r}")
    if not sidecars:
        raise EvidenceError("at least one immutable sidecar is required")

    roles = [sidecar.role for sidecar in sidecars]
    uris = [sidecar.uri for sidecar in sidecars]
    artifact_ids = [sidecar.artifact_id for sidecar in sidecars]
    for label, values in (
        ("role", roles),
        ("uri", uris),
        ("artifact_id", artifact_ids),
    ):
        if len(set(values)) != len(values):
            raise EvidenceError(f"duplicate evidence {label}")
    for role in required_roles:
        if role not in roles:
            raise EvidenceError(f"required evidence role is missing: {role}")

    run_root = Path(run_dir).resolve()
    attempts = {attempt.attempt_id for attempt in store.attempts()}
    json_payloads: dict[str, Any] = {}
    for sidecar in sidecars:
        descriptor_errors = _sidecar_shape_errors(sidecar)
        if descriptor_errors:
            raise EvidenceError(
                f"sidecar {sidecar.role}: " + "; ".join(descriptor_errors)
            )
        if sidecar.run_id != run_id:
            raise EvidenceError(f"sidecar {sidecar.role} has a different run_id")
        if sidecar.spec_hash != spec_hash:
            raise EvidenceError(f"sidecar {sidecar.role} has a different spec_hash")
        errors, _, parsed_json = _verify_sidecar_file(
            run_root,
            sidecar.uri,
            expected_sha256=sidecar.sha256,
            expected_size=sidecar.size_bytes,
            content_type=sidecar.content_type,
            is_event_trace=sidecar.is_event_trace,
            expected_run_id=run_id,
            expected_spec_hash=spec_hash,
            attempt_ids=attempts,
        )
        if errors:
            raise EvidenceError(f"sidecar {sidecar.role}: " + "; ".join(errors))
        if parsed_json is not None:
            json_payloads[sidecar.role] = parsed_json

    semantic_errors = _repository_evidence_errors(
        json_payloads,
        {sidecar.role: sidecar for sidecar in sidecars},
        run.metadata.to_dict(),
        expected_spec_hash=spec_hash,
    )
    if semantic_errors:
        raise EvidenceError("; ".join(semantic_errors))

    existing_evidence = [
        artifact
        for artifact in store.artifacts()
        if artifact.metadata.to_dict().get("contract") == EVIDENCE_ARTIFACT_CONTRACT
    ]
    if existing_evidence:
        raise EvidenceError("immutable evidence has already been registered")
    events = list(store.run_events())
    if any(event.status in _TERMINAL_STATUSES for event in events):
        raise EvidenceError("cannot register evidence after a terminal run event")
    if any(_event_kind(event) == EVIDENCE_REGISTRATION_KIND for event in events):
        raise EvidenceError("evidence registration event already exists")
    sequences = [event.sequence for event in events]
    if sequences != list(range(len(sequences))):
        raise EvidenceError("existing run event sequence must be contiguous from zero")
    integrity_errors = store.integrity_check()
    if integrity_errors:
        raise EvidenceError("canonical store integrity failed: " + "; ".join(integrity_errors))

    ordered = sorted(sidecars, key=lambda item: (item.role, item.uri))
    evidence_set_hash = _evidence_set_hash(ordered, spec_hash)
    artifacts = tuple(
        ArtifactReference(
            sidecar.artifact_id,
            run_id,
            scope_id,
            sidecar.kind,
            sidecar.uri,
            sha256=sidecar.sha256,
            size_bytes=sidecar.size_bytes,
            metadata={
                "contract": EVIDENCE_ARTIFACT_CONTRACT,
                "content_type": sidecar.content_type,
                "evidence_role": sidecar.role,
                "immutable": True,
                "is_event_trace": sidecar.is_event_trace,
                "schema_version": sidecar.schema_version,
                "spec_hash": spec_hash,
            },
        )
        for sidecar in ordered
    )
    next_sequence = len(events)
    registration_event = RunLifecycleEvent(
        f"run-evidence:{evidence_set_hash[:20]}",
        run_id,
        next_sequence,
        RunStatus.RUNNING,
        detail={
            "kind": EVIDENCE_REGISTRATION_KIND,
            "payload": {
                "artifact_ids": [artifact.artifact_id for artifact in artifacts],
                "evidence_set_hash": evidence_set_hash,
                "schema_version": EVIDENCE_REGISTRATION_SCHEMA,
                "spec_hash": spec_hash,
            },
        },
    )
    terminal_event = RunLifecycleEvent(
        f"run-terminal:{evidence_set_hash[:20]}",
        run_id,
        next_sequence + 1,
        terminal_status,
        detail={
            "kind": TERMINAL_DETAIL_KIND,
            "payload": dict(terminal_detail or {}),
        },
    )
    with store.transaction():
        for artifact in artifacts:
            store.add_artifact(artifact)
        store.add_run_event(registration_event)
        store.add_run_event(terminal_event)
    return EvidenceRegistration(
        artifacts=artifacts,
        registration_event=registration_event,
        terminal_event=terminal_event,
        evidence_set_hash=evidence_set_hash,
    )


def verify_registered_evidence(
    store_or_db: SQLiteRunStore | str | Path,
    *,
    run_dir: str | Path | None = None,
    expected_run_id: str | None = None,
    expected_spec_hash: str | None = None,
    required_roles: Iterable[str] = DEFAULT_REQUIRED_EVIDENCE_ROLES,
    scope_id: str = "scope-root",
) -> EvidenceVerification:
    """Verify SQLite links, immutable files, trace identity, and event ordering."""

    owns_store = not isinstance(store_or_db, SQLiteRunStore)
    if owns_store:
        db_path = Path(store_or_db)
        if db_path.is_dir() or db_path.suffix != ".db":
            db_path = db_path / "run.db"
        store = SQLiteRunStore(db_path, migrate=False)
    else:
        store = store_or_db
    assert isinstance(store, SQLiteRunStore)
    try:
        errors: list[str] = []
        if store.schema_version != CURRENT_SCHEMA_VERSION:
            errors.append(
                f"schema version is {store.schema_version}, expected {CURRENT_SCHEMA_VERSION}"
            )
        run = store.get_run()
        if run is None:
            errors.append("run.db has no Run fact")
            return EvidenceVerification(tuple(errors), (), ())
        run_id = expected_run_id or run.run_id
        if run.run_id != run_id:
            errors.append(f"run_id is {run.run_id!r}, expected {run_id!r}")
        if expected_spec_hash is None:
            errors.append("expected_spec_hash is required")
            spec_hash = ""
        else:
            spec_hash = expected_spec_hash
            try:
                _require_sha256("expected_spec_hash", spec_hash)
            except ValueError as exc:
                errors.append(str(exc))

        root = Path(run_dir).resolve() if run_dir is not None else store.path.parent.resolve()
        attempts = {attempt.attempt_id for attempt in store.attempts()}
        artifacts = [
            artifact
            for artifact in store.artifacts()
            if artifact.metadata.to_dict().get("contract") == EVIDENCE_ARTIFACT_CONTRACT
        ]
        artifact_ids = tuple(sorted(artifact.artifact_id for artifact in artifacts))
        roles: list[str] = []
        sidecars_for_set: list[ImmutableSidecar] = []
        json_payloads: dict[str, Any] = {}
        for artifact in artifacts:
            metadata = artifact.metadata.to_dict()
            role = metadata.get("evidence_role")
            if not isinstance(role, str):
                errors.append(f"artifact {artifact.artifact_id} has no evidence_role")
                continue
            roles.append(role)
            if artifact.run_id != run_id:
                errors.append(f"artifact {artifact.artifact_id} has wrong run_id")
            if artifact.scope_id != scope_id:
                errors.append(f"artifact {artifact.artifact_id} has wrong scope_id")
            if metadata.get("immutable") is not True:
                errors.append(f"artifact {artifact.artifact_id} is not marked immutable")
            if metadata.get("spec_hash") != spec_hash:
                errors.append(f"artifact {artifact.artifact_id} has wrong spec_hash")
            content_type = metadata.get("content_type")
            schema_version = metadata.get("schema_version")
            is_event_trace = metadata.get("is_event_trace") is True
            if content_type not in {JSON_CONTENT_TYPE, JSONL_CONTENT_TYPE}:
                errors.append(f"artifact {artifact.artifact_id} has unsupported content_type")
                continue
            if not isinstance(schema_version, str) or not schema_version:
                errors.append(f"artifact {artifact.artifact_id} has no schema_version")
                continue
            if artifact.sha256 is None or artifact.size_bytes is None:
                errors.append(f"artifact {artifact.artifact_id} lacks hash or size")
                continue
            file_errors, _, parsed_json = _verify_sidecar_file(
                root,
                artifact.uri,
                expected_sha256=artifact.sha256,
                expected_size=artifact.size_bytes,
                content_type=content_type,
                is_event_trace=is_event_trace,
                expected_run_id=run_id,
                expected_spec_hash=spec_hash,
                attempt_ids=attempts,
            )
            errors.extend(
                f"artifact {artifact.artifact_id}: {error}" for error in file_errors
            )
            sidecar = ImmutableSidecar(
                role=role,
                kind=artifact.kind,
                uri=artifact.uri,
                sha256=artifact.sha256,
                size_bytes=artifact.size_bytes,
                schema_version=schema_version,
                content_type=content_type,
                run_id=artifact.run_id,
                spec_hash=spec_hash,
                is_event_trace=is_event_trace,
            )
            sidecars_for_set.append(sidecar)
            if parsed_json is not None:
                json_payloads[role] = parsed_json
            errors.extend(
                f"artifact {artifact.artifact_id}: {error}"
                for error in _sidecar_shape_errors(sidecar)
            )
            if artifact.artifact_id != sidecar.artifact_id:
                errors.append(f"artifact {artifact.artifact_id} has non-canonical artifact_id")

        if len(roles) != len(set(roles)):
            errors.append("evidence roles are not unique")
        required = tuple(required_roles)
        for role in required:
            if role not in roles:
                errors.append(f"required evidence role is missing: {role}")

        errors.extend(
            _repository_evidence_errors(
                json_payloads,
                {sidecar.role: sidecar for sidecar in sidecars_for_set},
                run.metadata.to_dict(),
                expected_spec_hash=spec_hash,
            )
        )

        events = list(store.run_events())
        sequences = [event.sequence for event in events]
        if sequences != list(range(len(sequences))):
            errors.append("run event sequence is not contiguous from zero")
        terminal_events = [event for event in events if event.status in _TERMINAL_STATUSES]
        if len(terminal_events) != 1:
            errors.append(f"expected one terminal event, found {len(terminal_events)}")
            terminal = None
        else:
            terminal = terminal_events[0]
            if not events or events[-1].event_id != terminal.event_id:
                errors.append("terminal event is not the final run event")
        registration_events = [
            event for event in events if _event_kind(event) == EVIDENCE_REGISTRATION_KIND
        ]
        if len(registration_events) != 1:
            errors.append(
                f"expected one evidence registration event, found {len(registration_events)}"
            )
        else:
            registration = registration_events[0]
            if registration.status is not RunStatus.RUNNING:
                errors.append("evidence registration event is not RUNNING")
            if terminal is not None and registration.sequence >= terminal.sequence:
                errors.append("evidence registration does not precede terminal event")
            if terminal is not None and registration.sequence + 1 != terminal.sequence:
                errors.append("evidence registration is not immediately before terminal event")
            detail = registration.detail.to_dict()
            payload = detail.get("payload") if isinstance(detail.get("payload"), dict) else {}
            expected_ids = sorted(artifact_ids)
            registered_ids = payload.get("artifact_ids")
            normalized_registered_ids = (
                sorted(registered_ids) if isinstance(registered_ids, list) else []
            )
            if normalized_registered_ids != expected_ids:
                errors.append("registration artifact_ids do not match evidence artifacts")
            if payload.get("spec_hash") != spec_hash:
                errors.append("registration event has wrong spec_hash")
            if payload.get("schema_version") != EVIDENCE_REGISTRATION_SCHEMA:
                errors.append("registration event has wrong schema_version")
            expected_set_hash = _evidence_set_hash(sidecars_for_set, spec_hash) if spec_hash else ""
            if payload.get("evidence_set_hash") != expected_set_hash:
                errors.append("registration event has wrong evidence_set_hash")

        errors.extend(f"run.db: {error}" for error in store.integrity_check())
        return EvidenceVerification(
            errors=tuple(errors),
            artifact_ids=artifact_ids,
            roles=tuple(sorted(roles)),
        )
    finally:
        if owns_store:
            store.close()


def _event_kind(event: RunLifecycleEvent) -> object:
    return event.detail.to_dict().get("kind")


def _require_role(role: str) -> None:
    if not isinstance(role, str) or not _ROLE_RE.fullmatch(role):
        raise ValueError(
            "evidence role must match ^[a-z][a-z0-9_.-]*$"
        )


def _require_schema_version(schema_version: str) -> None:
    if not isinstance(schema_version, str) or not schema_version.strip():
        raise ValueError("schema_version must be a non-empty string")


def _require_sha256(name: str, value: str) -> None:
    if not isinstance(value, str) or not _SHA256_RE.fullmatch(value):
        raise ValueError(f"{name} must be a lowercase SHA-256 hex digest")


def _sidecar_shape_errors(sidecar: ImmutableSidecar) -> list[str]:
    errors: list[str] = []
    try:
        _require_role(sidecar.role)
    except ValueError as exc:
        errors.append(str(exc))
    try:
        _require_schema_version(sidecar.schema_version)
    except ValueError as exc:
        errors.append(str(exc))
    try:
        _require_sha256("sidecar sha256", sidecar.sha256)
    except ValueError as exc:
        errors.append(str(exc))
    if not isinstance(sidecar.size_bytes, int) or sidecar.size_bytes < 0:
        errors.append("sidecar size_bytes must be a non-negative integer")
    if sidecar.is_event_trace:
        if sidecar.kind is not ArtifactKind.LOG:
            errors.append("event trace kind must be LOG")
        if sidecar.content_type != JSONL_CONTENT_TYPE:
            errors.append("event trace content_type must be application/x-ndjson")
    elif sidecar.content_type != JSON_CONTENT_TYPE:
        errors.append("non-trace sidecar content_type must be application/json")
    elif sidecar.kind not in {ArtifactKind.MANIFEST, ArtifactKind.REPORT}:
        errors.append("JSON sidecar kind must be MANIFEST or REPORT")
    expected_kind = _EXPECTED_ROLE_KINDS.get(sidecar.role)
    if expected_kind is not None and sidecar.kind is not expected_kind:
        errors.append(
            f"evidence role {sidecar.role} must use kind {expected_kind.value}"
        )
    return errors


def _confined_path(run_dir: Path, uri: str) -> tuple[Path, str]:
    if not isinstance(uri, str) or not uri:
        raise ValueError("evidence uri must be a non-empty relative URI")
    if "\\" in uri or "\x00" in uri:
        raise ValueError("evidence uri must be a POSIX relative URI")
    raw_parts = uri.split("/")
    if any(part in {"", ".", ".."} for part in raw_parts):
        raise ValueError("evidence uri cannot contain empty, '.' or '..' segments")
    pure = PurePosixPath(uri)
    if pure.is_absolute():
        raise ValueError("evidence uri must be relative")
    normalized = pure.as_posix()
    root = run_dir.resolve()
    candidate = (root / Path(*pure.parts)).resolve(strict=False)
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise ValueError("evidence uri escapes the run directory") from exc
    if candidate == root:
        raise ValueError("evidence uri must name a file")
    return candidate, normalized


def _hash_and_size(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def _evidence_set_hash(
    sidecars: Iterable[ImmutableSidecar], spec_hash: str
) -> str:
    rows = [
        {
            "artifact_id": sidecar.artifact_id,
            "kind": sidecar.kind.value,
            "role": sidecar.role,
            "sha256": sidecar.sha256,
            "size_bytes": sidecar.size_bytes,
            "uri": sidecar.uri,
        }
        for sidecar in sorted(sidecars, key=lambda item: (item.role, item.uri))
    ]
    payload = {"artifacts": rows, "spec_hash": spec_hash}
    return hashlib.sha256(canonical_json_bytes(payload, newline=False)).hexdigest()


def _repository_evidence_errors(
    payloads: Mapping[str, Any],
    sidecars: Mapping[str, ImmutableSidecar],
    run_metadata: Mapping[str, Any],
    *,
    expected_spec_hash: str,
) -> list[str]:
    """Cross-check declared constraints and runtime-observed Git evidence."""

    required = (
        "experiment_manifest",
        "repository_provenance",
        "tool_environment_manifest",
        "result_summary",
    )
    experiment_candidate = payloads.get("experiment_manifest")
    gate_zero_contract = (
        isinstance(experiment_candidate, Mapping)
        and experiment_candidate.get("schema_version") == "dof.experiment-manifest.v2"
    ) or (
        run_metadata.get("runner_version") == "dof.mock-runner.v3"
    ) or "repository_provenance" in payloads or "repository_provenance" in sidecars
    if not gate_zero_contract:
        return []
    missing = [
        role
        for role in required
        if role not in payloads or role not in sidecars
    ]
    if missing:
        return ["repository provenance evidence role missing: " + ",".join(missing)]
    experiment = payloads["experiment_manifest"]
    observation = payloads["repository_provenance"]
    tool_environment = payloads["tool_environment_manifest"]
    result_summary = payloads["result_summary"]
    errors: list[str] = []
    if not all(
        isinstance(item, Mapping)
        for item in (experiment, observation, tool_environment, result_summary)
    ):
        return ["repository provenance semantic payload is not an object"]

    errors.extend(
        f"repository_provenance: {reason}"
        for reason in validate_repository_observation_payload(observation)
    )
    repository_sidecar = sidecars["repository_provenance"]
    if repository_sidecar.schema_version != REPOSITORY_PROVENANCE_SCHEMA_VERSION:
        errors.append("repository_provenance sidecar schema mismatch")
    expected_binding = {
        "artifact_id": repository_sidecar.artifact_id,
        "schema_version": repository_sidecar.schema_version,
        "sha256": repository_sidecar.sha256,
    }
    for role, payload in (
        ("experiment_manifest", experiment),
        ("tool_environment_manifest", tool_environment),
        ("result_summary", result_summary),
    ):
        if payload.get("repository_provenance") != expected_binding:
            errors.append(f"{role} repository_provenance binding mismatch")

    manifest_schema = experiment.get("schema_version")
    constraint: Mapping[str, Any] | None = None
    if manifest_schema == "dof.experiment-manifest.v2":
        spec_payload = experiment.get("spec")
        try:
            from .spec import ExperimentSpec

            parsed_spec = ExperimentSpec.from_dict(spec_payload)
        except (TypeError, ValueError) as exc:
            errors.append(f"experiment_manifest spec is invalid: {exc}")
        else:
            if parsed_spec.spec_hash != expected_spec_hash:
                errors.append("experiment_manifest spec_hash semantic mismatch")
            constraint = parsed_spec.provenance.repository_constraint.to_dict()
    elif manifest_schema == "dof.real-preflight-manifest.v1":
        try:
            from .spec import RepositoryConstraintSpec

            constraint = RepositoryConstraintSpec.from_dict(
                experiment.get("repository_constraint")
            ).to_dict()
        except (TypeError, ValueError) as exc:
            errors.append(f"real preflight repository constraint is invalid: {exc}")
        if experiment.get("spec_hash") != expected_spec_hash:
            errors.append("real preflight spec_hash semantic mismatch")
    else:
        errors.append("experiment_manifest provenance schema is unsupported")

    if constraint is not None:
        if tool_environment.get("repository_constraint") != constraint:
            errors.append("tool_environment_manifest repository_constraint mismatch")
        mismatches = repository_constraint_mismatches(constraint, observation)
        errors.extend(
            f"declared repository constraint mismatch: {reason}"
            for reason in mismatches
        )

    declared_hashes = run_metadata.get("declared_manifest_hashes")
    if not isinstance(declared_hashes, Mapping) or declared_hashes.get(
        "repository_provenance"
    ) != repository_sidecar.sha256:
        errors.append("run metadata repository_provenance hash mismatch")
    if "git_commit" in tool_environment or "dirty_tree" in tool_environment:
        errors.append("tool_environment_manifest contains config-supplied repository facts")
    return errors


def _verify_sidecar_file(
    run_dir: Path,
    uri: str,
    *,
    expected_sha256: str,
    expected_size: int,
    content_type: str,
    is_event_trace: bool,
    expected_run_id: str,
    expected_spec_hash: str,
    attempt_ids: set[str],
) -> tuple[list[str], list[dict[str, Any]], Any | None]:
    errors: list[str] = []
    rows: list[dict[str, Any]] = []
    parsed_json: Any | None = None
    try:
        path, _ = _confined_path(run_dir, uri)
    except ValueError as exc:
        return [str(exc)], rows, parsed_json
    if path.is_symlink():
        return ["sidecar must not be a symlink"], rows, parsed_json
    if not path.is_file():
        return ["sidecar file is missing"], rows, parsed_json
    mode = stat.S_IMODE(path.stat().st_mode)
    if mode & (stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH):
        errors.append("sidecar file is writable")
    digest, size = _hash_and_size(path)
    if digest != expected_sha256:
        errors.append("sha256 mismatch")
    if size != expected_size:
        errors.append("size mismatch")
    try:
        raw = path.read_bytes()
        if content_type == JSON_CONTENT_TYPE:
            parsed = json.loads(raw.decode("utf-8"))
            parsed_json = parsed
            if raw != canonical_json_bytes(parsed):
                errors.append("JSON is not in canonical format")
        elif content_type == JSONL_CONTENT_TYPE:
            rows, jsonl_errors = _parse_canonical_jsonl(raw)
            errors.extend(jsonl_errors)
        else:
            errors.append(f"unsupported content type {content_type!r}")
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        errors.append(f"cannot parse sidecar: {exc}")
    if is_event_trace:
        errors.extend(
            _verify_trace_rows(
                rows,
                expected_run_id=expected_run_id,
                expected_spec_hash=expected_spec_hash,
                attempt_ids=attempt_ids,
            )
        )
    return errors, rows, parsed_json


def _parse_canonical_jsonl(raw: bytes) -> tuple[list[dict[str, Any]], list[str]]:
    errors: list[str] = []
    rows: list[dict[str, Any]] = []
    if raw and not raw.endswith(b"\n"):
        errors.append("JSONL does not end with a newline")
    for index, line in enumerate(raw.splitlines(keepends=True)):
        try:
            parsed = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            errors.append(f"trace row {index} cannot be parsed: {exc}")
            continue
        if not isinstance(parsed, dict):
            errors.append(f"trace row {index} is not an object")
            continue
        if line != canonical_json_bytes(parsed):
            errors.append(f"trace row {index} is not in canonical format")
        rows.append(parsed)
    return rows, errors


def _verify_trace_rows(
    rows: Sequence[Mapping[str, Any]],
    *,
    expected_run_id: str,
    expected_spec_hash: str,
    attempt_ids: set[str],
) -> list[str]:
    errors: list[str] = []
    for index, row in enumerate(rows):
        if row.get("sequence") != index:
            errors.append(f"trace row {index} has non-contiguous sequence")
        if row.get("run_id") != expected_run_id:
            errors.append(f"trace row {index} has wrong run_id")
        if row.get("spec_hash") != expected_spec_hash:
            errors.append(f"trace row {index} has wrong spec_hash")
        outer_attempt_id = row.get("outer_attempt_id")
        if outer_attempt_id is not None and outer_attempt_id not in attempt_ids:
            errors.append(
                f"trace row {index} references unknown outer_attempt_id {outer_attempt_id!r}"
            )
    return errors
