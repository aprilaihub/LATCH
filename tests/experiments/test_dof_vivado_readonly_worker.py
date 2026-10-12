from __future__ import annotations

import asyncio
import base64
import json
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest

from optimizer_pipeline.dof import vivado_readonly_worker as readonly_worker
from optimizer_pipeline.dof.processes import (
    OwnedLineProtocolSession,
    OwnedProtocolCallTimedOut,
    OwnedProtocolSessionError,
    ProcessEndpoint,
    ProcessLimits,
)
from optimizer_pipeline.dof.vivado_readonly_worker import (
    EFFECTIVE_ENV_PREFIX,
    INIT_POLICY_LINE,
    LAUNCHED_LINE,
    PYTHON_VERSION_PREFIX,
    READY_LINE,
    STATUS_PREFIX,
    STARTED_LINE,
    VERSION_PREFIX,
    WORKER_REQUEST_SCHEMA,
    WorkerProtocolError,
    build_fixed_tcl,
    effective_vivado_environment_hash,
    parse_request_line,
    parse_tcl_result,
    request_bytes,
    terminal_marker,
)


def _worker_limits() -> ProcessLimits:
    return ProcessLimits(
        initialization_timeout_seconds=2.0,
        whole_run_timeout_seconds=3.0,
        graceful_close_seconds=0.10,
        terminate_grace_seconds=0.30,
        kill_grace_seconds=0.75,
        cleanup_timeout_seconds=1.50,
        output_limit_bytes=32 * 1024,
    )


def _write_fake_vivado(
    tmp_path: Path,
    *,
    hang_on_tcl: bool,
    escaped_descendant: bool = False,
) -> tuple[Path, Path, Path, Path]:
    install_root = tmp_path / "Vivado" / "2025.1"
    install_bin = install_root / "bin"
    install_bin.mkdir(parents=True)
    executable = install_bin / "vivado"
    record = tmp_path / "process-tree.json"
    executable.write_text(
        f"""#!{sys.executable}
import json
import os
import re
import subprocess
import sys
import time

descendant = subprocess.Popen(
    [sys.executable, "-c", "import time; time.sleep(60)"],
    start_new_session={escaped_descendant!r},
)
payload = {{}}
for name, pid in (
    ("worker", os.getppid()),
    ("vivado", os.getpid()),
    ("descendant", descendant.pid),
):
    payload[name] = {{
        "pid": pid,
        "pgrp": os.getpgid(pid),
        "session": os.getsid(pid),
    }}
payload["environment"] = {{
    "home": os.environ.get("HOME"),
    "xdg_config_home": os.environ.get("XDG_CONFIG_HOME"),
    "cwd": os.getcwd(),
}}
with open(os.environ["DOF_FAKE_TREE_RECORD"], "w", encoding="utf-8") as handle:
    json.dump(payload, handle, sort_keys=True)

print("Vivado v2025.1", flush=True)
print("Vivado% ", end="", flush=True)
try:
    for raw in sys.stdin.buffer:
        if raw.strip() == b"exit":
            break
        if {hang_on_tcl!r}:
            time.sleep(60)
        match = re.search(
            rb"DOF_VIVADO_TCL_STATUS_V1:([A-Za-z0-9_-]+):", raw
        )
        if match is None:
            continue
        print("fixture report", flush=True)
        print(
            "DOF_VIVADO_TCL_STATUS_V1:"
            + match.group(1).decode("ascii")
            + ":0",
            flush=True,
        )
        print("Vivado% ", end="", flush=True)
finally:
    if descendant.poll() is None:
        descendant.terminate()
    try:
        descendant.wait(timeout=1.0)
    except subprocess.TimeoutExpired:
        descendant.kill()
        descendant.wait(timeout=1.0)
""",
        encoding="utf-8",
    )
    executable.chmod(0o755)
    workspace = tmp_path / "owned-run"
    workspace.mkdir()
    home = workspace / "isolated-home"
    home.mkdir(mode=0o700)
    return executable, record, workspace, home


def _worker_argv(
    fake_vivado: Path, *, workspace: Path, isolated_home: Path
) -> list[str]:
    return [
        sys.executable,
        "-I",
        os.fspath(
            Path("optimizer_pipeline/dof/vivado_readonly_worker.py").resolve()
        ),
        "--vivado-exec",
        os.fspath(fake_vivado),
        "--run-workspace",
        os.fspath(workspace),
        "--isolated-home",
        os.fspath(isolated_home),
        "--initialization-timeout-seconds",
        "1.5",
    ]


