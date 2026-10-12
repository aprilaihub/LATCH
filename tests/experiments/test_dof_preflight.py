from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import sys
import time
from typing import Any

import pytest

from optimizer_pipeline.core.domain import RunStatus, ValidationStatus
from optimizer_pipeline.core.store import CURRENT_SCHEMA_VERSION, SQLiteRunStore
import optimizer_pipeline.dof.preflight as preflight_module
import optimizer_pipeline.dof.processes as processes_module
from optimizer_pipeline.dof.evidence import verify_registered_evidence
from optimizer_pipeline.dof.preflight import (
    PREFLIGHT_REQUIRED_ROLES,
    PreflightPlanError,
    RealPreflightPlan,
    run_l0_real_preflight,
)
from optimizer_pipeline.dof.process_registry import create_registry
from optimizer_pipeline.dof.processes import (
    CapturedOutput,
    OwnedProcessCancelled,
    OwnedProcessResult,
    OwnedProtocolCallTimedOut,
    ProcessEndpoint,
    ProtocolCallResult,
)
from optimizer_pipeline.dof.provenance import (
    RepositoryConstraintMismatch,
    RepositoryObservation,
)
from optimizer_pipeline.dof.vivado_readonly_worker import (
    EFFECTIVE_ENV_PREFIX,
    INIT_POLICY_LINE,
    LAUNCHED_LINE,
    PYTHON_VERSION_PREFIX,
    READY_LINE,
    STARTED_LINE,
    STATUS_PREFIX,
    VERSION_PREFIX,
    effective_vivado_environment_hash,
    parse_request_line,
    terminal_marker,
)


_HEAD = "a" * 40
_EMPTY_HASH = hashlib.sha256(b"").hexdigest()


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _captured(raw: bytes) -> CapturedOutput:
    return CapturedOutput(raw, _sha(raw), len(raw), False)


def _observation(
    *, head: str = _HEAD, untracked_entry_count: int = 0
) -> RepositoryObservation:
    return RepositoryObservation(
        observed_at="2026-08-06T12:00:00+00:00",
        repository_root_identity_sha256=_sha(b"test-repository"),
        head_commit=head,
        head_tree="b" * 40,
        ref_state="branch",
        branch_name="phase0-policy-evidence-replan",
        tracked_tree_dirty=False,
        staged_entry_count=0,
        unstaged_entry_count=0,
        conflicted_entry_count=0,
        assume_unchanged_entry_count=0,
        skip_worktree_entry_count=0,
        tracked_status_sha256=_EMPTY_HASH,
        index_flag_status_sha256=_sha(b"tracked index flags"),
        binary_diff_sha256=_sha(b"cached\0\0working\0"),
        submodule_status_sha256=_EMPTY_HASH,
        submodule_count=0,
        nonclean_submodule_count=0,
        untracked_entry_count=untracked_entry_count,
        untracked_status_sha256=(
            _EMPTY_HASH
            if untracked_entry_count == 0
            else _sha(b"untracked-control-plane-entry")
        ),
        git_version="git version 2.43.0",
    )


def _plan_payload(dcp: Path, *, expected_sha256: str | None = None) -> dict[str, Any]:
    raw = dcp.read_bytes()
    return {
        "schema_version": "dof.real-preflight-plan.v2",
        "run_id": "run-real-preflight-test",
        "preflight_id": "wp003-fake-preflight",
        "input_dcp": {
            "pseudonym": "smallest-audited-fixture",
            "sha256": expected_sha256 or _sha(raw),
            "size_bytes": len(raw),
            "source_regime": "offline-fake-routed-checkpoint",
        },
        "selection_rule": "smallest-audited-no-outcome-selection",
        "environment_profile_id": "april-vivado-2025.1",
        "expected_tool_versions": {
            "vivado": "2025.1",
            "python": "3.12.3",
        },
        "repository_constraint": {
            "repository_mode": "git",
            "required_head": _HEAD,
            "require_tracked_clean": True,
            "untracked_policy": "require_none",
        },
        "process_limits": {
            "initialization_timeout_seconds": 5.0,
            "whole_run_timeout_seconds": 30.0,
            "graceful_close_seconds": 1.0,
            "terminate_grace_seconds": 1.0,
            "kill_grace_seconds": 1.0,
            "cleanup_timeout_seconds": 4.0,
            "output_limit_bytes": 1_000_000,
        },
        "call_timeouts_seconds": {
            "host.session.start": 5.0,
            "host.session.close": 5.0,
            "validation.hash_root": 5.0,
            "validation.validate_root": 5.0,
            "vivado.open_checkpoint": 5.0,
            "vivado.report_timing_summary": 5.0,
            "vivado.report_route_status": 5.0,
            "vivado.report_utilization": 5.0,
            "vivado.close_design": 5.0,
        },
        "event_limits": {
            "READ_ONLY": 5,
            "MUTATION_LIGHT": 0,
            "IMPLEMENTATION_HEAVY": 0,
            "VALIDATION": 3,
            "LLM_REQUEST": 0,
            "OUTER_ATTEMPT": 0,
        },
    }


def _write_plan(tmp_path: Path, payload: dict[str, Any]) -> Path:
    path = tmp_path / "preflight-plan.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_e0_preflight_schema_records_but_does_not_reject_prior_untracked_evidence(
    tmp_path: Path,
) -> None:
    dcp = tmp_path / "root.dcp"
    dcp.write_bytes(b"fake routed dcp")
    payload = _plan_payload(dcp)
    payload["schema_version"] = "dof.e0-real-preflight-plan.v1"
    payload["repository_constraint"]["untracked_policy"] = "record_only"
    plan = RealPreflightPlan.from_dict(payload)
    assert plan.schema_version == "dof.e0-real-preflight-plan.v1"
    assert plan.repository_constraint.untracked_policy == "record_only"

    legacy = _plan_payload(dcp)
    legacy["repository_constraint"]["untracked_policy"] = "record_only"
    with pytest.raises(PreflightPlanError):
        RealPreflightPlan.from_dict(legacy)


