"""Fixed read-only Vivado line-protocol worker for the WP-003 preflight.

The parent process owns this worker and its Vivado child as one POSIX session.
Requests are strict JSON envelopes selecting one closed operation; no caller
supplies Tcl.  The parent authorizes each operation before writing the request
and waits for the unique completion line emitted here.
"""

from __future__ import annotations

import argparse
import base64
from collections.abc import Mapping
from dataclasses import dataclass
import hashlib
import json
import math
import os
import platform
import re
import signal
import subprocess
import sys
import termios
import time
from typing import Any
from pathlib import Path


WORKER_REQUEST_SCHEMA = "dof.vivado-readonly-worker-request.v1"
WORKER_VERSION = "dof.vivado-readonly-worker.v1"
READY_LINE = "DOF_VIVADO_READONLY_READY_V1"
LAUNCHED_LINE = "DOF_VIVADO_PROCESS_LAUNCHED_V1"
STARTED_LINE = "DOF_VIVADO_PROCESS_STARTED_V1"
VERSION_PREFIX = "DOF_VIVADO_VERSION_V1:"
PYTHON_VERSION_PREFIX = "DOF_WORKER_PYTHON_VERSION_V1:"
INIT_POLICY_LINE = "DOF_VIVADO_INIT_POLICY_V1:PASS"
EFFECTIVE_ENV_PREFIX = "DOF_VIVADO_EFFECTIVE_ENV_SHA256_V1:"
EFFECTIVE_ENVIRONMENT_EVIDENCE_VERSION = "dof.public-safe-environment-evidence.v2"
DONE_PREFIX = "DOF_VIVADO_CALL_DONE_V1:"
STATUS_PREFIX = "DOF_VIVADO_TCL_STATUS_V1:"
_TOKEN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,95}$")
_ALLOWED_OPERATIONS = frozenset(
    {
        "vivado.open_checkpoint",
        "vivado.report_timing_summary",
        "vivado.report_route_status",
        "vivado.report_utilization",
        "vivado.close_design",
        "worker.exit",
    }
)