def _assert_recorded_processes_gone(record: dict[str, dict[str, object]]) -> None:
    processes = {name: record[name] for name in ("worker", "vivado", "descendant")}
    deadline = time.monotonic() + 1.0
    while time.monotonic() < deadline:
        if all(not Path(f"/proc/{row['pid']}").exists() for row in processes.values()):
            return
        time.sleep(0.02)
    assert {
        name: Path(f"/proc/{row['pid']}").exists()
        for name, row in processes.items()
    } == {name: False for name in processes}


def test_worker_request_is_closed_and_rejects_raw_or_mutating_operations() -> None:
    legal = request_bytes(
        "vivado.report_route_status",
        "call-001",
        timeout_seconds=5.0,
    )
    assert parse_request_line(legal.rstrip()).operation_id == "vivado.report_route_status"

    for operation in ("vivado.place_design", "vivado.run_tcl", "flow.auto_top3"):
        payload = {
            "schema_version": WORKER_REQUEST_SCHEMA,
            "operation_id": operation,
            "call_token": "call-002",
            "timeout_seconds": 5.0,
            "arguments": {},
        }
        with pytest.raises(WorkerProtocolError, match="operation_not_allowed"):
            parse_request_line(json.dumps(payload).encode())


def test_open_checkpoint_path_is_data_not_tcl() -> None:
    path = "/private/site/design with spaces.dcp"
    request = parse_request_line(
        request_bytes(
            "vivado.open_checkpoint",
            "call-open",
            timeout_seconds=30.0,
            arguments={
                "dcp_path_b64": base64.b64encode(path.encode()).decode("ascii")
            },
        ).rstrip()
    )
    tcl = build_fixed_tcl(request)

    assert path not in tcl
    assert "binary decode base64" in tcl
    assert "open_checkpoint $__dof_path" in tcl
    assert not any(
        forbidden in tcl
        for forbidden in (
            "place_design",
            "route_design",
            "phys_opt_design",
            "write_checkpoint",
            "unroute",
            "unplace",
        )
    )


def test_tcl_result_requires_one_exact_status_line() -> None:
    token = "call-report"
    raw = (
        b"report body\n"
        + f"{STATUS_PREFIX}{token}:0\n".encode("ascii")
    )
    code, report = parse_tcl_result(token, raw)
    assert code == 0
    assert report == b"report body\n"
    assert terminal_marker(token).endswith(token.encode())

    with pytest.raises(WorkerProtocolError, match="cardinality"):
        parse_tcl_result(token, b"report body only\n")


def test_worker_source_has_no_mutation_or_generic_tcl_surface() -> None:
    source = Path(
        "optimizer_pipeline/dof/vivado_readonly_worker.py"
    ).read_text(encoding="utf-8")
    for forbidden in (
        '"vivado.place_design"',
        '"vivado.route_design"',
        '"vivado.phys_opt_design"',
        '"vivado.write_checkpoint"',
        '"vivado.run_tcl"',
    ):
        assert forbidden not in source