def _process_result(
    endpoint: ProcessEndpoint = ProcessEndpoint.SUCCEEDED,
    *,
    cleanup_errors: tuple[str, ...] = (),
    stdout_override: bytes | None = None,
    ownership_verified: bool = True,
    effective_environment_hash: str | None = None,
) -> OwnedProcessResult:
    environment_binding = effective_environment_hash or _sha(b"fake effective env")
    stdout = (
        f"{PYTHON_VERSION_PREFIX}3.12.3\n"
        f"{INIT_POLICY_LINE}\n"
        f"{EFFECTIVE_ENV_PREFIX}{environment_binding}\n"
        f"{LAUNCHED_LINE}\n"
        f"{STARTED_LINE}\n"
        f"{VERSION_PREFIX}2025.1\n"
        f"{READY_LINE}\n"
    ).encode("ascii") if stdout_override is None else stdout_override
    lifecycle_rows = [
        {
            "sequence": 0,
            "schema_version": "dof.process-lifecycle.v1",
            "event_type": "PROCESS_STARTED",
            "monotonic_seconds": 0.0,
            "root_process_token": _sha(b"fake process"),
            "session_owned": True,
        },
    ]
    if ownership_verified:
        lifecycle_rows.append(
            {
                "sequence": 1,
                "schema_version": "dof.process-lifecycle.v1",
                "event_type": "PROCESS_TREE_OWNERSHIP_VERIFIED",
                "monotonic_seconds": 0.05,
            }
        )
    lifecycle_rows.append(
        {
            "sequence": len(lifecycle_rows),
            "schema_version": "dof.process-lifecycle.v1",
            "event_type": "PROCESS_EVIDENCE_FINALIZED",
            "monotonic_seconds": 0.1,
        }
    )
    lifecycle = tuple(lifecycle_rows)
    return OwnedProcessResult(
        endpoint=endpoint,
        returncode=0 if endpoint is ProcessEndpoint.SUCCEEDED else None,
        command_hash=_sha(b"fake command"),
        environment_hash=_sha(b"fake environment"),
        executable_name="python",
        root_process_token=_sha(b"fake process"),
        session_owned=True,
        duration_seconds=0.1,
        lifecycle=lifecycle,
        observed_process_tokens=(_sha(b"fake process"),),
        orphan_process_tokens=(),
        stdout=_captured(stdout),
        stderr=_captured(b""),
        cleanup_errors=cleanup_errors,
    )


class _FakeSession:
    def __init__(
        self,
        argv: tuple[str, ...],
        *,
        ready_line: bytes | str,
        close_command: bytes | str,
        limits: object,
        cwd: str | Path,
        env: dict[str, str],
        fail_operation: str | None = None,
        truncate_operation: str | None = None,
        mutate_on_operation: tuple[str, Path] | None = None,
        cleanup_errors: tuple[str, ...] = (),
        startup_stdout: bytes | None = None,
        ownership_verified: bool = True,
        hang_close: bool = False,
    ) -> None:
        self.argv = tuple(argv)
        self.ready_line = ready_line
        self.close_command = close_command
        self.limits = limits
        self.cwd = Path(cwd)
        self.env = dict(env)
        self.fail_operation = fail_operation
        self.truncate_operation = truncate_operation
        self.mutate_on_operation = mutate_on_operation
        self.cleanup_errors = cleanup_errors
        self.startup_stdout = startup_stdout
        self.ownership_verified = ownership_verified
        self.hang_close = hang_close
        self.operations: list[str] = []
        self.started = False
        self._result: OwnedProcessResult | None = None

    @property
    def result(self) -> OwnedProcessResult | None:
        return self._result

    @property
    def root_process_token(self) -> str | None:
        return _sha(b"fake process") if self.started else None

    @property
    def session_owned(self) -> bool:
        return self.started

    @property
    def startup_output(self) -> CapturedOutput:
        return _process_result(
            stdout_override=self._attested_startup_stdout(),
            effective_environment_hash=self._effective_environment_hash(),
        ).stdout

    def _effective_environment_hash(self) -> str:
        home = self.argv[self.argv.index("--isolated-home") + 1]
        return effective_vivado_environment_hash(self.env, home)

    def _attested_startup_stdout(self) -> bytes | None:
        if self.startup_stdout is None or EFFECTIVE_ENV_PREFIX.encode() in self.startup_stdout:
            return self.startup_stdout
        return (
            self.startup_stdout
            + f"{EFFECTIVE_ENV_PREFIX}{self._effective_environment_hash()}\n".encode(
                "ascii"
            )
        )

    async def start(self) -> "_FakeSession":
        self.started = True
        return self

    async def call(
        self,
        command: bytes | str,
        *,
        terminal_marker: bytes | str,
        timeout_seconds: float,
    ) -> ProtocolCallResult:
        raw = command.encode() if isinstance(command, str) else command
        request = parse_request_line(raw.rstrip(b"\r\n"))
        assert terminal_marker == globals()["terminal_marker"](request.call_token)
        assert timeout_seconds == request.timeout_seconds
        operation = request.operation_id
        self.operations.append(operation)
        if self.mutate_on_operation is not None and operation == self.mutate_on_operation[0]:
            self.mutate_on_operation[1].write_bytes(b"mutated fake checkpoint")
        if operation == self.fail_operation:
            self._result = _process_result(ProcessEndpoint.CALL_TIMEOUT)
            raise OwnedProtocolCallTimedOut(
                f"fake-{request.call_token}", self._result, whole_run=False
            )
        report = {
            "vivado.open_checkpoint": b"",
            "vivado.report_timing_summary": (
                b"Timing Summary\nWNS(ns) TNS(ns) TNS Failing Endpoints\n"
                b"------- ------- ---------------------\n-0.125 -2.500 3\n"
            ),
            "vivado.report_route_status": (
                b"Design Route Status\n"
                b"       # of routable nets..................... :        3501 :\n"
                b"           # of unrouted nets................. :           0 :\n"
                b"       # of nets with routing errors.......... :           0 :\n"
            ),
            "vivado.report_utilization": (
                b"| CLB LUTs | 1,234 | 10000 | 12.34 |\n"
                b"| CLB Registers | 2,345 | 20000 | 11.72 |\n"
                b"| DSPs | 4 | 100 | 4.00 |\n"
            ),
            "vivado.close_design": b"",
        }[operation]
        output = report + f"{STATUS_PREFIX}{request.call_token}:0\n".encode("ascii")
        captured = (
            CapturedOutput(
                output,
                _sha(output + b"not-retained"),
                len(output) + len(b"not-retained"),
                True,
            )
            if operation == self.truncate_operation
            else _captured(output)
        )
        return ProtocolCallResult(
            call_id=f"fake-{request.call_token}",
            command_hash=_sha(raw),
            terminal_marker_hash=_sha(globals()["terminal_marker"](request.call_token)),
            duration_seconds=0.001,
            stdout=captured,
        )

    async def close(self) -> OwnedProcessResult:
        if self.hang_close:
            import asyncio

            await asyncio.Future()
        if self._result is None:
            close = parse_request_line(
                self.close_command.rstrip(b"\r\n")
                if isinstance(self.close_command, bytes)
                else self.close_command.encode().rstrip(b"\r\n")
            )
            assert close.operation_id == "worker.exit"
            self._result = _process_result(
                ProcessEndpoint.SUCCEEDED,
                cleanup_errors=self.cleanup_errors,
                stdout_override=self._attested_startup_stdout(),
                ownership_verified=self.ownership_verified,
                effective_environment_hash=self._effective_environment_hash(),
            )
        return self._result


