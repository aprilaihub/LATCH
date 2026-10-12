"""Exactly-one, read-only L0 real-DCP preflight with immutable evidence.

This adapter is deliberately separate from the mock scientific runner and the
legacy contest flow.  It accepts one strict public plan plus private local tool
paths, observes every fixed operation through :mod:`toolcalls`, owns one
persistent worker process tree, never exposes a mutation operation, and keeps
the canonical store at schema v2.
"""

from __future__ import annotations

import asyncio
import base64
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import platform
import re
import sys
from typing import Any

from optimizer_pipeline.core.domain import (
    ArtifactKind,
    ArtifactReference,
    DesignState,
    ExecutionScope,
    ResultSelection,
    Run,
    RunLifecycleEvent,
    RunStatus,
    ScopeKind,
    SelectionKind,
    StateEligibility,
    ValidationLevel,
    ValidationRecord,
    ValidationStatus,
)
from optimizer_pipeline.core.store import CURRENT_SCHEMA_VERSION, SQLiteRunStore

from .artifacts import RootFingerprint, fingerprint_file
from .bounded_hash_worker import HASH_WORKER_SCHEMA
from .catalogues import (
    CATALOGUE_SCHEMA_VERSION,
    NO_ACTION_CATALOGUE_HASH,
    NO_ACTION_CATALOGUE_VERSION,
)
from .contracts import DesignStage, DoFLevel, EventClass
from .evidence import (
    DEFAULT_REQUIRED_EVIDENCE_ROLES,
    ImmutableEvidenceWriter,
    ImmutableSidecar,
    register_evidence_and_finalize,
    verify_registered_evidence,
)
from .manifests import capability_manifest
from .processes import (
    OwnedLineProtocolSession,
    OwnedProcessCancelled,
    OwnedProcessRunner,
    OwnedProcessResult,
    OwnedProtocolCallTimedOut,
    OwnedProtocolSessionError,
    ProcessEndpoint,
    ProcessLimits,
    build_target_process_environment,
)
from .provenance import (
    REPOSITORY_PROVENANCE_SCHEMA_VERSION,
    RepositoryConstraintMismatch,
    RepositoryObservation,
    RepositoryProvenanceError,
    enforce_repository_constraint,
    observe_repository,
    tracked_repository_observations_match,
)
from .spec import RepositoryConstraintSpec
from .toolcalls import (
    ActualCallBudget,
    BackendContractMismatch,
    CallStatus,
    ObservedCallExecutor,
    ToolCallContractError,
    declared_l0_read_only_plan,
    default_tool_call_registry,
)
from .vivado_readonly_worker import (
    EFFECTIVE_ENV_PREFIX,
    INIT_POLICY_LINE,
    LAUNCHED_LINE,
    PYTHON_VERSION_PREFIX,
    READY_LINE,
    STARTED_LINE,
    VERSION_PREFIX,
    WORKER_VERSION,
    effective_vivado_environment_hash,
    parse_tcl_result,
    request_bytes,
    terminal_marker,
)


PREFLIGHT_PLAN_SCHEMA = "dof.real-preflight-plan.v2"
E0_PREFLIGHT_PLAN_SCHEMA = "dof.e0-real-preflight-plan.v1"
PREFLIGHT_RUNNER_VERSION = "dof.l0-real-preflight.v1"
PREFLIGHT_MANIFEST_SCHEMA = "dof.real-preflight-manifest.v1"
PREFLIGHT_RESULT_SCHEMA = "dof.real-preflight-result.v1"
PREFLIGHT_TOOL_ENV_SCHEMA = "dof.real-tool-environment.v1"
PREFLIGHT_ROOT_INTEGRITY_SCHEMA = "dof.root-integrity.v1"
PREFLIGHT_VALIDATION_SCHEMA = "dof.real-preflight-validation.v1"
PREFLIGHT_DECISION_TRACE_SCHEMA = "dof.real-preflight-event.v1"
PREFLIGHT_PROCESS_SCHEMA = "dof.real-preflight-process.v1"
PREFLIGHT_CALL_TRACE_SCHEMA = "dof.observed-call-trace.v1"
PREFLIGHT_REPOSITORY_FINAL_CHECK_SCHEMA = "dof.repository-final-check.v1"
PREFLIGHT_REQUIRED_ROLES = DEFAULT_REQUIRED_EVIDENCE_ROLES + (
    "repository_provenance",
    "repository_final_check",
    "call_registry_manifest",
    "declared_call_plan",
    "backend_call_trace",
    "process_lifecycle",
    "root_integrity",
    "validation_summary",
)

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_EXPECTED_TIMEOUT_KEYS = frozenset(
    {
        "host.session.start",
        "host.session.close",
        "validation.hash_root",
        "validation.validate_root",
        "vivado.open_checkpoint",
        "vivado.report_timing_summary",
        "vivado.report_route_status",
        "vivado.report_utilization",
        "vivado.close_design",
    }
)
_EXPECTED_EVENT_LIMITS = {
    EventClass.READ_ONLY.value: 5,
    EventClass.MUTATION_LIGHT.value: 0,
    EventClass.IMPLEMENTATION_HEAVY.value: 0,
    EventClass.VALIDATION.value: 3,
    EventClass.LLM_REQUEST.value: 0,
    EventClass.OUTER_ATTEMPT.value: 0,
}
_ROOT_SCOPE_ID = "scope-root"
_ROOT_STATE_ID = "state-root"
_ROOT_ARTIFACT_ID = "artifact-root"


@dataclass(frozen=True, slots=True)
class _ExpectedRoot:
    root_path: Path
    before: RootFingerprint


class PreflightPlanError(ValueError):
    pass


class PreflightExecutionError(RuntimeError):
    def __init__(self, endpoint: str, reason: str) -> None:
        self.endpoint = endpoint
        self.reason = reason
        super().__init__(reason)