@pytest.mark.skipif(os.name != "posix", reason="POSIX process groups required")
def test_manual_pty_keeps_fake_vivado_tree_owned_and_normal_close_reaps_it(
    tmp_path: Path,
) -> None:
    fake, record_path, workspace, home = _write_fake_vivado(
        tmp_path, hang_on_tcl=False
    )
    env = dict(os.environ)
    env["DOF_FAKE_TREE_RECORD"] = os.fspath(record_path)

    async def exercise():
        session = OwnedLineProtocolSession(
            _worker_argv(fake, workspace=workspace, isolated_home=home),
            ready_line=READY_LINE,
            close_command=request_bytes(
                "worker.exit", "close-normal", timeout_seconds=0.5
            ),
            limits=_worker_limits(),
            env=env,
        )
        await session.start()
        startup = session.startup_output
        call = await session.call(
            request_bytes(
                "vivado.report_route_status",
                "report-normal",
                timeout_seconds=0.5,
            ),
            terminal_marker=terminal_marker("report-normal"),
            timeout_seconds=0.75,
        )
        result = await session.close()
        return startup, call, result

    startup, call, result = asyncio.run(exercise())
    record = json.loads(record_path.read_text(encoding="utf-8"))
    ownership = {
        (record[name]["pgrp"], record[name]["session"])
        for name in ("worker", "vivado", "descendant")
    }
    assert len(ownership) == 1
    [(owned_pgrp, owned_session)] = ownership
    assert owned_pgrp == owned_session
    assert owned_pgrp != record["worker"]["pid"]
    assert record["environment"] == {
        "home": os.fspath(home),
        "xdg_config_home": os.fspath(home),
        "cwd": os.fspath(home),
    }
    code, report = parse_tcl_result("report-normal", call.stdout.data)
    assert code == 0
    assert b"fixture report" in report
    assert result.endpoint is ProcessEndpoint.SUCCEEDED
    assert result.cleanup_ok
    assert len(result.observed_process_tokens) >= 3
    stdout = result.stdout.data.decode("utf-8", "replace")
    assert PYTHON_VERSION_PREFIX in stdout
    assert VERSION_PREFIX + "2025.1" in stdout
    assert LAUNCHED_LINE in stdout
    assert STARTED_LINE in stdout
    assert INIT_POLICY_LINE in stdout
    assert (
        EFFECTIVE_ENV_PREFIX + effective_vivado_environment_hash(env, home)
        in stdout
    )
    startup_text = startup.data.decode("utf-8", "replace")
    for marker in (
        PYTHON_VERSION_PREFIX,
        INIT_POLICY_LINE,
        EFFECTIVE_ENV_PREFIX,
        LAUNCHED_LINE,
        STARTED_LINE,
        VERSION_PREFIX + "2025.1",
        READY_LINE,
    ):
        assert marker in startup_text
    assert "PROCESS_TREE_OWNERSHIP_VERIFIED" in {
        row["event_type"] for row in result.lifecycle
    }
    _assert_recorded_processes_gone(record)


@pytest.mark.skipif(os.name != "posix", reason="POSIX process groups required")
def test_manual_pty_fake_vivado_and_descendant_are_reaped_on_call_timeout(
    tmp_path: Path,
) -> None:
    fake, record_path, workspace, home = _write_fake_vivado(
        tmp_path, hang_on_tcl=True
    )
    env = dict(os.environ)
    env["DOF_FAKE_TREE_RECORD"] = os.fspath(record_path)

    async def exercise():
        session = OwnedLineProtocolSession(
            _worker_argv(fake, workspace=workspace, isolated_home=home),
            ready_line=READY_LINE,
            close_command=request_bytes(
                "worker.exit", "close-timeout", timeout_seconds=0.5
            ),
            limits=_worker_limits(),
            env=env,
        )
        await session.start()
        with pytest.raises(OwnedProtocolCallTimedOut) as caught:
            await session.call(
                request_bytes(
                    "vivado.report_route_status",
                    "report-timeout",
                    timeout_seconds=2.0,
                ),
                terminal_marker=terminal_marker("report-timeout"),
                timeout_seconds=0.08,
            )
        return caught.value.result

    started = time.monotonic()
    result = asyncio.run(exercise())
    record = json.loads(record_path.read_text(encoding="utf-8"))
    assert time.monotonic() - started < 2.5
    assert result.endpoint is ProcessEndpoint.CALL_TIMEOUT
    assert result.cleanup_ok
    assert result.orphan_process_tokens == ()
    assert len(result.observed_process_tokens) >= 3
    ownership = {
        (record[name]["pgrp"], record[name]["session"])
        for name in ("worker", "vivado", "descendant")
    }
    assert len(ownership) == 1
    [(owned_pgrp, owned_session)] = ownership
    assert owned_pgrp == owned_session
    assert owned_pgrp != record["worker"]["pid"]
    _assert_recorded_processes_gone(record)


@pytest.mark.skipif(os.name != "posix", reason="POSIX process groups required")
def test_worker_rejects_and_reaps_pre_ready_escaped_descendant(tmp_path: Path) -> None:
    fake, record_path, workspace, home = _write_fake_vivado(
        tmp_path,
        hang_on_tcl=False,
        escaped_descendant=True,
    )
    env = dict(os.environ)
    env["DOF_FAKE_TREE_RECORD"] = os.fspath(record_path)

    async def exercise():
        session = OwnedLineProtocolSession(
            _worker_argv(fake, workspace=workspace, isolated_home=home),
            ready_line=READY_LINE,
            close_command=request_bytes(
                "worker.exit", "close-rejected", timeout_seconds=0.25
            ),
            limits=_worker_limits(),
            env=env,
        )
        with pytest.raises(OwnedProtocolSessionError) as caught:
            await session.start()
        return caught.value, session.result

    failure, result = asyncio.run(exercise())
    assert "READY" in str(failure) or "initial" in str(failure)
    assert result is not None
    assert result.endpoint in {ProcessEndpoint.FAILED, ProcessEndpoint.OWNERSHIP_FAILURE}
    record = json.loads(record_path.read_text(encoding="utf-8"))
    assert record["descendant"]["session"] != record["worker"]["session"]
    _assert_recorded_processes_gone(record)