def _factory(
    instances: list[_FakeSession],
    *,
    fail_operation: str | None = None,
    truncate_operation: str | None = None,
    mutate_on_operation: tuple[str, Path] | None = None,
    cleanup_errors: tuple[str, ...] = (),
    startup_stdout: bytes | None = None,
    ownership_verified: bool = True,
    hang_close: bool = False,
):
    def create(argv: tuple[str, ...], **kwargs: Any) -> _FakeSession:
        session = _FakeSession(
            argv,
            **kwargs,
            fail_operation=fail_operation,
            truncate_operation=truncate_operation,
            mutate_on_operation=mutate_on_operation,
            cleanup_errors=cleanup_errors,
            startup_stdout=startup_stdout,
            ownership_verified=ownership_verified,
            hang_close=hang_close,
        )
        instances.append(session)
        return session

    return create


def _patch_clean_repository(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(preflight_module, "observe_repository", lambda _anchor: _observation())


def _recovery_close_rows(run_dir: Path) -> list[dict[str, Any]]:
    rows = [
        json.loads(line)
        for line in (run_dir / "evidence/backend_calls.jsonl").read_text().splitlines()
    ]
    return [
        row
        for row in rows
        if row.get("operation_id") == "host.session.close"
        and row.get("recovery_after_failure") is True
    ]


def test_fake_session_preflight_is_exactly_one_read_only_l0_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry = tmp_path / "registry"
    create_registry(registry, job_id="queue-job", attempt_id="queue-attempt")
    for key, value in {
        "DOF_PROCESS_REGISTRY_DIR": os.fspath(registry.resolve()),
        "DOF_JOB_ID": "queue-job",
        "DOF_ATTEMPT_ID": "queue-attempt",
    }.items():
        monkeypatch.setenv(key, value)
    dcp = tmp_path / "root.dcp"
    dcp.write_bytes(b"frozen routed checkpoint test fixture")
    plan = _write_plan(tmp_path, _plan_payload(dcp))
    run_dir = tmp_path / "run"
    sessions: list[_FakeSession] = []
    _patch_clean_repository(monkeypatch)

    result = run_l0_real_preflight(
        plan,
        dcp_path=dcp,
        run_dir=run_dir,
        vivado_exec="/private/tools/Vivado/2025.1/bin/vivado",
        python_exec=sys.executable,
        repository_anchor=tmp_path / "repository-anchor",
        session_factory=_factory(sessions),
    )

    assert result["endpoint"] == "VALID_COMPLETION"
    assert result["endpoint_success"] is True
    assert result["canonical_schema_version"] == CURRENT_SCHEMA_VERSION == 2
    assert result["real_dcp_attempts"] == 1
    assert result["vivado_starts"] == 1
    assert result["rapidwright_starts"] == 0
    assert result["mcp_starts"] == 0
    assert result["external_model_requests"] == 0
    assert result["mutation_calls"] == 0
    assert result["heavy_calls"] == 0
    assert result["action_attempts"] == 0
    assert result["child_dcp_produced"] is False
    assert "run_dir" not in result
    assert result["call_reconciliation"]["ok"] is True
    assert result["repository_final_check"]["sha256"] == _sha(
        (run_dir / "evidence/repository_final_check.json").read_bytes()
    )
    assert result["call_reconciliation"]["observed_started_operations"] == [
        "validation.hash_root",
        "host.session.start",
        "vivado.open_checkpoint",
        "vivado.report_timing_summary",
        "vivado.report_route_status",
        "vivado.report_utilization",
        "vivado.close_design",
        "host.session.close",
        "validation.hash_root",
        "validation.validate_root",
    ]
    assert result["call_reconciliation"]["observed_terminal_status_counts"] == {
        "SUCCEEDED": 10,
        "FAILED": 0,
        "TIMED_OUT": 0,
        "CANCELLED": 0,
        "REJECTED_BEFORE_CALL": 0,
    }
    assert result["budget"]["used"] == {
        "READ_ONLY": 5,
        "MUTATION_LIGHT": 0,
        "IMPLEMENTATION_HEAVY": 0,
        "VALIDATION": 3,
        "LLM_REQUEST": 0,
        "OUTER_ATTEMPT": 0,
    }
    assert len(sessions) == 1
    assert sessions[0].started is True
    assert all(
        key not in sessions[0].env
        for key in processes_module.QUEUE_CONTROL_ENV_KEYS
    )
    assert sessions[0].operations == [
        "vivado.open_checkpoint",
        "vivado.report_timing_summary",
        "vivado.report_route_status",
        "vivado.report_utilization",
        "vivado.close_design",
    ]
    assert sessions[0].argv[1] == "-I"
    assert sessions[0].argv[2].endswith("vivado_readonly_worker.py")
    assert "--run-workspace" in sessions[0].argv
    assert "--isolated-home" in sessions[0].argv
    isolated_home = Path(
        sessions[0].argv[sessions[0].argv.index("--isolated-home") + 1]
    )
    assert isolated_home.is_dir()
    assert isolated_home.stat().st_mode & 0o077 == 0

    root_evidence = json.loads((run_dir / "evidence/root_integrity.json").read_text())
    assert root_evidence["before"] == root_evidence["after"]
    assert root_evidence["before"]["sha256"] == _sha(dcp.read_bytes())
    assert root_evidence["unchanged"] is True
    assert root_evidence["child_dcp_produced"] is False
    validation = json.loads((run_dir / "evidence/validation_summary.json").read_text())
    assert validation["valid"] is True
    assert validation["timing"] == {
        "wns_ns": -0.125,
        "tns_ns": -2.5,
        "failing_endpoints": 3,
    }
    assert validation["route_status"]["is_fully_routed"] is True
    assert validation["route_status"]["unrouted_nets"] == 0
    assert validation["utilization"]["resources"]["CLB LUTs"] == 1234

    trace_rows = [
        json.loads(line)
        for line in (run_dir / "evidence/backend_calls.jsonl").read_text().splitlines()
    ]
    assert len(trace_rows) == 20
    assert [row["sequence"] for row in trace_rows] == list(range(20))
    assert all(row["run_id"] == result["run_id"] for row in trace_rows)
    assert all(row["spec_hash"] == result["spec_hash"] for row in trace_rows)
    process_evidence = json.loads(
        (run_dir / "evidence/process_lifecycle.json").read_text()
    )
    host_token = process_evidence["process"]["root_process_token"]
    hash_tokens = [
        item["root_process_token"]
        for item in process_evidence["validation_hash_processes"]
    ]
    terminal_rows = [
        row for row in trace_rows if row["record_type"] == "HOST_CALL_TERMINAL"
    ]
    hash_terminal_rows = [
        row for row in terminal_rows if row["operation_id"] == "validation.hash_root"
    ]
    assert [
        row["operation_process_binding"]["root_process_token_hash"]
        for row in hash_terminal_rows
    ] == hash_tokens
    for row in terminal_rows:
        binding = row["operation_process_binding"]
        if row["operation_id"].startswith("vivado.") or row[
            "operation_id"
        ].startswith("host.session."):
            assert binding == {
                "binding_type": "owned_vivado_session",
                "root_process_token_hash": host_token,
                "session_owned": True,
            }
        elif row["operation_id"] == "validation.validate_root":
            assert binding == {
                "binding_type": "in_process_validation",
                "observer_process_token_hash": process_evidence[
                    "observer_process_token_hash"
                ],
            }
    public_bytes = json.dumps(result, sort_keys=True).encode()
    for sidecar in (run_dir / "evidence").iterdir():
        public_bytes += sidecar.read_bytes()
    for private in (
        str(dcp),
        str(run_dir),
        "/private/tools/Vivado/2025.1/bin/vivado",
        sys.executable,
    ):
        assert private.encode() not in public_bytes

    with SQLiteRunStore(run_dir / "run.db", migrate=False) as store:
        assert store.schema_version == CURRENT_SCHEMA_VERSION
        assert store.integrity_check() == ()
        assert store.actions() == ()
        assert store.proposals() == ()
        assert store.attempts() == ()
        assert store.outcomes() == ()
        assert len(store.states()) == 1
        assert store.states()[0].artifact_id == "artifact-root"
        assert store.states()[0].parent_state_id is None
        assert len(store.selections()) == 1
        assert store.selections()[0].artifact_id == "artifact-root"
        assert [item.status for item in store.validations()] == [
            ValidationStatus.NOT_RUN,
            ValidationStatus.PASSED,
        ]
        assert store.run_events()[-1].status is RunStatus.FINALIZED
        verification = verify_registered_evidence(
            store,
            run_dir=run_dir,
            expected_run_id=result["run_id"],
            expected_spec_hash=result["spec_hash"],
            required_roles=PREFLIGHT_REQUIRED_ROLES,
        )
        assert verification.errors == ()


@pytest.mark.parametrize(
    ("route_report", "expected_unrouted"),
    [
        ("Design is not fully routed", None),
        ("Design is partially routed", None),
        ("Design is not routed", None),
        ("Design is unrouted", None),
        ("Design is fully routed\n# of unrouted nets : 7", 7),
    ],
)
def test_route_validation_rejects_negative_status_phrases(
    route_report: str,
    expected_unrouted: int | None,
) -> None:
    parsed = preflight_module._parse_route(route_report)

    assert parsed == {
        "is_fully_routed": False,
        "unrouted_nets": expected_unrouted,
    }

@pytest.mark.parametrize(
    ("startup_stdout", "expected_endpoint"),
    (
        (
            (
                f"{PYTHON_VERSION_PREFIX}3.12.3\n"
                f"{INIT_POLICY_LINE}\n"
                f"{LAUNCHED_LINE}\n{STARTED_LINE}\n"
                f"{VERSION_PREFIX}2024.2\n{READY_LINE}\n"
            ).encode("ascii"),
            "TOOL_VERSION_MISMATCH",
        ),
        (
            (
                f"{PYTHON_VERSION_PREFIX}3.12.3\n"
                f"{LAUNCHED_LINE}\n{STARTED_LINE}\n"
                f"{VERSION_PREFIX}2025.1\n{READY_LINE}\n"
            ).encode("ascii"),
            "STARTUP_POLICY_VIOLATION",
        ),
        (
            (
                f"{PYTHON_VERSION_PREFIX}3.12.3\n"
                f"{INIT_POLICY_LINE}\n"
                f"{EFFECTIVE_ENV_PREFIX}{'0' * 64}\n"
                f"{LAUNCHED_LINE}\n{STARTED_LINE}\n"
                f"{VERSION_PREFIX}2025.1\n{READY_LINE}\n"
            ).encode("ascii"),
            "STARTUP_ATTESTATION_FAILURE",
        ),
    ),
)
def test_startup_attestation_fails_before_open_and_never_selects_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    startup_stdout: bytes,
    expected_endpoint: str,
) -> None:
    dcp = tmp_path / "root.dcp"
    dcp.write_bytes(b"frozen attestation fixture")
    plan = _write_plan(tmp_path, _plan_payload(dcp))
    sessions: list[_FakeSession] = []
    _patch_clean_repository(monkeypatch)

    result = run_l0_real_preflight(
        plan,
        dcp_path=dcp,
        run_dir=tmp_path / "run",
        vivado_exec="/not-real/vivado",
        python_exec=sys.executable,
        repository_anchor=tmp_path,
        session_factory=_factory(sessions, startup_stdout=startup_stdout),
    )

    assert result["endpoint"] == expected_endpoint
    assert result["endpoint_success"] is False
    assert result["call_reconciliation"]["ok"] is False
    assert result["vivado_starts"] == 1
    assert sessions[0].operations == []
    recovery_close = _recovery_close_rows(tmp_path / "run")
    assert [row["record_type"] for row in recovery_close] == [
        "HOST_CALL_START",
        "HOST_CALL_TERMINAL",
    ]
    assert recovery_close[0]["event_class"] == "INFRASTRUCTURE"
    assert recovery_close[-1]["status"] == "SUCCEEDED"
    assert recovery_close[-1]["host_callable_entered"] is True
    assert recovery_close[-1]["operation_process_binding"]["binding_type"] == (
        "owned_vivado_session"
    )
    assert result["selected_state_id"] is None
    with SQLiteRunStore(tmp_path / "run" / "run.db", migrate=False) as store:
        assert store.selections() == ()
        assert store.eligibility()[0].selection_eligible is False