@dataclass(frozen=True, slots=True)
class RealPreflightPlan:
    run_id: str
    preflight_id: str
    dcp_pseudonym: str
    dcp_sha256: str
    dcp_size_bytes: int
    source_regime: str
    selection_rule: str
    environment_profile_id: str
    expected_vivado_version: str
    expected_python_version: str
    repository_constraint: RepositoryConstraintSpec
    initialization_timeout_seconds: float
    whole_run_timeout_seconds: float
    graceful_close_seconds: float
    terminate_grace_seconds: float
    kill_grace_seconds: float
    cleanup_timeout_seconds: float
    output_limit_bytes: int
    call_timeouts_seconds: Mapping[str, float]
    event_limits: Mapping[str, int]
    schema_version: str = PREFLIGHT_PLAN_SCHEMA

    @classmethod
    def from_dict(cls, value: Any) -> "RealPreflightPlan":
        if not isinstance(value, dict) or set(value) != {
            "schema_version",
            "run_id",
            "preflight_id",
            "input_dcp",
            "selection_rule",
            "environment_profile_id",
            "expected_tool_versions",
            "repository_constraint",
            "process_limits",
            "call_timeouts_seconds",
            "event_limits",
        }:
            raise PreflightPlanError("preflight plan fields are not exact")
        if value["schema_version"] not in {
            PREFLIGHT_PLAN_SCHEMA,
            E0_PREFLIGHT_PLAN_SCHEMA,
        }:
            raise PreflightPlanError("preflight plan schema mismatch")
        for name in ("run_id", "preflight_id"):
            if not isinstance(value[name], str) or not _ID_RE.fullmatch(value[name]):
                raise PreflightPlanError(f"{name} is not canonical")
        input_dcp = value["input_dcp"]
        if not isinstance(input_dcp, dict) or set(input_dcp) != {
            "pseudonym",
            "sha256",
            "size_bytes",
            "source_regime",
        }:
            raise PreflightPlanError("input_dcp fields are not exact")
        if not isinstance(input_dcp["pseudonym"], str) or not _ID_RE.fullmatch(
            input_dcp["pseudonym"]
        ):
            raise PreflightPlanError("input DCP pseudonym is not canonical")
        if not isinstance(input_dcp["sha256"], str) or not _SHA256_RE.fullmatch(
            input_dcp["sha256"]
        ):
            raise PreflightPlanError("input DCP sha256 is invalid")
        if (
            not isinstance(input_dcp["size_bytes"], int)
            or isinstance(input_dcp["size_bytes"], bool)
            or input_dcp["size_bytes"] <= 0
        ):
            raise PreflightPlanError("input DCP size must be positive")
        for name in (
            "source_regime",
            "selection_rule",
            "environment_profile_id",
        ):
            raw = input_dcp[name] if name == "source_regime" else value[name]
            if not isinstance(raw, str) or not _ID_RE.fullmatch(raw):
                raise PreflightPlanError(f"{name} is not canonical")
        expected_tools = value["expected_tool_versions"]
        if not isinstance(expected_tools, dict) or set(expected_tools) != {
            "vivado",
            "python",
        }:
            raise PreflightPlanError("expected_tool_versions fields are not exact")
        for name, pattern in (
            ("vivado", r"^[0-9]{4}\.[0-9]+(?:\.[0-9]+)?$"),
            ("python", r"^[0-9]+\.[0-9]+\.[0-9]+$"),
        ):
            raw = expected_tools[name]
            if not isinstance(raw, str) or re.fullmatch(pattern, raw) is None:
                raise PreflightPlanError(f"expected {name} version is invalid")
        expected_profile = f"april-vivado-{expected_tools['vivado']}"
        if value["environment_profile_id"] != expected_profile:
            raise PreflightPlanError(
                "environment_profile_id does not bind the expected Vivado version"
            )

        process = value["process_limits"]
        process_keys = {
            "initialization_timeout_seconds",
            "whole_run_timeout_seconds",
            "graceful_close_seconds",
            "terminate_grace_seconds",
            "kill_grace_seconds",
            "cleanup_timeout_seconds",
            "output_limit_bytes",
        }
        if not isinstance(process, dict) or set(process) != process_keys:
            raise PreflightPlanError("process_limits fields are not exact")
        for name in process_keys - {"output_limit_bytes"}:
            raw = process[name]
            if (
                not isinstance(raw, (int, float))
                or isinstance(raw, bool)
                or not math.isfinite(float(raw))
                or float(raw) <= 0
            ):
                raise PreflightPlanError(f"invalid process limit: {name}")
        output_limit = process["output_limit_bytes"]
        if (
            not isinstance(output_limit, int)
            or isinstance(output_limit, bool)
            or not 64 * 1024 <= output_limit <= 16 * 1024 * 1024
        ):
            raise PreflightPlanError("invalid process limit: output_limit_bytes")
        minimum_cleanup = sum(
            float(process[name])
            for name in (
                "graceful_close_seconds",
                "terminate_grace_seconds",
                "kill_grace_seconds",
            )
        )
        if float(process["cleanup_timeout_seconds"]) < minimum_cleanup:
            raise PreflightPlanError(
                "cleanup total does not cover graceful+terminate+kill"
            )
        # Construct the shared value object only after the public manifest has
        # passed strict JSON type checks.  ``float('5')`` and ``float(True)``
        # must not make string/bool manifest values silently valid.
        ProcessLimits(**process)
        if process["whole_run_timeout_seconds"] > 1200.0:
            raise PreflightPlanError("whole-run timeout exceeds the 20-minute cap")

        timeouts = value["call_timeouts_seconds"]
        if not isinstance(timeouts, dict) or set(timeouts) != _EXPECTED_TIMEOUT_KEYS:
            raise PreflightPlanError("call timeout keys are not exact")
        registry = default_tool_call_registry()
        for operation_id, timeout in timeouts.items():
            if (
                not isinstance(timeout, (int, float))
                or isinstance(timeout, bool)
                or not math.isfinite(float(timeout))
                or float(timeout) <= 0
                or float(timeout) > registry.operation(operation_id).default_timeout_seconds
            ):
                raise PreflightPlanError(f"invalid call timeout: {operation_id}")
        if (
            float(process["initialization_timeout_seconds"])
            > float(timeouts["host.session.start"])
        ):
            raise PreflightPlanError(
                "initialization timeout exceeds host.session.start timeout"
            )
        cleanup_bound = float(process["cleanup_timeout_seconds"])
        if cleanup_bound >= float(timeouts["host.session.close"]):
            raise PreflightPlanError(
                "cleanup total must be below host.session.close hard timeout"
            )

        limits = value["event_limits"]
        if (
            not isinstance(limits, dict)
            or set(limits) != set(_EXPECTED_EVENT_LIMITS)
            or any(
                not isinstance(raw, int) or isinstance(raw, bool) or raw < 0
                for raw in limits.values()
            )
            or limits != _EXPECTED_EVENT_LIMITS
        ):
            raise PreflightPlanError("event limits must equal the frozen L0 budget")
        constraint = RepositoryConstraintSpec.from_dict(
            value["repository_constraint"]
        )
        expected_untracked_policy = (
            "record_only"
            if value["schema_version"] == E0_PREFLIGHT_PLAN_SCHEMA
            else "require_none"
        )
        if (
            constraint.required_head is None
            or not constraint.require_tracked_clean
            or constraint.untracked_policy != expected_untracked_policy
        ):
            raise PreflightPlanError(
                "selected preflight requires exact HEAD, tracked clean, and no untracked files"
            )
        return cls(
            run_id=value["run_id"],
            preflight_id=value["preflight_id"],
            dcp_pseudonym=input_dcp["pseudonym"],
            dcp_sha256=input_dcp["sha256"],
            dcp_size_bytes=input_dcp["size_bytes"],
            source_regime=input_dcp["source_regime"],
            selection_rule=value["selection_rule"],
            environment_profile_id=value["environment_profile_id"],
            expected_vivado_version=expected_tools["vivado"],
            expected_python_version=expected_tools["python"],
            repository_constraint=constraint,
            initialization_timeout_seconds=float(
                process["initialization_timeout_seconds"]
            ),
            whole_run_timeout_seconds=float(process["whole_run_timeout_seconds"]),
            graceful_close_seconds=float(process["graceful_close_seconds"]),
            terminate_grace_seconds=float(process["terminate_grace_seconds"]),
            kill_grace_seconds=float(process["kill_grace_seconds"]),
            cleanup_timeout_seconds=float(process["cleanup_timeout_seconds"]),
            output_limit_bytes=process["output_limit_bytes"],
            call_timeouts_seconds={
                key: float(timeouts[key]) for key in sorted(timeouts)
            },
            event_limits={key: int(limits[key]) for key in sorted(limits)},
            schema_version=value["schema_version"],
        )

    @classmethod
    def load(cls, path: str | Path) -> "RealPreflightPlan":
        raw = Path(path).read_bytes()
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise PreflightPlanError("preflight plan is not UTF-8 JSON") from exc
        return cls.from_dict(value)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "run_id": self.run_id,
            "preflight_id": self.preflight_id,
            "input_dcp": {
                "pseudonym": self.dcp_pseudonym,
                "sha256": self.dcp_sha256,
                "size_bytes": self.dcp_size_bytes,
                "source_regime": self.source_regime,
            },
            "selection_rule": self.selection_rule,
            "environment_profile_id": self.environment_profile_id,
            "expected_tool_versions": {
                "vivado": self.expected_vivado_version,
                "python": self.expected_python_version,
            },
            "repository_constraint": self.repository_constraint.to_dict(),
            "process_limits": {
                "initialization_timeout_seconds": self.initialization_timeout_seconds,
                "whole_run_timeout_seconds": self.whole_run_timeout_seconds,
                "graceful_close_seconds": self.graceful_close_seconds,
                "terminate_grace_seconds": self.terminate_grace_seconds,
                "kill_grace_seconds": self.kill_grace_seconds,
                "cleanup_timeout_seconds": self.cleanup_timeout_seconds,
                "output_limit_bytes": self.output_limit_bytes,
            },
            "call_timeouts_seconds": dict(self.call_timeouts_seconds),
            "event_limits": dict(self.event_limits),
        }

    @property
    def spec_hash(self) -> str:
        return hashlib.sha256(_canonical_bytes(self.to_dict())).hexdigest()

    @property
    def process_limits(self) -> ProcessLimits:
        return ProcessLimits(
            initialization_timeout_seconds=self.initialization_timeout_seconds,
            whole_run_timeout_seconds=self.whole_run_timeout_seconds,
            graceful_close_seconds=self.graceful_close_seconds,
            terminate_grace_seconds=self.terminate_grace_seconds,
            kill_grace_seconds=self.kill_grace_seconds,
            cleanup_timeout_seconds=self.cleanup_timeout_seconds,
            output_limit_bytes=self.output_limit_bytes,
        )


SessionFactory = Callable[..., OwnedLineProtocolSession]


def freeze_e0_preflight_plan(
    dcp_path: str | Path,
    *,
    dcp_pseudonym: str,
    run_id: str,
    repository_anchor: str | Path,
) -> RealPreflightPlan:
    """Freeze an E0 L0 plan while recording unrelated untracked evidence.

    E0 executes in the long-lived research worktree, whose retained prior raw
    evidence is intentionally untracked.  The plan still binds exact HEAD and
    requires the tracked product tree to stay clean.
    """

    root = fingerprint_file(Path(dcp_path).resolve())
    repository = observe_repository(repository_anchor)
    if repository.tracked_tree_dirty:
        raise PreflightPlanError("tracked_product_tree_not_clean")
    value = {
        "schema_version": E0_PREFLIGHT_PLAN_SCHEMA,
        "run_id": run_id,
        "preflight_id": "e0-root-readonly",
        "input_dcp": {
            "pseudonym": dcp_pseudonym,
            "sha256": root.sha256,
            "size_bytes": root.size_bytes,
            "source_regime": "fpl26-supplied-dcp",
        },
        "selection_rule": "frozen-e0-benchmark",
        "environment_profile_id": "april-vivado-2025.1",
        "expected_tool_versions": {
            "vivado": "2025.1",
            "python": platform.python_version(),
        },
        "repository_constraint": {
            "repository_mode": "git",
            "required_head": repository.head_commit,
            "require_tracked_clean": True,
            "untracked_policy": "record_only",
        },
        "process_limits": {
            "initialization_timeout_seconds": 30.0,
            "whole_run_timeout_seconds": 300.0,
            "graceful_close_seconds": 1.0,
            "terminate_grace_seconds": 3.0,
            "kill_grace_seconds": 5.0,
            "cleanup_timeout_seconds": 12.0,
            "output_limit_bytes": 4 * 1024 * 1024,
        },
        "call_timeouts_seconds": {
            "host.session.start": 45.0,
            "host.session.close": 15.0,
            "validation.hash_root": 10.0,
            "validation.validate_root": 5.0,
            "vivado.open_checkpoint": 120.0,
            "vivado.report_timing_summary": 30.0,
            "vivado.report_route_status": 30.0,
            "vivado.report_utilization": 30.0,
            "vivado.close_design": 10.0,
        },
        "event_limits": dict(_EXPECTED_EVENT_LIMITS),
    }
    return RealPreflightPlan.from_dict(value)


