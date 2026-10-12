"""Queue-owned registry and exact cleanup for nested backend supervisors."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import signal
import time
from typing import Any, Mapping, Sequence


REGISTRY_ENV = "DOF_PROCESS_REGISTRY_DIR"
REGISTRY_SCHEMA = "dof.queue-process-ownership-registry.v1"
RECORD_SCHEMA = "dof.queue-owned-nested-process.v1"
RECORD_STATES = frozenset({"REGISTERED", "REAPED", "CLEANUP_FAILED"})
_SUPERVISOR_SCRIPT = Path(__file__).with_name("process_supervisor.py").resolve()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def content_hash(value: Any) -> str:
    return hashlib.sha256(value if isinstance(value, bytes) else _bytes(value)).hexdigest()


def atomic_write(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_bytes(json.dumps(value, sort_keys=True, indent=2).encode() + b"\n")
    os.replace(temporary, path)


def _sealed(value: Mapping[str, Any], hash_field: str) -> dict[str, Any]:
    core = dict(value)
    core.pop(hash_field, None)
    return core | {hash_field: content_hash(core)}


@dataclass(frozen=True, slots=True)
class ProcessIdentity:
    pid: int
    ppid: int
    pgid: int
    sid: int
    starttime: int


def read_process_identity(pid: int) -> ProcessIdentity | None:
    try:
        text = (Path("/proc") / str(pid) / "stat").read_text()
        tail = text.rsplit(")", 1)[1].split()
        if tail[0] == "Z":
            return None
        return ProcessIdentity(
            pid=pid,
            ppid=int(tail[1]),
            pgid=int(tail[2]),
            sid=int(tail[3]),
            starttime=int(tail[19]),
        )
    except (OSError, IndexError, ValueError):
        return None


def all_process_identities() -> tuple[ProcessIdentity, ...]:
    try:
        names = os.listdir("/proc")
    except OSError:
        return ()
    identities = []
    for name in names:
        if name.isdigit():
            identity = read_process_identity(int(name))
            if identity is not None:
                identities.append(identity)
    return tuple(sorted(identities, key=lambda row: row.pid))


def _process_arguments(pid: int) -> tuple[str, ...]:
    try:
        raw = (Path("/proc") / str(pid) / "cmdline").read_bytes()
    except OSError:
        return ()
    try:
        return tuple(
            item.decode("utf-8", "surrogateescape")
            for item in raw.split(b"\0")
            if item
        )
    except UnicodeError:
        return ()


def _argument_is_attempt_bound(argument: str, attempt_directory: Path) -> bool:
    values = [argument]
    if "=" in argument:
        values.append(argument.split("=", 1)[1])
    for value in values:
        candidate = Path(value)
        if not candidate.is_absolute():
            continue
        try:
            if candidate.resolve(strict=False).is_relative_to(attempt_directory):
                return True
        except (OSError, RuntimeError):
            continue
    return False


def scan_attempt_path_supervisors(
    attempt_directory: Path,
) -> tuple[ProcessIdentity, ...]:
    """Find exact supervisor session leaders whose argv names this attempt.

    This is intentionally independent of the cooperative registry.  Both the
    exact supervisor script and an absolute argv path below the attempt
    directory must match; process-name-only matching is never sufficient.
    """

    attempt = attempt_directory.resolve(strict=False)
    matches = []
    for identity in all_process_identities():
        if identity.pid != identity.pgid or identity.pid != identity.sid:
            continue
        arguments = _process_arguments(identity.pid)
        if not arguments:
            continue
        script_match = any(
            Path(argument).is_absolute()
            and Path(argument).resolve(strict=False) == _SUPERVISOR_SCRIPT
            for argument in arguments
        )
        if script_match and any(
            _argument_is_attempt_bound(argument, attempt) for argument in arguments
        ):
            current = read_process_identity(identity.pid)
            if current is not None and current.starttime == identity.starttime:
                matches.append(current)
    return tuple(sorted(matches, key=lambda row: row.pid))


def _identity_hash(identity: ProcessIdentity, attempt_directory: Path) -> str:
    return content_hash(
        {
            "attempt_directory": os.fspath(attempt_directory.resolve(strict=False)),
            "pid": identity.pid,
            "pgid": identity.pgid,
            "sid": identity.sid,
            "starttime": identity.starttime,
        }
    )


def scan_owned_descendants(root: ProcessIdentity) -> tuple[ProcessIdentity, ...]:
    current = read_process_identity(root.pid)
    if current is None or current.starttime != root.starttime:
        return ()
    identities = all_process_identities()
    owned = {
        row.pid: row
        for row in identities
        if row.pid == root.pid or (row.pgid == root.pgid and row.sid == root.sid)
    }
    changed = True
    while changed:
        changed = False
        for row in identities:
            if row.pid not in owned and row.ppid in owned:
                owned[row.pid] = row
                changed = True
    return tuple(sorted(owned.values(), key=lambda row: row.pid))


def scan_verified_group(root: ProcessIdentity) -> tuple[ProcessIdentity, ...]:
    current = read_process_identity(root.pid)
    if current is None or current.starttime != root.starttime:
        return ()
    return tuple(
        row
        for row in all_process_identities()
        if row.pgid == root.pgid and row.sid == root.sid
    )


def signal_verified_identity(identity: ProcessIdentity, signum: int) -> bool:
    pidfd_open = getattr(os, "pidfd_open", None)
    pidfd_send_signal = getattr(signal, "pidfd_send_signal", None)
    if pidfd_open is None or pidfd_send_signal is None:
        raise RuntimeError("pidfd_signal_unavailable")
    descriptor = pidfd_open(identity.pid, 0)
    try:
        current = read_process_identity(identity.pid)
        if current is None or current.starttime != identity.starttime:
            return False
        pidfd_send_signal(descriptor, signum, None, 0)
        return True
    finally:
        os.close(descriptor)


def create_registry(directory: Path, *, job_id: str, attempt_id: str) -> None:
    directory.mkdir(parents=True, exist_ok=False)
    (directory / "records").mkdir()
    core = {
        "schema_version": REGISTRY_SCHEMA,
        "job_id": job_id,
        "attempt_id": attempt_id,
        "created_at": _now(),
        "state": "OPEN",
    }
    atomic_write(directory / "registry.json", _sealed(core, "registry_sha256"))


def _load_sealed(path: Path, hash_field: str) -> tuple[dict[str, Any] | None, str | None]:
    try:
        value = json.loads(path.read_text())
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None, f"corrupt_json:{path.name}"
    if not isinstance(value, dict):
        return None, f"non_object_json:{path.name}"
    core = dict(value)
    claimed = core.pop(hash_field, None)
    if not isinstance(claimed, str) or claimed != content_hash(core):
        return None, f"hash_mismatch:{path.name}"
    return value, None


def seal_registry(directory: Path, *, job_id: str, attempt_id: str) -> tuple[str, ...]:
    manifest, error = _load_sealed(directory / "registry.json", "registry_sha256")
    if error is not None or manifest is None:
        return (error or "registry_manifest_missing",)
    errors = list(_validate_manifest(manifest, job_id=job_id, attempt_id=attempt_id))
    if errors:
        return tuple(errors)
    core = dict(manifest)
    core.pop("registry_sha256", None)
    core["state"] = "SEALED"
    core.setdefault("sealed_at", _now())
    atomic_write(directory / "registry.json", _sealed(core, "registry_sha256"))
    return ()


def _validate_manifest(
    manifest: Mapping[str, Any], *, job_id: str, attempt_id: str
) -> tuple[str, ...]:
    errors = []
    if manifest.get("schema_version") != REGISTRY_SCHEMA:
        errors.append("registry_schema_mismatch")
    if manifest.get("job_id") != job_id or manifest.get("attempt_id") != attempt_id:
        errors.append("registry_attempt_binding_mismatch")
    if manifest.get("state") not in {"OPEN", "SEALED"}:
        errors.append("registry_state_invalid")
    return tuple(errors)


def _validate_record(
    record: Mapping[str, Any], path: Path, *, job_id: str, attempt_id: str
) -> tuple[str, ...]:
    errors = []
    if record.get("schema_version") != RECORD_SCHEMA:
        errors.append(f"record_schema_mismatch:{path.name}")
    if record.get("job_id") != job_id or record.get("attempt_id") != attempt_id:
        errors.append(f"record_attempt_binding_mismatch:{path.name}")
    integers = ("supervisor_pid", "procfs_start_time", "pgid", "sid")
    if not all(
        isinstance(record.get(name), int)
        and not isinstance(record.get(name), bool)
        and int(record[name]) > 1
        for name in integers
    ):
        errors.append(f"record_identity_invalid:{path.name}")
    else:
        expected_name = (
            f"supervisor-{record['supervisor_pid']}-{record['procfs_start_time']}.json"
        )
        if path.name != expected_name:
            errors.append(f"record_filename_mismatch:{path.name}")
        if record["pgid"] != record["supervisor_pid"] or record["sid"] != record["supervisor_pid"]:
            errors.append(f"record_session_identity_invalid:{path.name}")
    worker_hash = record.get("command_worker_identity_hash")
    if (
        not isinstance(worker_hash, str)
        or len(worker_hash) != 64
        or any(character not in "0123456789abcdef" for character in worker_hash)
    ):
        errors.append(f"record_worker_hash_invalid:{path.name}")
    if record.get("state") not in RECORD_STATES:
        errors.append(f"record_state_invalid:{path.name}")
    if not isinstance(record.get("registered_at"), str) or not record["registered_at"]:
        errors.append(f"record_registration_time_invalid:{path.name}")
    return tuple(errors)


def load_registry(
    directory: Path, *, job_id: str, attempt_id: str
) -> tuple[dict[str, Any] | None, list[tuple[Path, dict[str, Any]]], tuple[str, ...]]:
    errors: list[str] = []
    manifest, manifest_error = _load_sealed(
        directory / "registry.json", "registry_sha256"
    )
    if manifest_error is not None or manifest is None:
        errors.append(manifest_error or "registry_manifest_missing")
    else:
        errors.extend(_validate_manifest(manifest, job_id=job_id, attempt_id=attempt_id))
    records_dir = directory / "records"
    if not records_dir.is_dir():
        errors.append("registry_records_directory_missing")
        return manifest, [], tuple(errors)
    if any(path.name.endswith(".tmp") for path in records_dir.iterdir()):
        errors.append("registry_partial_write_present")
    records: list[tuple[Path, dict[str, Any]]] = []
    for path in sorted(records_dir.iterdir()):
        if not path.is_file() or path.suffix != ".json":
            if not path.name.endswith(".tmp"):
                errors.append(f"registry_unexpected_entry:{path.name}")
            continue
        record, record_error = _load_sealed(path, "record_sha256")
        if record_error is not None or record is None:
            errors.append(record_error or f"record_missing:{path.name}")
            continue
        row_errors = _validate_record(
            record, path, job_id=job_id, attempt_id=attempt_id
        )
        errors.extend(row_errors)
        if not row_errors:
            records.append((path, record))
    return manifest, records, tuple(errors)


def _record_identity(record: Mapping[str, Any]) -> ProcessIdentity:
    return ProcessIdentity(
        pid=int(record["supervisor_pid"]),
        ppid=0,
        pgid=int(record["pgid"]),
        sid=int(record["sid"]),
        starttime=int(record["procfs_start_time"]),
    )


def _rewrite_record(path: Path, record: Mapping[str, Any], state: str, **updates: Any) -> None:
    core = dict(record)
    core.pop("record_sha256", None)
    core.update(updates)
    core["state"] = state
    atomic_write(path, _sealed(core, "record_sha256"))


def _live_exact(identity: ProcessIdentity) -> ProcessIdentity | None:
    current = read_process_identity(identity.pid)
    if current is None or current.starttime != identity.starttime:
        return None
    return current


def cleanup_registry(
    directory: Path,
    *,
    job_id: str,
    attempt_id: str,
    terminate_grace_seconds: float,
    kill_grace_seconds: float,
) -> dict[str, Any]:
    manifest, records, load_errors = load_registry(
        directory, job_id=job_id, attempt_id=attempt_id
    )
    errors = list(load_errors)
    events: list[dict[str, Any]] = []
    for path, record in records:
        identity = _record_identity(record)
        current = read_process_identity(identity.pid)
        if current is not None and current.starttime != identity.starttime:
            errors.append(f"registry_pid_starttime_mismatch:{path.name}")
            events.append({"record": path.name, "result": "IDENTITY_MISMATCH"})
            continue
        if current is None:
            _rewrite_record(path, record, "REAPED", reaped_at=_now(), reaped_by="queue_observation")
            events.append({"record": path.name, "result": "ALREADY_GONE"})
            continue
        if record["state"] == "REAPED":
            errors.append(f"reaped_record_still_live:{path.name}")
        root = ProcessIdentity(
            current.pid, current.ppid, current.pgid, current.sid, current.starttime
        )
        signalled_term = 0
        for member in reversed(scan_owned_descendants(root)):
            try:
                signalled_term += int(signal_verified_identity(member, signal.SIGTERM))
            except (ProcessLookupError, RuntimeError, OSError) as exc:
                errors.append(f"registry_sigterm_failed:{path.name}:{type(exc).__name__}")
        deadline = time.monotonic() + terminate_grace_seconds
        while _live_exact(root) is not None and time.monotonic() < deadline:
            time.sleep(min(0.02, max(0.0, deadline - time.monotonic())))
        signalled_kill = 0
        current = _live_exact(root)
        if current is not None:
            members = scan_owned_descendants(root)
            for member in reversed([row for row in members if row.pid != root.pid]):
                try:
                    signalled_kill += int(signal_verified_identity(member, signal.SIGKILL))
                except (ProcessLookupError, RuntimeError, OSError) as exc:
                    errors.append(f"registry_sigkill_failed:{path.name}:{type(exc).__name__}")
            current = _live_exact(root)
            if current is not None:
                try:
                    signalled_kill += int(signal_verified_identity(current, signal.SIGKILL))
                except (ProcessLookupError, RuntimeError, OSError) as exc:
                    errors.append(f"registry_sigkill_failed:{path.name}:{type(exc).__name__}")
        deadline = time.monotonic() + kill_grace_seconds
        while _live_exact(root) is not None and time.monotonic() < deadline:
            time.sleep(min(0.02, max(0.0, deadline - time.monotonic())))
        if _live_exact(root) is None:
            _rewrite_record(
                path,
                record,
                "REAPED",
                reaped_at=_now(),
                reaped_by="queue_exact_cleanup",
            )
            result = "REAPED"
        else:
            _rewrite_record(
                path,
                record,
                "CLEANUP_FAILED",
                cleanup_failed_at=_now(),
                cleanup_failure="verified_identity_remained_live",
            )
            errors.append(f"registry_identity_remained_live:{path.name}")
            result = "CLEANUP_FAILED"
        events.append(
            {
                "record": path.name,
                "result": result,
                "sigterm_count": signalled_term,
                "sigkill_count": signalled_kill,
            }
        )
    return {
        "schema_version": "dof.queue-process-cleanup.v1",
        "manifest_state": None if manifest is None else manifest.get("state"),
        "record_count": len(records),
        "events": events,
        "errors": sorted(set(errors)),
        "complete": not errors,
    }


def audit_registry(
    directory: Path, *, job_id: str, attempt_id: str
) -> dict[str, Any]:
    manifest, records, load_errors = load_registry(
        directory, job_id=job_id, attempt_id=attempt_id
    )
    errors = list(load_errors)
    live_records = []
    states: dict[str, int] = {}
    for path, record in records:
        state = str(record["state"])
        states[state] = states.get(state, 0) + 1
        expected = _record_identity(record)
        current = read_process_identity(expected.pid)
        if current is not None and current.starttime != expected.starttime:
            errors.append(f"registry_pid_starttime_mismatch:{path.name}")
        elif current is not None:
            live_records.append(path.name)
        if state != "REAPED":
            errors.append(f"registry_record_not_reaped:{path.name}")
    if manifest is not None and manifest.get("state") != "SEALED":
        errors.append("registry_not_sealed")
    unique_errors = sorted(set(errors))
    return {
        "schema_version": "dof.queue-process-ownership-audit.v1",
        "record_count": len(records),
        "state_counts": states,
        "live_record_count": len(live_records),
        "live_records": live_records,
        "errors": unique_errors,
        "quiescent": not live_records and not unique_errors,
    }


def audit_attempt_path_supervisors(
    attempt_directory: Path,
    registry_directory: Path,
    *,
    job_id: str,
    attempt_id: str,
) -> dict[str, Any]:
    """Compare live exact path-bound supervisors with valid registry records."""

    _manifest, records, load_errors = load_registry(
        registry_directory, job_id=job_id, attempt_id=attempt_id
    )
    registered = {
        (int(record["supervisor_pid"]), int(record["procfs_start_time"]))
        for _path, record in records
    }
    live = scan_attempt_path_supervisors(attempt_directory)
    unmatched = [
        row for row in live if (row.pid, row.starttime) not in registered
    ]
    errors = list(load_errors)
    errors.extend(
        "unregistered_path_bound_supervisor:" + _identity_hash(row, attempt_directory)
        for row in unmatched
    )
    return {
        "schema_version": "dof.queue-attempt-path-audit.v1",
        "live_supervisor_count": len(live),
        "live_identity_hashes": [
            _identity_hash(row, attempt_directory) for row in live
        ],
        "unregistered_supervisor_count": len(unmatched),
        "unregistered_identity_hashes": [
            _identity_hash(row, attempt_directory) for row in unmatched
        ],
        "registry_complete": not unmatched and not load_errors,
        "errors": sorted(set(errors)),
        "quiescent": not live and not errors,
    }


def cleanup_attempt_path_supervisors(
    attempt_directory: Path,
    identities: Sequence[ProcessIdentity],
    *,
    terminate_grace_seconds: float,
    kill_grace_seconds: float,
) -> dict[str, Any]:
    """Bounded cleanup after exact PID/start-time/script/path validation."""

    errors: list[str] = []
    events: list[dict[str, Any]] = []
    verified_live = {
        (row.pid, row.starttime): row
        for row in scan_attempt_path_supervisors(attempt_directory)
    }
    for claimed in identities:
        identity = verified_live.get((claimed.pid, claimed.starttime))
        identity_sha256 = _identity_hash(claimed, attempt_directory)
        if identity is None:
            events.append(
                {"identity_sha256": identity_sha256, "result": "ALREADY_GONE"}
            )
            continue
        term_count = 0
        for member in reversed(scan_owned_descendants(identity)):
            try:
                term_count += int(signal_verified_identity(member, signal.SIGTERM))
            except (ProcessLookupError, RuntimeError, OSError) as exc:
                errors.append(
                    f"path_audit_sigterm_failed:{identity_sha256}:{type(exc).__name__}"
                )
        deadline = time.monotonic() + terminate_grace_seconds
        while _live_exact(identity) is not None and time.monotonic() < deadline:
            time.sleep(min(0.02, max(0.0, deadline - time.monotonic())))
        kill_count = 0
        if _live_exact(identity) is not None:
            members = scan_owned_descendants(identity)
            for member in reversed([row for row in members if row.pid != identity.pid]):
                try:
                    kill_count += int(signal_verified_identity(member, signal.SIGKILL))
                except (ProcessLookupError, RuntimeError, OSError) as exc:
                    errors.append(
                        f"path_audit_sigkill_failed:{identity_sha256}:{type(exc).__name__}"
                    )
            if _live_exact(identity) is not None:
                try:
                    kill_count += int(signal_verified_identity(identity, signal.SIGKILL))
                except (ProcessLookupError, RuntimeError, OSError) as exc:
                    errors.append(
                        f"path_audit_sigkill_failed:{identity_sha256}:{type(exc).__name__}"
                    )
        deadline = time.monotonic() + kill_grace_seconds
        while _live_exact(identity) is not None and time.monotonic() < deadline:
            time.sleep(min(0.02, max(0.0, deadline - time.monotonic())))
        result = "REAPED" if _live_exact(identity) is None else "CLEANUP_FAILED"
        if result == "CLEANUP_FAILED":
            errors.append("path_audit_identity_remained_live:" + identity_sha256)
        events.append(
            {
                "identity_sha256": identity_sha256,
                "result": result,
                "sigterm_count": term_count,
                "sigkill_count": kill_count,
            }
        )
    return {
        "schema_version": "dof.queue-attempt-path-cleanup.v1",
        "validated_identity_count": len(identities),
        "events": events,
        "errors": sorted(set(errors)),
        "complete": not errors,
    }
