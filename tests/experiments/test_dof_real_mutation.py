from __future__ import annotations

import base64
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import platform
import sys

import pytest

from optimizer_pipeline.dof.artifacts import RootFingerprint
from optimizer_pipeline.dof.processes import (
    CapturedOutput,
    OwnedProcessResult,
    OwnedProtocolSessionError,
    ProcessEndpoint,
    ProtocolCallResult,
)
from optimizer_pipeline.dof import real_mutation
from optimizer_pipeline.dof.mutation_worker_binding import (
    build_mutation_worker_environment,
    content_hash,
)
from optimizer_pipeline.dof.real_mutation import (
    EXPECTED_ROOT_SHA256,
    EXPECTED_ROOT_SIZE,
    RealMutationPlanError,
    build_e0_dispatch_authorization,
    execute_e0_real_action,
    execute_e0_real_gated_plan,
    _verify_worker_attestation,
    execute_real_mutation,
    freeze_e0_real_action_plan,
    freeze_e0_real_gated_plan,
    freeze_real_mutation_plan,
    validate_e0_real_action_plan,
    validate_e0_real_gated_plan,
    validate_real_mutation_plan,
)
from optimizer_pipeline.dof.contracts import DoFLevel
from optimizer_pipeline.dof.vivado_mutation_worker import (
    EFFECTIVE_ENV_PREFIX,
    INIT_POLICY_LINE,
    LAUNCHED_LINE,
    PYTHON_VERSION_PREFIX,
    READY_LINE,
    STARTED_LINE,
    STATUS_PREFIX,
    VERSION_PREFIX,
    WORKER_PROTOCOL_PREFIX,
    WORKER_TRANSPORT_PROTOCOL_VERSION,
    WORKER_VERSION,
    WORKER_VERSION_PREFIX,
    effective_vivado_environment_hash,
    parse_request_line,
)


@dataclass(frozen=True)
class _Repository:
    head_commit: str = "a" * 40
    head_tree: str = "b" * 40
    binary_diff_sha256: str = "c" * 64
    tracked_tree_dirty: bool = False
    branch_name: str = "phase0-policy-evidence-replan"
    schema_version: str = "dof.repository-provenance.v1"
    sha256: str = "d" * 64

    def to_dict(self):
        return {
            "schema_version": self.schema_version,
            "collector_version": "dof.git-observer.v1",
            "observed_at": "2026-01-01T00:00:00+00:00",
            "repository_mode": "git",
            "repository_root_identity_sha256": "e" * 64,
            "head_commit": self.head_commit,
            "head_tree": self.head_tree,
            "ref_state": "branch",
            "branch_name": self.branch_name,
            "tracked_cleanliness_policy": "git-tracked-only.v1",
            "tracked_tree_dirty": self.tracked_tree_dirty,
            "tracked_status_summary": {
                "staged_entry_count": 0,
                "unstaged_entry_count": 0,
                "conflicted_entry_count": 0,
                "assume_unchanged_entry_count": 0,
                "skip_worktree_entry_count": 0,
            },
            "tracked_status_sha256": hashlib.sha256(b"").hexdigest(),
            "index_flag_status_sha256": "f" * 64,
            "binary_diff_sha256": self.binary_diff_sha256,
            "submodule_status_sha256": "1" * 64,
            "submodule_summary": {"submodule_count": 0, "nonclean_submodule_count": 0},
            "untracked_summary": {"entry_count": 0, "status_sha256": "2" * 64, "affects_tracked_cleanliness": False},
            "git_version": "git version test",
        }


class _Guard:
    before = RootFingerprint(EXPECTED_ROOT_SHA256, EXPECTED_ROOT_SIZE)


def _captured(data: bytes) -> CapturedOutput:
    return CapturedOutput(data, hashlib.sha256(data).hexdigest(), len(data), False)


def _process_result(
    endpoint: ProcessEndpoint = ProcessEndpoint.SUCCEEDED,
    returncode: int = 0,
) -> OwnedProcessResult:
    token = "3" * 64
    return OwnedProcessResult(
        endpoint,
        returncode,
        "4" * 64,
        "5" * 64,
        "fake-vivado-worker",
        token,
        True,
        0.1,
        ({"event_type": "PROCESS_STARTED", "root_process_token": token},),
        (token,),
        (),
        _captured(b""),
        _captured(b""),
        (),
    )