def test_init_policy_rejects_nonempty_home_and_enterprise_tcl(tmp_path: Path) -> None:
    fake, _record_path, workspace, home = _write_fake_vivado(
        tmp_path, hang_on_tcl=False
    )
    from optimizer_pipeline.dof.vivado_readonly_worker import _validate_init_policy

    (home / "unexpected").write_text("x", encoding="utf-8")
    with pytest.raises(WorkerProtocolError, match="not_empty"):
        _validate_init_policy(
            os.fspath(fake),
            run_workspace=os.fspath(workspace),
            isolated_home=os.fspath(home),
        )
    (home / "unexpected").unlink()
    scripts = fake.parent.parent / "scripts"
    scripts.mkdir()
    (scripts / "Vivado_init.tcl").write_text("puts unsafe", encoding="utf-8")
    with pytest.raises(WorkerProtocolError, match="implicit_vivado_init"):
        _validate_init_policy(
            os.fspath(fake),
            run_workspace=os.fspath(workspace),
            isolated_home=os.fspath(home),
        )


def test_bounded_stop_never_rescans_or_signals_a_reused_root_pid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    launched = readonly_worker._ProcessIdentity(
        pid=43210,
        state="S",
        ppid=1,
        pgrp=43210,
        session=43210,
        starttime=100,
    )
    reused = readonly_worker._ProcessIdentity(
        pid=launched.pid,
        state="S",
        ppid=1,
        pgrp=launched.pid,
        session=launched.pid,
        starttime=101,
    )

    class FakeProcess:
        pid = launched.pid
        kill_calls = 0

        def poll(self):
            return None

        def wait(self, timeout):
            raise subprocess.TimeoutExpired("fake-vivado", timeout)

        def kill(self):
            self.kill_calls += 1
            raise AssertionError("numeric PID must not be killed after reuse")

    class FakeChannel:
        closed = False

        def close(self):
            self.closed = True

    process = FakeProcess()
    channel = FakeChannel()
    owned = readonly_worker._OwnedPtyProcess(process, channel, launched)
    monkeypatch.setattr(
        readonly_worker,
        "_read_process_identity",
        lambda pid: reused if pid == launched.pid else None,
    )

    def forbidden_descendant_scan(_pid):
        raise AssertionError("reused root PID must not seed a descendant scan")

    monkeypatch.setattr(
        readonly_worker, "_descendant_identities", forbidden_descendant_scan
    )
    monkeypatch.setattr(
        readonly_worker.os,
        "kill",
        lambda *_args: (_ for _ in ()).throw(
            AssertionError("reused PID must not receive a signal")
        ),
    )

    assert not readonly_worker._bounded_stop(owned)
    assert process.kill_calls == 0
    assert channel.closed


def test_spawn_identity_unavailable_never_uses_numeric_pid_signals(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeProcess:
        pid = 54321
        terminate_calls = 0
        kill_calls = 0

        def wait(self, timeout):
            raise subprocess.TimeoutExpired("fake-vivado", timeout)

        def terminate(self):
            self.terminate_calls += 1
            raise AssertionError("unverified numeric PID must not be terminated")

        def kill(self):
            self.kill_calls += 1
            raise AssertionError("unverified numeric PID must not be killed")

    process = FakeProcess()
    monkeypatch.setattr(readonly_worker.subprocess, "Popen", lambda *_a, **_k: process)
    monkeypatch.setattr(readonly_worker, "_read_process_identity", lambda _pid: None)

    with pytest.raises(RuntimeError, match="vivado_identity_unavailable"):
        readonly_worker._spawn_owned_pty(
            "/fake/vivado",
            0.1,
            cwd=os.fspath(tmp_path),
            env={"PATH": os.defpath},
        )

    assert process.terminate_calls == 0
    assert process.kill_calls == 0
