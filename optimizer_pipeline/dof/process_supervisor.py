#!/usr/bin/env python3
"""Persistent Linux session anchor for owned backend process trees.

The outer owner launches this stdlib-only process as the session leader.  It
launches the requested worker in the same session/group and does not exit
after that direct child until every surviving descendant is gone.  The outer
owner therefore retains a live PID/start-time anchor whenever cleanup may be
required and never infers ownership from a reused numeric PGID.
"""

from __future__ import annotations

import ctypes
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time


_PR_SET_CHILD_SUBREAPER = 36
_REGISTRY_ENV = "DOF_PROCESS_REGISTRY_DIR"
_REGISTRY_SCHEMA = "dof.queue-process-ownership-registry.v1"
_RECORD_SCHEMA = "dof.queue-owned-nested-process.v1"
_QUEUE_CONTROL_KEYS = (
    _REGISTRY_ENV,
    "DOF_JOB_ID",
    "DOF_ATTEMPT_ID",
)


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def _content_hash(value: object) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _atomic_write(path: Path, value: dict[str, object]) -> None:
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_bytes(json.dumps(value, sort_keys=True, indent=2).encode() + b"\n")
    os.replace(temporary, path)


def _load_registry_manifest(directory: Path, job_id: str, attempt_id: str) -> None:
    value = json.loads((directory / "registry.json").read_text())
    core = dict(value)
    claimed = core.pop("registry_sha256", None)
    if claimed != _content_hash(core):
        raise ValueError("registry manifest hash mismatch")
    if (
        value.get("schema_version") != _REGISTRY_SCHEMA
        or value.get("job_id") != job_id
        or value.get("attempt_id") != attempt_id
        or value.get("state") != "OPEN"
    ):
        raise ValueError("registry manifest binding mismatch")


def _self_process_identity() -> tuple[int, int, int, int]:
    text = (Path("/proc") / str(os.getpid()) / "stat").read_text()
    tail = text.rsplit(")", 1)[1].split()
    return os.getpid(), int(tail[19]), int(tail[2]), int(tail[3])


def _register_supervisor(arguments: list[str]) -> tuple[Path, dict[str, object]] | None:
    registry_value = os.environ.get(_REGISTRY_ENV)
    job_id = os.environ.get("DOF_JOB_ID")
    attempt_id = os.environ.get("DOF_ATTEMPT_ID")
    if registry_value is None and job_id is None and attempt_id is None:
        return None
    if not registry_value or not job_id or not attempt_id:
        raise ValueError("incomplete queue process-registry binding")
    directory = Path(registry_value)
    if not directory.is_absolute() or not (directory / "records").is_dir():
        raise ValueError("invalid queue process-registry directory")
    _load_registry_manifest(directory, job_id, attempt_id)
    pid, starttime, pgid, sid = _self_process_identity()
    if pid != pgid or pid != sid:
        raise ValueError("supervisor is not a session leader")
    core: dict[str, object] = {
        "schema_version": _RECORD_SCHEMA,
        "job_id": job_id,
        "attempt_id": attempt_id,
        "supervisor_pid": pid,
        "procfs_start_time": starttime,
        "pgid": pgid,
        "sid": sid,
        "command_worker_identity_hash": _content_hash(arguments),
        "registered_at": datetime.now(timezone.utc).isoformat(),
        "state": "REGISTERED",
    }
    record = core | {"record_sha256": _content_hash(core)}
    path = directory / "records" / f"supervisor-{pid}-{starttime}.json"
    _atomic_write(path, record)
    return path, record


def _mark_reaped(registration: tuple[Path, dict[str, object]] | None) -> None:
    if registration is None:
        return
    path, record = registration
    core = dict(record)
    core.pop("record_sha256", None)
    core.update(
        {
            "state": "REAPED",
            "reaped_at": datetime.now(timezone.utc).isoformat(),
            "reaped_by": "nested_supervisor",
        }
    )
    _atomic_write(path, core | {"record_sha256": _content_hash(core)})


def _enable_child_subreaper() -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(_PR_SET_CHILD_SUBREAPER, 1, 0, 0, 0) != 0:
        raise OSError(ctypes.get_errno(), "PR_SET_CHILD_SUBREAPER failed")


