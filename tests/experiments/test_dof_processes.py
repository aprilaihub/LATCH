from __future__ import annotations

import asyncio
import hashlib
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

import pytest

from optimizer_pipeline.dof.contracts import EventClass
import optimizer_pipeline.dof.processes as processes_module
import optimizer_pipeline.dof.process_supervisor as supervisor_module
from optimizer_pipeline.dof.processes import (
    OwnedLineProtocolSession,
    OwnedProcessCancelled,
    OwnedProcessRunner,
    OwnedProtocolCallTimedOut,
    OwnedProtocolSessionError,
    ProcessEndpoint,
    ProcessLimits,
)
from optimizer_pipeline.dof.process_registry import create_registry
from optimizer_pipeline.dof.toolcalls import (
    ActualCallBudget,
    CallRule,
    DeclaredCallPlan,
    HostCallTimedOut,
    ObservedCallExecutor,
    default_tool_call_registry,
)


def _limits(**updates) -> ProcessLimits:
    values = {
        # Positive paths must tolerate ordinary interpreter/scheduler load.
        # Individual timeout tests override the relevant bound explicitly.
        "initialization_timeout_seconds": 1.0,
        "whole_run_timeout_seconds": 2.0,
        "graceful_close_seconds": 0.03,
        "terminate_grace_seconds": 0.12,
        "kill_grace_seconds": 0.35,
        "cleanup_timeout_seconds": 0.75,
        "output_limit_bytes": 4096,
    }
    values.update(updates)
    return ProcessLimits(**values)


def _python(code: str) -> list[str]:
    # The test process is launched with the APRIL project Python.  Child
    # fixtures deliberately reuse that exact interpreter.
    return [sys.executable, "-c", code]


def _events(result) -> list[str]:
    return [row["event_type"] for row in result.lifecycle]


def test_supervisor_kernel_live_child_overrides_empty_proc_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(supervisor_module.os, "waitpid", lambda *_args: (0, 0))
    monkeypatch.setattr(
        supervisor_module,
        "_owned_survivor_exists",
        lambda _anchor_pid: False,
    )

    assert supervisor_module._owned_survivors_remain(12345) is True


def test_pidfd_signal_rejects_pid_reuse_before_signal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected = processes_module._ProcIdentity(12345, 1, 2, 2, 100)
    reused = processes_module._ProcIdentity(12345, 999, 888, 888, 200)
    closed: list[int] = []

    monkeypatch.setattr(processes_module.os, "pidfd_open", lambda *_args: 77)
    monkeypatch.setattr(
        processes_module,
        "_read_proc_identity",
        lambda _pid: reused,
    )
    monkeypatch.setattr(processes_module.os, "close", closed.append)
    monkeypatch.setattr(
        processes_module.signal,
        "pidfd_send_signal",
        lambda *_args: (_ for _ in ()).throw(
            AssertionError("reused PID must never be signalled")
        ),
    )

    assert processes_module._signal_verified_identity(expected, signal.SIGKILL) is False
    assert closed == [77]


async def _wait_for_session_event(
    session: OwnedLineProtocolSession,
    event_type: str,
    *,
    timeout_seconds: float = 1.0,
) -> None:
    async with asyncio.timeout(timeout_seconds):
        while not any(
            row.get("event_type") == event_type
            for row in session.lifecycle_snapshot
        ):
            await asyncio.sleep(0.005)


def _line_worker() -> list[str]:
    return _python(
        "import sys,time; "
        "print('READY',flush=True); "
        "\nfor raw in sys.stdin.buffer:\n"
        " line=raw.rstrip(b'\\n')\n"
        " if line == b'CLOSE': break\n"
        " command,marker=line.split(b'|',1)\n"
        " if command == b'HANG': time.sleep(60)\n"
        " sys.stdout.buffer.write(b'RECEIVED:'+line+b'\\n'+marker+b'\\n')\n"
        " sys.stdout.buffer.flush()\n"
    )


def test_cleanup_deadline_overrun_is_typed_and_cancels_capture_wait() -> None:
    async def exercise():
        async def never_finishes():
            await asyncio.Future()

        task = asyncio.create_task(never_finishes())
        deadline = processes_module._CleanupDeadline(0.001)
        await asyncio.sleep(0.01)
        errors: list[str] = []
        events: list[tuple[str, dict]] = []

        def event(event_type: str, **detail) -> None:
            events.append((event_type, detail))

        started = time.monotonic()
        captured = await processes_module._finish_capture(
            task,
            10.0,
            "stdout",
            errors,
            cleanup_deadline=deadline,
            event=event,
        )
        return captured, errors, events, time.monotonic() - started, task

    captured, errors, events, elapsed, task = asyncio.run(exercise())

    assert captured.total_bytes == 0
    assert errors == ["cleanup_deadline_exceeded", "stdout_capture_timeout"]
    assert events == [
        (
            "PROCESS_CLEANUP_DEADLINE_EXCEEDED",
            {
                "phase": "stdout_capture",
                "cleanup_timeout_seconds": 0.001,
            },
        )
    ]
    assert elapsed < 0.2
    assert task.cancelled()


def test_public_environment_hash_never_depends_on_arbitrary_values() -> None:
    first = {
        "DATABASE_URL": "postgres://user:first@private/db",
        "LM_LICENSE_FILE": "first-license-endpoint",
        "PATH": "/first/private/path",
    }
    second = {
        "DATABASE_URL": "postgres://user:second@private/db",
        "LM_LICENSE_FILE": "second-license-endpoint",
        "PATH": "/second/private/path",
    }
    assert processes_module._public_environment_hash(first) == (
        processes_module._public_environment_hash(second)
    )
    second["EXTRA_KEY"] = "anything"
    assert processes_module._public_environment_hash(first) != (
        processes_module._public_environment_hash(second)
    )


