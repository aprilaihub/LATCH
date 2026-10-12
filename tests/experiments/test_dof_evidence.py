from __future__ import annotations

import hashlib
import json
from pathlib import Path
import stat

import pytest

from optimizer_pipeline.core.domain import (
    ActionSpecification,
    ArtifactKind,
    Attempt,
    RunLifecycleEvent,
    RunStatus,
)
from optimizer_pipeline.core.store import CURRENT_SCHEMA_VERSION
from optimizer_pipeline.dof.evidence import (
    DEFAULT_REQUIRED_EVIDENCE_ROLES,
    EVIDENCE_REGISTRATION_KIND,
    EvidenceError,
    ImmutableEvidenceWriter,
    register_evidence_and_finalize,
    verify_registered_evidence,
)
from optimizer_pipeline.run.store_bridge import initialize_run_store


RUN_ID = "dof-run-001"
SPEC_HASH = hashlib.sha256(b"stable experiment spec").hexdigest()


def _seed_store(tmp_path: Path):
    run_dir = tmp_path / "run"
    input_dcp = tmp_path / "input.dcp"
    input_dcp.write_bytes(b"mock dcp\n")
    store = initialize_run_store(
        run_dir=run_dir,
        run_id=RUN_ID,
        mode="dof-common-mock",
        input_dcp=input_dcp,
        root_metrics={"wns_ns": -0.1},
    )
    return run_dir, store


def _complete_sidecars(run_dir: Path, *, outer_attempt_id: str | None = None):
    writer = ImmutableEvidenceWriter(run_dir, RUN_ID, SPEC_HASH)
    sidecars = []
    for role in DEFAULT_REQUIRED_EVIDENCE_ROLES:
        if role in {"decision_event_trace", "result_summary"}:
            continue
        sidecars.append(
            writer.write_json(
                f"evidence/{role}.json",
                {"schema_version": f"{role}.v1", "z": 2, "a": "evidence"},
                role=role,
                schema_version=f"{role}.v1",
            )
        )
    trace = writer.open_trace("evidence/decision_events.jsonl")
    trace.append(
        {
            "event_class": "READ_ONLY",
            "event_type": "mock_observation",
            "outer_attempt_id": outer_attempt_id,
        }
    )
    sidecars.append(trace.close())
    sidecars.append(
        writer.write_json(
            "evidence/result_summary.json",
            {"endpoint": "valid_completion", "success": True},
            role="result_summary",
            schema_version="dof.result-summary.v1",
            kind=ArtifactKind.REPORT,
        )
    )
    return tuple(sidecars)


def test_writer_is_canonical_write_once_confined_and_read_only(tmp_path: Path):
    run_dir = tmp_path / "run"
    writer = ImmutableEvidenceWriter(run_dir, RUN_ID, SPEC_HASH)
    sidecar = writer.write_json(
        "evidence/manifest.json",
        {"z": 1, "a": "\u03bb"},
        role="experiment_manifest",
        schema_version="experiment.v1",
    )

    path = run_dir / sidecar.uri
    assert path.read_bytes() == '{"a":"\u03bb","z":1}\n'.encode()
    assert sidecar.role == "experiment_manifest"
    assert sidecar.kind is ArtifactKind.MANIFEST
    assert sidecar.hash == sidecar.sha256
    assert sidecar.sha256 == hashlib.sha256(path.read_bytes()).hexdigest()
    assert sidecar.size_bytes == path.stat().st_size
    assert stat.S_IMODE(path.stat().st_mode) & 0o222 == 0

    with pytest.raises(FileExistsError, match="already exists"):
        writer.write_json(
            sidecar.uri,
            {"replacement": True},
            role="replacement_manifest",
            schema_version="replacement.v1",
        )
    for unsafe in ("../escape.json", "/absolute.json", "evidence/../escape.json"):
        with pytest.raises(ValueError, match="relative|cannot contain|escapes"):
            writer.write_json(
                unsafe,
                {},
                role="unsafe_manifest",
                schema_version="unsafe.v1",
            )


def test_trace_injects_stable_identity_and_sequence_and_cannot_reopen(tmp_path: Path):
    run_dir = tmp_path / "run"
    writer = ImmutableEvidenceWriter(run_dir, RUN_ID, SPEC_HASH)
    trace = writer.open_trace("evidence/events.jsonl")
    assert trace.append({"event_type": "first"}) == 0
    assert trace.append({"event_type": "second", "sequence": 1}) == 1
    sidecar = trace.close()
    assert trace.close() == sidecar
    with pytest.raises(EvidenceError, match="closed"):
        trace.append({"event_type": "late"})

    rows = [json.loads(line) for line in (run_dir / sidecar.uri).read_text().splitlines()]
    assert [row["sequence"] for row in rows] == [0, 1]
    assert {row["run_id"] for row in rows} == {RUN_ID}
    assert {row["spec_hash"] for row in rows} == {SPEC_HASH}
    with pytest.raises(FileExistsError):
        writer.open_trace("evidence/events.jsonl")