def _retain_anchor_on_sigterm(_signum: int, _frame: object) -> None:
    """Keep the verified session leader alive through TERM-to-KILL cleanup."""

    return None


def _read_identity(pid: int) -> tuple[str, int, int, int] | None:
    try:
        raw = (Path("/proc") / str(pid) / "stat").read_bytes()
        close = raw.rfind(b")")
        fields = raw[close + 2 :].split()
        return (
            fields[0].decode("ascii", "replace"),
            int(fields[1]),
            int(fields[2]),
            int(fields[3]),
        )
    except (OSError, IndexError, ValueError):
        return None


def _reap_adopted_zombies() -> bool:
    """Reap zombies and report whether the kernel still sees a live child."""

    while True:
        try:
            pid, _status = os.waitpid(-1, os.WNOHANG)
        except ChildProcessError:
            return False
        if pid == 0:
            return True


def _owned_survivor_exists(anchor_pid: int) -> bool:
    try:
        names = os.listdir("/proc")
    except OSError:
        return True
    for name in names:
        if not name.isdigit() or int(name) == anchor_pid:
            continue
        identity = _read_identity(int(name))
        if identity is None:
            continue
        state, ppid, pgrp, session = identity
        if state == "Z":
            continue
        # Same-session members remain kernel-bound to this live anchor.
        # PR_SET_CHILD_SUBREAPER makes an escaped orphan a direct child.
        if (pgrp == anchor_pid and session == anchor_pid) or ppid == anchor_pid:
            return True
    return False


def _owned_survivors_remain(anchor_pid: int) -> bool:
    # Once the direct target is reaped, PR_SET_CHILD_SUBREAPER makes surviving
    # descendants children (possibly through another live child) of this
    # process.  waitpid(...)=0 is authoritative live-child evidence and must
    # never be overridden by a racing, non-atomic /proc snapshot.
    live_child = _reap_adopted_zombies()
    return live_child or _owned_survivor_exists(anchor_pid)


def _mirror_target_signal(returncode: int) -> None:
    """Make the supervisor expose the direct target's signal return code."""

    signum = -returncode
    if signum not in {signal.SIGKILL, signal.SIGSTOP}:
        signal.signal(signum, signal.SIG_DFL)
        if hasattr(signal, "pthread_sigmask"):
            signal.pthread_sigmask(signal.SIG_UNBLOCK, {signum})
    os.kill(os.getpid(), signum)
    # A valid terminating signal cannot return here.  Keep a fail-closed
    # fallback for unusual kernels instead of silently mapping to success.
    os._exit(128 + signum)


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if not arguments or arguments[0] != "--" or len(arguments) == 1:
        return 64
    if os.name != "posix" or not Path("/proc/self").is_dir():
        return 69
    registration = None
    try:
        # Group cleanup deliberately sends SIGTERM before SIGKILL.  The
        # supervisor must survive that first signal so its PID/start-time
        # remains a kernel-backed ownership anchor even when a known member
        # forks a previously unseen same-session child while exiting.
        signal.signal(signal.SIGTERM, _retain_anchor_on_sigterm)
        _enable_child_subreaper()
        # Registration precedes launching the target, so a backend can never
        # reach READY or mutate state without a crash-surviving queue binding.
        registration = _register_supervisor(arguments[1:])
        target_environment = dict(os.environ)
        for key in _QUEUE_CONTROL_KEYS:
            target_environment.pop(key, None)
        target = subprocess.Popen(
            arguments[1:],
            stdin=0,
            stdout=1,
            stderr=2,
            close_fds=True,
            start_new_session=False,
            env=target_environment,
        )
    except FileNotFoundError:
        return 127
    except Exception:
        return 70
    returncode = target.wait()
    anchor_pid = os.getpid()
    while True:
        if not _owned_survivors_remain(anchor_pid):
            _mark_reaped(registration)
            if returncode < 0:
                _mirror_target_signal(returncode)
            return returncode
        time.sleep(0.01)


if __name__ == "__main__":
    raise SystemExit(main())