def test_target_process_environment_removes_only_queue_controls() -> None:
    source = {
        "PATH": "/tools/bin",
        "HOME": "/private/home",
        "DOF_PROCESS_REGISTRY_DIR": "/registry",
        "DOF_JOB_ID": "job",
        "DOF_ATTEMPT_ID": "attempt",
    }

    target = processes_module.build_target_process_environment(source)

    assert target == {"PATH": "/tools/bin", "HOME": "/private/home"}
    assert source["DOF_JOB_ID"] == "job"


@pytest.mark.skipif(os.name != "posix", reason="POSIX process groups required")
def test_queue_binding_registers_one_shot_supervisor_but_never_reaches_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry = tmp_path / "registry"
    create_registry(registry, job_id="job", attempt_id="attempt")
    for key, value in {
        "DOF_PROCESS_REGISTRY_DIR": os.fspath(registry.resolve()),
        "DOF_JOB_ID": "job",
        "DOF_ATTEMPT_ID": "attempt",
    }.items():
        monkeypatch.setenv(key, value)
    target_environment = {"PATH": os.environ.get("PATH", "")}
    code = (
        "import os;"
        "assert not any(k in os.environ for k in "
        "('DOF_PROCESS_REGISTRY_DIR','DOF_JOB_ID','DOF_ATTEMPT_ID'));"
        "print('READY',flush=True)"
    )
    result = asyncio.run(
        OwnedProcessRunner(_limits()).run(
            _python(code), env=target_environment, ready_marker="READY"
        )
    )
    records = list((registry / "records").glob("*.json"))
    assert result.endpoint is ProcessEndpoint.SUCCEEDED
    assert result.environment_hash == processes_module._public_environment_hash(
        target_environment
    )
    assert len(records) == 1
    assert '"state": "REAPED"' in records[0].read_text()


@pytest.mark.skipif(os.name != "posix", reason="POSIX process groups required")
def test_queue_binding_registers_line_protocol_supervisor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry = tmp_path / "registry"
    create_registry(registry, job_id="job", attempt_id="attempt")
    for key, value in {
        "DOF_PROCESS_REGISTRY_DIR": os.fspath(registry.resolve()),
        "DOF_JOB_ID": "job",
        "DOF_ATTEMPT_ID": "attempt",
    }.items():
        monkeypatch.setenv(key, value)

    async def exercise():
        session = OwnedLineProtocolSession(
            _line_worker(),
            ready_line="READY",
            close_command=b"CLOSE\n",
            limits=_limits(),
            env={"PATH": os.environ.get("PATH", "")},
        )
        await session.start()
        return await session.close()

    result = asyncio.run(exercise())
    records = list((registry / "records").glob("*.json"))
    assert result.endpoint is ProcessEndpoint.SUCCEEDED
    assert len(records) == 1
    assert '"state": "REAPED"' in records[0].read_text()


def test_incomplete_queue_binding_fails_before_target_spawn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for key in processes_module.QUEUE_CONTROL_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("DOF_JOB_ID", "job")
    marker = tmp_path / "target-started"
    command = _python(f"import pathlib;pathlib.Path({os.fspath(marker)!r}).touch()")
    with pytest.raises(ValueError, match="incomplete queue process-registry binding"):
        asyncio.run(OwnedProcessRunner(_limits()).run(command, env={"PATH": ""}))
    assert not marker.exists()