def test_success_claim_requires_cross_evidence_process_ownership_event(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dcp = tmp_path / "root.dcp"
    dcp.write_bytes(b"frozen semantic verifier fixture")
    plan = _write_plan(tmp_path, _plan_payload(dcp))
    sessions: list[_FakeSession] = []
    _patch_clean_repository(monkeypatch)

    result = run_l0_real_preflight(
        plan,
        dcp_path=dcp,
        run_dir=tmp_path / "run",
        vivado_exec="/not-real/vivado",
        python_exec=sys.executable,
        repository_anchor=tmp_path,
        session_factory=_factory(sessions, ownership_verified=False),
    )

    assert result["endpoint"] == "EVIDENCE_INTEGRITY_FAILURE"
    assert result["endpoint_success"] is False
    assert result["semantic_success_checks"]["errors"] == [
        "semantic:host_process_cleanup_or_ownership_invalid"
    ]
    decision_events = [
        json.loads(line)
        for line in (tmp_path / "run/evidence/decision_events.jsonl")
        .read_text()
        .splitlines()
    ]
    endpoint_events = [
        row for row in decision_events if row["record_type"] == "RUN_ENDPOINT"
    ]
    assert len(endpoint_events) == 1
    assert endpoint_events[0]["endpoint"] == result["endpoint"]
    with SQLiteRunStore(tmp_path / "run" / "run.db", migrate=False) as store:
        assert store.selections() == ()
        assert store.run_events()[-1].status is RunStatus.FAILED


@pytest.mark.parametrize(
    ("section", "field", "invalid"),
    (
        ("process_limits", "initialization_timeout_seconds", "5"),
        ("process_limits", "graceful_close_seconds", True),
        ("process_limits", "output_limit_bytes", False),
        ("process_limits", "output_limit_bytes", 65_535),
        ("process_limits", "output_limit_bytes", 16 * 1024 * 1024 + 1),
        ("process_limits", "whole_run_timeout_seconds", 1200.01),
        ("event_limits", "MUTATION_LIGHT", False),
        ("call_timeouts_seconds", "vivado.open_checkpoint", True),
    ),
)
def test_plan_rejects_coercible_or_out_of_contract_limits(
    tmp_path: Path,
    section: str,
    field: str,
    invalid: object,
) -> None:
    dcp = tmp_path / "root.dcp"
    dcp.write_bytes(b"fixture")
    payload = _plan_payload(dcp)
    payload[section][field] = invalid
    with pytest.raises(PreflightPlanError):
        RealPreflightPlan.from_dict(payload)


@pytest.mark.parametrize("kind", ("initialization", "cleanup"))
def test_plan_timeout_envelope_covers_start_and_cleanup_graces(
    tmp_path: Path, kind: str
) -> None:
    dcp = tmp_path / "root.dcp"
    dcp.write_bytes(b"fixture")
    payload = _plan_payload(dcp)
    if kind == "initialization":
        payload["process_limits"]["initialization_timeout_seconds"] = 5.1
        expected = "initialization timeout exceeds"
    else:
        payload["process_limits"]["cleanup_timeout_seconds"] = 5.0
        expected = "cleanup total must be below"
    with pytest.raises(PreflightPlanError, match=expected):
        RealPreflightPlan.from_dict(payload)


def test_plan_cleanup_total_covers_stages_and_leaves_outer_hard_timeout_margin(
    tmp_path: Path,
) -> None:
    dcp = tmp_path / "root.dcp"
    dcp.write_bytes(b"fixture")
    payload = _plan_payload(dcp)

    payload["process_limits"]["cleanup_timeout_seconds"] = 2.99
    with pytest.raises(PreflightPlanError, match="does not cover"):
        RealPreflightPlan.from_dict(payload)

    payload = _plan_payload(dcp)
    payload["process_limits"]["cleanup_timeout_seconds"] = 5.0
    with pytest.raises(PreflightPlanError, match="below host.session.close"):
        RealPreflightPlan.from_dict(payload)


@pytest.mark.parametrize(
    ("field", "invalid"),
    (
        ("source_regime", "/private/inventory/fpl26"),
        ("selection_rule", "smallest design; api_key=do-not-leak"),
        ("environment_profile_id", "file:///private/.env"),
    ),
)
def test_plan_public_labels_are_canonical_and_cannot_carry_paths_or_secrets(
    tmp_path: Path, field: str, invalid: str
) -> None:
    dcp = tmp_path / "root.dcp"
    dcp.write_bytes(b"fixture")
    payload = _plan_payload(dcp)
    if field == "source_regime":
        payload["input_dcp"][field] = invalid
    else:
        payload[field] = invalid
    with pytest.raises(PreflightPlanError, match=f"{field} is not canonical"):
        RealPreflightPlan.from_dict(payload)


def test_environment_profile_must_bind_expected_vivado_version(
    tmp_path: Path,
) -> None:
    dcp = tmp_path / "root.dcp"
    dcp.write_bytes(b"fixture")
    payload = _plan_payload(dcp)
    payload["environment_profile_id"] = "april-vivado-2024.2"
    with pytest.raises(PreflightPlanError, match="does not bind"):
        RealPreflightPlan.from_dict(payload)


def test_repository_mismatch_fails_before_dcp_or_backend_access(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    missing_dcp = tmp_path / "must-not-be-read.dcp"
    placeholder = tmp_path / "placeholder.dcp"
    placeholder.write_bytes(b"only used to construct the plan")
    plan = _write_plan(tmp_path, _plan_payload(placeholder))
    run_dir = tmp_path / "run"
    monkeypatch.setattr(
        preflight_module,
        "observe_repository",
        lambda _anchor: _observation(head="c" * 40),
    )

    with pytest.raises(RepositoryConstraintMismatch, match="required_head_mismatch"):
        run_l0_real_preflight(
            plan,
            dcp_path=missing_dcp,
            run_dir=run_dir,
            vivado_exec="/not-started/vivado",
            session_factory=lambda *_args, **_kwargs: pytest.fail(
                "backend factory must not be entered"
            ),
        )
    assert not run_dir.exists()


def test_root_identity_mismatch_fails_before_output_or_backend_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dcp = tmp_path / "root.dcp"
    dcp.write_bytes(b"actual fixture")
    payload = _plan_payload(dcp, expected_sha256=_sha(b"different fixture"))
    plan = _write_plan(tmp_path, payload)
    run_dir = tmp_path / "run"
    _patch_clean_repository(monkeypatch)

    result = run_l0_real_preflight(
        plan,
        dcp_path=dcp,
        run_dir=run_dir,
        vivado_exec="/not-started/vivado",
        session_factory=lambda *_args, **_kwargs: pytest.fail(
            "backend factory must not be entered"
        ),
    )
    assert result["endpoint"] == "INVALID_ARTIFACT"
    assert result["endpoint_success"] is False
    assert run_dir.is_dir()
    with SQLiteRunStore(run_dir / "run.db", migrate=False) as store:
        assert store.selections() == ()
        assert store.eligibility()[0].selection_eligible is False


def test_fake_call_timeout_finalizes_typed_failed_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dcp = tmp_path / "root.dcp"
    dcp.write_bytes(b"frozen timeout fixture")
    plan = _write_plan(tmp_path, _plan_payload(dcp))
    run_dir = tmp_path / "run"
    sessions: list[_FakeSession] = []
    _patch_clean_repository(monkeypatch)

    result = run_l0_real_preflight(
        plan,
        dcp_path=dcp,
        run_dir=run_dir,
        vivado_exec="/not-real/vivado",
        repository_anchor=tmp_path,
        session_factory=_factory(
            sessions, fail_operation="vivado.report_timing_summary"
        ),
    )

    assert result["endpoint"] == "TIMEOUT"
    assert result["endpoint_success"] is False
    assert result["call_reconciliation"]["ok"] is False
    assert result["call_reconciliation"]["observed_terminal_status_counts"] == {
        "SUCCEEDED": 5,
        "FAILED": 0,
        "TIMED_OUT": 1,
        "CANCELLED": 0,
        "REJECTED_BEFORE_CALL": 0,
    }
    assert result["mutation_calls"] == result["heavy_calls"] == 0
    assert result["budget"]["used"]["VALIDATION"] == 2
    assert result["validation_hash_process_starts"] == 2
    fallback_trace = [
        json.loads(line)
        for line in (run_dir / "evidence/backend_calls.jsonl").read_text().splitlines()
    ]
    fallback_rows = [
        row for row in fallback_trace if row.get("recovery_after_failure") is True
    ]
    assert [
        (row["operation_id"], row["record_type"]) for row in fallback_rows
    ] == [
        ("host.session.close", "HOST_CALL_START"),
        ("host.session.close", "HOST_CALL_TERMINAL"),
        ("validation.hash_root", "HOST_CALL_START"),
        ("validation.hash_root", "HOST_CALL_TERMINAL"),
    ]
    recovery_close = fallback_rows[:2]
    assert [row["record_type"] for row in recovery_close] == [
        "HOST_CALL_START",
        "HOST_CALL_TERMINAL",
    ]
    assert recovery_close[0]["event_class"] == "INFRASTRUCTURE"
    assert recovery_close[-1]["status"] == "SUCCEEDED"
    assert recovery_close[-1]["operation_process_binding"]["binding_type"] == (
        "owned_vivado_session"
    )
    assert result["budget"]["infrastructure_calls"] == 2
    assert fallback_rows[-1]["operation_process_binding"][
        "root_process_token_hash"
    ] == json.loads(
        (run_dir / "evidence/process_lifecycle.json").read_text()
    )["validation_hash_processes"][-1]["root_process_token"]
    assert result["action_attempts"] == result["external_model_requests"] == 0
    assert json.loads(
        (run_dir / "evidence/process_lifecycle.json").read_text()
    )["process"]["endpoint"] == "CALL_TIMEOUT"
    with SQLiteRunStore(run_dir / "run.db", migrate=False) as store:
        assert store.run_events()[-1].status is RunStatus.FAILED
        assert store.attempts() == ()
        assert store.outcomes() == ()
        assert store.selections() == ()
        assert len(store.eligibility()) == 1
        assert store.eligibility()[0].parent_eligible is False
        assert store.eligibility()[0].selection_eligible is False
    assert result["selected_state_id"] is None
    assert result["selected_artifact_id"] is None


def test_bounded_fingerprint_cancellation_preserves_started_process_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dcp = tmp_path / "root.dcp"
    dcp.write_bytes(b"cancelled hash fixture")
    cancelled_result = _process_result(ProcessEndpoint.CANCELLED)

    class CancellingRunner:
        def __init__(self, _limits: object) -> None:
            pass

        async def run(self, *_args: object, **_kwargs: object) -> OwnedProcessResult:
            raise OwnedProcessCancelled(cancelled_result)

    monkeypatch.setattr(preflight_module, "OwnedProcessRunner", CancellingRunner)
    sink: list[OwnedProcessResult] = []

    async def invoke() -> None:
        with pytest.raises(OwnedProcessCancelled):
            await preflight_module._bounded_fingerprint(
                dcp,
                python_exec=Path(sys.executable),
                cwd=tmp_path,
                timeout_seconds=1.0,
                output_limit_bytes=64 * 1024,
                result_sink=sink,
            )

    import asyncio

    asyncio.run(invoke())
    assert sink == [cancelled_result]
    assert sink[0].root_process_token == cancelled_result.root_process_token


def test_bounded_fingerprint_worker_receives_no_parent_secrets_or_licenses(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dcp = tmp_path / "root.dcp"
    dcp.write_bytes(b"bounded fingerprint environment fixture")
    observed = {}
    payload = json.dumps(
        {
            "schema_version": "dof.bounded-file-hash.v1",
            "sha256": _sha(dcp.read_bytes()),
            "size_bytes": dcp.stat().st_size,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode() + b"\n"

    class RecordingRunner:
        def __init__(self, _limits: object) -> None:
            pass

        async def run(self, *_args: object, **kwargs: object) -> OwnedProcessResult:
            observed["environment"] = dict(kwargs["env"])
            return _process_result(stdout_override=payload)

    for key, value in {
        "OPENAI_API_KEY": "provider-secret",
        "DATABASE_URL": "database-secret",
        "LM_LICENSE_FILE": "license-secret",
        "XILINXD_LICENSE_FILE": "license-secret",
        "PYTHONPATH": "/private/modules",
    }.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(preflight_module, "OwnedProcessRunner", RecordingRunner)

    fingerprint, _result = __import__("asyncio").run(
        preflight_module._bounded_fingerprint(
            dcp,
            python_exec=Path(sys.executable),
            cwd=tmp_path,
            timeout_seconds=1.0,
            output_limit_bytes=64 * 1024,
        )
    )
    assert fingerprint.sha256 == _sha(dcp.read_bytes())
    assert observed["environment"] == {
        "PATH": __import__("os").defpath,
        "LANG": "C",
        "LC_ALL": "C",
        "PYTHONNOUSERSITE": "1",
    }


def test_root_mutation_is_recorded_with_changed_after_fingerprint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dcp = tmp_path / "root.dcp"
    original = b"frozen root before fake mutation"
    dcp.write_bytes(original)
    plan = _write_plan(tmp_path, _plan_payload(dcp))
    run_dir = tmp_path / "run"
    sessions: list[_FakeSession] = []
    _patch_clean_repository(monkeypatch)

    result = run_l0_real_preflight(
        plan,
        dcp_path=dcp,
        run_dir=run_dir,
        vivado_exec="/not-real/vivado",
        repository_anchor=tmp_path,
        session_factory=_factory(
            sessions,
            mutate_on_operation=("vivado.report_utilization", dcp),
        ),
    )

    assert result["endpoint"] == "ROOT_ARTIFACT_MUTATION"
    assert result["endpoint_success"] is False
    root = json.loads((run_dir / "evidence/root_integrity.json").read_text())
    assert root["before"]["sha256"] == _sha(original)
    assert root["after"]["sha256"] == _sha(dcp.read_bytes())
    assert root["before"] != root["after"]
    assert root["unchanged"] is False
    with SQLiteRunStore(run_dir / "run.db", migrate=False) as store:
        assert store.run_events()[-1].status is RunStatus.FAILED
        assert store.attempts() == ()
        assert store.outcomes() == ()


def test_cleanup_failure_endpoint_matches_decision_and_result_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dcp = tmp_path / "root.dcp"
    dcp.write_bytes(b"frozen cleanup fixture")
    plan = _write_plan(tmp_path, _plan_payload(dcp))
    run_dir = tmp_path / "run"
    sessions: list[_FakeSession] = []
    _patch_clean_repository(monkeypatch)

    result = run_l0_real_preflight(
        plan,
        dcp_path=dcp,
        run_dir=run_dir,
        vivado_exec="/not-real/vivado",
        repository_anchor=tmp_path,
        session_factory=_factory(
            sessions, cleanup_errors=("injected_cleanup_failure",)
        ),
    )

    assert result["endpoint"] == "PROCESS_CLEANUP_FAILURE"
    events = [
        json.loads(line)
        for line in (run_dir / "evidence/decision_events.jsonl").read_text().splitlines()
    ]
    assert events[-1]["endpoint"] == result["endpoint"]
    assert result["endpoint_success"] is False
    assert "owned_process_cleanup_not_ok" in result["reasons"]


def test_final_repository_mismatch_is_typed_and_evidence_is_finalized(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dcp = tmp_path / "root.dcp"
    dcp.write_bytes(b"frozen final repository check fixture")
    plan = _write_plan(tmp_path, _plan_payload(dcp))
    run_dir = tmp_path / "run"
    sessions: list[_FakeSession] = []
    observations = iter(
        (_observation(), _observation(untracked_entry_count=1))
    )
    monkeypatch.setattr(
        preflight_module,
        "observe_repository",
        lambda _anchor: next(observations),
    )

    result = run_l0_real_preflight(
        plan,
        dcp_path=dcp,
        run_dir=run_dir,
        vivado_exec="/not-real/vivado",
        repository_anchor=tmp_path,
        session_factory=_factory(sessions),
    )

    assert result["endpoint"] == "DECLARED_CONSTRAINT_MISMATCH"
    assert result["endpoint_success"] is False
    final_check = json.loads(
        (run_dir / "evidence/repository_final_check.json").read_text()
    )
    assert final_check == {
        "schema_version": "dof.repository-final-check.v1",
        "status": "CONSTRAINT_MISMATCH",
        "constraint_satisfied": False,
        "tracked_identity_match": True,
        "observation_sha256": _observation(untracked_entry_count=1).sha256,
        "reason_codes": ["untracked_entries_not_permitted"],
    }
    assert result["repository_final_check"]["sha256"] == _sha(
        (run_dir / "evidence/repository_final_check.json").read_bytes()
    )
    decision = [
        json.loads(line)
        for line in (run_dir / "evidence/decision_events.jsonl").read_text().splitlines()
    ]
    assert decision[-1]["endpoint"] == result["endpoint"]
    with SQLiteRunStore(run_dir / "run.db", migrate=False) as store:
        assert store.run_events()[-1].status is RunStatus.FAILED
        verification = verify_registered_evidence(
            store,
            run_dir=run_dir,
            expected_run_id=result["run_id"],
            expected_spec_hash=result["spec_hash"],
            required_roles=PREFLIGHT_REQUIRED_ROLES,
        )
        assert verification.errors == ()


def test_truncated_protocol_output_cannot_be_accepted_as_valid_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dcp = tmp_path / "root.dcp"
    dcp.write_bytes(b"frozen truncated output fixture")
    plan = _write_plan(tmp_path, _plan_payload(dcp))
    run_dir = tmp_path / "run"
    sessions: list[_FakeSession] = []
    _patch_clean_repository(monkeypatch)

    result = run_l0_real_preflight(
        plan,
        dcp_path=dcp,
        run_dir=run_dir,
        vivado_exec="/not-real/vivado",
        repository_anchor=tmp_path,
        session_factory=_factory(
            sessions,
            truncate_operation="vivado.report_timing_summary",
        ),
    )

    assert result["endpoint"] == "EVIDENCE_INTEGRITY_FAILURE"
    assert result["endpoint_success"] is False
    assert result["call_reconciliation"]["ok"] is False
    assert result["call_reconciliation"]["observed_terminal_status_counts"][
        "FAILED"
    ] == 1
    recovery_close = _recovery_close_rows(run_dir)
    assert [row["record_type"] for row in recovery_close] == [
        "HOST_CALL_START",
        "HOST_CALL_TERMINAL",
    ]
    assert recovery_close[0]["event_class"] == "INFRASTRUCTURE"
    assert recovery_close[-1]["status"] == "SUCCEEDED"
    assert recovery_close[-1]["operation_process_binding"]["binding_type"] == (
        "owned_vivado_session"
    )
    assert result["mutation_calls"] == result["heavy_calls"] == 0
    with SQLiteRunStore(run_dir / "run.db", migrate=False) as store:
        assert store.run_events()[-1].status is RunStatus.FAILED
        assert store.attempts() == ()


def test_failure_recovery_close_has_outer_hard_timeout_and_typed_trace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dcp = tmp_path / "root.dcp"
    dcp.write_bytes(b"frozen bounded recovery-close fixture")
    payload = _plan_payload(dcp)
    payload["process_limits"].update(
        {
            "graceful_close_seconds": 0.005,
            "terminate_grace_seconds": 0.005,
            "kill_grace_seconds": 0.005,
            "cleanup_timeout_seconds": 0.02,
        }
    )
    payload["call_timeouts_seconds"]["host.session.close"] = 0.05
    plan = _write_plan(tmp_path, payload)
    run_dir = tmp_path / "run"
    sessions: list[_FakeSession] = []
    _patch_clean_repository(monkeypatch)

    started = time.monotonic()
    result = run_l0_real_preflight(
        plan,
        dcp_path=dcp,
        run_dir=run_dir,
        vivado_exec="/not-real/vivado",
        repository_anchor=tmp_path,
        session_factory=_factory(
            sessions,
            truncate_operation="vivado.report_timing_summary",
            hang_close=True,
        ),
    )
    elapsed = time.monotonic() - started

    scheduler_slack_seconds = 2.0
    declared_cleanup_envelope = (
        payload["call_timeouts_seconds"]["host.session.close"]
        + payload["process_limits"]["cleanup_timeout_seconds"]
    )
    assert elapsed < declared_cleanup_envelope + scheduler_slack_seconds
    assert result["endpoint"] == "PROCESS_CLEANUP_FAILURE"
    assert result["endpoint_success"] is False
    recovery_close = _recovery_close_rows(run_dir)
    assert [row["record_type"] for row in recovery_close] == [
        "HOST_CALL_START",
        "HOST_CALL_TERMINAL",
    ]
    assert recovery_close[0]["timeout_seconds"] == 0.05
    assert recovery_close[-1]["status"] == "TIMED_OUT"
    assert recovery_close[-1]["host_callable_entered"] is True
    assert result["call_reconciliation"]["ok"] is False
    assert "recovery_after_failure:host.session.close" in result[
        "call_reconciliation"
    ]["errors"]