def freeze_e1_observation_preflight_plan(
    dcp_path: str | Path,
    *,
    dcp_pseudonym: str,
    run_id: str,
    repository_anchor: str | Path,
) -> RealPreflightPlan:
    """Freeze the E1 decision-time read-only observation contract.

    E1 uses the same operation catalogue and evidence schema as the accepted E0
    preflight, but its characterised roots include designs whose read-only
    timing report takes longer than E0's 30-second smoke bound.  The E1 limits
    are fixed from the Gate-2 root audit and remain below the registry bounds
    and the 3,600-second trajectory cap.
    """

    value = freeze_e0_preflight_plan(
        dcp_path,
        dcp_pseudonym=dcp_pseudonym,
        run_id=run_id,
        repository_anchor=repository_anchor,
    ).to_dict()
    value["preflight_id"] = "e1-decision-readonly"
    value["selection_rule"] = "frozen-e1-current-state"
    value["process_limits"]["whole_run_timeout_seconds"] = 600.0
    value["call_timeouts_seconds"].update(
        {
            "vivado.open_checkpoint": 300.0,
            "vivado.report_timing_summary": 300.0,
            "vivado.report_route_status": 120.0,
            "vivado.report_utilization": 120.0,
            # The large MLCAD root repeatedly completed close_design just
            # beyond the E0 smoke bound (10.9 s observed).  Closing is still
            # mandatory; E1 gives it a bounded, evidence-backed margin.
            "vivado.close_design": 30.0,
        }
    )
    return RealPreflightPlan.from_dict(value)


def write_e0_preflight_plan(plan: RealPreflightPlan, path: str | Path) -> None:
    if plan.schema_version != E0_PREFLIGHT_PLAN_SCHEMA:
        raise PreflightPlanError("e0_preflight_schema_required")
    # Round-trip through the strict parser before publishing immutable bytes.
    validated = RealPreflightPlan.from_dict(plan.to_dict())
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        raise FileExistsError(target)
    target.write_bytes(json.dumps(validated.to_dict(), sort_keys=True, indent=2).encode() + b"\n")


def run_l0_real_preflight(
    plan_path: str | Path,
    *,
    dcp_path: str | Path,
    run_dir: str | Path,
    vivado_exec: str | Path,
    python_exec: str | Path = sys.executable,
    repository_anchor: str | Path = __file__,
    session_factory: SessionFactory = OwnedLineProtocolSession,
) -> dict[str, Any]:
    """Run exactly one fixed read-only preflight and return its public summary."""

    plan = RealPreflightPlan.load(plan_path)
    observation = observe_repository(repository_anchor)
    enforce_repository_constraint(plan.repository_constraint.to_dict(), observation)
    return asyncio.run(
        _prepare_and_run_preflight(
            plan,
            dcp_path=Path(dcp_path).absolute(),
            run_dir=Path(run_dir),
            vivado_exec=Path(vivado_exec).absolute(),
            python_exec=Path(python_exec).absolute(),
            repository_anchor=repository_anchor,
            repository_observation=observation,
            session_factory=session_factory,
        )
    )


async def _prepare_and_run_preflight(
    plan: RealPreflightPlan,
    *,
    dcp_path: Path,
    run_dir: Path,
    vivado_exec: Path,
    python_exec: Path,
    repository_anchor: str | Path,
    repository_observation: RepositoryObservation,
    session_factory: SessionFactory,
) -> dict[str, Any]:
    output_root = _prepare_output(run_dir)
    root_guard = _ExpectedRoot(
        dcp_path,
        RootFingerprint(plan.dcp_sha256, plan.dcp_size_bytes),
    )
    return await _run_preflight_async(
        plan,
        dcp_path=dcp_path,
        run_dir=output_root,
        vivado_exec=vivado_exec,
        python_exec=python_exec,
        repository_anchor=repository_anchor,
        repository_observation=repository_observation,
        root_guard=root_guard,
        session_factory=session_factory,
    )


