"""Bounded ownership of backend worker process trees.

The evidence-owning DoF runner should remain alive while a backend worker runs
in a separate POSIX session.  This module supplies that boundary without
knowing anything about Vivado or MCP: a later preflight can inject a Vivado
batch/MCP worker command, while unit tests use harmless Python subprocesses.

Only the verified owned session/descendant tree is ever signalled.  No
process-name matching is used.  Lifecycle timestamps use ``time.monotonic``
and output is continuously drained, hashed, and retained only up to a
declared byte cap.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import Enum
import hashlib
import json
import math
import os
from pathlib import Path
import signal
import shutil
import sys
import time
from types import MappingProxyType
from typing import Any


OWNED_PROCESS_SCHEMA_VERSION = "dof.owned-process-run.v1"
OWNED_PROCESS_RUNNER_VERSION = "dof.posix-process-owner.v3"
OWNED_LINE_PROTOCOL_RUNNER_VERSION = "dof.posix-line-protocol-owner.v3"
PROCESS_LIFECYCLE_SCHEMA_VERSION = "dof.process-lifecycle.v1"
QUEUE_CONTROL_ENV_KEYS = (
    "DOF_PROCESS_REGISTRY_DIR",
    "DOF_JOB_ID",
    "DOF_ATTEMPT_ID",
)


def build_target_process_environment(
    environment: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Materialize the target environment without queue ownership controls."""

    source = os.environ if environment is None else environment
    target = {str(key): str(value) for key, value in source.items()}
    for key in QUEUE_CONTROL_ENV_KEYS:
        target.pop(key, None)
    return target


def _split_target_and_supervisor_environments(
    environment: Mapping[str, str] | None,
) -> tuple[dict[str, str], dict[str, str]]:
    """Keep queue ownership on the supervisor and out of its target."""

    supplied = (
        {str(key): str(value) for key, value in os.environ.items()}
        if environment is None
        else {str(key): str(value) for key, value in environment.items()}
    )
    parent = {str(key): str(value) for key, value in os.environ.items()}
    observed = {
        key: tuple(
            value
            for value in (parent.get(key), supplied.get(key))
            if value is not None
        )
        for key in QUEUE_CONTROL_ENV_KEYS
    }
    if any(observed.values()):
        if any(not values or any(not value for value in values) for values in observed.values()):
            raise ValueError("incomplete queue process-registry binding")
        if any(len(set(values)) != 1 for values in observed.values()):
            raise ValueError("conflicting queue process-registry binding")
        binding = {key: values[0] for key, values in observed.items()}
    else:
        binding = {}
    target = build_target_process_environment(supplied)
    supervisor = target | binding
    return target, supervisor


class ProcessEndpoint(str, Enum):
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    INITIALIZATION_TIMEOUT = "INITIALIZATION_TIMEOUT"
    CALL_TIMEOUT = "CALL_TIMEOUT"
    WHOLE_RUN_TIMEOUT = "WHOLE_RUN_TIMEOUT"
    CANCELLED = "CANCELLED"
    OWNERSHIP_FAILURE = "OWNERSHIP_FAILURE"
    CLEANUP_FAILURE = "CLEANUP_FAILURE"
    ORPHAN_DETECTED = "ORPHAN_DETECTED"


@dataclass(frozen=True, slots=True)
class ProcessLimits:
    initialization_timeout_seconds: float = 30.0
    whole_run_timeout_seconds: float = 1200.0
    graceful_close_seconds: float = 2.0
    terminate_grace_seconds: float = 5.0
    kill_grace_seconds: float = 5.0
    output_limit_bytes: int = 1024 * 1024
    cleanup_timeout_seconds: float = 15.0

    def __post_init__(self) -> None:
        for name in (
            "initialization_timeout_seconds",
            "whole_run_timeout_seconds",
            "graceful_close_seconds",
            "terminate_grace_seconds",
            "kill_grace_seconds",
            "cleanup_timeout_seconds",
        ):
            value = float(getattr(self, name))
            if not math.isfinite(value) or value <= 0:
                raise ValueError(f"{name} must be finite and positive")
            object.__setattr__(self, name, value)
        if (
            not isinstance(self.output_limit_bytes, int)
            or isinstance(self.output_limit_bytes, bool)
            or self.output_limit_bytes < 0
        ):
            raise ValueError("output_limit_bytes must be a non-negative integer")
        minimum_cleanup = (
            self.graceful_close_seconds
            + self.terminate_grace_seconds
            + self.kill_grace_seconds
        )
        if self.cleanup_timeout_seconds < minimum_cleanup:
            raise ValueError(
                "cleanup_timeout_seconds must cover graceful+terminate+kill"
            )


@dataclass(slots=True)
class _CleanupDeadline:
    """One non-renewable monotonic deadline for the complete cleanup path."""

    timeout_seconds: float
    started_monotonic_seconds: float = field(init=False)
    deadline_monotonic_seconds: float = field(init=False)
    exceeded_recorded: bool = field(default=False, init=False)

    def __post_init__(self) -> None:
        timeout = float(self.timeout_seconds)
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("cleanup timeout must be finite and positive")
        self.timeout_seconds = timeout
        self.started_monotonic_seconds = time.monotonic()
        self.deadline_monotonic_seconds = (
            self.started_monotonic_seconds + timeout
        )

    def remaining_seconds(self, stage_cap: float | None = None) -> float:
        remaining = max(0.0, self.deadline_monotonic_seconds - time.monotonic())
        if stage_cap is None:
            return remaining
        return min(remaining, max(0.0, float(stage_cap)))

    def record_exceeded(
        self,
        phase: str,
        *,
        event: Any,
        errors: list[str],
    ) -> None:
        if self.exceeded_recorded:
            return
        self.exceeded_recorded = True
        errors.append("cleanup_deadline_exceeded")
        event(
            "PROCESS_CLEANUP_DEADLINE_EXCEEDED",
            phase=str(phase),
            cleanup_timeout_seconds=self.timeout_seconds,
        )


@dataclass(frozen=True, slots=True)
class CapturedOutput:
    data: bytes
    sha256: str
    total_bytes: int
    truncated: bool

    def public_dict(self, *, include_text: bool = False) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "sha256": self.sha256,
            "total_bytes": self.total_bytes,
            "retained_bytes": len(self.data),
            "truncated": self.truncated,
        }
        if include_text:
            payload["text"] = self.data.decode("utf-8", "replace")
        return payload


@dataclass(frozen=True, slots=True)
class OwnedProcessResult:
    endpoint: ProcessEndpoint
    returncode: int | None
    command_hash: str
    environment_hash: str
    executable_name: str
    root_process_token: str | None
    session_owned: bool
    duration_seconds: float
    lifecycle: tuple[Mapping[str, Any], ...]
    observed_process_tokens: tuple[str, ...]
    orphan_process_tokens: tuple[str, ...]
    stdout: CapturedOutput
    stderr: CapturedOutput
    cleanup_errors: tuple[str, ...]
    schema_version: str = OWNED_PROCESS_SCHEMA_VERSION
    runner_version: str = OWNED_PROCESS_RUNNER_VERSION

    @property
    def cleanup_ok(self) -> bool:
        return not self.cleanup_errors and not self.orphan_process_tokens

    def to_dict(self, *, include_output_text: bool = False) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "runner_version": self.runner_version,
            "endpoint": self.endpoint.value,
            "returncode": self.returncode,
            "command_hash": self.command_hash,
            "environment_hash": self.environment_hash,
            "executable_name": self.executable_name,
            "root_process_token": self.root_process_token,
            "session_owned": self.session_owned,
            "duration_seconds": self.duration_seconds,
            "lifecycle": [dict(item) for item in self.lifecycle],
            "observed_process_tokens": list(self.observed_process_tokens),
            "orphan_process_tokens": list(self.orphan_process_tokens),
            "stdout": self.stdout.public_dict(include_text=include_output_text),
            "stderr": self.stderr.public_dict(include_text=include_output_text),
            "cleanup_errors": list(self.cleanup_errors),
            "cleanup_ok": self.cleanup_ok,
        }


class OwnedProcessCancelled(asyncio.CancelledError):
    """Cancellation propagated after owned cleanup, carrying final evidence."""

    def __init__(self, result: OwnedProcessResult) -> None:
        self.result = result
        super().__init__("owned process run cancelled after bounded cleanup")


class OwnedProtocolSessionError(RuntimeError):
    """A persistent protocol session failed after producing final evidence."""

    def __init__(self, message: str, result: OwnedProcessResult) -> None:
        self.result = result
        super().__init__(message)


class OwnedProtocolCallTimedOut(TimeoutError):
    """A protocol call timed out after the owned process tree was cleaned."""

    def __init__(
        self,
        call_id: str,
        result: OwnedProcessResult,
        *,
        whole_run: bool,
    ) -> None:
        self.call_id = call_id
        self.result = result
        self.whole_run = whole_run
        reason = "whole-run deadline" if whole_run else "per-call deadline"
        super().__init__(f"protocol call {call_id} exceeded {reason}")


@dataclass(frozen=True, slots=True)
class ProtocolCallResult:
    """Bounded public result for one command in a persistent owned session."""

    call_id: str
    command_hash: str
    terminal_marker_hash: str
    duration_seconds: float
    stdout: CapturedOutput

    def to_dict(self, *, include_output_text: bool = False) -> dict[str, Any]:
        return {
            "call_id": self.call_id,
            "command_hash": self.command_hash,
            "terminal_marker_hash": self.terminal_marker_hash,
            "duration_seconds": self.duration_seconds,
            "stdout": self.stdout.public_dict(include_text=include_output_text),
        }