def test_reused_root_pid_blocks_rescan_and_root_signals(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    launched = processes_module._ProcIdentity(50001, 1, 50001, 50001, 10)
    reused = processes_module._ProcIdentity(50001, 1, 50001, 50001, 11)
    monkeypatch.setattr(
        processes_module, "_read_proc_identity", lambda _pid: reused
    )
    monkeypatch.setattr(
        processes_module,
        "_scan_owned_identities",
        lambda _pid: (_ for _ in ()).throw(
            AssertionError("reused root PID must not authorize a tree rescan")
        ),
    )
    assert processes_module._verified_scan_owned_identities(
        launched.pid, (launched.pid, launched.starttime)
    ) == ()

    class Process:
        pid = launched.pid
        returncode = None
        terminate_calls = 0
        kill_calls = 0

        def terminate(self):
            self.terminate_calls += 1

        def kill(self):
            self.kill_calls += 1

    process = Process()
    events = []
    errors = []

    async def exercise():
        await processes_module._terminate_root_only(
            process,
            limits=_limits(),
            cleanup_deadline=processes_module._CleanupDeadline(0.1),
            event=lambda event_type, **detail: events.append(
                (event_type, detail)
            ),
            errors=errors,
            expected_root_identity=(launched.pid, launched.starttime),
        )

    asyncio.run(exercise())
    assert process.terminate_calls == process.kill_calls == 0
    assert errors == ["owned_root_identity_lost_before_signal"]
    assert events == [("PROCESS_ROOT_SIGNAL_REJECTED", {"signal": "SIGTERM"})]


class _DelayedReapProcess:
    """Minimal process seam for deterministic empty-group reap ordering."""

    def __init__(self) -> None:
        self.pid = 424242
        self.returncode: int | None = None
        self.stdin = None

    def terminate(self) -> None:  # pragma: no cover - forbidden path
        raise AssertionError("empty owned group must not signal the root")

    def kill(self) -> None:  # pragma: no cover - forbidden path
        raise AssertionError("empty owned group must not signal the root")


def test_empty_group_delayed_root_reap_uses_one_deadline_without_signal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def exercise():
        process = _DelayedReapProcess()
        release_reap = asyncio.Event()

        async def delayed_wait() -> int:
            await release_reap.wait()
            process.returncode = 0
            return 0

        wait_task = asyncio.create_task(delayed_wait())
        session = OwnedLineProtocolSession(
            [sys.executable],
            ready_line="READY",
            close_command=b"CLOSE\n",
            limits=_limits(
                graceful_close_seconds=0.02,
                terminate_grace_seconds=0.02,
                kill_grace_seconds=0.02,
                cleanup_timeout_seconds=0.25,
            ),
        )
        session._start_attempted = True
        session._started = True
        session._process = process
        session._wait_task = wait_task
        session._started_at = time.monotonic()
        session._deadline = session._started_at + 1.0
        session._nonce = "deterministic-delayed-reap"
        session._session_owned = True
        cleanup = asyncio.create_task(
            session._finalize(ProcessEndpoint.SUCCEEDED)
        )
        async with asyncio.timeout(0.2):
            while not any(
                row["event_type"] == "PROCESS_EMPTY_GROUP_ROOT_REAP_STARTED"
                for row in session.lifecycle_snapshot
            ):
                await asyncio.sleep(0)
        release_reap.set()
        return await cleanup

    monkeypatch.setattr(processes_module, "_live_group_identities", lambda _pgid: ())
    monkeypatch.setattr(processes_module, "_scan_owned_identities", lambda _pid: ())
    monkeypatch.setattr(processes_module, "_read_proc_identity", lambda _pid: None)
    monkeypatch.setattr(
        processes_module.os,
        "killpg",
        lambda *_args: (_ for _ in ()).throw(
            AssertionError("empty owned group must not be signalled")
        ),
    )

    result = asyncio.run(exercise())
    names = _events(result)
    assert result.endpoint is ProcessEndpoint.SUCCEEDED
    assert result.returncode == 0
    assert result.cleanup_errors == ()
    assert names.count("PROCESS_EMPTY_GROUP_ROOT_REAP_STARTED") == 1
    assert "PROCESS_EMPTY_GROUP_ROOT_REAP_COMPLETED" in names
    assert "PROCESS_GROUP_SIGNALLED" not in names
    assert "PROCESS_ROOT_SIGNALLED" not in names
    assert "PROCESS_CLEANUP_COMPLETED" in names
    assert names[-1] == "PROCESS_EVIDENCE_FINALIZED"


def test_empty_group_unreaped_root_is_bounded_typed_failure_without_signal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def exercise():
        process = _DelayedReapProcess()

        async def never_reaped() -> int:
            await asyncio.Future()

        wait_task = asyncio.create_task(never_reaped())
        session = OwnedLineProtocolSession(
            [sys.executable],
            ready_line="READY",
            close_command=b"CLOSE\n",
            limits=_limits(
                graceful_close_seconds=0.01,
                terminate_grace_seconds=0.01,
                kill_grace_seconds=0.01,
                cleanup_timeout_seconds=0.05,
            ),
        )
        session._start_attempted = True
        session._started = True
        session._process = process
        session._wait_task = wait_task
        session._started_at = time.monotonic()
        session._deadline = session._started_at + 1.0
        session._nonce = "deterministic-unreaped-root"
        session._session_owned = True
        started = time.monotonic()
        result = await session._finalize(ProcessEndpoint.SUCCEEDED)
        elapsed = time.monotonic() - started
        wait_task.cancel()
        await asyncio.gather(wait_task, return_exceptions=True)
        return result, elapsed

    monkeypatch.setattr(processes_module, "_live_group_identities", lambda _pgid: ())
    monkeypatch.setattr(processes_module, "_scan_owned_identities", lambda _pid: ())
    monkeypatch.setattr(processes_module, "_read_proc_identity", lambda _pid: None)
    monkeypatch.setattr(
        processes_module.os,
        "killpg",
        lambda *_args: (_ for _ in ()).throw(
            AssertionError("empty owned group must not be signalled")
        ),
    )

    result, elapsed = asyncio.run(exercise())
    names = _events(result)
    assert result.endpoint is ProcessEndpoint.CLEANUP_FAILURE
    assert result.returncode is None
    assert elapsed < 0.25
    assert "owned_root_reap_timeout_after_group_exit" in result.cleanup_errors
    assert "cleanup_deadline_exceeded" in result.cleanup_errors
    assert "PROCESS_EMPTY_GROUP_ROOT_REAP_TIMEOUT" in names
    assert "PROCESS_CLEANUP_INCOMPLETE" in names
    assert "PROCESS_GROUP_SIGNALLED" not in names
    assert "PROCESS_ROOT_SIGNALLED" not in names


@pytest.mark.skipif(os.name != "posix", reason="POSIX process groups required")
def test_owned_process_normal_exit_records_session_output_and_no_orphan() -> None:
    runner = OwnedProcessRunner(_limits())
    result = asyncio.run(
        runner.run_vivado_batch_worker(
            _python("print('READY', flush=True)"),
            ready_marker="READY",
        )
    )

    assert result.endpoint is ProcessEndpoint.SUCCEEDED
    assert result.returncode == 0
    assert result.session_owned is True
    assert result.root_process_token and len(result.root_process_token) == 64
    assert result.stdout.data == b"READY\n"
    assert result.stdout.sha256 == hashlib.sha256(b"READY\n").hexdigest()
    assert result.cleanup_ok
    assert result.orphan_process_tokens == ()
    assert result.runner_version == "dof.posix-process-owner.v3"
    assert _events(result)[-1] == "PROCESS_EVIDENCE_FINALIZED"
    public = result.to_dict()
    assert "-c" not in repr(public)
    assert "READY" not in repr(public["stdout"])


@pytest.mark.skipif(os.name != "posix", reason="POSIX signal semantics required")
def test_owned_process_preserves_direct_target_signal_returncode() -> None:
    runner = OwnedProcessRunner(_limits())
    code = (
        "import os,signal; "
        "print('READY', flush=True); "
        "os.kill(os.getpid(), signal.SIGTERM)"
    )
    result = asyncio.run(runner.run(_python(code), ready_marker="READY"))

    assert result.endpoint is ProcessEndpoint.FAILED
    assert result.returncode == -signal.SIGTERM
    assert result.cleanup_ok
    assert result.orphan_process_tokens == ()
    assert "PROCESS_ROOT_EXITED" in _events(result)


@pytest.mark.skipif(os.name != "posix", reason="POSIX process groups required")
def test_initialization_hang_has_typed_timeout_and_bounded_cleanup() -> None:
    runner = OwnedProcessRunner(
        _limits(
            initialization_timeout_seconds=0.05,
            whole_run_timeout_seconds=0.3,
        )
    )
    started = time.monotonic()
    result = asyncio.run(
        runner.run(
            _python("import time; time.sleep(60)"),
            ready_marker="READY",
        )
    )

    assert result.endpoint is ProcessEndpoint.INITIALIZATION_TIMEOUT
    assert time.monotonic() - started < 1.5
    assert "PROCESS_INITIALIZATION_TIMEOUT" in _events(result)
    assert result.cleanup_ok
    assert result.orphan_process_tokens == ()


@pytest.mark.skipif(os.name != "posix", reason="POSIX process groups required")
def test_pre_ready_leader_exit_still_cleans_same_session_child(
    tmp_path: Path,
) -> None:
    pid_record = tmp_path / "same-session-child-pid"
    child_code = "import time; time.sleep(60)"
    parent_code = (
        "import pathlib,subprocess,sys,time; "
        f"child=subprocess.Popen([sys.executable,'-c',{child_code!r}]); "
        f"pathlib.Path({os.fspath(pid_record)!r}).write_text(str(child.pid)); "
        "time.sleep(0.05)"
    )
    runner = OwnedProcessRunner(
        _limits(
            initialization_timeout_seconds=0.5,
            whole_run_timeout_seconds=1.0,
        )
    )
    result = asyncio.run(
        runner.run(_python(parent_code), ready_marker="READY")
    )

    child_pid = int(pid_record.read_text())
    deadline = time.monotonic() + 2.0
    while Path(f"/proc/{child_pid}").exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert not Path(f"/proc/{child_pid}").exists()
    assert result.endpoint in {
        ProcessEndpoint.FAILED,
        ProcessEndpoint.INITIALIZATION_TIMEOUT,
    }
    # The direct target exited cleanly before READY.  The persistent
    # supervisor retains that exact target status while it owns and cleans the
    # surviving child rather than fabricating a signal return code.
    assert result.returncode == 0
    assert result.cleanup_ok
    assert result.orphan_process_tokens == ()
    assert len(result.observed_process_tokens) >= 2
    assert {
        "PROCESS_EXITED_BEFORE_INITIALIZATION",
        "PROCESS_INITIALIZATION_TIMEOUT",
    } & set(_events(result))
    assert "PROCESS_GROUP_SIGNALLED" in _events(result)


@pytest.mark.skipif(os.name != "posix", reason="POSIX process groups required")
def test_term_ignoring_child_escalates_to_sigkill_without_orphan() -> None:
    code = (
        "import signal,time; "
        "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        "print('READY', flush=True); time.sleep(60)"
    )
    runner = OwnedProcessRunner(
        _limits(whole_run_timeout_seconds=0.6, terminate_grace_seconds=0.05)
    )
    result = asyncio.run(runner.run(_python(code), ready_marker="READY"))

    assert result.endpoint is ProcessEndpoint.WHOLE_RUN_TIMEOUT
    assert "PROCESS_INITIALIZED" in _events(result)
    signals = [
        (row["event_type"], row.get("signal"))
        for row in result.lifecycle
        if row["event_type"]
        in {"PROCESS_GROUP_SIGNALLED", "PROCESS_DESCENDANTS_SIGNALLED"}
    ]
    assert signals == [
        ("PROCESS_GROUP_SIGNALLED", "SIGTERM"),
        ("PROCESS_DESCENDANTS_SIGNALLED", "SIGKILL"),
    ]
    assert result.cleanup_ok
    assert result.orphan_process_tokens == ()


@pytest.mark.skipif(os.name != "posix", reason="POSIX process groups required")
def test_sigterm_exit_spawned_unknown_child_is_killed_without_orphan(
    tmp_path: Path,
) -> None:
    pid_record = tmp_path / "term-handler-child-pid"
    child_code = (
        "import signal,time; "
        "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        "time.sleep(60)"
    )
    parent_code = (
        "import os,pathlib,signal,subprocess,sys,time\n"
        "def on_term(_signum,_frame):\n"
        " signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        f" child=subprocess.Popen([sys.executable,'-c',{child_code!r}])\n"
        f" pathlib.Path({os.fspath(pid_record)!r}).write_text(str(child.pid))\n"
        " os._exit(0)\n"
        "signal.signal(signal.SIGTERM,on_term)\n"
        "print('READY',flush=True)\n"
        "time.sleep(60)\n"
    )
    runner = OwnedProcessRunner(
        _limits(
            whole_run_timeout_seconds=0.25,
            terminate_grace_seconds=0.12,
            kill_grace_seconds=0.4,
            cleanup_timeout_seconds=1.2,
        )
    )
    result = asyncio.run(runner.run(_python(parent_code), ready_marker="READY"))

    child_pid = int(pid_record.read_text())
    deadline = time.monotonic() + 2.0
    while Path(f"/proc/{child_pid}").exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert not Path(f"/proc/{child_pid}").exists()
    assert result.endpoint is ProcessEndpoint.WHOLE_RUN_TIMEOUT
    assert result.returncode == 0
    assert result.cleanup_ok
    assert result.orphan_process_tokens == ()
    signals = [
        (row["event_type"], row.get("signal"))
        for row in result.lifecycle
        if row["event_type"]
        in {"PROCESS_GROUP_SIGNALLED", "PROCESS_DESCENDANTS_SIGNALLED"}
    ]
    assert signals == [
        ("PROCESS_GROUP_SIGNALLED", "SIGTERM"),
        ("PROCESS_DESCENDANTS_SIGNALLED", "SIGKILL"),
    ]


@pytest.mark.skipif(os.name != "posix", reason="POSIX process groups required")
def test_sigterm_exit_spawned_unknown_setsid_child_is_killed_and_recorded(
    tmp_path: Path,
) -> None:
    pid_record = tmp_path / "term-handler-setsid-child-pid"
    child_code = (
        "import signal,time; "
        "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        "time.sleep(60)"
    )
    parent_code = (
        "import os,pathlib,signal,subprocess,sys,time\n"
        "def on_term(_signum,_frame):\n"
        " signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        f" child=subprocess.Popen([sys.executable,'-c',{child_code!r}],start_new_session=True)\n"
        f" pathlib.Path({os.fspath(pid_record)!r}).write_text(str(child.pid))\n"
        " os._exit(0)\n"
        "signal.signal(signal.SIGTERM,on_term)\n"
        "print('READY',flush=True)\n"
        "time.sleep(60)\n"
    )
    runner = OwnedProcessRunner(
        _limits(
            whole_run_timeout_seconds=0.25,
            terminate_grace_seconds=0.12,
            kill_grace_seconds=0.4,
            cleanup_timeout_seconds=1.2,
        )
    )
    result = asyncio.run(runner.run(_python(parent_code), ready_marker="READY"))

    child_pid = int(pid_record.read_text())
    deadline = time.monotonic() + 2.0
    while Path(f"/proc/{child_pid}").exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert not Path(f"/proc/{child_pid}").exists()
    assert result.endpoint is ProcessEndpoint.WHOLE_RUN_TIMEOUT
    assert result.returncode == 0
    assert result.cleanup_ok
    assert result.orphan_process_tokens == ()
    assert len(result.observed_process_tokens) >= 3
    assert any(
        row["event_type"] == "PROCESS_DESCENDANTS_SIGNALLED"
        and row.get("signal") == "SIGKILL"
        for row in result.lifecycle
    )


@pytest.mark.skipif(os.name != "posix", reason="POSIX process groups required")
def test_child_and_grandchild_are_observed_and_group_cleaned() -> None:
    child = "import time; time.sleep(60)"
    code = (
        "import subprocess,sys,time; "
        f"subprocess.Popen([sys.executable,'-c',{child!r}]); "
        "print('READY', flush=True); time.sleep(60)"
    )
    runner = OwnedProcessRunner(_limits(whole_run_timeout_seconds=0.6))
    result = asyncio.run(runner.run(_python(code), ready_marker="READY"))

    assert result.endpoint is ProcessEndpoint.WHOLE_RUN_TIMEOUT
    assert "PROCESS_INITIALIZED" in _events(result)
    assert len(result.observed_process_tokens) >= 2
    assert result.orphan_process_tokens == ()
    assert result.cleanup_ok


@pytest.mark.skipif(os.name != "posix", reason="POSIX process groups required")
def test_whole_run_timeout_drains_and_bounds_large_output() -> None:
    code = (
        "import sys,time; print('READY', flush=True); "
        "sys.stdout.write('x'*10000); sys.stdout.flush(); time.sleep(60)"
    )
    runner = OwnedProcessRunner(
        _limits(whole_run_timeout_seconds=0.6, output_limit_bytes=128)
    )
    result = asyncio.run(runner.run(_python(code), ready_marker="READY"))

    assert result.endpoint is ProcessEndpoint.WHOLE_RUN_TIMEOUT
    assert "PROCESS_INITIALIZED" in _events(result)
    assert result.stdout.total_bytes >= 10000
    assert len(result.stdout.data) == 128
    assert result.stdout.truncated is True
    assert result.cleanup_ok


@pytest.mark.skipif(os.name != "posix", reason="POSIX process groups required")
def test_task_cancellation_propagates_after_evidence_and_cleanup(
    tmp_path: Path,
) -> None:
    runner = OwnedProcessRunner(_limits(whole_run_timeout_seconds=5.0))
    ready_record = tmp_path / "worker-ready"

    async def run_and_cancel():
        task = asyncio.create_task(
            runner.run(
                _python(
                    "import pathlib,time; "
                    f"pathlib.Path({os.fspath(ready_record)!r}).write_text('READY'); "
                    "print('READY', flush=True); time.sleep(60)"
                ),
                ready_marker="READY",
            )
        )
        async with asyncio.timeout(1.0):
            while not ready_record.exists():
                await asyncio.sleep(0.005)
        task.cancel()
        with pytest.raises(OwnedProcessCancelled) as caught:
            await task
        return caught.value.result

    result = asyncio.run(run_and_cancel())
    assert result.endpoint is ProcessEndpoint.CANCELLED
    assert result.cleanup_ok
    assert result.orphan_process_tokens == ()
    assert "PROCESS_TASK_CANCELLED" in _events(result)
    assert _events(result)[-1] == "PROCESS_EVIDENCE_FINALIZED"


@pytest.mark.skipif(os.name != "posix", reason="POSIX process groups required")
def test_cleanup_protocol_exception_is_recorded_but_group_is_still_killed() -> None:
    # Closing fd 0 makes the parent's protocol-close write fail while leaving
    # the worker alive.  Cleanup must continue to the owned-group signals.
    code = (
        "import os,time; os.close(0); print('READY', flush=True); time.sleep(60)"
    )
    runner = OwnedProcessRunner(_limits(whole_run_timeout_seconds=0.6))
    result = asyncio.run(
        runner.run(
            _python(code),
            ready_marker="READY",
            graceful_shutdown="CLOSE\n",
        )
    )

    assert result.endpoint in {
        ProcessEndpoint.WHOLE_RUN_TIMEOUT,
        ProcessEndpoint.CLEANUP_FAILURE,
    }
    assert result.orphan_process_tokens == ()
    assert _events(result)[-1] == "PROCESS_EVIDENCE_FINALIZED"
    assert "PROCESS_GROUP_SIGNALLED" in _events(result)


@pytest.mark.skipif(os.name != "posix", reason="POSIX process groups required")
def test_timeout_never_signals_an_unrelated_session() -> None:
    unrelated = subprocess.Popen(
        _python("import time; time.sleep(60)"),
        start_new_session=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        runner = OwnedProcessRunner(_limits(whole_run_timeout_seconds=0.6))
        result = asyncio.run(
            runner.run(
                _python("import time; print('READY', flush=True); time.sleep(60)"),
                ready_marker="READY",
            )
        )
        assert result.endpoint is ProcessEndpoint.WHOLE_RUN_TIMEOUT
        assert unrelated.poll() is None
        assert result.cleanup_ok
    finally:
        if unrelated.poll() is None:
            os.killpg(unrelated.pid, signal.SIGKILL)
        unrelated.wait(timeout=2)


@pytest.mark.skipif(os.name != "posix", reason="POSIX process groups required")
def test_persistent_line_session_sends_exact_commands_and_observes_unique_markers(
) -> None:
    async def exercise():
        session = OwnedLineProtocolSession(
            _line_worker(),
            ready_line="READY",
            close_command=b"CLOSE\n",
            limits=_limits(whole_run_timeout_seconds=1.0),
        )
        await session.start()
        first = await session.call(
            b"alpha|DONE-000\n",
            terminal_marker=b"DONE-000",
            timeout_seconds=0.25,
        )
        second = await session.call(
            b"beta|DONE-001\n",
            terminal_marker=b"DONE-001",
            timeout_seconds=0.25,
        )
        final = await session.close()
        return first, second, final

    first, second, final = asyncio.run(exercise())
    assert first.stdout.data == b"RECEIVED:alpha|DONE-000\n"
    assert second.stdout.data == b"RECEIVED:beta|DONE-001\n"
    assert first.command_hash == hashlib.sha256(b"alpha|DONE-000\n").hexdigest()
    assert second.command_hash == hashlib.sha256(b"beta|DONE-001\n").hexdigest()
    assert final.endpoint is ProcessEndpoint.SUCCEEDED
    assert final.returncode == 0
    assert final.cleanup_ok
    assert final.orphan_process_tokens == ()
    events = _events(final)
    assert events.count("PROCESS_STARTED") == 1
    assert events.count("PROTOCOL_CALL_BYTES_SENT") == 2
    assert events.count("PROTOCOL_CALL_TERMINAL_OBSERVED") == 2
    assert events[-1] == "PROCESS_EVIDENCE_FINALIZED"
    assert final.runner_version == "dof.posix-line-protocol-owner.v3"


@pytest.mark.skipif(os.name != "posix", reason="POSIX process groups required")
def test_protocol_close_with_delayed_wait_propagation_never_signals(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_create = processes_module.asyncio.create_subprocess_exec

    class DelayedWaitProcess:
        def __init__(self, delegate):
            self._delegate = delegate
            self.pid = delegate.pid
            self.stdin = delegate.stdin
            self.stdout = delegate.stdout
            self.stderr = delegate.stderr
            self._returncode = None

        @property
        def returncode(self):
            return self._returncode

        async def wait(self):
            returncode = await self._delegate.wait()
            # Force the exact race: /proc group membership has disappeared,
            # but the owner-visible wait result has not propagated yet.
            await asyncio.sleep(0.05)
            self._returncode = returncode
            return returncode

        def terminate(self):
            self._delegate.terminate()

        def kill(self):
            self._delegate.kill()

    async def delayed_create(*args, **kwargs):
        return DelayedWaitProcess(await original_create(*args, **kwargs))

    monkeypatch.setattr(
        processes_module.asyncio,
        "create_subprocess_exec",
        delayed_create,
    )

    async def exercise():
        session = OwnedLineProtocolSession(
            _line_worker(),
            ready_line="READY",
            close_command=b"CLOSE\n",
            limits=_limits(
                graceful_close_seconds=0.02,
                terminate_grace_seconds=0.08,
                kill_grace_seconds=0.12,
                cleanup_timeout_seconds=0.4,
            ),
        )
        await session.start()
        return await session.close()

    result = asyncio.run(exercise())
    events = _events(result)
    assert result.endpoint is ProcessEndpoint.SUCCEEDED
    assert result.returncode == 0
    assert result.cleanup_ok
    assert "PROCESS_EMPTY_GROUP_ROOT_REAP_STARTED" in events
    assert "PROCESS_EMPTY_GROUP_ROOT_REAP_COMPLETED" in events
    assert "PROCESS_GROUP_SIGNALLED" not in events
    assert "PROCESS_ROOT_SIGNALLED" not in events


@pytest.mark.skipif(os.name != "posix", reason="POSIX process groups required")
def test_persistent_line_session_rejects_reused_marker_before_second_send() -> None:
    async def exercise():
        session = OwnedLineProtocolSession(
            _line_worker(),
            ready_line="READY",
            close_command=b"CLOSE\n",
            limits=_limits(whole_run_timeout_seconds=1.0),
        )
        await session.start()
        await session.call(
            b"alpha|DONE\n", terminal_marker="DONE", timeout_seconds=0.2
        )
        with pytest.raises(ValueError, match="unique"):
            await session.call(
                b"beta|DONE\n", terminal_marker="DONE", timeout_seconds=0.2
            )
        return await session.close()

    final = asyncio.run(exercise())
    assert final.endpoint is ProcessEndpoint.SUCCEEDED
    assert _events(final).count("PROTOCOL_CALL_BYTES_SENT") == 1


@pytest.mark.skipif(os.name != "posix", reason="POSIX process groups required")
def test_persistent_line_call_timeout_closes_then_cleans_owned_group() -> None:
    async def exercise():
        session = OwnedLineProtocolSession(
            _line_worker(),
            ready_line="READY",
            close_command=b"CLOSE\n",
            limits=_limits(whole_run_timeout_seconds=1.0),
        )
        await session.start()
        with pytest.raises(OwnedProtocolCallTimedOut) as caught:
            await session.call(
                b"HANG|NEVER\n",
                terminal_marker="NEVER",
                timeout_seconds=0.05,
            )
        return caught.value

    started = time.monotonic()
    failure = asyncio.run(exercise())
    assert time.monotonic() - started < 1.5
    assert failure.whole_run is False
    assert failure.result.endpoint is ProcessEndpoint.CALL_TIMEOUT
    assert failure.result.cleanup_ok
    assert failure.result.orphan_process_tokens == ()
    events = _events(failure.result)
    assert "PROTOCOL_CALL_TIMEOUT" in events
    assert "PROCESS_GRACEFUL_CLOSE_REQUESTED" in events
    assert "PROCESS_GROUP_SIGNALLED" in events
    assert events[-1] == "PROCESS_EVIDENCE_FINALIZED"


@pytest.mark.skipif(os.name != "posix", reason="POSIX process groups required")
def test_persistent_call_timeout_reaps_owned_descendant() -> None:
    child = "import time; time.sleep(60)"
    worker = _python(
        "import subprocess,sys,time; "
        f"subprocess.Popen([sys.executable,'-c',{child!r}]); "
        "print('READY',flush=True); "
        "\nfor raw in sys.stdin.buffer:\n"
        " line=raw.rstrip(b'\\n')\n"
        " if line == b'CLOSE': break\n"
        " time.sleep(60)\n"
    )

    async def exercise():
        session = OwnedLineProtocolSession(
            worker,
            ready_line="READY",
            close_command=b"CLOSE\n",
            limits=_limits(whole_run_timeout_seconds=1.0),
        )
        await session.start()
        with pytest.raises(OwnedProtocolCallTimedOut) as caught:
            await session.call(
                b"HANG|NEVER\n",
                terminal_marker="NEVER",
                timeout_seconds=0.05,
            )
        return caught.value.result

    result = asyncio.run(exercise())
    assert result.endpoint is ProcessEndpoint.CALL_TIMEOUT
    assert len(result.observed_process_tokens) >= 2
    assert result.orphan_process_tokens == ()
    assert result.cleanup_ok


@pytest.mark.skipif(os.name != "posix", reason="POSIX process groups required")
def test_ready_worker_with_setsid_descendant_is_rejected_and_reaped(
    tmp_path: Path,
) -> None:
    pid_record = tmp_path / "escaped-pid"
    child = "import time; time.sleep(60)"
    worker = _python(
        "import pathlib,subprocess,sys,time; "
        f"child=subprocess.Popen([sys.executable,'-c',{child!r}],"
        "start_new_session=True); "
        f"pathlib.Path({os.fspath(pid_record)!r}).write_text(str(child.pid)); "
        "print('READY',flush=True); time.sleep(60)"
    )

    async def exercise():
        session = OwnedLineProtocolSession(
            worker,
            ready_line="READY",
            close_command=b"CLOSE\n",
            limits=_limits(whole_run_timeout_seconds=1.0),
        )
        with pytest.raises(OwnedProtocolSessionError) as caught:
            await session.start()
        return caught.value.result

    result = asyncio.run(exercise())
    escaped_pid = int(pid_record.read_text())
    assert result.endpoint is ProcessEndpoint.OWNERSHIP_FAILURE
    assert result.cleanup_ok
    assert result.orphan_process_tokens == ()
    assert "PROCESS_TREE_OWNERSHIP_REJECTED" in _events(result)
    assert "PROCESS_ESCAPED_MEMBERS_SIGNALLED" in _events(result)
    deadline = time.monotonic() + 1.0
    while Path(f"/proc/{escaped_pid}").exists() and time.monotonic() < deadline:
        time.sleep(0.02)
    assert not Path(f"/proc/{escaped_pid}").exists()


@pytest.mark.skipif(os.name != "posix", reason="POSIX process groups required")
def test_persistent_line_call_is_bounded_by_whole_run_deadline() -> None:
    async def exercise():
        session = OwnedLineProtocolSession(
            _line_worker(),
            ready_line="READY",
            close_command=b"CLOSE\n",
            limits=_limits(whole_run_timeout_seconds=0.8),
        )
        await session.start()
        with pytest.raises(OwnedProtocolCallTimedOut) as caught:
            await session.call(
                b"HANG|NEVER\n",
                terminal_marker="NEVER",
                timeout_seconds=5.0,
            )
        return caught.value

    failure = asyncio.run(exercise())
    assert failure.whole_run is True
    assert failure.result.endpoint is ProcessEndpoint.WHOLE_RUN_TIMEOUT
    assert failure.result.cleanup_ok
    assert failure.result.orphan_process_tokens == ()
    assert "PROCESS_WHOLE_RUN_TIMEOUT" in _events(failure.result)


@pytest.mark.skipif(os.name != "posix", reason="POSIX process groups required")
def test_persistent_idle_session_watchdog_enforces_whole_run_timeout() -> None:
    async def exercise():
        session = OwnedLineProtocolSession(
            _line_worker(),
            ready_line="READY",
            close_command=b"CLOSE\n",
            limits=_limits(whole_run_timeout_seconds=0.6),
        )
        await session.start()
        return await session.wait_closed()

    result = asyncio.run(exercise())
    assert result.endpoint is ProcessEndpoint.WHOLE_RUN_TIMEOUT
    assert result.cleanup_ok
    assert result.orphan_process_tokens == ()
    assert _events(result)[-1] == "PROCESS_EVIDENCE_FINALIZED"


@pytest.mark.skipif(os.name != "posix", reason="POSIX process groups required")
def test_persistent_line_call_cancellation_cleans_before_propagation() -> None:
    async def exercise():
        session = OwnedLineProtocolSession(
            _line_worker(),
            ready_line="READY",
            close_command=b"CLOSE\n",
            limits=_limits(whole_run_timeout_seconds=2.0),
        )
        await session.start()
        task = asyncio.create_task(
            session.call(
                b"HANG|NEVER\n",
                terminal_marker="NEVER",
                timeout_seconds=1.0,
            )
        )
        await _wait_for_session_event(session, "PROTOCOL_CALL_BYTES_SENT")
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert session.result is not None
        return session.result

    result = asyncio.run(exercise())
    assert result.endpoint is ProcessEndpoint.CANCELLED
    assert result.cleanup_ok
    assert result.orphan_process_tokens == ()
    assert "PROCESS_TASK_CANCELLED" in _events(result)
    assert _events(result)[-1] == "PROCESS_EVIDENCE_FINALIZED"


@pytest.mark.skipif(os.name != "posix", reason="POSIX process groups required")
def test_delayed_ready_profile_is_scheduler_tolerant_and_bounded() -> None:
    runner = OwnedProcessRunner(
        _limits(
            initialization_timeout_seconds=1.0,
            whole_run_timeout_seconds=1.5,
        )
    )
    started = time.monotonic()
    result = asyncio.run(
        runner.run(
            _python(
                "import time; time.sleep(0.25); "
                "print('READY', flush=True)"
            ),
            ready_marker="READY",
        )
    )

    assert result.endpoint is ProcessEndpoint.SUCCEEDED
    assert "PROCESS_INITIALIZED" in _events(result)
    assert result.cleanup_ok
    assert result.orphan_process_tokens == ()
    assert time.monotonic() - started < 1.5


@pytest.mark.skipif(os.name != "posix", reason="POSIX process groups required")
def test_missing_executable_is_failed_start_not_cleanup_failure() -> None:
    missing = "/definitely/not/a/real/wp003-worker"
    one_shot = asyncio.run(OwnedProcessRunner(_limits()).run([missing]))
    assert one_shot.endpoint is ProcessEndpoint.FAILED
    assert one_shot.cleanup_ok
    assert one_shot.returncode is None
    assert "PROCESS_START_FAILED" in _events(one_shot)

    async def persistent():
        session = OwnedLineProtocolSession(
            [missing],
            ready_line="READY",
            close_command=b"CLOSE\n",
            limits=_limits(),
        )
        with pytest.raises(OwnedProtocolSessionError) as caught:
            await session.start()
        return caught.value.result

    persistent_result = asyncio.run(persistent())
    assert persistent_result.endpoint is ProcessEndpoint.FAILED
    assert persistent_result.cleanup_ok
    assert persistent_result.returncode is None
    assert "PROCESS_START_FAILED" in _events(persistent_result)


@pytest.mark.skipif(os.name != "posix", reason="POSIX process groups required")
def test_line_session_preserves_observer_timeout_classification_and_cleanup() -> None:
    async def exercise():
        session = OwnedLineProtocolSession(
            _line_worker(),
            ready_line="READY",
            close_command=b"CLOSE\n",
            limits=_limits(whole_run_timeout_seconds=2.0),
        )
        await session.start()
        registry = default_tool_call_registry()
        plan = DeclaredCallPlan(
            "line-protocol-test.v1",
            "line-protocol-test",
            registry.sha256,
            (CallRule("vivado.report_route_status"),),
        )
        budget = ActualCallBudget(
            {event_class: 4 for event_class in EventClass},
            whole_run_timeout_seconds=3.0,
        )

        async def invoke(_operation, _arguments):
            return await session.call(
                b"HANG|NEVER\n",
                terminal_marker="NEVER",
                timeout_seconds=1.0,
            )

        observer = ObservedCallExecutor(
            registry=registry,
            plan=plan,
            budget=budget,
            invoke=invoke,
        )
        with pytest.raises(HostCallTimedOut):
            await observer.call(
                "vivado.report_route_status", {}, timeout_seconds=0.05
            )
        assert session.result is not None
        return observer, session.result

    observer, result = asyncio.run(exercise())
    assert observer.trace_rows[-1]["status"] == "TIMED_OUT"
    assert result.endpoint is ProcessEndpoint.CANCELLED
    assert result.cleanup_ok
    assert result.orphan_process_tokens == ()