async def _run_preflight_async(
    plan: RealPreflightPlan,
    *,
    dcp_path: Path,
    run_dir: Path,
    vivado_exec: Path,
    python_exec: Path,
    repository_anchor: str | Path,
    repository_observation: RepositoryObservation,
    root_guard: _ExpectedRoot,
    session_factory: SessionFactory,
) -> dict[str, Any]:
    registry = default_tool_call_registry()
    call_plan = declared_l0_read_only_plan(registry)
    writer = ImmutableEvidenceWriter(run_dir, plan.run_id, plan.spec_hash)
    static_sidecars = _write_static_evidence(
        writer, plan, registry.to_dict() | {"sha256": registry.sha256},
        call_plan.to_dict() | {"sha256": call_plan.sha256}, repository_observation
    )
    repository_sidecar = next(
        item for item in static_sidecars if item.role == "repository_provenance"
    )
    repository_binding = _binding(repository_sidecar)
    store = _initialize_store(
        run_dir, plan, repository_binding, static_sidecars
    )
    call_trace = writer.open_trace(
        "evidence/backend_calls.jsonl",
        role="backend_call_trace",
        schema_version=PREFLIGHT_CALL_TRACE_SCHEMA,
    )
    decision_trace = writer.open_trace(
        "evidence/decision_events.jsonl",
        schema_version=PREFLIGHT_DECISION_TRACE_SCHEMA,
    )
    budget = ActualCallBudget(
        {EventClass(key): value for key, value in plan.event_limits.items()},
        whole_run_timeout_seconds=plan.whole_run_timeout_seconds,
    )
    session: OwnedLineProtocolSession | None = None
    process_result: OwnedProcessResult | None = None
    report_bytes: dict[str, bytes] = {}
    report_fingerprints: dict[str, dict[str, Any]] = {}
    validation: dict[str, Any] = {
        "schema_version": PREFLIGHT_VALIDATION_SCHEMA,
        "valid": False,
        "timing": {},
        "route_status": {},
        "utilization": {},
        "missing_reasons": ["preflight_not_completed"],
    }
    endpoint = "BACKEND_ERROR"
    reasons: list[str] = []
    root_before_observed: RootFingerprint | None = None
    root_after: RootFingerprint | None = None
    hash_process_results: list[OwnedProcessResult] = []
    worker_attestation: dict[str, Any] = {
        "status": "NOT_OBSERVED",
        "vivado_version": None,
        "python_version": None,
        "init_policy": None,
    }
    reconciliation: dict[str, Any] = {}
    final_repository_check: dict[str, Any] = {
        "schema_version": PREFLIGHT_REPOSITORY_FINAL_CHECK_SCHEMA,
        "status": "NOT_RUN",
        "constraint_satisfied": None,
        "tracked_identity_match": None,
        "observation_sha256": None,
        "reason_codes": ["preflight_not_finalized"],
    }
    call_counter = 0
    normal_host_close_completed = False
    active_operation_process_result: OwnedProcessResult | None = None
    execution_binding_id = hashlib.sha256(
        f"{plan.run_id}:{plan.spec_hash}:real-l0-runtime".encode("utf-8")
    ).hexdigest()
    observer_process_token = hashlib.sha256(
        os.urandom(32) + f":{os.getpid()}:{execution_binding_id}".encode("ascii")
    ).hexdigest()

    private_home = run_dir / "private" / "vivado-home"
    private_home.mkdir(mode=0o700, parents=True, exist_ok=False)
    private_home.chmod(0o700)

    worker_argv = (
        str(python_exec),
        "-I",
        str(Path(__file__).resolve().with_name("vivado_readonly_worker.py")),
        "--vivado-exec",
        str(vivado_exec),
        "--initialization-timeout-seconds",
        str(plan.initialization_timeout_seconds),
        "--run-workspace",
        str(run_dir),
        "--isolated-home",
        str(private_home),
    )
    target_environment = build_target_process_environment(os.environ)
    expected_vivado_environment_hash = effective_vivado_environment_hash(
        target_environment,
        private_home,
    )
    close_command = request_bytes(
        "worker.exit",
        "cleanup",
        timeout_seconds=plan.call_timeouts_seconds["host.session.close"],
    )

    async def invoke(operation: Any, arguments: Mapping[str, Any]) -> Any:
        nonlocal session, process_result, call_counter
        nonlocal root_before_observed, root_after, validation, worker_attestation
        nonlocal active_operation_process_result
        operation_id = operation.operation_id
        active_operation_process_result = None
        if operation_id == "host.session.start":
            if session is not None:
                raise PreflightExecutionError(
                    "BACKEND_CONTRACT_MISMATCH", "host session started twice"
                )
            session = session_factory(
                worker_argv,
                ready_line=READY_LINE,
                close_command=close_command,
                limits=plan.process_limits,
                cwd=run_dir,
                env=target_environment,
            )
            await session.start()
            startup = session.startup_output
            if startup.truncated:
                raise PreflightExecutionError(
                    "EVIDENCE_INTEGRITY_FAILURE",
                    "bounded startup attestation was truncated",
                )
            worker_attestation = _verify_worker_attestation(
                startup.data,
                plan,
                expected_environment_hash=expected_vivado_environment_hash,
            )
            return {
                "session_started": True,
                "attestation_status": worker_attestation["status"],
            }
        if operation_id == "validation.hash_root":
            sink_count_before = len(hash_process_results)
            try:
                observed, hash_result = await _bounded_fingerprint(
                    root_guard.root_path,
                    python_exec=python_exec,
                    cwd=run_dir,
                    timeout_seconds=plan.call_timeouts_seconds[operation_id],
                    output_limit_bytes=min(plan.output_limit_bytes, 64 * 1024),
                    result_sink=hash_process_results,
                )
            finally:
                if len(hash_process_results) > sink_count_before:
                    active_operation_process_result = hash_process_results[-1]
            phase = arguments.get("phase")
            if phase == "before":
                root_before_observed = observed
            elif phase == "after":
                root_after = observed
            else:
                raise PreflightExecutionError(
                    "BACKEND_CONTRACT_MISMATCH", "root hash phase is invalid"
                )
            if observed != root_guard.before:
                raise PreflightExecutionError(
                    (
                        "INVALID_ARTIFACT"
                        if phase == "before"
                        else "ROOT_ARTIFACT_MUTATION"
                    ),
                    (
                        "input DCP hash/size does not match predeclared plan"
                        if phase == "before"
                        else "root fingerprint changed"
                    ),
                )
            return {"sha256": observed.sha256, "size_bytes": observed.size_bytes}
        if operation_id == "validation.validate_root":
            validation = _validate_reports(report_bytes)
            if not validation["valid"]:
                raise PreflightExecutionError(
                    "INVALID_ARTIFACT", "root report validation was incomplete"
                )
            return validation
        if operation_id == "host.session.close":
            if session is None:
                raise PreflightExecutionError(
                    "BACKEND_CONTRACT_MISMATCH", "host session was never started"
                )
            process_result = await session.close()
            active_operation_process_result = process_result
            recovery_close = arguments.get("recovery_after_failure") is True
            if not process_result.cleanup_ok or (
                not recovery_close
                and process_result.endpoint is not ProcessEndpoint.SUCCEEDED
            ):
                raise PreflightExecutionError(
                    "PROCESS_CLEANUP_FAILURE", "owned session did not close cleanly"
                )
            return process_result.to_dict()
        if not operation_id.startswith("vivado.") or session is None:
            raise PreflightExecutionError(
                "BACKEND_CONTRACT_MISMATCH", "operation has no fixed binding"
            )
        call_token = f"call-{call_counter:03d}"
        call_counter += 1
        request_arguments: dict[str, Any] = {}
        if operation_id == "vivado.open_checkpoint":
            request_arguments["dcp_path_b64"] = base64.b64encode(
                os.fsencode(dcp_path)
            ).decode("ascii")
        command = request_bytes(
            operation_id,
            call_token,
            timeout_seconds=plan.call_timeouts_seconds[operation_id],
            arguments=request_arguments,
        )
        call_result = await session.call(
            command,
            terminal_marker=terminal_marker(call_token),
            timeout_seconds=plan.call_timeouts_seconds[operation_id],
        )
        if call_result.stdout.truncated:
            raise PreflightExecutionError(
                "EVIDENCE_INTEGRITY_FAILURE",
                f"bounded output truncated:{operation_id}",
            )
        status, output = parse_tcl_result(call_token, call_result.stdout.data)
        if status != 0:
            raise PreflightExecutionError(
                "BACKEND_ERROR", f"fixed Vivado operation failed:{operation_id}"
            )
        if operation_id.startswith("vivado.report_"):
            report_bytes[operation_id] = output
            report_fingerprints[operation_id] = {
                "sha256": hashlib.sha256(output).hexdigest(),
                "size_bytes": len(output),
            }
        return {
            "status": "SUCCEEDED",
            "output_sha256": hashlib.sha256(output).hexdigest(),
            "output_size_bytes": len(output),
        }

    def resolve_operation_process_binding(
        operation: Any,
        _result: Any,
        call_error: BaseException | None,
    ) -> Mapping[str, Any] | None:
        """Bind a terminal row to the process that actually entered the call."""

        operation_id = operation.operation_id
        candidate = getattr(call_error, "result", None)
        if operation_id == "validation.hash_root":
            if not isinstance(candidate, OwnedProcessResult):
                candidate = active_operation_process_result
            if isinstance(candidate, OwnedProcessResult):
                return _owned_operation_binding(
                    candidate, binding_type="owned_validation_worker"
                )
            return None
        if operation_id == "validation.validate_root":
            return {
                "binding_type": "in_process_validation",
                "observer_process_token_hash": observer_process_token,
            }
        if operation_id == "host.session.close" and isinstance(
            process_result, OwnedProcessResult
        ):
            candidate = process_result
        if not isinstance(candidate, OwnedProcessResult) and session is not None:
            candidate = session.result
        if isinstance(candidate, OwnedProcessResult):
            return _owned_operation_binding(
                candidate, binding_type="owned_vivado_session"
            )
        if session is not None and session.root_process_token is not None:
            return {
                "binding_type": "owned_vivado_session",
                "root_process_token_hash": session.root_process_token,
                "session_owned": session.session_owned,
            }
        return None

    observer = ObservedCallExecutor(
        registry=registry,
        plan=call_plan,
        budget=budget,
        invoke=invoke,
        trace_sink=call_trace.append,
        execution_binding={"runtime_binding_id": execution_binding_id},
        operation_process_binding_resolver=resolve_operation_process_binding,
    )
    decision_trace.append(
        {
            "record_type": "RUN_START",
            "runner_version": PREFLIGHT_RUNNER_VERSION,
            "level": "L0",
            "real_dcp": True,
            "policy_requests": 0,
            "mutation_calls_allowed": 0,
            "heavy_calls_allowed": 0,
        }
    )
    try:
        # Prove the predeclared input identity in a killable validation worker
        # before any Vivado process is allowed to start.
        await observer.call(
            "validation.hash_root",
            {"dcp_path": str(dcp_path), "phase": "before"},
            timeout_seconds=plan.call_timeouts_seconds["validation.hash_root"],
        )
        await observer.call(
            "host.session.start",
            {
                "worker": WORKER_VERSION,
                "vivado_executable": str(vivado_exec),
                "python_executable": str(python_exec),
            },
            timeout_seconds=plan.call_timeouts_seconds["host.session.start"],
        )
        await observer.call(
            "vivado.open_checkpoint",
            {"dcp_path": str(dcp_path), "mode": "read_only"},
            timeout_seconds=plan.call_timeouts_seconds["vivado.open_checkpoint"],
        )
        for operation_id in (
            "vivado.report_timing_summary",
            "vivado.report_route_status",
            "vivado.report_utilization",
        ):
            await observer.call(
                operation_id,
                {"report": operation_id.rsplit(".", 1)[-1]},
                timeout_seconds=plan.call_timeouts_seconds[operation_id],
                current_stage=DesignStage.ROUTED,
            )
        await observer.call(
            "vivado.close_design",
            {},
            timeout_seconds=plan.call_timeouts_seconds["vivado.close_design"],
        )
        await observer.call(
            "host.session.close",
            {},
            timeout_seconds=plan.call_timeouts_seconds["host.session.close"],
        )
        normal_host_close_completed = True
        await observer.call(
            "validation.hash_root",
            {"dcp_path": str(dcp_path), "phase": "after"},
            timeout_seconds=plan.call_timeouts_seconds["validation.hash_root"],
        )
        await observer.call(
            "validation.validate_root",
            {"report_hashes": report_fingerprints},
            timeout_seconds=plan.call_timeouts_seconds["validation.validate_root"],
        )
        reconciled = observer.reconcile_or_raise()
        reconciliation = reconciled.to_dict()
        terminal_counts = reconciliation["observed_terminal_status_counts"]
        observed_call_count = len(reconciliation["observed_started_operations"])
        if terminal_counts.get(CallStatus.SUCCEEDED.value) != observed_call_count or any(
            terminal_counts.get(status.value, 0)
            for status in CallStatus
            if status is not CallStatus.SUCCEEDED
        ):
            raise PreflightExecutionError(
                "BACKEND_CONTRACT_MISMATCH", "required host calls did not all succeed"
            )
        endpoint = "VALID_COMPLETION"
        reasons = ["l0_root_opened_measured_validated_and_closed"]
    except (OwnedProtocolCallTimedOut, asyncio.TimeoutError) as exc:
        endpoint = "TIMEOUT"
        reasons = [type(exc).__name__]
    except asyncio.CancelledError:
        endpoint = "CANCELLATION"
        reasons = ["preflight_task_cancelled"]
    except (BackendContractMismatch, ToolCallContractError) as exc:
        endpoint = "BACKEND_CONTRACT_MISMATCH"
        reasons = [type(exc).__name__]
    except PreflightExecutionError as exc:
        endpoint = exc.endpoint
        reasons = [exc.reason]
    except OwnedProtocolSessionError as exc:
        process_result = exc.result
        endpoint = _process_endpoint(exc.result)
        reasons = [type(exc).__name__]
    except Exception as exc:
        candidate = getattr(exc, "result", None)
        if isinstance(candidate, OwnedProcessResult):
            process_result = candidate
            endpoint = _process_endpoint(candidate)
        else:
            endpoint = "BACKEND_ERROR"
        reasons = [type(exc).__name__]
    finally:
        if session is not None and not normal_host_close_completed:
            try:
                await observer.call(
                    "host.session.close",
                    {
                        "recovery_after_failure": True,
                        "recovery_reason": endpoint,
                    },
                    timeout_seconds=plan.call_timeouts_seconds[
                        "host.session.close"
                    ],
                    recovery_after_failure=True,
                )
                process_result = session.result
            except BaseException as cleanup_exc:
                candidate = getattr(cleanup_exc, "result", None)
                if isinstance(candidate, OwnedProcessResult):
                    process_result = candidate
                elif session.result is not None:
                    process_result = session.result
                endpoint = "PROCESS_CLEANUP_FAILURE"
                reasons.append(type(cleanup_exc).__name__)
        elif session is not None:
            process_result = session.result
        if process_result is not None and not process_result.cleanup_ok:
            endpoint = "PROCESS_CLEANUP_FAILURE"
            if "owned_process_cleanup_not_ok" not in reasons:
                reasons.append("owned_process_cleanup_not_ok")
        # If the normal after-hash was not reached, cleanup still performs one
        # bounded, owned verification after the host session is gone.  Never
        # fall back to synchronous hashing on the event-loop thread.
        if root_after is None and session is not None:
            try:
                await observer.call(
                    "validation.hash_root",
                    {
                        "dcp_path": str(dcp_path),
                        "phase": "after",
                        "recovery_reason": "normal_after_hash_not_reached",
                    },
                    timeout_seconds=plan.call_timeouts_seconds[
                        "validation.hash_root"
                    ],
                    recovery_after_failure=True,
                )
                if root_after != root_guard.before:
                    raise PreflightExecutionError(
                        "ROOT_ARTIFACT_MUTATION", "root fingerprint changed"
                    )
            except PreflightExecutionError as exc:
                endpoint = exc.endpoint
                reasons.append(exc.reason)
            except BaseException as exc:
                endpoint = "EVIDENCE_INTEGRITY_FAILURE"
                reasons.append(type(exc).__name__)
        integrity_endpoint = (
            endpoint
            if endpoint
            in {
                "ROOT_ARTIFACT_MUTATION",
                "PROCESS_CLEANUP_FAILURE",
                "EVIDENCE_INTEGRITY_FAILURE",
            }
            else None
        )
        try:
            final_observation = observe_repository(repository_anchor)
            final_repository_check = {
                "schema_version": PREFLIGHT_REPOSITORY_FINAL_CHECK_SCHEMA,
                "status": "OBSERVED",
                "constraint_satisfied": True,
                "tracked_identity_match": None,
                "observation_sha256": final_observation.sha256,
                "reason_codes": [],
            }
            try:
                enforce_repository_constraint(
                    plan.repository_constraint.to_dict(), final_observation
                )
            except RepositoryConstraintMismatch as exc:
                final_repository_check["status"] = "CONSTRAINT_MISMATCH"
                final_repository_check["constraint_satisfied"] = False
                final_repository_check["reason_codes"] = list(exc.reasons)
                endpoint = exc.code
                reasons.extend(exc.reasons)
            identity_match = tracked_repository_observations_match(
                repository_observation, final_observation
            )
            final_repository_check["tracked_identity_match"] = identity_match
            if not identity_match:
                final_repository_check["status"] = "TRACKED_IDENTITY_CHANGED"
                final_repository_check["reason_codes"] = [
                    *final_repository_check["reason_codes"],
                    "tracked_repository_identity_changed",
                ]
                endpoint = "REPOSITORY_CHANGED_DURING_RUN"
                reasons.append("tracked_repository_identity_changed")
            elif final_repository_check["status"] == "OBSERVED":
                final_repository_check["status"] = "MATCH"
        except RepositoryProvenanceError as exc:
            final_repository_check = {
                "schema_version": PREFLIGHT_REPOSITORY_FINAL_CHECK_SCHEMA,
                "status": "OBSERVATION_UNAVAILABLE",
                "constraint_satisfied": None,
                "tracked_identity_match": None,
                "observation_sha256": None,
                "reason_codes": list(exc.reasons),
            }
            endpoint = exc.code
            reasons.extend(exc.reasons)
        except Exception as exc:
            final_repository_check = {
                "schema_version": PREFLIGHT_REPOSITORY_FINAL_CHECK_SCHEMA,
                "status": "OBSERVATION_UNAVAILABLE",
                "constraint_satisfied": None,
                "tracked_identity_match": None,
                "observation_sha256": None,
                "reason_codes": [type(exc).__name__],
            }
            endpoint = "REPOSITORY_OBSERVATION_UNAVAILABLE"
            reasons.append(type(exc).__name__)
        # A later provenance problem is still recorded, but must not obscure
        # evidence that the root changed, owned cleanup failed, or bounded
        # protocol output was truncated.
        if integrity_endpoint is not None:
            endpoint = integrity_endpoint
        reconciliation = observer.reconciliation().to_dict()

    call_sidecar = call_trace.close()
    if process_result is None:
        process_payload: dict[str, Any] = {
            "schema_version": PREFLIGHT_PROCESS_SCHEMA,
            "runtime_binding_id": execution_binding_id,
            "observer_process_token_hash": observer_process_token,
            "process": None,
            "validation_hash_processes": [
                item.to_dict(include_output_text=False)
                for item in hash_process_results
            ],
        }
    else:
        process_payload = {
            "schema_version": PREFLIGHT_PROCESS_SCHEMA,
            "runtime_binding_id": execution_binding_id,
            "observer_process_token_hash": observer_process_token,
            "process": process_result.to_dict(include_output_text=False),
            "validation_hash_processes": [
                item.to_dict(include_output_text=False)
                for item in hash_process_results
            ],
        }

    root_payload = {
        "schema_version": PREFLIGHT_ROOT_INTEGRITY_SCHEMA,
        "pseudonym": plan.dcp_pseudonym,
        "declared": {
            "sha256": root_guard.before.sha256,
            "size_bytes": root_guard.before.size_bytes,
        },
        "before": (
            {
                "sha256": root_before_observed.sha256,
                "size_bytes": root_before_observed.size_bytes,
            }
            if root_before_observed is not None
            else None
        ),
        "after": (
            {"sha256": root_after.sha256, "size_bytes": root_after.size_bytes}
            if root_after is not None
            else None
        ),
        "matches_declared": root_before_observed == root_guard.before,
        "unchanged": (
            root_before_observed is not None
            and root_after is not None
            and root_after == root_before_observed
        ),
        "child_dcp_produced": False,
    }
    process_sidecar = writer.write_json(
        "evidence/process_lifecycle.json",
        process_payload,
        role="process_lifecycle",
        schema_version=PREFLIGHT_PROCESS_SCHEMA,
    )
    root_sidecar = writer.write_json(
        "evidence/root_integrity.json",
        root_payload,
        role="root_integrity",
        schema_version=PREFLIGHT_ROOT_INTEGRITY_SCHEMA,
    )
    validation_sidecar = writer.write_json(
        "evidence/validation_summary.json",
        validation,
        role="validation_summary",
        schema_version=PREFLIGHT_VALIDATION_SCHEMA,
    )
    final_repository_sidecar = writer.write_json(
        "evidence/repository_final_check.json",
        final_repository_check,
        role="repository_final_check",
        schema_version=PREFLIGHT_REPOSITORY_FINAL_CHECK_SCHEMA,
    )
    runtime_sidecars: list[ImmutableSidecar] = [
        call_sidecar,
        process_sidecar,
        root_sidecar,
        validation_sidecar,
        final_repository_sidecar,
    ]
    process_binding = _binding(process_sidecar)
    worker_stdout = process_result.stdout.data if process_result is not None else b""
    observed_vivado_version = _marker_value(worker_stdout, VERSION_PREFIX)
    observed_python_version = _marker_value(worker_stdout, PYTHON_VERSION_PREFIX)
    vivado_starts = _exact_line_count(worker_stdout, LAUNCHED_LINE)
    vivado_ready_starts = _exact_line_count(worker_stdout, STARTED_LINE)
    init_policy_passes = _exact_line_count(worker_stdout, INIT_POLICY_LINE)
    tool_payload = {
        "schema_version": PREFLIGHT_TOOL_ENV_SCHEMA,
        "execution_mode": "real_read_only_l0",
        "runner_version": PREFLIGHT_RUNNER_VERSION,
        "worker_version": WORKER_VERSION,
        "repository_constraint": plan.repository_constraint.to_dict(),
        "repository_provenance": repository_binding,
        "environment_profile_id": plan.environment_profile_id,
        "expected_tools": {
            "vivado": plan.expected_vivado_version,
            "python": plan.expected_python_version,
        },
        "startup_attestation": worker_attestation,
        "observed_tools": {
            "vivado": observed_vivado_version,
            "python": observed_python_version,
            "rapidwright": "NOT_STARTED",
            "mcp": "NOT_STARTED",
            "external_model_api": "NOT_STARTED",
        },
        "observed_start_counts": {
            "vivado": vivado_starts,
            "vivado_ready": vivado_ready_starts,
            "init_policy_pass": init_policy_passes,
            "rapidwright": 0,
            "mcp": 0,
            "external_model_api": 0,
        },
        "process_lifecycle": process_binding,
        "runtime_binding_id": execution_binding_id,
        "repository_final_check": _binding(final_repository_sidecar),
        "command_hash": (
            process_result.command_hash if process_result is not None else None
        ),
        "environment_hash": (
            process_result.environment_hash if process_result is not None else None
        ),
    }
    tool_sidecar = writer.write_json(
        "evidence/tool_environment_manifest.json",
        tool_payload,
        role="tool_environment_manifest",
        schema_version=PREFLIGHT_TOOL_ENV_SCHEMA,
    )
    runtime_sidecars.append(tool_sidecar)

    semantic_errors: tuple[str, ...] = ()
    if endpoint == "VALID_COMPLETION":
        semantic_errors = _successful_preflight_semantic_errors(
            plan=plan,
            call_plan=call_plan,
            observer=observer,
            reconciliation=reconciliation,
            process_result=process_result,
            hash_process_results=hash_process_results,
            root_payload=root_payload,
            validation=validation,
            worker_attestation=worker_attestation,
            observed_vivado_version=observed_vivado_version,
            observed_python_version=observed_python_version,
            vivado_starts=vivado_starts,
            vivado_ready_starts=vivado_ready_starts,
            init_policy_passes=init_policy_passes,
            final_repository_check=final_repository_check,
            execution_binding_id=execution_binding_id,
            observer_process_token=observer_process_token,
        )
        if semantic_errors:
            endpoint = "EVIDENCE_INTEGRITY_FAILURE"
            reasons.extend(semantic_errors)

    success = endpoint == "VALID_COMPLETION"
    # Seal the one authoritative decision endpoint only after every semantic
    # success check has had the opportunity to downgrade the provisional
    # backend result.  This keeps the immutable trace, run.db and summary in
    # agreement.
    decision_trace.append(
        {
            "record_type": "RUN_ENDPOINT",
            "endpoint": endpoint,
            "reasons": reasons,
            "real_dcp_attempts": 1,
            "external_model_requests": 0,
            "action_attempts": 0,
            "mutation_calls": budget.used[EventClass.MUTATION_LIGHT],
            "heavy_calls": budget.used[EventClass.IMPLEMENTATION_HEAVY],
        }
    )
    decision_sidecar = decision_trace.close()
    runtime_sidecars.append(decision_sidecar)
    fact_time = _now()
    with store.transaction():
        store.add_validation(
            ValidationRecord(
                "validation-root-real",
                plan.run_id,
                _ROOT_SCOPE_ID,
                _ROOT_STATE_ID,
                ValidationLevel.CERTIFICATION,
                ValidationStatus.PASSED if success else ValidationStatus.FAILED,
                checked_at=fact_time,
                details={
                    "validator": PREFLIGHT_RUNNER_VERSION,
                    "endpoint": endpoint,
                    "root_unchanged": root_payload["unchanged"],
                    "zero_mutation_calls": budget.used[EventClass.MUTATION_LIGHT] == 0,
                    "zero_heavy_calls": budget.used[EventClass.IMPLEMENTATION_HEAVY] == 0,
                },
            )
        )
        store.add_eligibility(
            StateEligibility(
                "eligibility-root",
                plan.run_id,
                _ROOT_STATE_ID,
                success,
                success,
                (
                    "observed valid unchanged L0 root"
                    if success
                    else f"root is not eligible after endpoint:{endpoint}"
                ),
                fact_time,
            )
        )
        if success:
            store.add_selection(
                ResultSelection(
                    "selection-baseline",
                    plan.run_id,
                    _ROOT_SCOPE_ID,
                    _ROOT_STATE_ID,
                    _ROOT_ARTIFACT_ID,
                    SelectionKind.BASELINE,
                    "observed valid exact root selected under L0 no-action semantics",
                    fact_time,
                )
            )
    summary = {
        "schema_version": PREFLIGHT_RESULT_SCHEMA,
        "runner_version": PREFLIGHT_RUNNER_VERSION,
        "run_id": plan.run_id,
        "preflight_id": plan.preflight_id,
        "spec_hash": plan.spec_hash,
        "endpoint": endpoint,
        "endpoint_success": success,
        "reasons": reasons,
        "repository_provenance": repository_binding,
        "repository_final_check": _binding(final_repository_sidecar),
        "root_integrity": _binding(root_sidecar),
        "validation_summary": _binding(validation_sidecar),
        "process_lifecycle": process_binding,
        "tool_environment": _binding(tool_sidecar),
        "decision_events": _binding(decision_sidecar),
        "declared_call_plan_hash": call_plan.sha256,
        "tool_call_registry_hash": registry.sha256,
        "call_reconciliation": reconciliation,
        "semantic_success_checks": (
            {"status": "PASS", "errors": []}
            if success
            else {"status": "NOT_APPLICABLE_OR_FAILED", "errors": list(semantic_errors)}
        ),
        "report_fingerprints": report_fingerprints,
        "budget": budget.summary(),
        "canonical_schema_version": store.schema_version,
        "canonical_integrity_errors": list(store.integrity_check()),
        "selected_state_id": _ROOT_STATE_ID if success else None,
        "selected_artifact_id": _ROOT_ARTIFACT_ID if success else None,
        "real_dcp_attempts": 1,
        "vivado_starts": vivado_starts,
        "vivado_ready_starts": vivado_ready_starts,
        "validation_hash_process_starts": len(hash_process_results),
        "rapidwright_starts": 0,
        "mcp_starts": 0,
        "external_model_requests": 0,
        "mutation_calls": budget.used[EventClass.MUTATION_LIGHT],
        "heavy_calls": budget.used[EventClass.IMPLEMENTATION_HEAVY],
        "action_attempts": 0,
        "child_dcp_produced": False,
        "input_dcp_included": False,
        "runtime_binding_id": execution_binding_id,
    }
    result_sidecar = writer.write_json(
        "evidence/result_summary.json",
        summary,
        role="result_summary",
        schema_version=PREFLIGHT_RESULT_SCHEMA,
        kind=ArtifactKind.REPORT,
    )
    all_sidecars = [*static_sidecars, *runtime_sidecars, result_sidecar]
    registration = register_evidence_and_finalize(
        store,
        run_dir=run_dir,
        sidecars=all_sidecars,
        run_id=plan.run_id,
        spec_hash=plan.spec_hash,
        terminal_status=RunStatus.FINALIZED if success else RunStatus.FAILED,
        terminal_detail={
            "endpoint": endpoint,
            "root_state_id": _ROOT_STATE_ID if success else None,
            "root_artifact_id": _ROOT_ARTIFACT_ID if success else None,
            "real_dcp_attempts": 1,
        },
        required_roles=PREFLIGHT_REQUIRED_ROLES,
    )
    verification = verify_registered_evidence(
        store,
        run_dir=run_dir,
        expected_run_id=plan.run_id,
        expected_spec_hash=plan.spec_hash,
        required_roles=PREFLIGHT_REQUIRED_ROLES,
    )
    verification.raise_for_errors()
    final_integrity = list(store.integrity_check())
    store.close()
    if final_integrity:
        raise PreflightExecutionError(
            "EVIDENCE_INTEGRITY_FAILURE", "; ".join(final_integrity)
        )
    return summary | {
        "evidence_set_hash": registration.evidence_set_hash,
        "evidence_verification": "PASS",
    }