class _FakeSession:
    last_environment = None

    def __init__(self, argv, **kwargs):
        self.argv = tuple(argv)
        self.kwargs = kwargs
        type(self).last_environment = dict(kwargs["env"])
        home = Path(self.argv[self.argv.index("--isolated-home") + 1])
        environment_hash = effective_vivado_environment_hash(
            kwargs["env"], home
        )
        startup = b"\n".join(
            (
                (PYTHON_VERSION_PREFIX + platform.python_version()).encode(),
                (WORKER_VERSION_PREFIX + WORKER_VERSION).encode(),
                (
                    WORKER_PROTOCOL_PREFIX
                    + WORKER_TRANSPORT_PROTOCOL_VERSION
                ).encode(),
                INIT_POLICY_LINE.encode(),
                (EFFECTIVE_ENV_PREFIX + environment_hash).encode(),
                LAUNCHED_LINE.encode(),
                STARTED_LINE.encode(),
                (VERSION_PREFIX + "2025.1").encode(),
                READY_LINE.encode(),
            )
        ) + b"\n"
        self.startup_output = _captured(startup)
        self.result = None

    async def start(self):
        return self

    async def call(self, raw, *, terminal_marker, timeout_seconds):
        request = parse_request_line(raw.rstrip())
        if request.operation_id == "vivado.report_timing_summary":
            output = b"WNS(ns) TNS(ns) TNS Failing Endpoints\n0.125 0.000 0\n"
        elif request.operation_id == "vivado.report_route_status":
            output = b"Design is fully routed\n# of unrouted nets : 0\n"
        else:
            output = b"operation complete\n"
        if request.operation_id == "vivado.write_checkpoint":
            path = Path(base64.b64decode(request.arguments["dcp_path_b64"]).decode())
            path.write_bytes(b"fake valid child dcp\n")
        output += f"{STATUS_PREFIX}{request.call_token}:0\n".encode()
        return ProtocolCallResult(
            request.call_token,
            hashlib.sha256(raw).hexdigest(),
            hashlib.sha256(terminal_marker).hexdigest(),
            0.01,
            _captured(output),
        )

    async def close(self):
        self.result = _process_result()
        return self.result


class _PreReadyFailureSession(_FakeSession):
    async def start(self):
        self.result = _process_result(ProcessEndpoint.FAILED, 71)
        raise OwnedProtocolSessionError(
            "worker exited before READY", self.result
        )

    async def close(self):
        assert self.result is not None
        return self.result


class _FailAtPlaceSession(_FakeSession):
    operations = []

    async def call(self, raw, *, terminal_marker, timeout_seconds):
        request = parse_request_line(raw.rstrip())
        type(self).operations.append(request.operation_id)
        if request.operation_id == "vivado.place_design":
            raise RuntimeError("injected place failure")
        return await super().call(
            raw,
            terminal_marker=terminal_marker,
            timeout_seconds=timeout_seconds,
        )


async def _fingerprint(path: Path, **kwargs):
    if path.name == "technical-child-staging.dcp":
        data = path.read_bytes()
        fingerprint = RootFingerprint(hashlib.sha256(data).hexdigest(), len(data))
    else:
        fingerprint = RootFingerprint(EXPECTED_ROOT_SHA256, EXPECTED_ROOT_SIZE)
    result = _process_result()
    sink = kwargs.get("result_sink")
    if sink is not None:
        sink.append(result)
    return fingerprint, result