def effective_vivado_environment_hash(
    environment: Mapping[str, str],
    isolated_home: str | os.PathLike[str],
) -> str:
    """Return public evidence for the environment key set passed to Vivado.

    Values are never hashed: even an unconventionally named variable may hold
    a secret, licence endpoint, or private site path.  The mutation launcher
    separately constrains the input to a closed allowlist.
    """

    effective = {str(key): str(value) for key, value in environment.items()}
    home = os.fspath(isolated_home)
    effective["HOME"] = home
    effective["XDG_CONFIG_HOME"] = home
    private_tmp = os.fspath(Path(home) / "tmp")
    effective["TMPDIR"] = private_tmp
    effective["TMP"] = private_tmp
    effective["TEMP"] = private_tmp
    evidence = [
        {"key": key, "value_policy": "REDACTED_PRESENT"}
        for key in sorted(effective)
    ]
    canonical = json.dumps(
        {
            "schema_version": EFFECTIVE_ENVIRONMENT_EVIDENCE_VERSION,
            "entries": evidence,
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


class WorkerProtocolError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class WorkerRequest:
    operation_id: str
    call_token: str
    timeout_seconds: float
    arguments: dict[str, Any]


@dataclass(frozen=True, slots=True)
class _ProcessIdentity:
    """PID-reuse-safe identity used only for bounded worker cleanup."""

    pid: int
    state: str
    ppid: int
    pgrp: int
    session: int
    starttime: int


@dataclass(slots=True)
class _OwnedPtyProcess:
    """A Vivado process attached to a PTY without changing POSIX ownership."""

    process: subprocess.Popen[bytes]
    channel: Any
    identity: _ProcessIdentity


def _read_process_identity(pid: int) -> _ProcessIdentity | None:
    """Read one Linux procfs identity without relying on process names."""

    try:
        raw = open(f"/proc/{pid}/stat", "rb").read()
        close = raw.rfind(b")")
        if close < 0:
            return None
        fields = raw[close + 2 :].split()
        return _ProcessIdentity(
            pid=pid,
            state=fields[0].decode("ascii", "replace"),
            ppid=int(fields[1]),
            pgrp=int(fields[2]),
            session=int(fields[3]),
            starttime=int(fields[19]),
        )
    except (FileNotFoundError, PermissionError, IndexError, ValueError, OSError):
        return None


def _descendant_identities(root_pid: int) -> tuple[_ProcessIdentity, ...]:
    """Snapshot the live root and descendants for ownership verification."""

    identities: list[_ProcessIdentity] = []
    try:
        names = os.listdir("/proc")
    except OSError:
        return ()
    for name in names:
        if not name.isdigit():
            continue
        identity = _read_process_identity(int(name))
        if identity is not None:
            identities.append(identity)
    by_parent: dict[int, list[_ProcessIdentity]] = {}
    for identity in identities:
        by_parent.setdefault(identity.ppid, []).append(identity)
    selected: dict[int, _ProcessIdentity] = {}
    pending = [root_pid]
    while pending:
        pid = pending.pop()
        identity = next((item for item in identities if item.pid == pid), None)
        if identity is not None:
            selected[pid] = identity
        for child in by_parent.get(pid, ()):
            if child.pid not in selected:
                pending.append(child.pid)
    return tuple(selected[pid] for pid in sorted(selected))


def _same_owned_session(identity: _ProcessIdentity) -> bool:
    return identity.pgrp == os.getpgrp() and identity.session == os.getsid(0)


def _tree_is_owned(root_pid: int) -> tuple[bool, tuple[_ProcessIdentity, ...]]:
    identities = _descendant_identities(root_pid)
    root = next((item for item in identities if item.pid == root_pid), None)
    return (
        root is not None
        and bool(identities)
        and all(_same_owned_session(item) for item in identities),
        identities,
    )


def _live_verified(
    identities: tuple[_ProcessIdentity, ...],
) -> tuple[_ProcessIdentity, ...]:
    live: list[_ProcessIdentity] = []
    for expected in identities:
        current = _read_process_identity(expected.pid)
        if (
            current is not None
            and current.starttime == expected.starttime
            and current.state != "Z"
        ):
            live.append(current)
    return tuple(live)


def _identity_still_matches(expected: _ProcessIdentity) -> bool:
    current = _read_process_identity(expected.pid)
    return current is not None and current.starttime == expected.starttime


def _signal_verified(
    identities: tuple[_ProcessIdentity, ...], signum: signal.Signals
) -> None:
    # Descendants are signalled before the direct child.  PID reuse cannot
    # widen this cleanup because start time is rechecked immediately first.
    for expected in reversed(identities):
        current = _read_process_identity(expected.pid)
        if current is None or current.starttime != expected.starttime:
            continue
        try:
            os.kill(current.pid, signum)
        except ProcessLookupError:
            pass


def _wait_verified_empty(
    identities: tuple[_ProcessIdentity, ...], timeout_seconds: float
) -> bool:
    deadline = time.monotonic() + max(0.0, timeout_seconds)
    while time.monotonic() < deadline:
        if not _live_verified(identities):
            return True
        time.sleep(min(0.02, max(0.0, deadline - time.monotonic())))
    return not _live_verified(identities)


def _bounded_stop(
    owned: _OwnedPtyProcess,
    *,
    known_identities: tuple[_ProcessIdentity, ...] = (),
    graceful: bool = False,
    graceful_timeout_seconds: float = 0.25,
) -> bool:
    """Stop and reap the verified process tree within fixed local bounds.

    The outer :class:`OwnedLineProtocolSession` remains the authoritative
    owner of the worker session and independently TERM/KILLs that whole PGID.
    This local cleanup is defence in depth, including the fail-closed path if
    an executable creates an unexpected session before the READY marker.
    """

    # Never widen ownership from a numeric PID after the launch identity has
    # disappeared or been reused.  Previously observed identities remain safe
    # to verify individually, but a fresh descendant scan is only trustworthy
    # while the root PID still has its launch-time start tick.
    fresh_identities = (
        _descendant_identities(owned.identity.pid)
        if _identity_still_matches(owned.identity)
        else ()
    )
    identities_by_key = {
        (item.pid, item.starttime): item
        for item in (
            *known_identities,
            owned.identity,
            *fresh_identities,
        )
    }
    identities = tuple(identities_by_key.values())
    clean = True
    if graceful and owned.process.poll() is None:
        try:
            owned.channel.sendline(b"exit")
            owned.channel.expect(
                __import__("pexpect").EOF,
                timeout=max(0.01, min(float(graceful_timeout_seconds), 2.0)),
            )
        except Exception:
            clean = False
    if owned.process.poll() is None or _live_verified(identities):
        _signal_verified(identities, signal.SIGTERM)
        if not _wait_verified_empty(identities, 1.0):
            _signal_verified(identities, signal.SIGKILL)
            if not _wait_verified_empty(identities, 1.0):
                clean = False
    try:
        owned.process.wait(timeout=1.0)
    except subprocess.TimeoutExpired:
        # Popen.kill() signals the current occupant of the numeric PID and has
        # no start-time guard.  Reuse the verified signal path instead.
        _signal_verified((owned.identity,), signal.SIGKILL)
        try:
            owned.process.wait(timeout=1.0)
        except subprocess.TimeoutExpired:
            clean = False
    try:
        owned.channel.close()
    except Exception:
        clean = False
    return clean and not _live_verified(identities)


def _validate_init_policy(
    vivado_exec: str,
    *,
    run_workspace: str,
    isolated_home: str,
) -> tuple[str, str, dict[str, str]]:
    """Return a sealed startup environment or reject all implicit Tcl init.

    Vivado can auto-source installation and per-user Tcl files before the
    fixed request protocol exists.  The worker therefore uses an empty,
    private HOME/cwd inside its owned run workspace and refuses an install
    containing either supported or legacy enterprise init filename.
    """

    if os.name != "posix" or not Path("/proc/self").is_dir():
        raise WorkerProtocolError("init_policy_requires_linux_procfs")
    executable = Path(vivado_exec)
    workspace = Path(run_workspace)
    home = Path(isolated_home)
    for name, path in (("run_workspace", workspace), ("isolated_home", home)):
        if not path.is_absolute():
            raise WorkerProtocolError(f"{name}_must_be_absolute")
        if os.path.abspath(os.fspath(path)) != os.path.realpath(os.fspath(path)):
            raise WorkerProtocolError(f"{name}_must_not_traverse_symlink")

    executable = executable.resolve(strict=True)
    workspace = workspace.resolve(strict=True)
    home = home.resolve(strict=True)
    if not executable.is_file() or not os.access(executable, os.X_OK):
        raise WorkerProtocolError("vivado_exec_not_executable_file")
    if executable.parent.name != "bin":
        raise WorkerProtocolError("vivado_exec_not_under_version_bin")
    install_root = executable.parent.parent
    if not workspace.is_dir() or not home.is_dir():
        raise WorkerProtocolError("init_policy_directory_missing")
    try:
        relative_home = home.relative_to(workspace)
    except ValueError as exc:
        raise WorkerProtocolError("isolated_home_outside_run_workspace") from exc
    if not relative_home.parts:
        raise WorkerProtocolError("isolated_home_must_be_strict_child")
    workspace_stat = workspace.stat()
    home_stat = home.stat()
    if workspace_stat.st_uid != os.getuid() or home_stat.st_uid != os.getuid():
        raise WorkerProtocolError("init_policy_directory_not_owned")
    if home_stat.st_mode & 0o077:
        raise WorkerProtocolError("isolated_home_permissions_not_private")
    if any(home.iterdir()):
        raise WorkerProtocolError("isolated_home_not_empty")

    version = install_root.name
    candidates = (
        install_root / "scripts" / "Vivado_init.tcl",
        install_root / "scripts" / "init.tcl",
        home / ".Xilinx" / "Vivado" / "Vivado_init.tcl",
        home / ".Xilinx" / "Vivado" / version / "Vivado_init.tcl",
        home / "Vivado_init.tcl",
        home / "init.tcl",
    )
    if any(candidate.exists() or candidate.is_symlink() for candidate in candidates):
        raise WorkerProtocolError("implicit_vivado_init_candidate_present")

    effective_env = {str(key): str(value) for key, value in os.environ.items()}
    effective_env["HOME"] = os.fspath(home)
    effective_env["XDG_CONFIG_HOME"] = os.fspath(home)
    private_tmp = home / "tmp"
    private_tmp.mkdir(mode=0o700)
    effective_env["TMPDIR"] = os.fspath(private_tmp)
    effective_env["TMP"] = os.fspath(private_tmp)
    effective_env["TEMP"] = os.fspath(private_tmp)
    return os.fspath(executable), os.fspath(home), effective_env


def _spawn_owned_pty(
    vivado_exec: str,
    timeout_seconds: float,
    *,
    cwd: str,
    env: dict[str, str],
) -> _OwnedPtyProcess:
    """Spawn Vivado on a PTY while inheriting the worker's session and PGID.

    ``pexpect.spawn`` cannot be used here: its ``pty.fork`` creates a new
    session leader, escaping the outer owner's process group.  ``Popen`` has
    no ``setsid``/``process_group`` option here, and ``fdspawn`` only drives
    the already-open PTY master.
    """

    try:
        from pexpect.fdpexpect import fdspawn
    except Exception as exc:  # pragma: no cover - environment contract
        raise RuntimeError("pexpect_fdspawn_unavailable") from exc

    master_fd, slave_fd = os.openpty()
    try:
        attributes = termios.tcgetattr(slave_fd)
        attributes[3] &= ~termios.ECHO
        termios.tcsetattr(slave_fd, termios.TCSANOW, attributes)
        process = subprocess.Popen(
            [vivado_exec, "-mode", "tcl", "-nolog", "-nojournal"],
            cwd=cwd,
            env=env,
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            bufsize=0,
            close_fds=True,
            start_new_session=False,
        )
    except BaseException:
        os.close(master_fd)
        os.close(slave_fd)
        raise
    finally:
        # The parent owns only the PTY master.  Keeping an extra slave open
        # would prevent EOF and defeat bounded shutdown detection.
        try:
            os.close(slave_fd)
        except OSError:
            pass

    identity = _read_process_identity(process.pid)
    if identity is None:
        # Without the launch-time start tick, neither Popen.terminate() nor
        # Popen.kill() can prove that the numeric PID still names our child.
        # Closing the owned PTY may let the original child exit naturally;
        # the outer OwnedLineProtocolSession remains responsible for bounded
        # verified process-group cleanup when this worker fails pre-READY.
        os.close(master_fd)
        try:
            process.wait(timeout=1.0)
        except subprocess.TimeoutExpired:
            pass
        raise RuntimeError("vivado_identity_unavailable")
    channel = fdspawn(
        master_fd,
        timeout=timeout_seconds,
        encoding=None,
        use_poll=True,
    )
    return _OwnedPtyProcess(process, channel, identity)


def parse_request_line(raw: bytes) -> WorkerRequest:
    """Parse one exact, closed request envelope without repairs/defaults."""

    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise WorkerProtocolError("request_not_utf8") from exc
    try:
        value = json.loads(text)
    except json.JSONDecodeError as exc:
        raise WorkerProtocolError("request_not_exact_json") from exc
    if not isinstance(value, dict) or set(value) != {
        "schema_version",
        "operation_id",
        "call_token",
        "timeout_seconds",
        "arguments",
    }:
        raise WorkerProtocolError("request_fields_not_exact")
    if value["schema_version"] != WORKER_REQUEST_SCHEMA:
        raise WorkerProtocolError("request_schema_mismatch")
    operation = value["operation_id"]
    if operation not in _ALLOWED_OPERATIONS:
        raise WorkerProtocolError("operation_not_allowed")
    token = value["call_token"]
    if not isinstance(token, str) or not _TOKEN_RE.fullmatch(token):
        raise WorkerProtocolError("call_token_invalid")
    timeout = value["timeout_seconds"]
    if (
        not isinstance(timeout, (int, float))
        or isinstance(timeout, bool)
        or not math.isfinite(float(timeout))
        or not 0 < float(timeout) <= 600.0
    ):
        raise WorkerProtocolError("timeout_invalid")
    arguments = value["arguments"]
    if not isinstance(arguments, dict):
        raise WorkerProtocolError("arguments_not_object")
    expected_arguments = (
        {"dcp_path_b64"}
        if operation == "vivado.open_checkpoint"
        else set()
    )
    if set(arguments) != expected_arguments:
        raise WorkerProtocolError("operation_arguments_not_exact")
    if operation == "vivado.open_checkpoint":
        encoded = arguments["dcp_path_b64"]
        if not isinstance(encoded, str) or len(encoded) > 32768:
            raise WorkerProtocolError("dcp_path_encoding_invalid")
        try:
            path_bytes = base64.b64decode(encoded.encode("ascii"), validate=True)
            path_bytes.decode("utf-8")
        except (UnicodeEncodeError, UnicodeDecodeError, ValueError) as exc:
            raise WorkerProtocolError("dcp_path_encoding_invalid") from exc
        if not path_bytes or b"\x00" in path_bytes:
            raise WorkerProtocolError("dcp_path_encoding_invalid")
    return WorkerRequest(operation, token, float(timeout), dict(arguments))


def request_bytes(
    operation_id: str,
    call_token: str,
    *,
    timeout_seconds: float,
    arguments: dict[str, Any] | None = None,
) -> bytes:
    payload = {
        "schema_version": WORKER_REQUEST_SCHEMA,
        "operation_id": operation_id,
        "call_token": call_token,
        "timeout_seconds": timeout_seconds,
        "arguments": dict(arguments or {}),
    }
    # Parse our own output so callers cannot accidentally construct a request
    # that this version of the worker interprets differently.
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    parse_request_line(encoded)
    return encoded + b"\n"


def terminal_marker(call_token: str) -> bytes:
    if not _TOKEN_RE.fullmatch(call_token):
        raise WorkerProtocolError("call_token_invalid")
    return f"{DONE_PREFIX}{call_token}".encode("ascii")


def build_fixed_tcl(request: WorkerRequest) -> str:
    """Build only a closed, non-mutating Tcl command."""

    operation = request.operation_id
    if operation == "worker.exit":
        return "exit"
    if operation == "vivado.open_checkpoint":
        encoded = request.arguments["dcp_path_b64"]
        body = (
            "set __dof_path [encoding convertfrom utf-8 "
            f"[binary decode base64 {{{encoded}}}]]; "
            "open_checkpoint $__dof_path"
        )
    elif operation == "vivado.report_timing_summary":
        body = "report_timing_summary -delay_type max -max_paths 10"
    elif operation == "vivado.report_route_status":
        body = "report_route_status"
    elif operation == "vivado.report_utilization":
        body = "report_utilization"
    elif operation == "vivado.close_design":
        body = "close_design"
    else:  # defensive even though WorkerRequest is normally parser-produced
        raise WorkerProtocolError("operation_not_allowed")
    marker = f"{STATUS_PREFIX}{request.call_token}:"
    return (
        f"set __dof_code [catch {{{body}}} __dof_result __dof_options]; "
        f"puts \"{marker}$__dof_code\"; flush stdout"
    )


def parse_tcl_result(call_token: str, output: bytes) -> tuple[int, bytes]:
    """Extract the one worker-owned Tcl status line from bounded call output."""

    if not _TOKEN_RE.fullmatch(call_token):
        raise WorkerProtocolError("call_token_invalid")
    prefix = f"{STATUS_PREFIX}{call_token}:".encode("ascii")
    matches: list[int] = []
    retained: list[bytes] = []
    for line in output.splitlines(keepends=True):
        normalized = line.rstrip(b"\r\n")
        if normalized.startswith(prefix):
            suffix = normalized[len(prefix) :]
            if suffix not in {b"0", b"1", b"2", b"3", b"4", b"5"}:
                raise WorkerProtocolError("tcl_status_code_invalid")
            matches.append(int(suffix))
        else:
            retained.append(line)
    if len(matches) != 1:
        raise WorkerProtocolError("tcl_status_line_cardinality_invalid")
    return matches[0], b"".join(retained)


def _run(
    vivado_exec: str,
    initialization_timeout: float,
    *,
    run_workspace: str,
    isolated_home: str,
) -> int:
    # This is runtime-observed inside the actual worker interpreter, not
    # reconstructed from the outer launch command or environment.
    print(PYTHON_VERSION_PREFIX + platform.python_version(), flush=True)
    try:
        resolved_exec, vivado_cwd, vivado_env = _validate_init_policy(
            vivado_exec,
            run_workspace=run_workspace,
            isolated_home=isolated_home,
        )
    except (OSError, WorkerProtocolError) as exc:
        print(f"Vivado init policy rejected: {type(exc).__name__}", file=sys.stderr)
        return 79
    print(INIT_POLICY_LINE, flush=True)
    print(
        EFFECTIVE_ENV_PREFIX
        + effective_vivado_environment_hash(vivado_env, vivado_cwd),
        flush=True,
    )
    try:
        import pexpect
    except Exception as exc:  # pragma: no cover - environment contract
        print(f"worker dependency unavailable: {type(exc).__name__}", file=sys.stderr)
        return 72

    try:
        owned = _spawn_owned_pty(
            resolved_exec,
            initialization_timeout,
            cwd=vivado_cwd,
            env=vivado_env,
        )
    except Exception as exc:
        print(f"Vivado process start failed: {type(exc).__name__}", file=sys.stderr)
        return 72
    child = owned.channel
    if not _same_owned_session(owned.identity):
        _bounded_stop(owned, known_identities=(owned.identity,))
        print("Vivado process ownership verification failed", file=sys.stderr)
        return 78
    # This marker means the operating system actually created the Vivado
    # process.  It intentionally precedes prompt/readiness checks so an early
    # initialization failure cannot be reported as zero Vivado starts.
    print(LAUNCHED_LINE, flush=True)

    prompt = rb"\r?\nVivado% "
    try:
        child.expect(prompt, timeout=initialization_timeout)
    except (pexpect.TIMEOUT, pexpect.EOF):
        print("Vivado initialization did not reach the prompt", file=sys.stderr)
        _bounded_stop(owned)
        return 73


    # Fail closed if Vivado has already created any child outside the worker
    # session/PGID.  Crucially, this verification precedes STARTED and READY.
    tree_owned, initial_tree = _tree_is_owned(owned.process.pid)
    if not tree_owned:
        _bounded_stop(owned, known_identities=initial_tree)
        print("Vivado process-tree ownership verification failed", file=sys.stderr)
        return 78
    print(STARTED_LINE, flush=True)

    banner = bytes(child.before or b"").decode("utf-8", "replace")
    version_match = re.search(
        r"\bVivado\s+v([0-9]{4}\.[0-9]+(?:\.[0-9]+)?)", banner
    )
    print(
        VERSION_PREFIX
        + (version_match.group(1) if version_match is not None else "NOT_PARSED"),
        flush=True,
    )
    print(READY_LINE, flush=True)
    for raw in sys.stdin.buffer:
        try:
            request = parse_request_line(raw.rstrip(b"\r\n"))
        except WorkerProtocolError as exc:
            print(f"worker request rejected: {exc}", file=sys.stderr, flush=True)
            return 74
        if request.operation_id == "worker.exit":
            if not _bounded_stop(
                owned,
                known_identities=initial_tree,
                graceful=True,
                graceful_timeout_seconds=request.timeout_seconds,
            ):
                return 75
            return 0

        tree_owned, observed_tree = _tree_is_owned(owned.process.pid)
        if not tree_owned:
            _bounded_stop(owned, known_identities=observed_tree)
            return 78

        tcl = build_fixed_tcl(request).encode("utf-8")
        child.sendline(tcl)
        try:
            child.expect(prompt, timeout=request.timeout_seconds)
        except (pexpect.TIMEOUT, pexpect.EOF):
            # No completion marker is emitted.  The parent call deadline owns
            # bounded process-tree cleanup and records the typed failure.
            _bounded_stop(owned, known_identities=observed_tree)
            return 76
        tree_owned, completed_tree = _tree_is_owned(owned.process.pid)
        if not tree_owned:
            _bounded_stop(owned, known_identities=completed_tree)
            return 78
        output = bytes(child.before or b"")
        sys.stdout.buffer.write(output)
        if output and not output.endswith(b"\n"):
            sys.stdout.buffer.write(b"\n")
        sys.stdout.buffer.write(terminal_marker(request.call_token) + b"\n")
        sys.stdout.buffer.flush()
    _bounded_stop(owned, known_identities=initial_tree)
    return 77


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Owned fixed read-only Vivado worker for WP-003."
    )
    parser.add_argument("--vivado-exec", required=True)
    parser.add_argument("--run-workspace", required=True)
    parser.add_argument("--isolated-home", required=True)
    parser.add_argument(
        "--initialization-timeout-seconds", type=float, default=60.0
    )
    args = parser.parse_args(argv)
    return _run(
        args.vivado_exec,
        args.initialization_timeout_seconds,
        run_workspace=args.run_workspace,
        isolated_home=args.isolated_home,
    )


if __name__ == "__main__":
    raise SystemExit(main())