def _write_static_evidence(
    writer: ImmutableEvidenceWriter,
    plan: RealPreflightPlan,
    registry_payload: Mapping[str, Any],
    call_plan_payload: Mapping[str, Any],
    observation: RepositoryObservation,
) -> list[ImmutableSidecar]:
    repository = writer.write_json(
        "evidence/repository_provenance.json",
        observation.to_dict(),
        role="repository_provenance",
        schema_version=REPOSITORY_PROVENANCE_SCHEMA_VERSION,
    )
    repository_binding = _binding(repository)
    capability = capability_manifest(DoFLevel.L0)
    definitions = (
        (
            "evidence/experiment_manifest.json",
            {
                "schema_version": PREFLIGHT_MANIFEST_SCHEMA,
                "runner_version": PREFLIGHT_RUNNER_VERSION,
                "spec_hash": plan.spec_hash,
                "plan": plan.to_dict(),
                "repository_constraint": plan.repository_constraint.to_dict(),
                "repository_provenance": repository_binding,
                "canonical_store_schema_version": CURRENT_SCHEMA_VERSION,
                "level": "L0",
                "real_execution": True,
                "read_only": True,
            },
            "experiment_manifest",
            PREFLIGHT_MANIFEST_SCHEMA,
        ),
        (
            "evidence/capability_manifest.json",
            capability.to_dict() | {"sha256": capability.sha256},
            "capability_manifest",
            str(capability.to_dict()["schema_version"]),
        ),
        (
            "evidence/catalogue_manifest.json",
            {
                "schema_version": CATALOGUE_SCHEMA_VERSION,
                "version": NO_ACTION_CATALOGUE_VERSION,
                "level_contract": "L0",
                "actions": [],
                "sha256": NO_ACTION_CATALOGUE_HASH,
            },
            "catalogue_manifest",
            CATALOGUE_SCHEMA_VERSION,
        ),
        (
            "evidence/mask_manifest.json",
            {
                "schema_version": "dof.mask-manifest.v1",
                "level": "L0",
                "status": "NOT_APPLICABLE",
                "hidden_policy_mask": False,
            },
            "mask_manifest",
            "dof.mask-manifest.v1",
        ),
        (
            "evidence/prompt_model_manifest.json",
            {
                "schema_version": "dof.prompt-model-manifest.v1",
                "level": "L0",
                "prompt": "NOT_RUN",
                "provider": "NOT_RUN",
                "model": "NOT_RUN",
                "external_model_requests": 0,
            },
            "prompt_model_manifest",
            "dof.prompt-model-manifest.v1",
        ),
        (
            "evidence/call_registry_manifest.json",
            dict(registry_payload),
            "call_registry_manifest",
            str(registry_payload["schema_version"]),
        ),
        (
            "evidence/declared_call_plan.json",
            dict(call_plan_payload),
            "declared_call_plan",
            str(call_plan_payload["schema_version"]),
        ),
    )
    return [repository] + [
        writer.write_json(uri, payload, role=role, schema_version=schema)
        for uri, payload, role, schema in definitions
    ]