def _write_executable(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\nexit 0\n")
    path.chmod(0o700)
    return path


def _plan(monkeypatch, root: Path) -> tuple[dict, Path, Path]:
    repository = _Repository()
    monkeypatch.setattr(
        real_mutation,
        "fingerprint_file",
        lambda _path: RootFingerprint(EXPECTED_ROOT_SHA256, EXPECTED_ROOT_SIZE),
    )
    monkeypatch.setattr(real_mutation, "observe_repository", lambda _path: repository)
    vivado = _write_executable(
        root.parent / "Vivado" / "2025.1" / "bin" / "vivado"
    )
    run_dir = root.parent / "planned-run"
    plan = freeze_real_mutation_plan(
        root,
        repository_anchor=root.parent,
        python_exec=sys.executable,
        vivado_exec=vivado,
        run_dir=run_dir,
        fake_startup_attestation_sha256="9" * 64,
    )
    return plan, vivado, run_dir


def test_e0_generic_plan_freezes_and_executes_a_multi_call_macro(
    tmp_path: Path,
    monkeypatch,
) -> None:
    root = tmp_path / "root.dcp"
    root.write_bytes(b"private fixture placeholder")
    repository = _Repository()
    monkeypatch.setattr(
        real_mutation,
        "fingerprint_file",
        lambda _path: RootFingerprint(EXPECTED_ROOT_SHA256, EXPECTED_ROOT_SIZE),
    )
    monkeypatch.setattr(real_mutation, "observe_repository", lambda _path: repository)
    vivado = _write_executable(tmp_path / "Vivado" / "2025.1" / "bin" / "vivado")
    run_dir = tmp_path / "e0-real-plan-run"
    plan = freeze_e0_real_action_plan(
        root,
        root_pseudonym="e0-test-root",
        level=DoFLevel.L2,
        action_key="macro.place_route",
        parameters={},
        run_id="e0-test-l2-place-route",
        repository_anchor=tmp_path,
        python_exec=sys.executable,
        vivado_exec=vivado,
        run_dir=run_dir,
        fake_startup_attestation_sha256="9" * 64,
    )
    assert validate_e0_real_action_plan(plan) == plan
    assert plan["limits"]["event_limits"]["IMPLEMENTATION_HEAVY"] == 2
    assert plan["limits"]["event_limits"]["MUTATION_LIGHT"] == 2
    assert "vivado.place_design" in plan["limits"]["call_timeouts_seconds"]
    assert "vivado.route_design" in plan["limits"]["call_timeouts_seconds"]

    result = execute_e0_real_action(
        plan,
        root,
        run_dir,
        dispatch_authorization=build_e0_dispatch_authorization(
            plan, {"source": "bound-test-decision", "root_sha256": EXPECTED_ROOT_SHA256}
        ),
        causal_evidence={"source": "bound-test-decision", "root_sha256": EXPECTED_ROOT_SHA256},
        vivado_exec=vivado,
        python_exec=sys.executable,
        repository_anchor=tmp_path,
        session_factory=_FakeSession,
        fingerprint_function=_fingerprint,
        repository_observer=lambda _path: repository,
        root_guard_factory=lambda _path: _Guard(),
    )
    assert result["endpoint_success"] is True
    assert result["pilot_non_claim"] is True
    assert result["real_mutation_invocations"] == 2
    assert result["external_model_requests"] == 1
    assert result["root_unchanged"] is True
    assert result["sqlite_quick_check"] == "ok"
    assert result["foreign_key_errors"] == 0


def test_e0_bound_multistep_l3_plan_executes_only_with_exact_authorization(
    tmp_path: Path,
    monkeypatch,
) -> None:
    root = tmp_path / "root.dcp"
    root.write_bytes(b"private fixture placeholder")
    repository = _Repository()
    monkeypatch.setattr(
        real_mutation,
        "fingerprint_file",
        lambda _path: RootFingerprint(EXPECTED_ROOT_SHA256, EXPECTED_ROOT_SIZE),
    )
    monkeypatch.setattr(real_mutation, "observe_repository", lambda _path: repository)
    vivado = _write_executable(tmp_path / "Vivado" / "2025.1" / "bin" / "vivado")
    run_dir = tmp_path / "bound-l3-run"
    causal = {
        "job_spec_sha256": "1" * 64,
        "observation_sha256": "2" * 64,
        "context_sha256": "3" * 64,
        "request_contract_hash": "4" * 64,
        "raw_response_sha256": "5" * 64,
        "proposal_sha256": "6" * 64,
    }
    plan = freeze_e0_real_gated_plan(
        root,
        root_pseudonym="e0-test-root",
        level=DoFLevel.L3,
        decision_id="decision-bound-l3",
        proposal_id="proposal-bound-l3",
        proposal_hash="6" * 64,
        gate_decision_sha256="7" * 64,
        gated_steps=(
            {"step_index": 0, "step_id": "s0", "action_key": "manual.unroute_all", "parameters": {}, "stage_before": "ROUTED", "stage_after": "PLACED"},
            {"step_index": 1, "step_id": "s1", "action_key": "manual.unplace_all", "parameters": {}, "stage_before": "PLACED", "stage_after": "UNPLACED"},
            {"step_index": 2, "step_id": "s2", "action_key": "manual.place_design", "parameters": {"directive": "Explore"}, "stage_before": "UNPLACED", "stage_after": "PLACED"},
            {"step_index": 3, "step_id": "s3", "action_key": "manual.route_design", "parameters": {"directive": "Default"}, "stage_before": "PLACED", "stage_after": "ROUTED"},
            {"step_index": 4, "step_id": "s4", "action_key": "manual.phys_opt_design", "parameters": {"directive": "AddRetime"}, "stage_before": "ROUTED", "stage_after": "ROUTED"},
        ),
        causal_evidence_sha256=real_mutation._hash(causal),
        run_id="e0-test-bound-l3",
        repository_anchor=tmp_path,
        python_exec=sys.executable,
        vivado_exec=vivado,
        run_dir=run_dir,
        fake_startup_attestation_sha256="9" * 64,
    )
    assert validate_e0_real_gated_plan(plan) == plan
    assert plan["limits"]["event_limits"]["IMPLEMENTATION_HEAVY"] == 3
    authorization = build_e0_dispatch_authorization(plan, causal)
    result = execute_e0_real_gated_plan(
        plan,
        root,
        run_dir,
        dispatch_authorization=authorization,
        causal_evidence=causal,
        vivado_exec=vivado,
        python_exec=sys.executable,
        repository_anchor=tmp_path,
        session_factory=_FakeSession,
        fingerprint_function=_fingerprint,
        repository_observer=lambda _path: repository,
        root_guard_factory=lambda _path: _Guard(),
    )
    assert result["endpoint_success"] is True
    assert result["real_mutation_invocations"] == 3


def test_e0_dispatch_tamper_is_rejected_before_backend_directory_creation(
    tmp_path: Path,
    monkeypatch,
) -> None:
    root = tmp_path / "root.dcp"
    root.write_bytes(b"private fixture placeholder")
    repository = _Repository()
    monkeypatch.setattr(
        real_mutation,
        "fingerprint_file",
        lambda _path: RootFingerprint(EXPECTED_ROOT_SHA256, EXPECTED_ROOT_SIZE),
    )
    monkeypatch.setattr(real_mutation, "observe_repository", lambda _path: repository)
    vivado = _write_executable(tmp_path / "Vivado" / "2025.1" / "bin" / "vivado")
    run_dir = tmp_path / "tamper-run"
    plan = freeze_e0_real_action_plan(
        root,
        root_pseudonym="e0-test-root",
        level=DoFLevel.L2,
        action_key="macro.phys_opt",
        parameters={},
        run_id="e0-test-tamper",
        repository_anchor=tmp_path,
        python_exec=sys.executable,
        vivado_exec=vivado,
        run_dir=run_dir,
        fake_startup_attestation_sha256="9" * 64,
    )
    causal = {"observation_sha256": "1" * 64, "proposal_sha256": "2" * 64}
    authorization = build_e0_dispatch_authorization(plan, causal)
    tampered = dict(authorization)
    tampered["plan_sha256"] = "3" * 64
    with pytest.raises(RealMutationPlanError, match="authorization_hash_mismatch"):
        execute_e0_real_action(
            plan,
            root,
            run_dir,
            dispatch_authorization=tampered,
            causal_evidence=causal,
            vivado_exec=vivado,
            python_exec=sys.executable,
            repository_anchor=tmp_path,
        )
    assert not run_dir.exists()


def test_e0_multistep_failure_never_dispatches_later_steps(
    tmp_path: Path,
    monkeypatch,
) -> None:
    root = tmp_path / "root.dcp"
    root.write_bytes(b"private fixture placeholder")
    repository = _Repository()
    monkeypatch.setattr(
        real_mutation,
        "fingerprint_file",
        lambda _path: RootFingerprint(EXPECTED_ROOT_SHA256, EXPECTED_ROOT_SIZE),
    )
    monkeypatch.setattr(real_mutation, "observe_repository", lambda _path: repository)
    vivado = _write_executable(tmp_path / "Vivado" / "2025.1" / "bin" / "vivado")
    run_dir = tmp_path / "failed-plan-run"
    causal = {"observation_sha256": "1" * 64, "proposal_sha256": "2" * 64}
    plan = freeze_e0_real_gated_plan(
        root,
        root_pseudonym="e0-test-root",
        level=DoFLevel.L3,
        decision_id="decision-failed-plan",
        proposal_id="proposal-failed-plan",
        proposal_hash="2" * 64,
        gate_decision_sha256="3" * 64,
        gated_steps=(
            {"step_index": 0, "step_id": "s0", "action_key": "manual.unroute_all", "parameters": {}, "stage_before": "ROUTED", "stage_after": "PLACED"},
            {"step_index": 1, "step_id": "s1", "action_key": "manual.unplace_all", "parameters": {}, "stage_before": "PLACED", "stage_after": "UNPLACED"},
            {"step_index": 2, "step_id": "s2", "action_key": "manual.place_design", "parameters": {"directive": "Explore"}, "stage_before": "UNPLACED", "stage_after": "PLACED"},
            {"step_index": 3, "step_id": "s3", "action_key": "manual.route_design", "parameters": {"directive": "Default"}, "stage_before": "PLACED", "stage_after": "ROUTED"},
        ),
        causal_evidence_sha256=real_mutation._hash(causal),
        run_id="e0-test-failed-plan",
        repository_anchor=tmp_path,
        python_exec=sys.executable,
        vivado_exec=vivado,
        run_dir=run_dir,
        fake_startup_attestation_sha256="9" * 64,
    )
    authorization = build_e0_dispatch_authorization(plan, causal)
    _FailAtPlaceSession.operations = []
    result = execute_e0_real_gated_plan(
        plan,
        root,
        run_dir,
        dispatch_authorization=authorization,
        causal_evidence=causal,
        vivado_exec=vivado,
        python_exec=sys.executable,
        repository_anchor=tmp_path,
        session_factory=_FailAtPlaceSession,
        fingerprint_function=_fingerprint,
        repository_observer=lambda _path: repository,
        root_guard_factory=lambda _path: _Guard(),
    )
    assert result["endpoint_success"] is False
    assert "vivado.place_design" in _FailAtPlaceSession.operations
    assert "vivado.route_design" not in _FailAtPlaceSession.operations


def test_frozen_plan_is_exact_hash_bound_and_rejects_tampering(
    tmp_path: Path,
    monkeypatch,
) -> None:
    root = tmp_path / "root.dcp"
    root.write_bytes(b"private fixture placeholder")
    plan, _vivado, _run_dir = _plan(monkeypatch, root)
    assert validate_real_mutation_plan(plan) == plan
    assert plan["action"]["action_key"] == "manual.phys_opt_design"
    assert plan["action"]["parameters"] == {"directive": "Explore"}
    assert plan["action"]["policy_source"] == "deterministic technical harness"
    assert plan["limits"]["event_limits"]["IMPLEMENTATION_HEAVY"] == 1
    worker = plan["bindings"]["worker_launch"]
    assert worker["worker_version"] == WORKER_VERSION
    assert (
        worker["transport_protocol_version"]
        == WORKER_TRANSPORT_PROTOCOL_VERSION
    )
    assert worker["production_argv_sha256"]
    assert worker["transitive_source_manifest"]["manifest_sha256"]
    assert worker["python_executable"]["version"] == platform.python_version()
    assert worker["fake_startup_attestation_sha256"] == "9" * 64
    tampered = json.loads(json.dumps(plan))
    tampered["action"]["parameters"]["directive"] = "AddRetime"
    with pytest.raises(RealMutationPlanError, match="plan_hash_mismatch"):
        validate_real_mutation_plan(tampered)


def test_fake_session_exercises_full_real_runner_without_selecting_child(
    tmp_path: Path,
    monkeypatch,
) -> None:
    root = tmp_path / "root.dcp"
    root.write_bytes(b"private fixture placeholder")
    plan, vivado, run_dir = _plan(monkeypatch, root)
    repository = _Repository()

    result = execute_real_mutation(
        plan,
        root,
        run_dir,
        vivado_exec=vivado,
        python_exec=sys.executable,
        repository_anchor=tmp_path,
        session_factory=_FakeSession,
        fingerprint_function=_fingerprint,
        repository_observer=lambda _path: repository,
        root_guard_factory=lambda _path: _Guard(),
    )

    assert result["endpoint"] == "TECHNICAL_MUTATION_VALID_NONSCIENTIFIC", (
        result["reasons"],
        result["call_reconciliation"],
    )
    assert result["real_mutation_invocations"] == 1
    assert result["external_model_requests"] == 0
    assert result["root_unchanged"] is True
    assert result["root_is_sole_selection"] is True
    assert result["child_selection_eligible"] is False
    assert result["transaction"]["valid"] is True
    assert result["transaction"]["selected"] is False
    assert result["call_reconciliation"]["ok"] is True
    assert result["sqlite_quick_check"] == "ok"
    assert result["foreign_key_errors"] == 0
    worker_environment = _FakeSession.last_environment
    assert worker_environment is not None
    assert set(worker_environment) <= {
        "PATH",
        "LANG",
        "LC_ALL",
        "XILINXD_LICENSE_FILE",
        "XILINXLM_LICENSE_FILE",
        "XILINX_LICENSE_FILE",
        "LM_LICENSE_FILE",
        "FLEXLM_TIMEOUT",
        "FLEXLM_BATCH",
    }
    assert not any(
        marker in key.upper()
        for key in worker_environment
        for marker in (
            "API_KEY",
            "TOKEN",
            "PASSWORD",
            "DATABASE",
            "RAPIDWRIGHT",
            "PYTHON",
            "JAVA",
        )
    )


def test_pre_ready_session_failure_is_initialization_not_execution(
    tmp_path: Path,
    monkeypatch,
) -> None:
    root = tmp_path / "root.dcp"
    root.write_bytes(b"private fixture placeholder")
    plan, vivado, run_dir = _plan(monkeypatch, root)
    repository = _Repository()

    result = execute_real_mutation(
        plan,
        root,
        run_dir,
        vivado_exec=vivado,
        python_exec=sys.executable,
        repository_anchor=tmp_path,
        session_factory=_PreReadyFailureSession,
        fingerprint_function=_fingerprint,
        repository_observer=lambda _path: repository,
        root_guard_factory=lambda _path: _Guard(),
    )

    assert result["endpoint"] == "TOOL_INITIALIZATION_FAILURE"
    assert result["worker_start_attempted"] is True
    assert result["worker_attested"] is False
    assert result["real_mutation_invocations"] == 0
    assert result["backend_dispatches"] == 0
    assert result["root_unchanged"] is True


def test_worker_attestation_rejects_duplicate_or_mismatched_protocol_marker(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    environment_hash = effective_vivado_environment_hash(
        build_mutation_worker_environment(os.environ), home
    )
    lines = [
        PYTHON_VERSION_PREFIX + platform.python_version(),
        WORKER_VERSION_PREFIX + WORKER_VERSION,
        WORKER_PROTOCOL_PREFIX + WORKER_TRANSPORT_PROTOCOL_VERSION,
        WORKER_PROTOCOL_PREFIX + "dof.invalid-protocol.v1",
        INIT_POLICY_LINE,
        EFFECTIVE_ENV_PREFIX + environment_hash,
        LAUNCHED_LINE,
        STARTED_LINE,
        VERSION_PREFIX + "2025.1",
        READY_LINE,
    ]
    with pytest.raises(
        real_mutation.RealMutationExecutionError,
        match="startup_attestation_mismatch",
    ):
        _verify_worker_attestation(
            ("\n".join(lines) + "\n").encode(),
            expected_vivado="2025.1",
            expected_python=platform.python_version(),
            expected_environment_hash=environment_hash,
            expected_worker=WORKER_VERSION,
            expected_protocol=WORKER_TRANSPORT_PROTOCOL_VERSION,
        )


def test_execution_rejects_tampered_frozen_worker_argv_before_session(
    tmp_path: Path,
    monkeypatch,
) -> None:
    root = tmp_path / "root.dcp"
    root.write_bytes(b"private fixture placeholder")
    plan, vivado, run_dir = _plan(monkeypatch, root)
    tampered = json.loads(json.dumps(plan))
    launch = tampered["bindings"]["worker_launch"]
    launch["production_argv_sha256"] = "8" * 64
    launch_payload = dict(launch)
    launch_payload.pop("binding_sha256")
    launch["binding_sha256"] = content_hash(launch_payload)
    plan_payload = dict(tampered)
    plan_payload.pop("plan_sha256")
    tampered["plan_sha256"] = real_mutation._hash(plan_payload)

    class _MustNotStart:
        def __init__(self, *_args, **_kwargs):
            raise AssertionError("session factory must not be reached")

    with pytest.raises(RealMutationPlanError, match="frozen_binding_mismatch"):
        execute_real_mutation(
            tampered,
            root,
            run_dir,
            vivado_exec=vivado,
            python_exec=sys.executable,
            repository_anchor=tmp_path,
            session_factory=_MustNotStart,
            fingerprint_function=_fingerprint,
            repository_observer=lambda _path: _Repository(),
            root_guard_factory=lambda _path: _Guard(),
        )
    assert not run_dir.exists()


@pytest.mark.parametrize(
    "field,value,error",
    [
        ("runner_version", "dof.wp004a-real-mutation-runner.other", "plan_identity_mismatch"),
        ("expected_tool_versions", {"vivado": "2024.2", "python": platform.python_version()}, "plan_tool_versions_mismatch"),
    ],
)
def test_rehashed_plan_cannot_change_runner_or_tool_versions(
    tmp_path: Path,
    monkeypatch,
    field,
    value,
    error,
) -> None:
    root = tmp_path / "root.dcp"
    root.write_bytes(b"private fixture placeholder")
    plan, _vivado, _run_dir = _plan(monkeypatch, root)
    plan[field] = value
    payload = dict(plan)
    payload.pop("plan_sha256")
    plan["plan_sha256"] = real_mutation._hash(payload)
    with pytest.raises(RealMutationPlanError, match=error):
        validate_real_mutation_plan(plan)


def test_rehashed_worker_environment_policy_is_rejected(
    tmp_path: Path,
    monkeypatch,
) -> None:
    root = tmp_path / "root.dcp"
    root.write_bytes(b"private fixture placeholder")
    plan, _vivado, _run_dir = _plan(monkeypatch, root)
    launch = plan["bindings"]["worker_launch"]
    policy = launch["environment_policy"]
    policy["policy_version"] = "dof.unbound-environment-policy.v1"
    policy_payload = dict(policy)
    policy_payload.pop("policy_sha256")
    policy["policy_sha256"] = content_hash(policy_payload)
    launch_payload = dict(launch)
    launch_payload.pop("binding_sha256")
    launch["binding_sha256"] = content_hash(launch_payload)
    plan_payload = dict(plan)
    plan_payload.pop("plan_sha256")
    plan["plan_sha256"] = real_mutation._hash(plan_payload)
    with pytest.raises(
        RealMutationPlanError, match="worker_environment_policy_mismatch"
    ):
        validate_real_mutation_plan(plan)


def test_rehashed_process_supervisor_binding_is_rejected(
    tmp_path: Path,
    monkeypatch,
) -> None:
    root = tmp_path / "root.dcp"
    root.write_bytes(b"private fixture placeholder")
    plan, _vivado, _run_dir = _plan(monkeypatch, root)
    launch = plan["bindings"]["worker_launch"]
    launch["process_supervisor"]["sha256"] = "9" * 64
    launch_payload = dict(launch)
    launch_payload.pop("binding_sha256")
    launch["binding_sha256"] = content_hash(launch_payload)
    plan_payload = dict(plan)
    plan_payload.pop("plan_sha256")
    plan["plan_sha256"] = real_mutation._hash(plan_payload)
    with pytest.raises(
        RealMutationPlanError, match="process_supervisor_binding_mismatch"
    ):
        validate_real_mutation_plan(plan)