@dataclass(frozen=True, slots=True)
class _ProcIdentity:
    pid: int
    ppid: int
    pgrp: int
    session: int
    starttime: int

    def token(self, nonce: str) -> str:
        payload = f"{nonce}:{self.pid}:{self.starttime}".encode("ascii")
        return hashlib.sha256(payload).hexdigest()


def _canonical_hash(value: Any) -> str:
    data = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def _public_environment_hash(environment: Mapping[str, str]) -> str:
    """Bind the environment key set without deriving evidence from values.

    Environment values are an open-ended secret surface: a custom variable can
    carry a credential even when its name has no conventional secret marker.
    Public process evidence therefore records presence only for every key.  A
    caller that needs stronger value-level provenance must use a closed,
    domain-specific manifest rather than this generic process result.
    """

    entries = [
        {"key": str(key), "value_policy": "REDACTED_PRESENT"}
        for key in sorted(environment)
    ]
    return _canonical_hash(
        {
            "schema_version": "dof.public-safe-environment-evidence.v2",
            "entries": entries,
        }
    )


class OwnedProcessRunner:
    """Launch one worker in an owned POSIX session and clean it boundedly."""

    def __init__(
        self,
        limits: ProcessLimits | None = None,
        *,
        clock: Any = time.monotonic,
    ) -> None:
        self.limits = limits or ProcessLimits()
        self.clock = clock

    async def run_vivado_batch_worker(
        self,
        argv: Sequence[str | os.PathLike[str]],
        *,
        cwd: str | os.PathLike[str] | None = None,
        env: Mapping[str, str] | None = None,
        ready_marker: bytes | str | None = None,
        graceful_shutdown: bytes | str | None = None,
        stdin_payload: bytes | str | None = None,
    ) -> OwnedProcessResult:
        """Run an injected batch worker; this method never constructs Vivado args."""

        return await self.run(
            argv,
            cwd=cwd,
            env=env,
            ready_marker=ready_marker,
            graceful_shutdown=graceful_shutdown,
            stdin_payload=stdin_payload,
        )

    async def run(
        self,
        argv: Sequence[str | os.PathLike[str]],
        *,
        cwd: str | os.PathLike[str] | None = None,
        env: Mapping[str, str] | None = None,
        ready_marker: bytes | str | None = None,
        graceful_shutdown: bytes | str | None = None,
        stdin_payload: bytes | str | None = None,
    ) -> OwnedProcessResult:
        if os.name != "posix" or not hasattr(os, "killpg"):
            raise RuntimeError("owned process sessions require POSIX killpg semantics")
        command = tuple(str(item) for item in argv)
        if not command or any(not item for item in command):
            raise ValueError("argv must contain non-empty strings")
        marker = _as_bytes(ready_marker)
        graceful = _as_bytes(graceful_shutdown)
        initial_input = _as_bytes(stdin_payload)
        command_hash = _canonical_hash(list(command))
        # Materialize inherited values so the provenance hash describes the
        # environment actually supplied to the child, not an empty sentinel.
        effective_env, supervisor_env = _split_target_and_supervisor_environments(env)
        environment_hash = _public_environment_hash(effective_env)
        nonce = hashlib.sha256(
            f"{command_hash}:{self.clock():.9f}".encode("ascii")
        ).hexdigest()
        started = float(self.clock())
        lifecycle: list[Mapping[str, Any]] = []
        cleanup_errors: list[str] = []
        observed: dict[tuple[int, int], _ProcIdentity] = {}

        def event(event_type: str, **detail: Any) -> None:
            lifecycle.append(
                MappingProxyType(
                    {
                        "sequence": len(lifecycle),
                        "schema_version": PROCESS_LIFECYCLE_SCHEMA_VERSION,
                        "event_type": event_type,
                        "monotonic_seconds": max(0.0, float(self.clock()) - started),
                        **detail,
                    }
                )
            )

        event(
            "PROCESS_LAUNCH_REQUESTED",
            command_hash=command_hash,
            executable_name=Path(command[0]).name,
        )
        process: asyncio.subprocess.Process | None = None
        stdout_task: asyncio.Task[CapturedOutput] | None = None
        stderr_task: asyncio.Task[CapturedOutput] | None = None
        wait_task: asyncio.Task[int] | None = None
        ready_event = asyncio.Event()
        endpoint = ProcessEndpoint.FAILED
        session_owned = False
        root_token: str | None = None
        root_identity: tuple[int, int] | None = None
        cancelled = False

        try:
            _require_launchable_executable(command[0], effective_env)
            process = await asyncio.wait_for(
                asyncio.create_subprocess_exec(
                    *_supervised_command(command),
                    cwd=None if cwd is None else os.fspath(cwd),
                    env=supervisor_env,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    start_new_session=True,
                ),
                timeout=self.limits.initialization_timeout_seconds,
            )
            identity = _read_proc_identity(process.pid)
            if identity is not None:
                observed[(identity.pid, identity.starttime)] = identity
                root_identity = (identity.pid, identity.starttime)
                root_token = identity.token(nonce)
                session_owned = (
                    identity.pid == identity.pgrp == identity.session
                )
            event(
                "PROCESS_STARTED",
                root_process_token=root_token,
                session_owned=session_owned,
            )
            if not session_owned:
                endpoint = ProcessEndpoint.OWNERSHIP_FAILURE
                cleanup_errors.append("worker_is_not_owned_session_leader")
                event("PROCESS_OWNERSHIP_REJECTED")

            assert process.stdout is not None
            assert process.stderr is not None
            stdout_task = asyncio.create_task(
                _capture_stream(
                    process.stdout,
                    self.limits.output_limit_bytes,
                    marker=marker,
                    marker_event=ready_event,
                )
            )
            stderr_task = asyncio.create_task(
                _capture_stream(process.stderr, self.limits.output_limit_bytes)
            )
            wait_task = asyncio.create_task(process.wait())

            if initial_input:
                assert process.stdin is not None
                process.stdin.write(initial_input)
                await asyncio.wait_for(
                    process.stdin.drain(),
                    timeout=self.limits.initialization_timeout_seconds,
                )
                event("PROCESS_STDIN_PAYLOAD_SENT", size_bytes=len(initial_input))

            if session_owned:
                if marker is None:
                    event("PROCESS_INITIALIZED", marker_required=False)
                else:
                    ready_task = asyncio.create_task(ready_event.wait())
                    done, _ = await asyncio.wait(
                        {ready_task, wait_task},
                        timeout=self.limits.initialization_timeout_seconds,
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    if ready_task in done and ready_event.is_set():
                        event("PROCESS_INITIALIZED", marker_required=True)
                    elif wait_task in done:
                        endpoint = ProcessEndpoint.FAILED
                        event(
                            "PROCESS_EXITED_BEFORE_INITIALIZATION",
                            returncode=wait_task.result(),
                        )
                    else:
                        endpoint = ProcessEndpoint.INITIALIZATION_TIMEOUT
                        event("PROCESS_INITIALIZATION_TIMEOUT")
                    if not ready_task.done():
                        ready_task.cancel()
                    await asyncio.gather(ready_task, return_exceptions=True)

                if endpoint not in {
                    ProcessEndpoint.INITIALIZATION_TIMEOUT,
                    ProcessEndpoint.OWNERSHIP_FAILURE,
                } and not wait_task.done():
                    remaining = max(
                        0.0,
                        self.limits.whole_run_timeout_seconds
                        - (float(self.clock()) - started),
                    )
                    if remaining <= 0:
                        endpoint = ProcessEndpoint.WHOLE_RUN_TIMEOUT
                        event("PROCESS_WHOLE_RUN_TIMEOUT")
                    else:
                        try:
                            returncode = await asyncio.wait_for(
                                asyncio.shield(wait_task), timeout=remaining
                            )
                            endpoint = (
                                ProcessEndpoint.SUCCEEDED
                                if returncode == 0
                                else ProcessEndpoint.FAILED
                            )
                            event("PROCESS_ROOT_EXITED", returncode=returncode)
                        except TimeoutError:
                            endpoint = ProcessEndpoint.WHOLE_RUN_TIMEOUT
                            event("PROCESS_WHOLE_RUN_TIMEOUT")
                elif wait_task.done() and endpoint is ProcessEndpoint.FAILED:
                    event("PROCESS_ROOT_EXITED", returncode=wait_task.result())
        except TimeoutError:
            endpoint = ProcessEndpoint.INITIALIZATION_TIMEOUT
            event("PROCESS_INITIALIZATION_TIMEOUT")
        except asyncio.CancelledError:
            cancelled = True
            endpoint = ProcessEndpoint.CANCELLED
            event("PROCESS_TASK_CANCELLED")
        except FileNotFoundError:
            # A launch failure is an execution failure, not evidence that an
            # already-owned tree failed cleanup.  In particular, do not add a
            # cleanup error here: there is no child to clean or reap.
            endpoint = ProcessEndpoint.FAILED
            event("PROCESS_START_FAILED", error_class="FileNotFoundError")
        except Exception as exc:
            endpoint = ProcessEndpoint.FAILED
            cleanup_errors.append(f"runner_exception:{type(exc).__name__}")
            event("PROCESS_RUNNER_EXCEPTION", error_class=type(exc).__name__)

        cleanup_deadline = _CleanupDeadline(
            self.limits.cleanup_timeout_seconds
        )
        if process is not None:
            identities = _verified_scan_owned_identities(
                process.pid,
                root_identity,
            )
            for item in identities:
                observed[(item.pid, item.starttime)] = item
            cleanup_needed = (
                process.returncode is None
                or bool(_live_group_identities(process.pid))
            )
            if cleanup_needed:
                await _bounded_cleanup(
                    process,
                    process.pid,
                    limits=self.limits,
                    graceful_shutdown=graceful,
                    wait_task=wait_task,
                    event=event,
                    errors=cleanup_errors,
                    expected_identities=frozenset(observed),
                    observed_identities=observed,
                    expected_root_identity=root_identity,
                    cleanup_deadline=cleanup_deadline,
                )

            # Reap the owned root under a final bound.  This is deliberately
            # bounded even after SIGKILL; an unreapable root is a fatal cleanup
            # fact rather than an unbounded wait.
            if wait_task is not None and not wait_task.done():
                remaining = cleanup_deadline.remaining_seconds(
                    self.limits.kill_grace_seconds
                )
                if remaining <= 0:
                    cleanup_deadline.record_exceeded(
                        "root_reap", event=event, errors=cleanup_errors
                    )
                    cleanup_errors.append("owned_root_not_reaped")
                    event("PROCESS_ROOT_REAP_TIMEOUT")
                else:
                    try:
                        await asyncio.wait_for(
                            asyncio.shield(wait_task), timeout=remaining
                        )
                    except TimeoutError:
                        cleanup_errors.append("owned_root_not_reaped")
                        event("PROCESS_ROOT_REAP_TIMEOUT")
                        if cleanup_deadline.remaining_seconds() <= 0:
                            cleanup_deadline.record_exceeded(
                                "root_reap", event=event, errors=cleanup_errors
                            )

            final_identities = _verified_scan_owned_identities(
                process.pid,
                root_identity,
            )
            for item in final_identities:
                observed[(item.pid, item.starttime)] = item

        stdout = await _finish_capture(
            stdout_task,
            self.limits.kill_grace_seconds,
            "stdout",
            cleanup_errors,
            cleanup_deadline=cleanup_deadline,
            event=event,
        )
        stderr = await _finish_capture(
            stderr_task,
            self.limits.kill_grace_seconds,
            "stderr",
            cleanup_errors,
            cleanup_deadline=cleanup_deadline,
            event=event,
        )

        orphaned: list[_ProcIdentity] = []
        for identity in observed.values():
            current = _read_proc_identity(identity.pid)
            if current is not None and current.starttime == identity.starttime:
                orphaned.append(identity)
        # A process that was too short-lived to be sampled cannot be an orphan;
        # any surviving member of the owned PGID is sampled here and included.
        if process is not None:
            for identity in _live_group_identities(process.pid):
                key = (identity.pid, identity.starttime)
                if key in observed and key not in {
                    (item.pid, item.starttime) for item in orphaned
                }:
                    orphaned.append(identity)

        orphan_tokens = tuple(sorted(item.token(nonce) for item in orphaned))
        returncode = None if process is None else process.returncode
        if endpoint is ProcessEndpoint.SUCCEEDED and returncode != 0:
            endpoint = ProcessEndpoint.FAILED
        if orphan_tokens:
            cleanup_errors.append("owned_process_orphan_detected")
            endpoint = ProcessEndpoint.ORPHAN_DETECTED
            event("PROCESS_ORPHAN_DETECTED", orphan_count=len(orphan_tokens))
        elif cleanup_errors and endpoint in {
            ProcessEndpoint.SUCCEEDED,
            ProcessEndpoint.FAILED,
            ProcessEndpoint.OWNERSHIP_FAILURE,
        }:
            endpoint = ProcessEndpoint.CLEANUP_FAILURE
        event(
            "PROCESS_EVIDENCE_FINALIZED",
            endpoint=endpoint.value,
            cleanup_ok=not cleanup_errors and not orphan_tokens,
        )

        result = OwnedProcessResult(
            endpoint=endpoint,
            returncode=returncode,
            command_hash=command_hash,
            environment_hash=environment_hash,
            executable_name=Path(command[0]).name,
            root_process_token=root_token,
            session_owned=session_owned,
            duration_seconds=max(0.0, float(self.clock()) - started),
            lifecycle=tuple(lifecycle),
            observed_process_tokens=tuple(
                sorted(item.token(nonce) for item in observed.values())
            ),
            orphan_process_tokens=orphan_tokens,
            stdout=stdout,
            stderr=stderr,
            cleanup_errors=tuple(cleanup_errors),
        )
        if cancelled:
            raise OwnedProcessCancelled(result)
        return result


class _CaptureAccumulator:
    """Incrementally hash a stream while retaining only a bounded prefix."""

    __slots__ = ("_digest", "_limit", "_retained", "_total")

    def __init__(self, limit: int) -> None:
        self._limit = limit
        self._digest = hashlib.sha256()
        self._retained = bytearray()
        self._total = 0

    def update(self, data: bytes) -> None:
        if not data:
            return
        self._digest.update(data)
        self._total += len(data)
        remaining = self._limit - len(self._retained)
        if remaining > 0:
            self._retained.extend(data[:remaining])

    def snapshot(self) -> CapturedOutput:
        return CapturedOutput(
            bytes(self._retained),
            self._digest.hexdigest(),
            self._total,
            self._total > self._limit,
        )


@dataclass(slots=True)
class _ActiveProtocolCall:
    call_id: str
    marker: bytes
    marker_future: asyncio.Future[None]
    output: _CaptureAccumulator
    command_hash: str
    started: float


class _ProtocolStreamEnded(RuntimeError):
    pass


class OwnedLineProtocolSession:
    """One persistent worker session with an exact line-framed protocol.

    The class deliberately does not construct tool commands.  A caller first
    authorizes a host operation, then passes its already-built bytes to
    :meth:`call`; those bytes are written exactly once.  The call completes
    only when stdout contains its unique terminal marker as a complete line.
    This makes ``call`` directly usable as the body of an
    ``ObservedCallExecutor`` injected callable.
    """

    _MAX_PROTOCOL_LINE_BYTES = 1024 * 1024

    def __init__(
        self,
        argv: Sequence[str | os.PathLike[str]],
        *,
        ready_line: bytes | str,
        close_command: bytes | str,
        limits: ProcessLimits | None = None,
        cwd: str | os.PathLike[str] | None = None,
        env: Mapping[str, str] | None = None,
        clock: Any = time.monotonic,
    ) -> None:
        if os.name != "posix" or not hasattr(os, "killpg"):
            raise RuntimeError("owned protocol sessions require POSIX killpg semantics")
        command = tuple(str(item) for item in argv)
        if not command or any(not item for item in command):
            raise ValueError("argv must contain non-empty strings")
        ready = _protocol_line(ready_line, "ready_line")
        close = _as_bytes(close_command)
        if not close:
            raise ValueError("close_command must be non-empty")

        self.argv = command
        self.ready_line = ready
        self.close_command = close
        self.limits = limits or ProcessLimits()
        self.cwd = None if cwd is None else os.fspath(cwd)
        self.effective_env, self.supervisor_env = (
            _split_target_and_supervisor_environments(env)
        )
        self.clock = clock
        self.command_hash = _canonical_hash(list(command))
        self.environment_hash = _public_environment_hash(self.effective_env)

        self._start_attempted = False
        self._started = False
        self._process: asyncio.subprocess.Process | None = None
        self._wait_task: asyncio.Task[int] | None = None
        self._stdout_task: asyncio.Task[None] | None = None
        self._stderr_task: asyncio.Task[CapturedOutput] | None = None
        self._watchdog_task: asyncio.Task[None] | None = None
        self._ready_future: asyncio.Future[None] | None = None
        self._closed_future: asyncio.Future[OwnedProcessResult] | None = None
        self._active_call: _ActiveProtocolCall | None = None
        self._call_lock = asyncio.Lock()
        self._finalize_lock = asyncio.Lock()
        self._stdout_capture = _CaptureAccumulator(self.limits.output_limit_bytes)
        self._cleanup_errors: list[str] = []
        self._lifecycle: list[Mapping[str, Any]] = []
        self._observed: dict[tuple[int, int], _ProcIdentity] = {}
        self._used_markers: set[bytes] = set()
        self._result: OwnedProcessResult | None = None
        self._session_owned = False
        self._root_token: str | None = None
        self._root_identity: tuple[int, int] | None = None
        self._next_call = 0
        self._started_at = 0.0
        self._deadline = 0.0
        self._cleanup_deadline: _CleanupDeadline | None = None
        self._nonce = ""

    @property
    def result(self) -> OwnedProcessResult | None:
        return self._result

    @property
    def root_process_token(self) -> str | None:
        """Audit-safe token for the actually started session leader."""

        return self._root_token

    @property
    def session_owned(self) -> bool:
        """Whether the live worker is the owned process-group/session leader."""

        return self._session_owned

    @property
    def lifecycle_snapshot(self) -> tuple[Mapping[str, Any], ...]:
        """Return the immutable lifecycle prefix observed so far.

        This is a read-only synchronization seam for deterministic timeout and
        cancellation tests.  It does not wait, mutate the session, or expose
        command arguments.
        """

        return tuple(self._lifecycle)

    @property
    def startup_output(self) -> CapturedOutput:
        """Return the bounded bytes observed through the exact READY line.

        ``start`` only returns after the stdout pump has consumed READY, so a
        caller can validate worker-owned version/policy markers before sending
        the first tool request.  The snapshot is immutable and contains no
        command arguments supplied by the caller.
        """

        if not self._started:
            raise RuntimeError("owned protocol session is not initialized")
        return self._stdout_capture.snapshot()

    async def start(self) -> "OwnedLineProtocolSession":
        """Start exactly one child and await its exact READY line boundedly."""

        if self._start_attempted:
            raise RuntimeError(
                "owned protocol session start may be attempted only once"
            )
        self._start_attempted = True
        loop = asyncio.get_running_loop()
        self._ready_future = loop.create_future()
        self._closed_future = loop.create_future()
        self._started_at = float(self.clock())
        self._deadline = self._started_at + self.limits.whole_run_timeout_seconds
        self._nonce = hashlib.sha256(
            f"{self.command_hash}:{self._started_at:.9f}".encode("ascii")
        ).hexdigest()
        self._event(
            "PROCESS_LAUNCH_REQUESTED",
            command_hash=self.command_hash,
            executable_name=Path(self.argv[0]).name,
            persistent_line_protocol=True,
        )

        try:
            _require_launchable_executable(self.argv[0], self.effective_env)
            self._process = await asyncio.wait_for(
                asyncio.create_subprocess_exec(
                    *_supervised_command(self.argv),
                    cwd=self.cwd,
                    env=self.supervisor_env,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    start_new_session=True,
                ),
                timeout=self.limits.initialization_timeout_seconds,
            )
        except TimeoutError:
            self._event("PROCESS_INITIALIZATION_TIMEOUT", phase="process_start")
            self._cancel_ready_waiter()
            result = await self._finalize(ProcessEndpoint.INITIALIZATION_TIMEOUT)
            raise OwnedProtocolSessionError(
                "protocol worker process start timed out", result
            )
        except FileNotFoundError:
            self._event("PROCESS_START_FAILED", error_class="FileNotFoundError")
            self._cancel_ready_waiter()
            result = await self._finalize(ProcessEndpoint.FAILED)
            raise OwnedProtocolSessionError(
                "protocol worker executable not found", result
            )
        except asyncio.CancelledError:
            self._event("PROCESS_TASK_CANCELLED", phase="initialization")
            self._cancel_ready_waiter()
            result = await self._finalize(ProcessEndpoint.CANCELLED)
            raise OwnedProcessCancelled(result)
        except Exception as exc:
            self._event("PROCESS_START_FAILED", error_class=type(exc).__name__)
            self._cancel_ready_waiter()
            result = await self._finalize(ProcessEndpoint.FAILED)
            raise OwnedProtocolSessionError(
                "protocol worker failed to start", result
            ) from exc

        process = self._process
        identity = _read_proc_identity(process.pid)
        if identity is not None:
            self._observed[(identity.pid, identity.starttime)] = identity
            self._root_identity = (identity.pid, identity.starttime)
            self._root_token = identity.token(self._nonce)
            self._session_owned = identity.pid == identity.pgrp == identity.session
        self._event(
            "PROCESS_STARTED",
            root_process_token=self._root_token,
            session_owned=self._session_owned,
        )
        if not self._session_owned:
            self._cleanup_errors.append("worker_is_not_owned_session_leader")
            self._event("PROCESS_OWNERSHIP_REJECTED")
            result = await self._finalize(ProcessEndpoint.OWNERSHIP_FAILURE)
            raise OwnedProtocolSessionError(
                "protocol worker ownership rejected", result
            )

        assert process.stdout is not None
        assert process.stderr is not None
        self._stdout_task = asyncio.create_task(self._pump_stdout(process.stdout))
        self._stderr_task = asyncio.create_task(
            _capture_stream(process.stderr, self.limits.output_limit_bytes)
        )
        self._wait_task = asyncio.create_task(process.wait())

        init_bound = min(
            self.limits.initialization_timeout_seconds,
            max(0.0, self._deadline - float(self.clock())),
        )
        try:
            done, _ = await asyncio.wait(
                {self._ready_future, self._wait_task},
                timeout=init_bound,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if self._ready_future in done:
                # Surface a protocol-pump exception instead of accepting a
                # merely completed Future as readiness.
                self._ready_future.result()
            elif self._wait_task in done:
                self._event(
                    "PROCESS_EXITED_BEFORE_INITIALIZATION",
                    returncode=self._wait_task.result(),
                )
                self._cancel_ready_waiter()
                result = await self._finalize(ProcessEndpoint.FAILED)
                raise OwnedProtocolSessionError(
                    "protocol worker exited before READY", result
                )
            else:
                whole = float(self.clock()) >= self._deadline
                endpoint = (
                    ProcessEndpoint.WHOLE_RUN_TIMEOUT
                    if whole
                    else ProcessEndpoint.INITIALIZATION_TIMEOUT
                )
                self._event(
                    "PROCESS_WHOLE_RUN_TIMEOUT"
                    if whole
                    else "PROCESS_INITIALIZATION_TIMEOUT"
                )
                self._cancel_ready_waiter()
                result = await self._finalize(endpoint)
                raise OwnedProtocolSessionError("protocol READY timed out", result)
        except OwnedProtocolSessionError:
            raise
        except asyncio.CancelledError:
            self._event("PROCESS_TASK_CANCELLED", phase="initialization")
            self._cancel_ready_waiter()
            result = await self._finalize(ProcessEndpoint.CANCELLED)
            raise OwnedProcessCancelled(result)
        except Exception as exc:
            self._event(
                "PROCESS_PROTOCOL_FAILURE",
                phase="initialization",
                error_class=type(exc).__name__,
            )
            self._cancel_ready_waiter()
            result = await self._finalize(ProcessEndpoint.FAILED)
            raise OwnedProtocolSessionError(
                "invalid protocol initialization", result
            ) from exc

        # READY is not sufficient evidence of ownership.  Snapshot the actual
        # worker tree and fail closed if a nested launcher (historically
        # pexpect.pty.fork) escaped the session/PGID owned by this class.
        initialized_tree = _verified_scan_owned_identities(
            process.pid,
            self._root_identity,
        )
        for identity in initialized_tree:
            self._observed[(identity.pid, identity.starttime)] = identity
        initialized_tokens = tuple(
            sorted(identity.token(self._nonce) for identity in initialized_tree)
        )
        ownership_violations = tuple(
            identity
            for identity in initialized_tree
            if identity.pgrp != process.pid or identity.session != process.pid
        )
        if not initialized_tree or ownership_violations:
            self._event(
                "PROCESS_TREE_OWNERSHIP_REJECTED",
                member_count=len(initialized_tree),
                process_tokens=initialized_tokens,
                violation_count=len(ownership_violations),
            )
            result = await self._finalize(ProcessEndpoint.OWNERSHIP_FAILURE)
            raise OwnedProtocolSessionError(
                "protocol worker tree ownership rejected after READY", result
            )
        self._event(
            "PROCESS_TREE_OWNERSHIP_VERIFIED",
            member_count=len(initialized_tree),
            process_tokens=initialized_tokens,
        )
        self._started = True
        self._event("PROCESS_INITIALIZED", marker_required=True, exact_line=True)
        self._watchdog_task = asyncio.create_task(self._whole_run_watchdog())
        return self

    async def call(
        self,
        command: bytes | str,
        *,
        terminal_marker: bytes | str,
        timeout_seconds: float,
    ) -> ProtocolCallResult:
        """Write prebuilt bytes once and wait for one unique terminal line."""

        payload = _as_bytes(command)
        if not payload:
            raise ValueError("protocol command must be non-empty")
        marker = _protocol_line(terminal_marker, "terminal_marker")
        if marker == self.ready_line:
            raise ValueError("terminal_marker must differ from ready_line")
        if (
            not isinstance(timeout_seconds, (int, float))
            or isinstance(timeout_seconds, bool)
            or not math.isfinite(float(timeout_seconds))
            or float(timeout_seconds) <= 0
        ):
            raise ValueError("timeout_seconds must be finite and positive")

        # Do not queue a second already-authorized host call behind the first:
        # an ObservedCallExecutor authorizes immediately before entering this
        # callable, so queued sends would separate authorization from I/O.
        if self._call_lock.locked():
            raise RuntimeError("concurrent protocol calls are not permitted")
        async with self._call_lock:
            if not self._started or self._process is None:
                raise RuntimeError("owned protocol session is not initialized")
            if self._result is not None:
                raise OwnedProtocolSessionError(
                    "owned protocol session is closed", self._result
                )
            if marker in self._used_markers:
                raise ValueError("terminal_marker must be unique within the session")
            self._used_markers.add(marker)

            call_id = f"protocol-call-{self._next_call:06d}"
            self._next_call += 1
            now = float(self.clock())
            if now >= self._deadline:
                self._event("PROCESS_WHOLE_RUN_TIMEOUT", phase="before_call")
                result = await self._finalize(ProcessEndpoint.WHOLE_RUN_TIMEOUT)
                raise OwnedProtocolCallTimedOut(call_id, result, whole_run=True)

            loop = asyncio.get_running_loop()
            command_hash = hashlib.sha256(payload).hexdigest()
            active = _ActiveProtocolCall(
                call_id,
                marker,
                loop.create_future(),
                _CaptureAccumulator(self.limits.output_limit_bytes),
                command_hash,
                now,
            )
            self._active_call = active
            self._event(
                "PROTOCOL_CALL_SEND_STARTED",
                call_id=call_id,
                command_hash=command_hash,
                command_size_bytes=len(payload),
                terminal_marker_hash=hashlib.sha256(marker).hexdigest(),
            )
            try:
                assert self._process.stdin is not None
                self._process.stdin.write(payload)
                send_bound = min(
                    float(timeout_seconds),
                    max(0.0, self._deadline - float(self.clock())),
                )
                await asyncio.wait_for(
                    self._process.stdin.drain(), timeout=send_bound
                )
                self._event("PROTOCOL_CALL_BYTES_SENT", call_id=call_id)

                remaining = max(0.0, self._deadline - float(self.clock()))
                effective_timeout = min(float(timeout_seconds), remaining)
                whole_bound = remaining <= float(timeout_seconds)
                try:
                    await asyncio.wait_for(
                        asyncio.shield(active.marker_future),
                        timeout=effective_timeout,
                    )
                except TimeoutError:
                    endpoint = (
                        ProcessEndpoint.WHOLE_RUN_TIMEOUT
                        if whole_bound
                        else ProcessEndpoint.CALL_TIMEOUT
                    )
                    self._event(
                        "PROCESS_WHOLE_RUN_TIMEOUT"
                        if whole_bound
                        else "PROTOCOL_CALL_TIMEOUT",
                        call_id=call_id,
                    )
                    active.marker_future.cancel()
                    result = await self._finalize(endpoint)
                    raise OwnedProtocolCallTimedOut(
                        call_id,
                        result,
                        whole_run=whole_bound,
                    )
                except _ProtocolStreamEnded as exc:
                    # A concurrently firing whole-run watchdog finalizes first.
                    result = self._result or await self._finalize(
                        ProcessEndpoint.FAILED
                    )
                    if result.endpoint is ProcessEndpoint.WHOLE_RUN_TIMEOUT:
                        raise OwnedProtocolCallTimedOut(
                            call_id, result, whole_run=True
                        ) from exc
                    raise OwnedProtocolSessionError(
                        "protocol stream ended before terminal marker", result
                    ) from exc

                duration = max(0.0, float(self.clock()) - active.started)
                self._event(
                    "PROTOCOL_CALL_TERMINAL_OBSERVED",
                    call_id=call_id,
                    duration_seconds=duration,
                )
                return ProtocolCallResult(
                    call_id,
                    active.command_hash,
                    hashlib.sha256(active.marker).hexdigest(),
                    duration,
                    active.output.snapshot(),
                )
            except OwnedProtocolCallTimedOut:
                raise
            except OwnedProtocolSessionError:
                raise
            except asyncio.CancelledError:
                self._event(
                    "PROCESS_TASK_CANCELLED",
                    phase="protocol_call",
                    call_id=call_id,
                )
                active.marker_future.cancel()
                await self._finalize(ProcessEndpoint.CANCELLED)
                # Re-raise the same cancellation.  In particular, this lets
                # an enclosing ``asyncio.timeout`` in ObservedCallExecutor
                # translate its own cancellation into a typed call timeout.
                raise
            except Exception as exc:
                self._event(
                    "PROCESS_PROTOCOL_FAILURE",
                    phase="protocol_call",
                    call_id=call_id,
                    error_class=type(exc).__name__,
                )
                active.marker_future.cancel()
                result = await self._finalize(ProcessEndpoint.FAILED)
                raise OwnedProtocolSessionError(
                    "protocol command failed", result
                ) from exc
            finally:
                if self._active_call is active:
                    self._active_call = None

    async def close(self) -> OwnedProcessResult:
        """Gracefully close, then boundedly TERM/KILL and reap the owned tree."""

        if not self._start_attempted:
            raise RuntimeError("owned protocol session was never started")
        if self._result is not None:
            return self._result
        async with self._call_lock:
            if self._result is not None:
                return self._result
            return await self._finalize(ProcessEndpoint.SUCCEEDED)

    async def wait_closed(self) -> OwnedProcessResult:
        """Wait for normal close or the bounded whole-run watchdog."""

        if self._closed_future is None:
            raise RuntimeError("owned protocol session was never started")
        return await asyncio.shield(self._closed_future)

    async def __aenter__(self) -> "OwnedLineProtocolSession":
        return await self.start()

    async def __aexit__(self, exc_type: object, exc: object, traceback: object) -> None:
        if self._result is not None:
            return
        if isinstance(exc, asyncio.CancelledError):
            result = await self._finalize(ProcessEndpoint.CANCELLED)
            raise OwnedProcessCancelled(result)
        await self._finalize(
            ProcessEndpoint.SUCCEEDED if exc is None else ProcessEndpoint.FAILED
        )

    async def _whole_run_watchdog(self) -> None:
        try:
            remaining = max(0.0, self._deadline - float(self.clock()))
            await asyncio.sleep(remaining)
            if self._result is None:
                self._event("PROCESS_WHOLE_RUN_TIMEOUT", phase="watchdog")
                await self._finalize(ProcessEndpoint.WHOLE_RUN_TIMEOUT)
        except asyncio.CancelledError:
            return

    async def _pump_stdout(self, stream: asyncio.StreamReader) -> None:
        pending = bytearray()
        try:
            while True:
                chunk = await stream.read(65536)
                if not chunk:
                    break
                self._stdout_capture.update(chunk)
                pending.extend(chunk)
                if len(pending) > self._MAX_PROTOCOL_LINE_BYTES:
                    raise RuntimeError("protocol_stdout_line_exceeds_bound")
                while True:
                    newline = pending.find(b"\n")
                    if newline < 0:
                        break
                    raw_line = bytes(pending[: newline + 1])
                    del pending[: newline + 1]
                    self._observe_protocol_line(raw_line)
            if pending:
                active = self._active_call
                if active is not None:
                    active.output.update(bytes(pending))
            reason = _ProtocolStreamEnded("protocol stdout reached EOF")
            self._fail_protocol_waiters(reason)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._fail_protocol_waiters(exc)
            raise

    def _observe_protocol_line(self, raw_line: bytes) -> None:
        normalized = raw_line[:-1]
        if normalized.endswith(b"\r"):
            normalized = normalized[:-1]
        ready = self._ready_future
        if ready is not None and not ready.done() and normalized == self.ready_line:
            ready.set_result(None)
            return
        active = self._active_call
        if active is None:
            return
        if normalized == active.marker:
            if not active.marker_future.done():
                active.marker_future.set_result(None)
            return
        active.output.update(raw_line)

    def _fail_protocol_waiters(self, exc: BaseException) -> None:
        ready = self._ready_future
        if ready is not None and not ready.done():
            ready.set_exception(exc)
        active = self._active_call
        if active is not None and not active.marker_future.done():
            active.marker_future.set_exception(exc)

    def _cancel_ready_waiter(self) -> None:
        ready = self._ready_future
        if ready is not None and not ready.done():
            ready.cancel()

    async def _finalize(self, requested: ProcessEndpoint) -> OwnedProcessResult:
        async with self._finalize_lock:
            if self._result is not None:
                return self._result

            # The deadline is created once and is deliberately retained if an
            # enclosing timeout cancels this finalizer and a recovery close
            # retries it.  Cleanup therefore cannot renew its own budget.
            if self._cleanup_deadline is None:
                self._cleanup_deadline = _CleanupDeadline(
                    self.limits.cleanup_timeout_seconds
                )
            cleanup_deadline = self._cleanup_deadline

            watchdog = self._watchdog_task
            current = asyncio.current_task()
            if watchdog is not None and watchdog is not current and not watchdog.done():
                watchdog.cancel()
                await asyncio.gather(watchdog, return_exceptions=True)

            process = self._process
            if process is not None:
                for item in _verified_scan_owned_identities(
                    process.pid,
                    self._root_identity,
                ):
                    self._observed[(item.pid, item.starttime)] = item
                cleanup_needed = (
                    process.returncode is None
                    or bool(_live_group_identities(process.pid))
                )
                if cleanup_needed:
                    await _bounded_cleanup(
                        process,
                        process.pid,
                        limits=self.limits,
                        graceful_shutdown=self.close_command,
                        wait_task=self._wait_task,
                        event=self._event,
                        errors=self._cleanup_errors,
                        expected_identities=frozenset(self._observed),
                        observed_identities=self._observed,
                        expected_root_identity=self._root_identity,
                        cleanup_deadline=cleanup_deadline,
                    )
                if self._wait_task is not None and not self._wait_task.done():
                    remaining = cleanup_deadline.remaining_seconds(
                        self.limits.kill_grace_seconds
                    )
                    if remaining <= 0:
                        cleanup_deadline.record_exceeded(
                            "root_reap",
                            event=self._event,
                            errors=self._cleanup_errors,
                        )
                        self._cleanup_errors.append("owned_root_not_reaped")
                        self._event("PROCESS_ROOT_REAP_TIMEOUT")
                    else:
                        try:
                            await asyncio.wait_for(
                                asyncio.shield(self._wait_task),
                                timeout=remaining,
                            )
                        except TimeoutError:
                            self._cleanup_errors.append("owned_root_not_reaped")
                            self._event("PROCESS_ROOT_REAP_TIMEOUT")
                            if cleanup_deadline.remaining_seconds() <= 0:
                                cleanup_deadline.record_exceeded(
                                    "root_reap",
                                    event=self._event,
                                    errors=self._cleanup_errors,
                                )
                for item in _verified_scan_owned_identities(
                    process.pid,
                    self._root_identity,
                ):
                    self._observed[(item.pid, item.starttime)] = item

            if self._stdout_task is not None:
                await _finish_void_task(
                    self._stdout_task,
                    self.limits.kill_grace_seconds,
                    "stdout",
                    self._cleanup_errors,
                    cleanup_deadline=cleanup_deadline,
                    event=self._event,
                )
            stderr = await _finish_capture(
                self._stderr_task,
                self.limits.kill_grace_seconds,
                "stderr",
                self._cleanup_errors,
                cleanup_deadline=cleanup_deadline,
                event=self._event,
            )

            orphaned: list[_ProcIdentity] = []
            for identity in self._observed.values():
                current_identity = _read_proc_identity(identity.pid)
                if (
                    current_identity is not None
                    and current_identity.starttime == identity.starttime
                ):
                    orphaned.append(identity)
            if process is not None:
                known = {(item.pid, item.starttime) for item in orphaned}
                for identity in _live_group_identities(process.pid):
                    key = (identity.pid, identity.starttime)
                    if key in self._observed and key not in known:
                        orphaned.append(identity)
                        known.add(key)

            orphan_tokens = tuple(
                sorted(item.token(self._nonce) for item in orphaned)
            )
            endpoint = requested
            returncode = None if process is None else process.returncode
            if endpoint is ProcessEndpoint.SUCCEEDED and returncode != 0:
                endpoint = ProcessEndpoint.FAILED
            if orphan_tokens:
                self._cleanup_errors.append("owned_process_orphan_detected")
                endpoint = ProcessEndpoint.ORPHAN_DETECTED
                self._event("PROCESS_ORPHAN_DETECTED", orphan_count=len(orphan_tokens))
            elif self._cleanup_errors and endpoint in {
                ProcessEndpoint.SUCCEEDED,
                ProcessEndpoint.FAILED,
                ProcessEndpoint.OWNERSHIP_FAILURE,
            }:
                endpoint = ProcessEndpoint.CLEANUP_FAILURE

            self._event(
                "PROCESS_EVIDENCE_FINALIZED",
                endpoint=endpoint.value,
                cleanup_ok=not self._cleanup_errors and not orphan_tokens,
            )
            self._result = OwnedProcessResult(
                endpoint=endpoint,
                returncode=returncode,
                command_hash=self.command_hash,
                environment_hash=self.environment_hash,
                executable_name=Path(self.argv[0]).name,
                root_process_token=self._root_token,
                session_owned=self._session_owned,
                duration_seconds=max(0.0, float(self.clock()) - self._started_at),
                lifecycle=tuple(self._lifecycle),
                observed_process_tokens=tuple(
                    sorted(item.token(self._nonce) for item in self._observed.values())
                ),
                orphan_process_tokens=orphan_tokens,
                stdout=self._stdout_capture.snapshot(),
                stderr=stderr,
                cleanup_errors=tuple(self._cleanup_errors),
                runner_version=OWNED_LINE_PROTOCOL_RUNNER_VERSION,
            )
            ended = _ProtocolStreamEnded(
                f"owned protocol session finalized as {endpoint.value}"
            )
            self._fail_protocol_waiters(ended)
            if self._closed_future is not None and not self._closed_future.done():
                self._closed_future.set_result(self._result)
            return self._result

    def _event(self, event_type: str, **detail: Any) -> None:
        self._lifecycle.append(
            MappingProxyType(
                {
                    "sequence": len(self._lifecycle),
                    "schema_version": PROCESS_LIFECYCLE_SCHEMA_VERSION,
                    "event_type": event_type,
                    "monotonic_seconds": max(
                        0.0, float(self.clock()) - self._started_at
                    ),
                    **detail,
                }
            )
        )


async def _bounded_cleanup(
    process: asyncio.subprocess.Process,
    pgid: int,
    *,
    limits: ProcessLimits,
    graceful_shutdown: bytes | None,
    wait_task: asyncio.Task[int] | None,
    event: Any,
    errors: list[str],
    expected_identities: frozenset[tuple[int, int]],
    observed_identities: dict[tuple[int, int], _ProcIdentity],
    expected_root_identity: tuple[int, int] | None,
    cleanup_deadline: _CleanupDeadline,
) -> None:
    event(
        "PROCESS_CLEANUP_STARTED",
        cleanup_timeout_seconds=cleanup_deadline.timeout_seconds,
    )
    await _cleanup_verified_outside_group(
        pgid,
        expected_identities,
        limits=limits,
        event=event,
        errors=errors,
        cleanup_deadline=cleanup_deadline,
    )
    if graceful_shutdown and process.returncode is None and process.stdin is not None:
        remaining = cleanup_deadline.remaining_seconds(
            limits.graceful_close_seconds
        )
        if remaining <= 0:
            cleanup_deadline.record_exceeded(
                "stdin_graceful_close", event=event, errors=errors
            )
        try:
            process.stdin.write(graceful_shutdown)
            if remaining > 0:
                await asyncio.wait_for(process.stdin.drain(), timeout=remaining)
            process.stdin.close()
            event("PROCESS_GRACEFUL_CLOSE_REQUESTED")
        except Exception as exc:
            errors.append(f"graceful_close_failed:{type(exc).__name__}")
            event(
                "PROCESS_GRACEFUL_CLOSE_FAILED",
                error_class=type(exc).__name__,
            )
    if await _wait_owned_empty(
        process,
        pgid,
        wait_task,
        limits.graceful_close_seconds,
        cleanup_deadline=cleanup_deadline,
        event=event,
        errors=errors,
        phase="graceful",
    ):
        event("PROCESS_CLEANUP_COMPLETED", escalation="graceful")
        return

    # Re-snapshot exactly once at the escalation boundary.  A vanished group
    # with a pending asyncio root wait is a reap-propagation state, not
    # evidence that a live process needs a signal.
    group_members = _live_group_identities(pgid)
    if not group_members:
        reaped = await _reap_root_after_group_exit(
            process,
            wait_task,
            cleanup_deadline=cleanup_deadline,
            event=event,
            errors=errors,
            phase="post_graceful",
        )
        if reaped:
            event("PROCESS_CLEANUP_COMPLETED", escalation="graceful_reap")
        else:
            event("PROCESS_CLEANUP_INCOMPLETE", phase="empty_group_root_reap")
        return

    if not _safe_owned_group(
        pgid, expected_identities, identities=group_members
    ):
        errors.append("owned_process_group_identity_lost")
        event("PROCESS_GROUP_SIGNAL_REJECTED", signal="SIGTERM")
        await _terminate_root_only(
            process,
            limits=limits,
            cleanup_deadline=cleanup_deadline,
            event=event,
            errors=errors,
            expected_root_identity=expected_root_identity,
        )
        return
    try:
        os.killpg(pgid, signal.SIGTERM)
        event("PROCESS_GROUP_SIGNALLED", signal="SIGTERM")
    except ProcessLookupError:
        pass
    except Exception as exc:
        errors.append(f"sigterm_failed:{type(exc).__name__}")
        event("PROCESS_GROUP_SIGNAL_FAILED", signal="SIGTERM", error_class=type(exc).__name__)
    if await _wait_owned_empty(
        process,
        pgid,
        wait_task,
        limits.terminate_grace_seconds,
        cleanup_deadline=cleanup_deadline,
        event=event,
        errors=errors,
        phase="sigterm",
    ):
        event("PROCESS_CLEANUP_COMPLETED", escalation="SIGTERM")
        return

    if await _kill_verified_descendant_tree(
        process,
        pgid,
        wait_task,
        limits=limits,
        cleanup_deadline=cleanup_deadline,
        event=event,
        errors=errors,
        observed_identities=observed_identities,
        expected_root_identity=expected_root_identity,
    ):
        event("PROCESS_CLEANUP_COMPLETED", escalation="SIGKILL")
    else:
        event("PROCESS_CLEANUP_INCOMPLETE")


async def _kill_verified_descendant_tree(
    process: asyncio.subprocess.Process,
    pgid: int,
    wait_task: asyncio.Task[int] | None,
    *,
    limits: ProcessLimits,
    cleanup_deadline: _CleanupDeadline,
    event: Any,
    errors: list[str],
    observed_identities: dict[tuple[int, int], _ProcIdentity],
    expected_root_identity: tuple[int, int] | None,
) -> bool:
    """KILL verified descendants while retaining the subreaper anchor.

    Killing the PGID would kill the supervisor first and make a child that
    calls ``setsid()`` during TERM undiscoverable.  Instead, retain the
    verified supervisor, repeatedly rescan its complete descendant tree, and
    signal every exact PID/start-time identity except the supervisor.  The
    supervisor exits only after the kernel reports that no adopted descendant
    remains, so its disappearance is the completion acknowledgement.
    """

    deadline = min(
        time.monotonic() + limits.kill_grace_seconds,
        cleanup_deadline.deadline_monotonic_seconds,
    )
    signalled: set[tuple[int, int]] = set()
    while True:
        if wait_task is not None and wait_task.done():
            wait_task.result()
        if not _same_root_identity(pgid, expected_root_identity):
            survivors = []
            for identity in observed_identities.values():
                current = _read_proc_identity(identity.pid)
                if (
                    current is not None
                    and current.starttime == identity.starttime
                ):
                    survivors.append(identity)
            if survivors:
                errors.append("owned_root_identity_lost_with_survivors")
                event(
                    "PROCESS_DESCENDANT_KILL_INCOMPLETE",
                    survivor_count=len(survivors),
                    reason="root_identity_lost",
                )
                return False
            return await _reap_root_after_group_exit(
                process,
                wait_task,
                cleanup_deadline=cleanup_deadline,
                event=event,
                errors=errors,
                phase="post_descendant_sigkill",
            )

        identities = _verified_scan_owned_identities(
            pgid,
            expected_root_identity,
        )
        for identity in identities:
            observed_identities[(identity.pid, identity.starttime)] = identity
        newly_signalled = 0
        for identity in identities:
            key = (identity.pid, identity.starttime)
            if key == expected_root_identity or key in signalled:
                continue
            signalled.add(key)
            try:
                if _signal_verified_identity(identity, signal.SIGKILL):
                    newly_signalled += 1
            except ProcessLookupError:
                pass
            except Exception as exc:
                errors.append(
                    f"descendant_sigkill_failed:{type(exc).__name__}"
                )
                event(
                    "PROCESS_DESCENDANT_SIGNAL_FAILED",
                    signal="SIGKILL",
                    error_class=type(exc).__name__,
                )
        if newly_signalled:
            event(
                "PROCESS_DESCENDANTS_SIGNALLED",
                signal="SIGKILL",
                member_count=newly_signalled,
            )
        if time.monotonic() >= deadline:
            errors.append("owned_descendant_tree_not_empty_after_sigkill")
            event("PROCESS_DESCENDANT_KILL_INCOMPLETE", reason="deadline")
            if cleanup_deadline.remaining_seconds() <= 0:
                cleanup_deadline.record_exceeded(
                    "descendant_sigkill_wait", event=event, errors=errors
                )
            return False
        await asyncio.sleep(
            min(0.01, max(0.0, deadline - time.monotonic()))
        )


async def _cleanup_verified_outside_group(
    pgid: int,
    expected_identities: frozenset[tuple[int, int]],
    *,
    limits: ProcessLimits,
    event: Any,
    errors: list[str],
    cleanup_deadline: _CleanupDeadline,
) -> None:
    """Boundedly reap sampled descendants that escaped the owned PGID.

    This is a fail-closed defence, not process-name matching.  Each PID is
    signalled only while its procfs start time still matches a descendant
    identity captured by the owner, so PID reuse cannot widen cleanup.
    """

    def live_escapees() -> tuple[_ProcIdentity, ...]:
        current: list[_ProcIdentity] = []
        for pid, starttime in expected_identities:
            identity = _read_proc_identity(pid)
            if (
                identity is not None
                and identity.starttime == starttime
                and identity.pgrp != pgid
            ):
                current.append(identity)
        return tuple(current)

    async def wait_empty(timeout: float) -> bool:
        deadline = min(
            time.monotonic() + timeout,
            cleanup_deadline.deadline_monotonic_seconds,
        )
        while time.monotonic() < deadline:
            if not live_escapees():
                return True
            await asyncio.sleep(min(0.02, max(0.0, deadline - time.monotonic())))
        return not live_escapees()

    escapees = live_escapees()
    if not escapees:
        return
    for identity in escapees:
        try:
            _signal_verified_identity(identity, signal.SIGTERM)
        except ProcessLookupError:
            pass
        except Exception as exc:
            errors.append(f"escaped_sigterm_failed:{type(exc).__name__}")
    event(
        "PROCESS_ESCAPED_MEMBERS_SIGNALLED",
        signal="SIGTERM",
        member_count=len(escapees),
    )
    if await wait_empty(limits.terminate_grace_seconds):
        return

    escapees = live_escapees()
    for identity in escapees:
        try:
            _signal_verified_identity(identity, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except Exception as exc:
            errors.append(f"escaped_sigkill_failed:{type(exc).__name__}")
    event(
        "PROCESS_ESCAPED_MEMBERS_SIGNALLED",
        signal="SIGKILL",
        member_count=len(escapees),
    )
    if not await wait_empty(limits.kill_grace_seconds):
        errors.append("verified_escaped_members_not_reaped")
        event("PROCESS_ESCAPED_MEMBERS_CLEANUP_INCOMPLETE")
        if cleanup_deadline.remaining_seconds() <= 0:
            cleanup_deadline.record_exceeded(
                "escaped_descendants", event=event, errors=errors
            )


async def _terminate_root_only(
    process: asyncio.subprocess.Process,
    *,
    limits: ProcessLimits,
    cleanup_deadline: _CleanupDeadline,
    event: Any,
    errors: list[str],
    expected_root_identity: tuple[int, int] | None,
) -> None:
    """Fail-safe for a lost group identity; never signal an uncertain group."""

    if process.returncode is not None:
        return
    if not _same_root_identity(process.pid, expected_root_identity):
        errors.append("owned_root_identity_lost_before_signal")
        event("PROCESS_ROOT_SIGNAL_REJECTED", signal="SIGTERM")
        return
    try:
        root = _read_proc_identity(process.pid)
        if root is None or not _signal_verified_identity(root, signal.SIGTERM):
            raise ProcessLookupError
        event("PROCESS_ROOT_SIGNALLED", signal="SIGTERM")
        remaining = cleanup_deadline.remaining_seconds(
            limits.terminate_grace_seconds
        )
        if remaining <= 0:
            raise TimeoutError
        await asyncio.wait_for(process.wait(), timeout=remaining)
    except TimeoutError:
        if not _same_root_identity(process.pid, expected_root_identity):
            errors.append("owned_root_identity_lost_before_kill")
            event("PROCESS_ROOT_SIGNAL_REJECTED", signal="SIGKILL")
            return
        try:
            root = _read_proc_identity(process.pid)
            if root is None or not _signal_verified_identity(root, signal.SIGKILL):
                raise ProcessLookupError
            event("PROCESS_ROOT_SIGNALLED", signal="SIGKILL")
            remaining = cleanup_deadline.remaining_seconds(
                limits.kill_grace_seconds
            )
            if remaining <= 0:
                cleanup_deadline.record_exceeded(
                    "root_sigkill_wait", event=event, errors=errors
                )
                raise TimeoutError
            await asyncio.wait_for(process.wait(), timeout=remaining)
        except Exception as exc:
            errors.append(f"root_kill_failed:{type(exc).__name__}")
            if cleanup_deadline.remaining_seconds() <= 0:
                cleanup_deadline.record_exceeded(
                    "root_sigkill_wait", event=event, errors=errors
                )
    except Exception as exc:
        errors.append(f"root_terminate_failed:{type(exc).__name__}")


async def _reap_root_after_group_exit(
    process: asyncio.subprocess.Process,
    wait_task: asyncio.Task[int] | None,
    *,
    cleanup_deadline: _CleanupDeadline,
    event: Any,
    errors: list[str],
    phase: str,
) -> bool:
    """Boundedly observe root wait/returncode after an empty-group snapshot."""

    if process.returncode is not None:
        return True
    if wait_task is None:
        if "owned_root_wait_task_missing_after_group_exit" not in errors:
            errors.append("owned_root_wait_task_missing_after_group_exit")
        event(
            "PROCESS_EMPTY_GROUP_ROOT_REAP_FAILED",
            phase=phase,
            reason="wait_task_missing",
        )
        return False

    remaining = cleanup_deadline.remaining_seconds()
    event(
        "PROCESS_EMPTY_GROUP_ROOT_REAP_STARTED",
        phase=phase,
        remaining_seconds=remaining,
    )
    if remaining <= 0:
        cleanup_deadline.record_exceeded(
            "empty_group_root_reap", event=event, errors=errors
        )
        if "owned_root_reap_timeout_after_group_exit" not in errors:
            errors.append("owned_root_reap_timeout_after_group_exit")
        event("PROCESS_EMPTY_GROUP_ROOT_REAP_TIMEOUT", phase=phase)
        return False
    try:
        returncode = await asyncio.wait_for(
            asyncio.shield(wait_task), timeout=remaining
        )
    except TimeoutError:
        if "owned_root_reap_timeout_after_group_exit" not in errors:
            errors.append("owned_root_reap_timeout_after_group_exit")
        event("PROCESS_EMPTY_GROUP_ROOT_REAP_TIMEOUT", phase=phase)
        if cleanup_deadline.remaining_seconds() <= 0:
            cleanup_deadline.record_exceeded(
                "empty_group_root_reap", event=event, errors=errors
            )
        return False
    if process.returncode is None:
        if "owned_root_returncode_missing_after_wait" not in errors:
            errors.append("owned_root_returncode_missing_after_wait")
        event(
            "PROCESS_EMPTY_GROUP_ROOT_REAP_FAILED",
            phase=phase,
            reason="returncode_not_observed",
        )
        return False
    event(
        "PROCESS_EMPTY_GROUP_ROOT_REAP_COMPLETED",
        phase=phase,
        returncode=returncode,
    )
    return True


async def _wait_owned_empty(
    process: asyncio.subprocess.Process,
    pgid: int,
    wait_task: asyncio.Task[int] | None,
    timeout: float,
    *,
    cleanup_deadline: _CleanupDeadline,
    event: Any,
    errors: list[str],
    phase: str,
) -> bool:
    deadline = min(
        time.monotonic() + timeout,
        cleanup_deadline.deadline_monotonic_seconds,
    )
    while True:
        if wait_task is not None and wait_task.done():
            # Read the result so task exceptions cannot become unobserved.
            wait_task.result()
        group_members = _live_group_identities(pgid)
        if not group_members:
            return await _reap_root_after_group_exit(
                process,
                wait_task,
                cleanup_deadline=cleanup_deadline,
                event=event,
                errors=errors,
                phase=phase,
            )
        if time.monotonic() >= deadline:
            return False
        await asyncio.sleep(min(0.02, max(0.0, deadline - time.monotonic())))


def _safe_owned_group(
    pgid: int,
    expected_identities: frozenset[tuple[int, int]],
    *,
    identities: tuple[_ProcIdentity, ...] | None = None,
) -> bool:
    if pgid <= 1 or pgid == os.getpgrp():
        return False
    identities = (
        _live_group_identities(pgid) if identities is None else identities
    )
    if not identities:
        return False
    # PID/PGID reuse must never turn cleanup into a broad unrelated kill.  At
    # least one current group member must have the same procfs start-time
    # identity sampled from our tree before cleanup began.
    has_owned_identity = any(
        (item.pid, item.starttime) in expected_identities for item in identities
    )
    return has_owned_identity and all(
        item.pgrp == pgid and item.session == pgid for item in identities
    )


def _same_root_identity(
    root_pid: int,
    expected_root_identity: tuple[int, int] | None,
) -> bool:
    if expected_root_identity is None or expected_root_identity[0] != root_pid:
        return False
    current = _read_proc_identity(root_pid)
    return current is not None and (
        current.pid,
        current.starttime,
    ) == expected_root_identity


def _verified_scan_owned_identities(
    root_pid: int,
    expected_root_identity: tuple[int, int] | None,
) -> tuple[_ProcIdentity, ...]:
    """Rescan only while the persistent supervisor identity still matches."""

    if not _same_root_identity(root_pid, expected_root_identity):
        return ()
    return _scan_owned_identities(root_pid)


def _supervisor_entry_path() -> Path:
    return Path(__file__).resolve().with_name("process_supervisor.py")


def _supervised_command(command: Sequence[str]) -> tuple[str, ...]:
    entry = _supervisor_entry_path()
    if not entry.is_file():
        raise FileNotFoundError(entry)
    return (
        sys.executable,
        "-I",
        "-S",
        os.fspath(entry),
        "--",
        *command,
    )


def _require_launchable_executable(
    executable: str,
    environment: Mapping[str, str],
) -> None:
    if os.path.dirname(executable):
        if not Path(executable).is_file() or not os.access(executable, os.X_OK):
            raise FileNotFoundError(executable)
        return
    if shutil.which(executable, path=environment.get("PATH")) is None:
        raise FileNotFoundError(executable)


def _scan_owned_identities(root_pid: int) -> tuple[_ProcIdentity, ...]:
    all_identities = _all_proc_identities()
    by_pid = {item.pid: item for item in all_identities}
    owned: dict[int, _ProcIdentity] = {
        item.pid: item for item in all_identities if item.pgrp == root_pid
    }
    changed = True
    while changed:
        changed = False
        for item in all_identities:
            if item.pid in owned:
                continue
            if item.ppid in owned or item.ppid == root_pid:
                owned[item.pid] = item
                changed = True
    root = by_pid.get(root_pid)
    if root is not None:
        owned[root.pid] = root
    return tuple(sorted(owned.values(), key=lambda item: item.pid))


def _live_group_identities(pgid: int) -> tuple[_ProcIdentity, ...]:
    return tuple(
        item for item in _all_proc_identities() if item.pgrp == pgid
    )


def _all_proc_identities() -> tuple[_ProcIdentity, ...]:
    proc = Path("/proc")
    if not proc.is_dir():
        return ()
    identities: list[_ProcIdentity] = []
    for entry in proc.iterdir():
        if not entry.name.isdigit():
            continue
        identity = _read_proc_identity(int(entry.name))
        if identity is not None:
            identities.append(identity)
    return tuple(identities)


def _read_proc_identity(pid: int) -> _ProcIdentity | None:
    try:
        text = (Path("/proc") / str(pid) / "stat").read_text()
        tail = text.rsplit(")", 1)[1].split()
        # A zombie cannot execute or retain resources.  Treat it as reaped for
        # bounded group-emptiness/orphan checks while asyncio/subprocess still
        # performs the actual wait(2) on direct children.
        if tail[0] == "Z":
            return None
        return _ProcIdentity(
            pid=pid,
            ppid=int(tail[1]),
            pgrp=int(tail[2]),
            session=int(tail[3]),
            starttime=int(tail[19]),
        )
    except (OSError, IndexError, ValueError):
        return None


def _signal_verified_identity(identity: _ProcIdentity, signum: int) -> bool:
    """Signal one exact process identity through a race-safe Linux pidfd."""

    pidfd_open = getattr(os, "pidfd_open", None)
    pidfd_send_signal = getattr(signal, "pidfd_send_signal", None)
    if pidfd_open is None or pidfd_send_signal is None:
        raise RuntimeError("pidfd_signal_unavailable")
    descriptor = pidfd_open(identity.pid, 0)
    try:
        # Opening first binds the descriptor to one kernel process object.
        # Rechecking start time afterwards rejects a PID that was reused
        # before pidfd_open; later reuse cannot redirect the pidfd signal.
        current = _read_proc_identity(identity.pid)
        if current is None or current.starttime != identity.starttime:
            return False
        pidfd_send_signal(descriptor, signum, None, 0)
        return True
    finally:
        os.close(descriptor)


async def _capture_stream(
    stream: asyncio.StreamReader,
    limit: int,
    *,
    marker: bytes | None = None,
    marker_event: asyncio.Event | None = None,
) -> CapturedOutput:
    digest = hashlib.sha256()
    retained = bytearray()
    total = 0
    marker_tail = b""
    while True:
        chunk = await stream.read(65536)
        if not chunk:
            break
        total += len(chunk)
        digest.update(chunk)
        if len(retained) < limit:
            retained.extend(chunk[: max(0, limit - len(retained))])
        if marker is not None and marker_event is not None and not marker_event.is_set():
            probe = marker_tail + chunk
            if marker in probe:
                marker_event.set()
            marker_tail = probe[-max(0, len(marker) - 1) :] if marker else b""
    return CapturedOutput(bytes(retained), digest.hexdigest(), total, total > limit)


async def _finish_capture(
    task: asyncio.Task[CapturedOutput] | None,
    timeout: float,
    label: str,
    errors: list[str],
    *,
    cleanup_deadline: _CleanupDeadline,
    event: Any,
) -> CapturedOutput:
    empty = CapturedOutput(b"", hashlib.sha256(b"").hexdigest(), 0, False)
    if task is None:
        return empty
    remaining = cleanup_deadline.remaining_seconds(timeout)
    if remaining <= 0:
        cleanup_deadline.record_exceeded(
            f"{label}_capture", event=event, errors=errors
        )
        errors.append(f"{label}_capture_timeout")
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        return empty
    try:
        return await asyncio.wait_for(asyncio.shield(task), timeout=remaining)
    except TimeoutError:
        errors.append(f"{label}_capture_timeout")
        if cleanup_deadline.remaining_seconds() <= 0:
            cleanup_deadline.record_exceeded(
                f"{label}_capture", event=event, errors=errors
            )
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        return empty
    except Exception as exc:
        errors.append(f"{label}_capture_failed:{type(exc).__name__}")
        return empty


async def _finish_void_task(
    task: asyncio.Task[None],
    timeout: float,
    label: str,
    errors: list[str],
    *,
    cleanup_deadline: _CleanupDeadline,
    event: Any,
) -> None:
    remaining = cleanup_deadline.remaining_seconds(timeout)
    if remaining <= 0:
        cleanup_deadline.record_exceeded(
            f"{label}_capture", event=event, errors=errors
        )
        errors.append(f"{label}_capture_timeout")
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        return
    try:
        await asyncio.wait_for(asyncio.shield(task), timeout=remaining)
    except TimeoutError:
        errors.append(f"{label}_capture_timeout")
        if cleanup_deadline.remaining_seconds() <= 0:
            cleanup_deadline.record_exceeded(
                f"{label}_capture", event=event, errors=errors
            )
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    except Exception as exc:
        errors.append(f"{label}_capture_failed:{type(exc).__name__}")


def _as_bytes(value: bytes | str | None) -> bytes | None:
    if value is None:
        return None
    if isinstance(value, bytes):
        return value
    if isinstance(value, str):
        return value.encode("utf-8")
    raise TypeError("process byte payload must be bytes, str, or None")


def _protocol_line(value: bytes | str, label: str) -> bytes:
    raw = _as_bytes(value)
    if not raw or b"\n" in raw or b"\r" in raw or b"\x00" in raw:
        raise ValueError(f"{label} must be one non-empty line without terminators")
    if len(raw) > 512:
        raise ValueError(f"{label} exceeds the 512-byte protocol bound")
    return raw


__all__ = [
    "CapturedOutput",
    "OwnedLineProtocolSession",
    "OwnedProcessCancelled",
    "OwnedProcessResult",
    "OwnedProcessRunner",
    "OwnedProtocolCallTimedOut",
    "OwnedProtocolSessionError",
    "ProcessEndpoint",
    "ProcessLimits",
    "ProtocolCallResult",
]