def _initialize_store(
    run_dir: Path,
    plan: RealPreflightPlan,
    repository_binding: Mapping[str, str],
    static_sidecars: list[ImmutableSidecar],
) -> SQLiteRunStore:
    now = _now()
    store = SQLiteRunStore.create(
        run_dir / "run.db",
        Run(
            plan.run_id,
            "dof_real_l0_preflight",
            now,
            {
                "artifact_contract": "run.db.v2+immutable-dof-sidecars",
                "runner_version": PREFLIGHT_RUNNER_VERSION,
                "spec_hash": plan.spec_hash,
                "level": "L0",
                "declared_manifest_hashes": {
                    item.role: item.sha256 for item in static_sidecars
                },
                "repository_provenance": dict(repository_binding),
            },
        ),
    )
    with store.transaction():
        store.add_run_event(
            RunLifecycleEvent(
                "run-event-000",
                plan.run_id,
                0,
                RunStatus.RUNNING,
                now,
                {"runner_version": PREFLIGHT_RUNNER_VERSION},
            )
        )
        store.add_scope(
            ExecutionScope(
                _ROOT_SCOPE_ID,
                plan.run_id,
                ScopeKind.ROOT,
                "WP-003 L0 read-only real-DCP preflight",
                created_at=now,
                metadata={"preflight_id": plan.preflight_id},
            )
        )
        store.add_artifact(
            ArtifactReference(
                _ROOT_ARTIFACT_ID,
                plan.run_id,
                _ROOT_SCOPE_ID,
                ArtifactKind.DCP,
                f"external://dcp/sha256/{plan.dcp_sha256}",
                plan.dcp_sha256,
                plan.dcp_size_bytes,
                now,
                {
                    "role": "frozen_real_root",
                    "pseudonym": plan.dcp_pseudonym,
                    "source_regime": plan.source_regime,
                    "included_in_evidence": False,
                    "read_only": True,
                },
            )
        )
        store.add_state(
            DesignState(
                _ROOT_STATE_ID,
                plan.run_id,
                _ROOT_SCOPE_ID,
                _ROOT_ARTIFACT_ID,
                created_at=now,
                metadata={
                    "stage": "UNVERIFIED_ROOT",
                    "pseudonym": plan.dcp_pseudonym,
                    "root_sha256": plan.dcp_sha256,
                    "level": "L0",
                },
            )
        )
        store.add_validation(
            ValidationRecord(
                "validation-root-registration",
                plan.run_id,
                _ROOT_SCOPE_ID,
                _ROOT_STATE_ID,
                ValidationLevel.CERTIFICATION,
                ValidationStatus.NOT_RUN,
                checked_at=now,
                details={"validator": "not_run_at_registration"},
            )
        )
    return store