def test_registers_complete_evidence_then_terminal_without_schema_change(tmp_path: Path):
    run_dir, store = _seed_store(tmp_path)
    sidecars = _complete_sidecars(run_dir)

    registration = register_evidence_and_finalize(
        store,
        run_dir=run_dir,
        sidecars=sidecars,
        run_id=RUN_ID,
        spec_hash=SPEC_HASH,
        terminal_status=RunStatus.FINALIZED,
        terminal_detail={"endpoint": "valid_completion"},
    )

    assert store.schema_version == CURRENT_SCHEMA_VERSION == 2
    assert [event.status for event in store.run_events()] == [
        RunStatus.RUNNING,
        RunStatus.RUNNING,
        RunStatus.FINALIZED,
    ]
    assert registration.registration_event.sequence + 1 == registration.terminal_event.sequence
    assert registration.registration_event.detail.to_dict()["kind"] == EVIDENCE_REGISTRATION_KIND
    assert {artifact.metadata.to_dict()["evidence_role"] for artifact in registration.artifacts} == set(
        DEFAULT_REQUIRED_EVIDENCE_ROLES
    )
    assert {artifact.scope_id for artifact in registration.artifacts} == {"scope-root"}
    assert store.connection.execute("PRAGMA quick_check").fetchone()[0] == "ok"
    assert store.connection.execute("PRAGMA foreign_key_check").fetchall() == []
    assert store.integrity_check() == ()

    verified = verify_registered_evidence(
        store,
        run_dir=run_dir,
        expected_run_id=RUN_ID,
        expected_spec_hash=SPEC_HASH,
    )
    assert verified.ok, verified.errors
    assert set(verified.roles) == set(DEFAULT_REQUIRED_EVIDENCE_ROLES)

    with pytest.raises(EvidenceError, match="already been registered|terminal"):
        register_evidence_and_finalize(
            store,
            run_dir=run_dir,
            sidecars=sidecars,
            run_id=RUN_ID,
            spec_hash=SPEC_HASH,
        )
    store.close()
    reopened = verify_registered_evidence(
        run_dir,
        expected_run_id=RUN_ID,
        expected_spec_hash=SPEC_HASH,
    )
    assert reopened.ok, reopened.errors


def test_registration_requires_the_complete_declared_role_set(tmp_path: Path):
    run_dir, store = _seed_store(tmp_path)
    writer = ImmutableEvidenceWriter(run_dir, RUN_ID, SPEC_HASH)
    one_manifest = writer.write_json(
        "evidence/experiment_manifest.json",
        {"schema_version": "experiment.v1"},
        role="experiment_manifest",
        schema_version="experiment.v1",
    )

    with pytest.raises(EvidenceError, match="required evidence role is missing"):
        register_evidence_and_finalize(
            store,
            run_dir=run_dir,
            sidecars=(one_manifest,),
            run_id=RUN_ID,
            spec_hash=SPEC_HASH,
        )

    assert store.integrity_check() == ()
    assert [artifact.artifact_id for artifact in store.artifacts()] == ["artifact-root"]
    store.close()


def test_registration_rejects_unknown_outer_attempt_without_partial_db_links(tmp_path: Path):
    run_dir, store = _seed_store(tmp_path)
    sidecars = _complete_sidecars(run_dir, outer_attempt_id="attempt-missing")

    with pytest.raises(EvidenceError, match="unknown outer_attempt_id"):
        register_evidence_and_finalize(
            store,
            run_dir=run_dir,
            sidecars=sidecars,
            run_id=RUN_ID,
            spec_hash=SPEC_HASH,
        )

    assert [artifact.artifact_id for artifact in store.artifacts()] == ["artifact-root"]
    assert [event.status for event in store.run_events()] == [RunStatus.RUNNING]
    store.close()


def test_trace_outer_attempt_must_join_canonical_attempt(tmp_path: Path):
    run_dir, store = _seed_store(tmp_path)
    store.add_action(
        ActionSpecification("action:mock", RUN_ID, "scope-root", "mock.action")
    )
    store.add_attempt(
        Attempt(
            "attempt:mock",
            RUN_ID,
            "scope-root",
            "node_000_root",
            "action:mock",
        )
    )
    sidecars = _complete_sidecars(run_dir, outer_attempt_id="attempt:mock")
    register_evidence_and_finalize(
        store,
        run_dir=run_dir,
        sidecars=sidecars,
        run_id=RUN_ID,
        spec_hash=SPEC_HASH,
    )
    verified = verify_registered_evidence(
        store,
        run_dir=run_dir,
        expected_run_id=RUN_ID,
        expected_spec_hash=SPEC_HASH,
    )
    assert verified.ok, verified.errors
    store.close()


def test_verifier_detects_file_tampering_and_terminal_not_last(tmp_path: Path):
    run_dir, store = _seed_store(tmp_path)
    sidecars = _complete_sidecars(run_dir)
    register_evidence_and_finalize(
        store,
        run_dir=run_dir,
        sidecars=sidecars,
        run_id=RUN_ID,
        spec_hash=SPEC_HASH,
    )

    trace = next(sidecar for sidecar in sidecars if sidecar.role == "decision_event_trace")
    trace_path = run_dir / trace.uri
    trace_path.chmod(0o644)
    trace_path.write_text(
        json.dumps(
            {
                "event_type": "tampered",
                "run_id": "wrong-run",
                "sequence": 4,
                "spec_hash": "0" * 64,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    )
    store.add_run_event(
        RunLifecycleEvent(
            "run-event-after-terminal",
            RUN_ID,
            len(store.run_events()),
            RunStatus.RUNNING,
        )
    )

    verified = verify_registered_evidence(
        store,
        run_dir=run_dir,
        expected_run_id=RUN_ID,
        expected_spec_hash=SPEC_HASH,
    )
    assert not verified.ok
    joined = " | ".join(verified.errors)
    assert "sha256 mismatch" in joined
    assert "sidecar file is writable" in joined
    assert "wrong run_id" in joined
    assert "wrong spec_hash" in joined
    assert "non-contiguous sequence" in joined
    assert "terminal event is not the final run event" in joined
    store.close()