def _validate_reports(reports: Mapping[str, bytes]) -> dict[str, Any]:
    timing_raw = reports.get("vivado.report_timing_summary", b"")
    route_raw = reports.get("vivado.report_route_status", b"")
    utilization_raw = reports.get("vivado.report_utilization", b"")
    timing = _parse_timing(timing_raw.decode("utf-8", "replace"))
    route = _parse_route(route_raw.decode("utf-8", "replace"))
    utilization = _parse_utilization(utilization_raw.decode("utf-8", "replace"))
    missing: list[str] = []
    if timing["wns_ns"] is None or timing["tns_ns"] is None:
        missing.append("timing_summary_not_parsed")
    if route["is_fully_routed"] is not True:
        missing.append("route_status_not_fully_routed")
    if not utilization["resources"]:
        missing.append("utilization_not_parsed")
    return {
        "schema_version": PREFLIGHT_VALIDATION_SCHEMA,
        "valid": not missing,
        "timing": timing,
        "route_status": route,
        "utilization": utilization,
        "missing_reasons": missing,
    }


def _parse_timing(text: str) -> dict[str, Any]:
    lines = text.splitlines()
    for index, line in enumerate(lines):
        if "WNS(ns)" in line and "TNS(ns)" in line:
            for candidate in lines[index + 1 : index + 5]:
                values = candidate.split()
                if len(values) < 3:
                    continue
                try:
                    return {
                        "wns_ns": float(values[0]),
                        "tns_ns": float(values[1]),
                        "failing_endpoints": int(values[2]),
                    }
                except ValueError:
                    continue
    return {"wns_ns": None, "tns_ns": None, "failing_endpoints": None}


def _parse_route(text: str) -> dict[str, Any]:
    lower = text.lower()
    unrouted = _first_int(
        (
            # Vivado's text table pads labels with dots before the colon.
            r"#\s*of\s*unrouted\s*nets[.\s]*[:|]\s*(\d+)",
            r"unrouted\s+nets[.\s]*[:|]\s*(\d+)",
        ),
        text,
    )
    # Prefer Vivado's numeric count when it is present.  A substring check for
    # ``fully routed`` must not turn ``not fully routed`` into a successful
    # validation.
    if unrouted is not None:
        fully = unrouted == 0
    elif re.search(
        r"\b(?:not\s+fully\s+routed|partially\s+routed|design\s+is\s+not\s+routed|design\s+is\s+unrouted)\b",
        lower,
    ):
        fully = False
    elif "fully routed" in lower or "design is routed" in lower:
        fully = True
    else:
        fully = None
    return {"is_fully_routed": fully, "unrouted_nets": unrouted}


def _parse_utilization(text: str) -> dict[str, Any]:
    resources: dict[str, int] = {}
    for label in ("CLB LUTs", "CLB Registers", "Block RAM Tile", "DSPs", "URAM"):
        match = re.search(
            rf"\|\s*{re.escape(label)}\s*\|\s*([\d,]+)\s*\|",
            text,
            re.IGNORECASE,
        )
        if match:
            resources[label] = int(match.group(1).replace(",", ""))
    return {"resources": resources}


def _first_int(patterns: tuple[str, ...], text: str) -> int | None:
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            return int(match.group(1))
    return None


async def _bounded_fingerprint(
    path: Path,
    *,
    python_exec: Path,
    cwd: Path,
    timeout_seconds: float,
    output_limit_bytes: int,
    result_sink: list[OwnedProcessResult] | None = None,
) -> tuple[RootFingerprint, OwnedProcessResult]:
    """Hash one regular file in a killable, owned subprocess."""

    timeout = float(timeout_seconds)
    cleanup_unit = min(0.5, max(0.05, timeout / 10.0))
    limits = ProcessLimits(
        initialization_timeout_seconds=min(5.0, timeout),
        whole_run_timeout_seconds=timeout,
        graceful_close_seconds=cleanup_unit,
        terminate_grace_seconds=cleanup_unit,
        kill_grace_seconds=cleanup_unit,
        cleanup_timeout_seconds=3.0 * cleanup_unit,
        output_limit_bytes=max(64 * 1024, int(output_limit_bytes)),
    )
    runner = OwnedProcessRunner(limits)
    # The isolated stdlib-only hash worker needs no inherited provider,
    # licence, user-site, or toolchain environment.  Keep this closed so file
    # integrity checks cannot become a credential propagation side channel.
    effective_env = {
        "PATH": os.defpath,
        "LANG": "C",
        "LC_ALL": "C",
        "PYTHONNOUSERSITE": "1",
    }
    try:
        result = await runner.run(
            (
                str(python_exec),
                "-I",
                str(Path(__file__).resolve().with_name("bounded_hash_worker.py")),
                str(path),
            ),
            cwd=cwd,
            env=effective_env,
        )
    except OwnedProcessCancelled as exc:
        # The subprocess really started and the runner finalized its evidence
        # before propagating cancellation.  Preserve that result exactly once
        # so the provider/host trace and process-lifecycle sidecar cannot claim
        # zero starts or lose the actual process token.
        if result_sink is not None:
            result_sink.append(exc.result)
        raise
    if result_sink is not None:
        result_sink.append(result)
    if result.endpoint in {
        ProcessEndpoint.INITIALIZATION_TIMEOUT,
        ProcessEndpoint.WHOLE_RUN_TIMEOUT,
        ProcessEndpoint.CALL_TIMEOUT,
    }:
        raise PreflightExecutionError("TIMEOUT", "bounded root fingerprint timed out")
    if result.endpoint is not ProcessEndpoint.SUCCEEDED or not result.cleanup_ok:
        endpoint = "INVALID_ARTIFACT" if result.returncode == 2 else "EVIDENCE_INTEGRITY_FAILURE"
        raise PreflightExecutionError(endpoint, "bounded root fingerprint failed")
    if result.stdout.truncated or result.stderr.truncated:
        raise PreflightExecutionError(
            "EVIDENCE_INTEGRITY_FAILURE", "bounded root fingerprint output truncated"
        )
    try:
        payload = json.loads(result.stdout.data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PreflightExecutionError(
            "EVIDENCE_INTEGRITY_FAILURE", "bounded root fingerprint output invalid"
        ) from exc
    if not isinstance(payload, dict) or set(payload) != {
        "schema_version",
        "sha256",
        "size_bytes",
    }:
        raise PreflightExecutionError(
            "EVIDENCE_INTEGRITY_FAILURE", "bounded root fingerprint fields invalid"
        )
    if (
        payload["schema_version"] != HASH_WORKER_SCHEMA
        or not isinstance(payload["sha256"], str)
        or _SHA256_RE.fullmatch(payload["sha256"]) is None
        or not isinstance(payload["size_bytes"], int)
        or isinstance(payload["size_bytes"], bool)
        or payload["size_bytes"] <= 0
    ):
        raise PreflightExecutionError(
            "EVIDENCE_INTEGRITY_FAILURE", "bounded root fingerprint values invalid"
        )
    return RootFingerprint(payload["sha256"], payload["size_bytes"]), result


def _exact_line_count(raw: bytes, line: str) -> int:
    expected = line.encode("ascii")
    return sum(item.rstrip(b"\r") == expected for item in raw.splitlines())


def _marker_values(raw: bytes, prefix: str) -> tuple[str, ...]:
    encoded = prefix.encode("ascii")
    values: list[str] = []
    for line in raw.splitlines():
        normalized = line.rstrip(b"\r")
        if normalized.startswith(encoded):
            values.append(normalized[len(encoded) :].decode("ascii", "replace"))
    return tuple(values)


def _marker_value(raw: bytes, prefix: str) -> str | None:
    values = _marker_values(raw, prefix)
    return values[0] if len(values) == 1 else None


def _verify_worker_attestation(
    raw: bytes,
    plan: RealPreflightPlan,
    *,
    expected_environment_hash: str,
) -> dict[str, Any]:
    if _exact_line_count(raw, INIT_POLICY_LINE) != 1:
        raise PreflightExecutionError(
            "STARTUP_POLICY_VIOLATION", "Vivado initialization policy was not attested"
        )
    required_lines = (LAUNCHED_LINE, STARTED_LINE, READY_LINE)
    if any(_exact_line_count(raw, line) != 1 for line in required_lines):
        raise PreflightExecutionError(
            "STARTUP_ATTESTATION_FAILURE", "worker startup marker cardinality invalid"
        )
    vivado = _marker_value(raw, VERSION_PREFIX)
    python = _marker_value(raw, PYTHON_VERSION_PREFIX)
    effective_environment = _marker_value(raw, EFFECTIVE_ENV_PREFIX)
    if vivado is None or python is None or effective_environment is None:
        raise PreflightExecutionError(
            "STARTUP_ATTESTATION_FAILURE", "worker version marker cardinality invalid"
        )
    if vivado != plan.expected_vivado_version or python != plan.expected_python_version:
        raise PreflightExecutionError(
            "TOOL_VERSION_MISMATCH", "observed tool version differs from predeclared plan"
        )
    if effective_environment != expected_environment_hash:
        raise PreflightExecutionError(
            "STARTUP_ATTESTATION_FAILURE",
            "Vivado effective environment binding differs from parent observation",
        )
    return {
        "status": "PASS",
        "vivado_version": vivado,
        "python_version": python,
        "init_policy": "PASS",
        "effective_environment_sha256": effective_environment,
        "launch_marker_count": 1,
        "ready_marker_count": 1,
    }


def _owned_operation_binding(
    result: OwnedProcessResult, *, binding_type: str
) -> dict[str, Any] | None:
    token = result.root_process_token
    if token is None or _SHA256_RE.fullmatch(token) is None:
        return None
    return {
        "binding_type": binding_type,
        "root_process_token_hash": token,
        "session_owned": result.session_owned,
    }


def _process_result_token_is_lifecycle_bound(result: OwnedProcessResult) -> bool:
    token = result.root_process_token
    return (
        token is not None
        and _SHA256_RE.fullmatch(token) is not None
        and any(
            item.get("event_type") == "PROCESS_STARTED"
            and item.get("root_process_token") == token
            for item in result.lifecycle
        )
    )


def _successful_preflight_semantic_errors(
    *,
    plan: RealPreflightPlan,
    call_plan: Any,
    observer: ObservedCallExecutor,
    reconciliation: Mapping[str, Any],
    process_result: OwnedProcessResult | None,
    hash_process_results: list[OwnedProcessResult],
    root_payload: Mapping[str, Any],
    validation: Mapping[str, Any],
    worker_attestation: Mapping[str, Any],
    observed_vivado_version: str | None,
    observed_python_version: str | None,
    vivado_starts: int,
    vivado_ready_starts: int,
    init_policy_passes: int,
    final_repository_check: Mapping[str, Any],
    execution_binding_id: str,
    observer_process_token: str,
) -> tuple[str, ...]:
    """Cross-check success claims across calls, processes, tools and root facts."""

    errors: list[str] = []
    declared_operations = [rule.operation_id for rule in call_plan.rules]
    observed_operations_raw = reconciliation.get("observed_started_operations")
    expected_operations = (
        [str(item) for item in observed_operations_raw]
        if isinstance(observed_operations_raw, list)
        and all(isinstance(item, str) for item in observed_operations_raw)
        else []
    )
    if reconciliation.get("ok") is not True:
        errors.append("semantic:call_reconciliation_not_ok")
    if not expected_operations:
        errors.append("semantic:observed_call_sequence_missing")
    if any(operation_id not in declared_operations for operation_id in expected_operations):
        errors.append("semantic:observed_call_sequence_unknown_operation")
    terminal = reconciliation.get("observed_terminal_status_counts")
    if not isinstance(terminal, Mapping) or terminal.get("SUCCEEDED") != len(
        expected_operations
    ) or any(
        terminal.get(status.value, 0) != 0
        for status in CallStatus
        if status is not CallStatus.SUCCEEDED
    ):
        errors.append("semantic:call_terminal_counts_mismatch")

    rows = [dict(item) for item in observer.trace_rows]
    if len(rows) != 2 * len(expected_operations) or [
        row.get("sequence") for row in rows
    ] != list(range(len(rows))):
        errors.append("semantic:call_trace_shape_mismatch")
    if any(
        row.get("execution_binding")
        != {"runtime_binding_id": execution_binding_id}
        for row in rows
    ):
        errors.append("semantic:call_process_binding_mismatch")
    by_call: dict[str, list[Mapping[str, Any]]] = {}
    for row in rows:
        call_id = row.get("call_id")
        if isinstance(call_id, str):
            by_call.setdefault(call_id, []).append(row)
    if len(by_call) != len(expected_operations) or any(
        [item.get("record_type") for item in pair]
        != ["HOST_CALL_START", "HOST_CALL_TERMINAL"]
        for pair in by_call.values()
    ):
        errors.append("semantic:call_start_terminal_pair_mismatch")

    terminal_rows = [
        row for row in rows if row.get("record_type") == "HOST_CALL_TERMINAL"
    ]
    hash_index = 0
    operation_binding_mismatch = False
    for operation_id, row in zip(expected_operations, terminal_rows, strict=False):
        if row.get("operation_id") != operation_id:
            operation_binding_mismatch = True
            continue
        expected_binding: Mapping[str, Any] | None
        if operation_id == "validation.hash_root":
            expected_binding = (
                _owned_operation_binding(
                    hash_process_results[hash_index],
                    binding_type="owned_validation_worker",
                )
                if hash_index < len(hash_process_results)
                else None
            )
            hash_index += 1
        elif operation_id == "validation.validate_root":
            expected_binding = {
                "binding_type": "in_process_validation",
                "observer_process_token_hash": observer_process_token,
            }
        else:
            expected_binding = (
                _owned_operation_binding(
                    process_result, binding_type="owned_vivado_session"
                )
                if process_result is not None
                else None
            )
        if expected_binding is None or row.get(
            "operation_process_binding"
        ) != expected_binding:
            operation_binding_mismatch = True
    if len(terminal_rows) != len(expected_operations) or operation_binding_mismatch:
        errors.append("semantic:operation_process_binding_mismatch")

    expected_used = {event_class: 0 for event_class in EventClass}
    for operation_id in expected_operations:
        try:
            event_class = observer.registry.operation(operation_id).event_class
        except ToolCallContractError:
            continue
        if isinstance(event_class, EventClass):
            expected_used[event_class] += 1
    configured_limits = {
        EventClass(key): value for key, value in plan.event_limits.items()
    }
    if observer.budget.used != expected_used or any(
        expected_used[event_class] > configured_limits[event_class]
        for event_class in EventClass
    ):
        errors.append("semantic:actual_event_budget_mismatch")
    if worker_attestation.get("status") != "PASS":
        errors.append("semantic:worker_attestation_not_pass")
    if (
        observed_vivado_version != plan.expected_vivado_version
        or observed_python_version != plan.expected_python_version
    ):
        errors.append("semantic:observed_tool_version_mismatch")
    if (vivado_starts, vivado_ready_starts, init_policy_passes) != (1, 1, 1):
        errors.append("semantic:tool_start_cardinality_mismatch")

    if process_result is None:
        errors.append("semantic:host_process_evidence_missing")
    else:
        lifecycle_types = {
            str(item.get("event_type")) for item in process_result.lifecycle
        }
        if (
            process_result.endpoint is not ProcessEndpoint.SUCCEEDED
            or not process_result.cleanup_ok
            or not process_result.session_owned
            or process_result.orphan_process_tokens
            or "PROCESS_TREE_OWNERSHIP_VERIFIED" not in lifecycle_types
            or not _process_result_token_is_lifecycle_bound(process_result)
        ):
            errors.append("semantic:host_process_cleanup_or_ownership_invalid")
    if len(hash_process_results) != 2 or any(
        item.endpoint is not ProcessEndpoint.SUCCEEDED
        or not item.cleanup_ok
        or not _process_result_token_is_lifecycle_bound(item)
        for item in hash_process_results
    ):
        errors.append("semantic:root_hash_process_evidence_invalid")
    if not (
        root_payload.get("declared")
        == root_payload.get("before")
        == root_payload.get("after")
        and root_payload.get("matches_declared") is True
        and root_payload.get("unchanged") is True
        and root_payload.get("child_dcp_produced") is False
    ):
        errors.append("semantic:root_integrity_mismatch")
    if validation.get("valid") is not True or validation.get("missing_reasons") != []:
        errors.append("semantic:validation_not_complete")
    if (
        final_repository_check.get("status") != "MATCH"
        or final_repository_check.get("constraint_satisfied") is not True
        or final_repository_check.get("tracked_identity_match") is not True
    ):
        errors.append("semantic:final_repository_check_mismatch")
    return tuple(errors)


def _process_endpoint(result: OwnedProcessResult) -> str:
    if result.endpoint in {
        ProcessEndpoint.INITIALIZATION_TIMEOUT,
        ProcessEndpoint.CALL_TIMEOUT,
        ProcessEndpoint.WHOLE_RUN_TIMEOUT,
    }:
        return "TIMEOUT"
    if result.endpoint is ProcessEndpoint.CANCELLED:
        return "CANCELLATION"
    if result.endpoint in {
        ProcessEndpoint.CLEANUP_FAILURE,
        ProcessEndpoint.ORPHAN_DETECTED,
        ProcessEndpoint.OWNERSHIP_FAILURE,
    }:
        return "PROCESS_CLEANUP_FAILURE"
    return "BACKEND_ERROR"


def _prepare_output(path: str | Path) -> Path:
    root = Path(path).resolve()
    if root.exists() and any(root.iterdir()):
        raise FileExistsError(f"preflight run directory must be new or empty: {root}")
    root.mkdir(parents=True, exist_ok=True)
    return root


def _binding(sidecar: ImmutableSidecar) -> dict[str, str]:
    return {
        "artifact_id": sidecar.artifact_id,
        "schema_version": sidecar.schema_version,
        "sha256": sidecar.sha256,
    }


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def _now() -> datetime:
    return datetime.now(timezone.utc)


__all__ = [
    "E0_PREFLIGHT_PLAN_SCHEMA",
    "PREFLIGHT_PLAN_SCHEMA",
    "PREFLIGHT_RUNNER_VERSION",
    "PreflightExecutionError",
    "PreflightPlanError",
    "RealPreflightPlan",
    "freeze_e0_preflight_plan",
    "freeze_e1_observation_preflight_plan",
    "run_l0_real_preflight",
    "write_e0_preflight_plan",
]
