"""Exactly-once WP-004A real technical mutation plan and evidence runner."""

from __future__ import annotations

import asyncio
import base64
from collections.abc import Awaitable, Callable, Mapping
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
from typing import Any, Sequence

from optimizer_pipeline.core.domain import (
    ActionSpecification,
    ArtifactKind,
    ArtifactReference,
    Attempt,
    AttemptEventKind,
    AttemptLifecycleEvent,
    DesignState,
    ExecutionOutcome,
    ExecutionScope,
    OutcomeStatus,
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

from .artifacts import (
    ArtifactTransaction,
    ArtifactTransactionError,
    ArtifactTransactionResult,
    ArtifactValidation,
    RootFingerprint,
    RootPreservationGuard,
    fingerprint_file,
    verify_artifact_lifecycle,
)
from .contracts import DesignStage, DoFLevel, EventClass
from .evidence import (
    ImmutableEvidenceWriter,
    register_evidence_and_finalize,
    verify_registered_evidence,
)
from .expansions import (
    expand_real_action,
    real_action_expansion_manifest,
    real_action_expansion_manifest_v2,
)
from .generated import GENERATED_OPERATION_ID, validate_generated_program
from .mutation_worker_binding import (
    MutationWorkerBindingError,
    argv_sha256,
    build_mutation_worker_environment,
    build_mutation_worker_argv,
    mutation_worker_entry_path,
    mutation_worker_launch_binding,
    validate_launch_binding,
)
from .preflight import (
    PreflightExecutionError,
    _bounded_fingerprint,
    _parse_route,
    _parse_timing,
)
from .processes import (
    CapturedOutput,
    OwnedLineProtocolSession,
    OwnedProcessResult,
    OwnedProtocolCallTimedOut,
    OwnedProtocolSessionError,
    ProcessEndpoint,
    ProcessLimits,
)
from .provenance import observe_repository
from .technical import technical_gated_plan_call_plan, technical_mutation_call_plan
from .toolcalls import (
    ActualCallBudget,
    BackendContractMismatch,
    CallStatus,
    ObservedCallExecutor,
    ToolCallContractError,
    default_tool_call_registry,
    e1v2_tool_call_registry,
)
from .vivado_mutation_worker import (
    EFFECTIVE_ENV_PREFIX,
    INIT_POLICY_LINE,
    LAUNCHED_LINE,
    PYTHON_VERSION_PREFIX,
    READY_LINE,
    STARTED_LINE,
    VERSION_PREFIX,
    WORKER_PROTOCOL_PREFIX,
    WORKER_REQUEST_SCHEMA,
    WORKER_TRANSPORT_PROTOCOL_VERSION,
    WORKER_VERSION_PREFIX,
    WORKER_VERSION,
    effective_vivado_environment_hash,
    parse_tcl_result,
    request_bytes,
    terminal_marker,
)


REAL_MUTATION_PLAN_SCHEMA = "dof.wp004a-real-mutation-plan.v1"
E0_REAL_ACTION_PLAN_SCHEMA = "dof.e0-real-action-plan.v1"
E0_REAL_GATED_PLAN_SCHEMA = "dof.e0-real-gated-plan.v1"
E1_REAL_ACTION_PLAN_SCHEMA = "dof.e1-real-action-plan.v1"
E1_REAL_GATED_PLAN_SCHEMA = "dof.e1-real-gated-plan.v1"
E1V2_REAL_GATED_PLAN_SCHEMA = "dof.e1v2-real-gated-plan.v1"
E0_DISPATCH_AUTHORIZATION_SCHEMA = "dof.e0-dispatch-authorization.v1"
REAL_MUTATION_RUNNER_VERSION = "dof.wp004a-real-mutation-runner.v1"
REAL_MUTATION_RESULT_SCHEMA = "dof.wp004a-real-mutation-result.v1"
REAL_MUTATION_REPORT_SCHEMA = "dof.wp004a-real-mutation-reports.v1"
REAL_MUTATION_PROCESS_SCHEMA = "dof.wp004a-real-mutation-process.v1"
REAL_MUTATION_ROOT_SCHEMA = "dof.wp004a-root-integrity.v1"
REAL_MUTATION_CALL_TRACE_SCHEMA = "dof.observed-call-trace.v1"
REAL_MUTATION_REPOSITORY_FINAL_SCHEMA = "dof.repository-final-check.v1"
REAL_MUTATION_WORKER_BINDING_SCHEMA = "dof.wp004a-worker-binding-evidence.v1"
EXPECTED_ROOT_PSEUDONYM = "fpl26-vexriscv-smallest-audited"
EXPECTED_ROOT_SHA256 = "8baf6451f9b20b8a82f035d14187c7350529a6ea0330e02d93c7100b19753149"
EXPECTED_ROOT_SIZE = 1_815_178
EXPECTED_VIVADO_VERSION = "2025.1"

_RUN_ID = "wp004a-real-technical-mutation"
_SCOPE_ID = "scope-wp004a-root"
_ROOT_ARTIFACT_ID = "artifact-wp004a-root"
_ROOT_STATE_ID = "state-wp004a-root"
_ACTION_ID = "action-wp004a-l3-phys-opt-explore"
_ATTEMPT_ID = "attempt-wp004a-real-mutation-001"
_CHILD_STATE_ID = "state-wp004a-real-technical-child"
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_GIT_OID_RE = re.compile(r"^[0-9a-f]{40}$")
_REQUIRED_ROLES = (
    "real_mutation_plan",
    "repository_observation",
    "repository_final_check",
    "call_registry_manifest",
    "real_action_expansion",
    "declared_call_plan",
    "worker_launch_binding",
    "backend_call_trace",
    "process_lifecycle",
    "root_integrity",
    "technical_report_summary",
    "real_mutation_result",
)


class RealMutationPlanError(ValueError):
    pass


class RealMutationExecutionError(RuntimeError):
    def __init__(self, endpoint: str, reason: str) -> None:
        self.endpoint = endpoint
        self.reason = reason
        super().__init__(reason)


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def _hash(value: Any) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _event_limits(
    plan: Any, registry: ToolCallRegistry | None = None
) -> dict[str, int]:
    active_registry = registry or default_tool_call_registry()
    limits = {item.value: 0 for item in EventClass}
    for rule in plan.rules:
        event_class = active_registry.operation(rule.operation_id).event_class
        if isinstance(event_class, EventClass):
            limits[event_class.value] += rule.max_calls
    return limits


def _real_process_limits() -> ProcessLimits:
    return ProcessLimits(
        initialization_timeout_seconds=60.0,
        whole_run_timeout_seconds=1200.0,
        graceful_close_seconds=2.0,
        terminate_grace_seconds=5.0,
        kill_grace_seconds=5.0,
        output_limit_bytes=8 * 1024 * 1024,
        cleanup_timeout_seconds=15.0,
    )


def _real_call_timeouts(call_plan: Any | None = None) -> dict[str, float]:
    timeouts = {
        "host.session.start": 60.0,
        "host.session.close": 15.0,
        "validation.hash_root": 30.0,
        "validation.hash_child": 30.0,
        "validation.validate_child": 30.0,
        "validation.register_child": 60.0,
        "evidence.finalize": 30.0,
        "vivado.open_checkpoint": 120.0,
        "vivado.report_timing_summary": 60.0,
        "vivado.report_route_status": 60.0,
        "vivado.unroute_all": 120.0,
        "vivado.unplace_all": 120.0,
        "vivado.place_design": 900.0,
        "vivado.route_design": 900.0,
        "vivado.phys_opt_design": 900.0,
        "vivado.generated_pblock_region": 120.0,
        "vivado.write_checkpoint": 120.0,
        "vivado.close_design": 60.0,
    }
    if call_plan is None:
        return timeouts
    declared = {rule.operation_id for rule in call_plan.rules}
    optional_mutations = {
        "vivado.unroute_all",
        "vivado.unplace_all",
        "vivado.place_design",
        "vivado.route_design",
        "vivado.generated_pblock_region",
    }
    return {
        key: value
        for key, value in timeouts.items()
        if key not in optional_mutations or key in declared
    }


def _real_limits(
    call_plan: Any, registry: ToolCallRegistry | None = None
) -> dict[str, Any]:
    process_limits = _real_process_limits()
    return {
        "real_mutation_invocations": 1,
        "external_model_requests": 0,
        "whole_run_wall_seconds": 1200.0,
        "event_limits": _event_limits(call_plan, registry),
        "call_timeouts_seconds": _real_call_timeouts(call_plan),
        "process_limits": {
            "initialization_timeout_seconds": (
                process_limits.initialization_timeout_seconds
            ),
            "whole_run_timeout_seconds": process_limits.whole_run_timeout_seconds,
            "graceful_close_seconds": process_limits.graceful_close_seconds,
            "terminate_grace_seconds": process_limits.terminate_grace_seconds,
            "kill_grace_seconds": process_limits.kill_grace_seconds,
            "output_limit_bytes": process_limits.output_limit_bytes,
            "cleanup_timeout_seconds": process_limits.cleanup_timeout_seconds,
        },
    }


def freeze_real_mutation_plan(
    root_dcp: str | Path,
    *,
    repository_anchor: str | Path,
    python_exec: str | Path,
    vivado_exec: str | Path,
    run_dir: str | Path,
    fake_startup_attestation_sha256: str,
) -> dict[str, Any]:
    """Create a public WP-004A plan without starting Vivado."""

    root = fingerprint_file(Path(root_dcp).resolve())
    if (root.sha256, root.size_bytes) != (EXPECTED_ROOT_SHA256, EXPECTED_ROOT_SIZE):
        raise RealMutationPlanError("audited_root_identity_mismatch")
    repository = observe_repository(repository_anchor)
    if repository.tracked_tree_dirty:
        raise RealMutationPlanError("tracked_product_tree_not_clean")
    registry = default_tool_call_registry()
    manifest = real_action_expansion_manifest(registry)
    expanded = expand_real_action(
        DoFLevel.L3,
        "manual.phys_opt_design",
        {"directive": "Explore"},
        current_stage=DesignStage.ROUTED,
        manifest=manifest,
    )
    call_plan = technical_mutation_call_plan(expanded)
    process_limits = _real_process_limits()
    planned_run_dir = Path(run_dir).resolve()
    worker_binding = mutation_worker_launch_binding(
        python_exec=python_exec,
        worker_entry=mutation_worker_entry_path(),
        vivado_exec=vivado_exec,
        initialization_timeout_seconds=process_limits.initialization_timeout_seconds,
        run_workspace=planned_run_dir,
        isolated_home=planned_run_dir / "private" / "vivado-home",
        fake_startup_attestation_sha256=fake_startup_attestation_sha256,
    )
    payload = {
        "schema_version": REAL_MUTATION_PLAN_SCHEMA,
        "runner_version": REAL_MUTATION_RUNNER_VERSION,
        "run_id": _RUN_ID,
        "technical_non_scientific": True,
        "root": {
            "pseudonym": EXPECTED_ROOT_PSEUDONYM,
            "sha256": root.sha256,
            "size_bytes": root.size_bytes,
        },
        "repository_constraint": {
            "head_commit": repository.head_commit,
            "head_tree": repository.head_tree,
            "binary_diff_sha256": repository.binary_diff_sha256,
            "tracked_tree_dirty": False,
            "branch_name": repository.branch_name,
        },
        "action": {
            "level": "L3",
            "action_key": "manual.phys_opt_design",
            "parameters": {"directive": "Explore"},
            "initial_stage": "ROUTED",
            "policy_source": "deterministic technical harness",
            "model_requests": 0,
        },
        "bindings": {
            "tool_call_registry_hash": registry.sha256,
            "real_action_expansion_hash": manifest.sha256,
            "expanded_action_hash": _hash(expanded.to_dict()),
            "declared_call_plan_hash": call_plan.sha256,
            "worker_launch": worker_binding,
        },
        "expected_tool_versions": {
            "vivado": EXPECTED_VIVADO_VERSION,
            "python": platform.python_version(),
        },
        "limits": _real_limits(call_plan, registry),
    }
    return payload | {"plan_sha256": _hash(payload)}


def freeze_e0_real_action_plan(
    root_dcp: str | Path,
    *,
    root_pseudonym: str,
    level: DoFLevel,
    action_key: str,
    parameters: Mapping[str, Any],
    run_id: str,
    repository_anchor: str | Path,
    python_exec: str | Path,
    vivado_exec: str | Path,
    run_dir: str | Path,
    fake_startup_attestation_sha256: str,
    protocol: str = "e0",
) -> dict[str, Any]:
    """Freeze one E0 action before policy results are observed.

    Callers freeze every legal action plan for a job and later select only the
    plan exactly matching the strictly parsed/gated proposal.
    """

    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", run_id):
        raise RealMutationPlanError("e0_run_id_not_canonical")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", root_pseudonym):
        raise RealMutationPlanError("e0_root_pseudonym_not_canonical")
    if level not in {DoFLevel.L1, DoFLevel.L2, DoFLevel.L3}:
        raise RealMutationPlanError("e0_real_action_level_invalid")
    root = fingerprint_file(Path(root_dcp).resolve())
    repository = observe_repository(repository_anchor)
    if repository.tracked_tree_dirty:
        raise RealMutationPlanError("tracked_product_tree_not_clean")
    registry = default_tool_call_registry()
    manifest = real_action_expansion_manifest(registry)
    expanded = expand_real_action(
        level,
        action_key,
        dict(parameters),
        current_stage=DesignStage.ROUTED,
        manifest=manifest,
    )
    if protocol not in {"e0", "e1"}:
        raise RealMutationPlanError("real_action_protocol_invalid")
    heavy_cap = 3 if protocol == "e0" else 4
    if expanded.heavy_call_count > heavy_cap:
        raise RealMutationPlanError(f"{protocol}_heavy_call_cap_exceeded")
    call_plan = technical_mutation_call_plan(expanded)
    process_limits = _real_process_limits()
    planned_run_dir = Path(run_dir).resolve()
    worker_binding = mutation_worker_launch_binding(
        python_exec=python_exec,
        worker_entry=mutation_worker_entry_path(),
        vivado_exec=vivado_exec,
        initialization_timeout_seconds=process_limits.initialization_timeout_seconds,
        run_workspace=planned_run_dir,
        isolated_home=planned_run_dir / "private" / "vivado-home",
        fake_startup_attestation_sha256=fake_startup_attestation_sha256,
    )
    payload = {
        "schema_version": (
            E0_REAL_ACTION_PLAN_SCHEMA
            if protocol == "e0"
            else E1_REAL_ACTION_PLAN_SCHEMA
        ),
        "runner_version": REAL_MUTATION_RUNNER_VERSION,
        "run_id": run_id,
        "technical_non_scientific": False,
        "root": {
            "pseudonym": root_pseudonym,
            "sha256": root.sha256,
            "size_bytes": root.size_bytes,
        },
        "repository_constraint": {
            "head_commit": repository.head_commit,
            "head_tree": repository.head_tree,
            "binary_diff_sha256": repository.binary_diff_sha256,
            "tracked_tree_dirty": False,
            "branch_name": repository.branch_name,
        },
        "action": {
            "level": level.value,
            "action_key": action_key,
            "parameters": json.loads(_canonical_bytes(dict(parameters))),
            "initial_stage": "ROUTED",
            "policy_source": f"{protocol}_frozen_legal_action_plan",
            "model_requests": 0 if level is DoFLevel.L1 else 1,
        },
        "bindings": {
            "tool_call_registry_hash": registry.sha256,
            "real_action_expansion_hash": manifest.sha256,
            "expanded_action_hash": _hash(expanded.to_dict()),
            "declared_call_plan_hash": call_plan.sha256,
            "worker_launch": worker_binding,
        },
        "expected_tool_versions": {
            "vivado": EXPECTED_VIVADO_VERSION,
            "python": platform.python_version(),
        },
        "limits": _real_limits(call_plan, registry),
        "selection_policy": (
            "validated_routed_child_if_wns_improves_else_root"
            if protocol == "e0"
            else "e1_valid_routed_fmax_external_scorer"
        ),
    }
    return payload | {"plan_sha256": _hash(payload)}


def freeze_e0_real_gated_plan(
    root_dcp: str | Path,
    *,
    root_pseudonym: str,
    level: DoFLevel,
    decision_id: str,
    proposal_id: str,
    proposal_hash: str,
    gate_decision_sha256: str,
    gated_steps: Sequence[Mapping[str, Any]],
    causal_evidence_sha256: str,
    run_id: str,
    repository_anchor: str | Path,
    python_exec: str | Path,
    vivado_exec: str | Path,
    run_dir: str | Path,
    fake_startup_attestation_sha256: str,
    protocol: str = "e0",
    scientific_selection_eligible: bool = True,
) -> dict[str, Any]:
    """Materialize one accepted decision into a closed serial host plan.

    This is intentionally post-gate construction.  The deterministic
    expansion semantics are frozen in the product commit; the selected plan
    is bound to the exact causal evidence before the first backend call.
    """

    if level not in {DoFLevel.L2, DoFLevel.L3, DoFLevel.L4}:
        raise RealMutationPlanError("e0_gated_plan_level_invalid")
    if level is DoFLevel.L4 and protocol != "e1v2":
        raise RealMutationPlanError("e0_gated_plan_level_invalid")
    if not isinstance(scientific_selection_eligible, bool):
        raise RealMutationPlanError("e0_gated_plan_selection_eligibility_invalid")
    if not scientific_selection_eligible and protocol != "e1v2":
        raise RealMutationPlanError("technical_admission_requires_e1v2")
    if not gated_steps:
        raise RealMutationPlanError("e0_gated_plan_empty")
    for name, value, pattern in (
        ("run_id", run_id, r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}"),
        ("root_pseudonym", root_pseudonym, r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}"),
        ("decision_id", decision_id, r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}"),
        ("proposal_id", proposal_id, r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}"),
        ("proposal_hash", proposal_hash, r"[0-9a-f]{64}"),
        ("gate_decision_sha256", gate_decision_sha256, r"[0-9a-f]{64}"),
        ("causal_evidence_sha256", causal_evidence_sha256, r"[0-9a-f]{64}"),
    ):
        if not isinstance(value, str) or re.fullmatch(pattern, value) is None:
            raise RealMutationPlanError(f"e0_gated_plan_{name}_invalid")
    root = fingerprint_file(Path(root_dcp).resolve())
    repository = observe_repository(repository_anchor)
    if repository.tracked_tree_dirty:
        raise RealMutationPlanError("tracked_product_tree_not_clean")
    is_e1v2 = protocol == "e1v2"
    registry = e1v2_tool_call_registry() if is_e1v2 else default_tool_call_registry()
    manifest = (
        real_action_expansion_manifest_v2(registry)
        if is_e1v2
        else real_action_expansion_manifest(registry)
    )
    current_stage = DesignStage.ROUTED
    normalized_steps: list[dict[str, Any]] = []
    expanded_steps = []
    for index, raw in enumerate(gated_steps):
        if not isinstance(raw, Mapping):
            raise RealMutationPlanError("e0_gated_step_not_object")
        step = dict(raw)
        if set(step) != {
            "step_index",
            "step_id",
            "action_key",
            "parameters",
            "stage_before",
            "stage_after",
        }:
            raise RealMutationPlanError("e0_gated_step_fields_not_exact")
        if step["step_index"] != index:
            raise RealMutationPlanError("e0_gated_step_index_mismatch")
        if step["stage_before"] != current_stage.value:
            raise RealMutationPlanError("e0_gated_step_stage_before_mismatch")
        try:
            expanded = expand_real_action(
                level,
                str(step["action_key"]),
                dict(step["parameters"]),
                current_stage=current_stage,
                registry=registry,
                manifest=manifest,
            )
        except (TypeError, ValueError, ToolCallContractError) as exc:
            raise RealMutationPlanError("e0_gated_step_expansion_invalid") from exc
        if step["stage_after"] != expanded.final_stage.value:
            raise RealMutationPlanError("e0_gated_step_stage_after_mismatch")
        normalized_steps.append(
            {
                "step_index": index,
                "step_id": str(step["step_id"]),
                "action_key": str(step["action_key"]),
                "parameters": json.loads(_canonical_bytes(dict(step["parameters"]))),
                "stage_before": current_stage.value,
                "stage_after": expanded.final_stage.value,
            }
        )
        expanded_steps.append(expanded)
        current_stage = expanded.final_stage
    total_heavy = sum(item.heavy_call_count for item in expanded_steps)
    if protocol not in {"e0", "e1", "e1v2"}:
        raise RealMutationPlanError("real_gated_protocol_invalid")
    heavy_cap = 3 if protocol == "e0" else 4
    if total_heavy > heavy_cap:
        raise RealMutationPlanError(f"{protocol}_heavy_call_cap_exceeded")
    call_plan = technical_gated_plan_call_plan(tuple(expanded_steps), registry)
    process_limits = _real_process_limits()
    planned_run_dir = Path(run_dir).resolve()
    worker_binding = mutation_worker_launch_binding(
        python_exec=python_exec,
        worker_entry=mutation_worker_entry_path(),
        vivado_exec=vivado_exec,
        initialization_timeout_seconds=process_limits.initialization_timeout_seconds,
        run_workspace=planned_run_dir,
        isolated_home=planned_run_dir / "private" / "vivado-home",
        fake_startup_attestation_sha256=fake_startup_attestation_sha256,
    )
    expanded_payload = [item.to_dict() for item in expanded_steps]
    payload = {
        "schema_version": (
            E0_REAL_GATED_PLAN_SCHEMA
            if protocol == "e0"
            else (
                E1_REAL_GATED_PLAN_SCHEMA
                if protocol == "e1"
                else E1V2_REAL_GATED_PLAN_SCHEMA
            )
        ),
        "runner_version": REAL_MUTATION_RUNNER_VERSION,
        "run_id": run_id,
        "technical_non_scientific": not scientific_selection_eligible,
        "root": {
            "pseudonym": root_pseudonym,
            "sha256": root.sha256,
            "size_bytes": root.size_bytes,
        },
        "repository_constraint": {
            "head_commit": repository.head_commit,
            "head_tree": repository.head_tree,
            "binary_diff_sha256": repository.binary_diff_sha256,
            "tracked_tree_dirty": False,
            "branch_name": repository.branch_name,
        },
        "action": {
            "level": level.value,
            "decision_id": decision_id,
            "proposal_id": proposal_id,
            "proposal_hash": proposal_hash,
            "gate_decision_sha256": gate_decision_sha256,
            "causal_evidence_sha256": causal_evidence_sha256,
            "initial_stage": "ROUTED",
            "final_stage": current_stage.value,
            "policy_source": f"{protocol}_bound_provider_gate",
            "model_requests": 1 if scientific_selection_eligible else 0,
            "steps": normalized_steps,
        },
        "bindings": {
            "tool_call_registry_hash": registry.sha256,
            "real_action_expansion_hash": manifest.sha256,
            "expanded_plan_hash": _hash(expanded_payload),
            "declared_call_plan_hash": call_plan.sha256,
            "worker_launch": worker_binding,
        },
        "expected_tool_versions": {
            "vivado": EXPECTED_VIVADO_VERSION,
            "python": platform.python_version(),
        },
        "limits": _real_limits(call_plan, registry),
        "selection_policy": (
            "root_only_technical_admission"
            if not scientific_selection_eligible
            else (
                "validated_routed_child_if_wns_improves_else_root"
                if protocol == "e0"
                else "e1_valid_routed_fmax_external_scorer"
            )
        ),
    }
    return payload | {"plan_sha256": _hash(payload)}


def build_e0_dispatch_authorization(
    plan: Mapping[str, Any], causal_evidence: Mapping[str, Any]
) -> dict[str, Any]:
    """Seal exact causal evidence and the materialized plan for dispatch."""

    validated = _validate_any_e0_plan(plan)
    evidence = json.loads(_canonical_bytes(dict(causal_evidence)))
    claimed_evidence_sha = evidence.get("causal_evidence_sha256")
    if claimed_evidence_sha is None:
        evidence_sha = _hash(evidence)
    else:
        evidence_core = {
            key: item
            for key, item in evidence.items()
            if key != "causal_evidence_sha256"
        }
        if claimed_evidence_sha != _hash(evidence_core):
            raise RealMutationPlanError("dispatch_causal_evidence_hash_mismatch")
        evidence_sha = claimed_evidence_sha
    embedded = validated["action"].get("causal_evidence_sha256")
    if embedded is not None and embedded != evidence_sha:
        raise RealMutationPlanError("dispatch_causal_evidence_plan_mismatch")
    if validated["schema_version"] in {
        E0_REAL_GATED_PLAN_SCHEMA,
        E1_REAL_GATED_PLAN_SCHEMA,
        E1V2_REAL_GATED_PLAN_SCHEMA,
    }:
        action_steps = list(validated["action"]["steps"])
    else:
        action_steps = [
            {
                "step_index": 0,
                "step_id": "deterministic-action",
                "action_key": validated["action"]["action_key"],
                "parameters": validated["action"]["parameters"],
                "stage_before": validated["action"]["initial_stage"],
                "stage_after": validated["action"]["initial_stage"],
            }
        ]
    step_authorizations = []
    prior_binding = validated["root"]["sha256"]
    for step in action_steps:
        step_payload = {
            "schema_version": "dof.e0-step-dispatch-authorization.v1",
            "dispatch_allowed": True,
            "plan_sha256": validated["plan_sha256"],
            "causal_evidence_sha256": evidence_sha,
            "prior_state_binding": prior_binding,
            "step": step,
        }
        sealed = step_payload | {"step_authorization_sha256": _hash(step_payload)}
        step_authorizations.append(sealed)
        prior_binding = sealed["step_authorization_sha256"]
    payload = {
        "schema_version": E0_DISPATCH_AUTHORIZATION_SCHEMA,
        "dispatch_allowed": True,
        "plan_sha256": validated["plan_sha256"],
        "root": validated["root"],
        "causal_evidence": evidence,
        "causal_evidence_sha256": evidence_sha,
        "action_binding_sha256": _hash(validated["action"]),
        "declared_call_plan_hash": validated["bindings"][
            "declared_call_plan_hash"
        ],
        "step_authorizations": step_authorizations,
    }
    return payload | {"authorization_sha256": _hash(payload)}


def validate_e0_dispatch_authorization(
    plan: Mapping[str, Any],
    authorization: Mapping[str, Any],
    causal_evidence: Mapping[str, Any],
) -> dict[str, Any]:
    validated = _validate_any_e0_plan(plan)
    if not isinstance(authorization, Mapping):
        raise RealMutationPlanError("dispatch_authorization_not_object")
    value = json.loads(_canonical_bytes(dict(authorization)))
    claimed = value.pop("authorization_sha256", None)
    if claimed != _hash(value):
        raise RealMutationPlanError("dispatch_authorization_hash_mismatch")
    expected = build_e0_dispatch_authorization(validated, causal_evidence)
    if value | {"authorization_sha256": claimed} != expected:
        raise RealMutationPlanError("dispatch_authorization_binding_mismatch")
    return expected


def write_frozen_plan(plan: Mapping[str, Any], path: str | Path) -> None:
    validated = validate_real_mutation_plan(plan)
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        raise FileExistsError(target)
    target.write_bytes(json.dumps(validated, sort_keys=True, indent=2).encode() + b"\n")


def load_real_mutation_plan(path: str | Path) -> dict[str, Any]:
    return validate_real_mutation_plan(json.loads(Path(path).read_text()))


def validate_real_mutation_plan(plan: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(plan, Mapping):
        raise RealMutationPlanError("plan_not_object")
    value = json.loads(_canonical_bytes(dict(plan)))
    claimed = value.pop("plan_sha256", None)
    if value.get("schema_version") != REAL_MUTATION_PLAN_SCHEMA:
        raise RealMutationPlanError("plan_schema_mismatch")
    if claimed != _hash(value):
        raise RealMutationPlanError("plan_hash_mismatch")
    if set(value) != {
        "schema_version",
        "runner_version",
        "run_id",
        "technical_non_scientific",
        "root",
        "repository_constraint",
        "action",
        "bindings",
        "expected_tool_versions",
        "limits",
    }:
        raise RealMutationPlanError("plan_fields_not_exact")
    if (
        value.get("runner_version") != REAL_MUTATION_RUNNER_VERSION
        or value.get("run_id") != _RUN_ID
        or value.get("technical_non_scientific") is not True
    ):
        raise RealMutationPlanError("plan_identity_mismatch")
    if value.get("expected_tool_versions") != {
        "vivado": EXPECTED_VIVADO_VERSION,
        "python": platform.python_version(),
    }:
        raise RealMutationPlanError("plan_tool_versions_mismatch")
    if value.get("root") != {
        "pseudonym": EXPECTED_ROOT_PSEUDONYM,
        "sha256": EXPECTED_ROOT_SHA256,
        "size_bytes": EXPECTED_ROOT_SIZE,
    }:
        raise RealMutationPlanError("plan_root_mismatch")
    if value.get("action") != {
        "level": "L3",
        "action_key": "manual.phys_opt_design",
        "parameters": {"directive": "Explore"},
        "initial_stage": "ROUTED",
        "policy_source": "deterministic technical harness",
        "model_requests": 0,
    }:
        raise RealMutationPlanError("plan_action_mismatch")
    repository = value.get("repository_constraint")
    if (
        not isinstance(repository, dict)
        or set(repository)
        != {
            "head_commit",
            "head_tree",
            "binary_diff_sha256",
            "tracked_tree_dirty",
            "branch_name",
        }
        or not _GIT_OID_RE.fullmatch(str(repository.get("head_commit")))
        or not _GIT_OID_RE.fullmatch(str(repository.get("head_tree")))
        or not _SHA256_RE.fullmatch(str(repository.get("binary_diff_sha256")))
        or repository.get("tracked_tree_dirty") is not False
        or repository.get("branch_name") != "phase0-policy-evidence-replan"
    ):
        raise RealMutationPlanError("plan_repository_constraint_mismatch")
    bindings = value.get("bindings")
    if not isinstance(bindings, dict) or set(bindings) != {
        "tool_call_registry_hash",
        "real_action_expansion_hash",
        "expanded_action_hash",
        "declared_call_plan_hash",
        "worker_launch",
    }:
        raise RealMutationPlanError("plan_bindings_mismatch")
    for key in (
        "tool_call_registry_hash",
        "real_action_expansion_hash",
        "expanded_action_hash",
        "declared_call_plan_hash",
    ):
        if not isinstance(bindings.get(key), str) or not _SHA256_RE.fullmatch(
            bindings[key]
        ):
            raise RealMutationPlanError("plan_binding_hash_invalid")
    registry = default_tool_call_registry()
    manifest = real_action_expansion_manifest(registry)
    expanded = expand_real_action(
        DoFLevel.L3,
        "manual.phys_opt_design",
        {"directive": "Explore"},
        current_stage=DesignStage.ROUTED,
        manifest=manifest,
    )
    call_plan = technical_mutation_call_plan(expanded)
    if {
        key: bindings[key]
        for key in (
            "tool_call_registry_hash",
            "real_action_expansion_hash",
            "expanded_action_hash",
            "declared_call_plan_hash",
        )
    } != {
        "tool_call_registry_hash": registry.sha256,
        "real_action_expansion_hash": manifest.sha256,
        "expanded_action_hash": _hash(expanded.to_dict()),
        "declared_call_plan_hash": call_plan.sha256,
    }:
        raise RealMutationPlanError("plan_binding_semantics_mismatch")
    try:
        bindings["worker_launch"] = validate_launch_binding(
            bindings["worker_launch"]
        )
    except MutationWorkerBindingError as exc:
        raise RealMutationPlanError(str(exc)) from exc
    if value.get("limits") != _real_limits(call_plan, registry):
        raise RealMutationPlanError("plan_limits_mismatch")
    return value | {"plan_sha256": claimed}


def validate_e0_real_action_plan(plan: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(plan, Mapping):
        raise RealMutationPlanError("plan_not_object")
    value = json.loads(_canonical_bytes(dict(plan)))
    claimed = value.pop("plan_sha256", None)
    if value.get("schema_version") not in {
        E0_REAL_ACTION_PLAN_SCHEMA,
        E1_REAL_ACTION_PLAN_SCHEMA,
    }:
        raise RealMutationPlanError("e0_plan_schema_mismatch")
    if claimed != _hash(value):
        raise RealMutationPlanError("e0_plan_hash_mismatch")
    if set(value) != {
        "schema_version",
        "runner_version",
        "run_id",
        "technical_non_scientific",
        "root",
        "repository_constraint",
        "action",
        "bindings",
        "expected_tool_versions",
        "limits",
        "selection_policy",
    }:
        raise RealMutationPlanError("e0_plan_fields_not_exact")
    if (
        value.get("runner_version") != REAL_MUTATION_RUNNER_VERSION
        or value.get("technical_non_scientific") is not False
        or value.get("selection_policy")
        not in {
            "validated_routed_child_if_wns_improves_else_root",
            "e1_valid_routed_fmax_external_scorer",
        }
        or not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", str(value.get("run_id"))
        )
    ):
        raise RealMutationPlanError("e0_plan_identity_mismatch")
    root = value.get("root")
    if (
        not isinstance(root, dict)
        or set(root) != {"pseudonym", "sha256", "size_bytes"}
        or not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", str(root.get("pseudonym"))
        )
        or not _SHA256_RE.fullmatch(str(root.get("sha256")))
        or not isinstance(root.get("size_bytes"), int)
        or isinstance(root.get("size_bytes"), bool)
        or root["size_bytes"] <= 0
    ):
        raise RealMutationPlanError("e0_plan_root_invalid")
    repository = value.get("repository_constraint")
    if (
        not isinstance(repository, dict)
        or set(repository)
        != {
            "head_commit",
            "head_tree",
            "binary_diff_sha256",
            "tracked_tree_dirty",
            "branch_name",
        }
        or not _GIT_OID_RE.fullmatch(str(repository.get("head_commit")))
        or not _GIT_OID_RE.fullmatch(str(repository.get("head_tree")))
        or not _SHA256_RE.fullmatch(str(repository.get("binary_diff_sha256")))
        or repository.get("tracked_tree_dirty") is not False
        or repository.get("branch_name") != "phase0-policy-evidence-replan"
    ):
        raise RealMutationPlanError("e0_plan_repository_constraint_mismatch")
    if value.get("expected_tool_versions") != {
        "vivado": EXPECTED_VIVADO_VERSION,
        "python": platform.python_version(),
    }:
        raise RealMutationPlanError("e0_plan_tool_versions_mismatch")
    action = value.get("action")
    if not isinstance(action, dict) or set(action) != {
        "level",
        "action_key",
        "parameters",
        "initial_stage",
        "policy_source",
        "model_requests",
    }:
        raise RealMutationPlanError("e0_plan_action_invalid")
    try:
        level = DoFLevel(action["level"])
    except (KeyError, ValueError) as exc:
        raise RealMutationPlanError("e0_plan_action_level_invalid") from exc
    if (
        level not in {DoFLevel.L1, DoFLevel.L2, DoFLevel.L3}
        or action.get("initial_stage") != "ROUTED"
        or action.get("policy_source")
        != (
            "e1_frozen_legal_action_plan"
            if value["schema_version"] == E1_REAL_ACTION_PLAN_SCHEMA
            else "e0_frozen_legal_action_plan"
        )
        or action.get("model_requests") != (0 if level is DoFLevel.L1 else 1)
        or not isinstance(action.get("action_key"), str)
        or not isinstance(action.get("parameters"), dict)
    ):
        raise RealMutationPlanError("e0_plan_action_semantics_invalid")
    registry = default_tool_call_registry()
    manifest = real_action_expansion_manifest(registry)
    try:
        expanded = expand_real_action(
            level,
            action["action_key"],
            action["parameters"],
            current_stage=DesignStage.ROUTED,
            manifest=manifest,
        )
    except (TypeError, ValueError, ToolCallContractError) as exc:
        raise RealMutationPlanError("e0_plan_action_expansion_invalid") from exc
    heavy_cap = 4 if value["schema_version"] == E1_REAL_ACTION_PLAN_SCHEMA else 3
    if expanded.heavy_call_count > heavy_cap:
        raise RealMutationPlanError("real_action_heavy_call_cap_exceeded")
    call_plan = technical_mutation_call_plan(expanded)
    bindings = value.get("bindings")
    if not isinstance(bindings, dict) or set(bindings) != {
        "tool_call_registry_hash",
        "real_action_expansion_hash",
        "expanded_action_hash",
        "declared_call_plan_hash",
        "worker_launch",
    }:
        raise RealMutationPlanError("e0_plan_bindings_invalid")
    expected_hashes = {
        "tool_call_registry_hash": registry.sha256,
        "real_action_expansion_hash": manifest.sha256,
        "expanded_action_hash": _hash(expanded.to_dict()),
        "declared_call_plan_hash": call_plan.sha256,
    }
    if {key: bindings.get(key) for key in expected_hashes} != expected_hashes:
        raise RealMutationPlanError("e0_plan_binding_semantics_mismatch")
    try:
        bindings["worker_launch"] = validate_launch_binding(
            bindings["worker_launch"]
        )
    except MutationWorkerBindingError as exc:
        raise RealMutationPlanError(str(exc)) from exc
    if value.get("limits") != _real_limits(call_plan, registry):
        raise RealMutationPlanError("e0_plan_limits_mismatch")
    return value | {"plan_sha256": claimed}


def validate_e0_real_gated_plan(plan: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(plan, Mapping):
        raise RealMutationPlanError("plan_not_object")
    value = json.loads(_canonical_bytes(dict(plan)))
    claimed = value.pop("plan_sha256", None)
    if value.get("schema_version") not in {
        E0_REAL_GATED_PLAN_SCHEMA,
        E1_REAL_GATED_PLAN_SCHEMA,
        E1V2_REAL_GATED_PLAN_SCHEMA,
    }:
        raise RealMutationPlanError("e0_gated_plan_schema_mismatch")
    if claimed != _hash(value):
        raise RealMutationPlanError("e0_gated_plan_hash_mismatch")
    if set(value) != {
        "schema_version",
        "runner_version",
        "run_id",
        "technical_non_scientific",
        "root",
        "repository_constraint",
        "action",
        "bindings",
        "expected_tool_versions",
        "limits",
        "selection_policy",
    }:
        raise RealMutationPlanError("e0_gated_plan_fields_not_exact")
    if (
        value.get("runner_version") != REAL_MUTATION_RUNNER_VERSION
        or value.get("technical_non_scientific")
        is not (value.get("selection_policy") == "root_only_technical_admission")
        or value.get("selection_policy")
        not in (
            {
                "validated_routed_child_if_wns_improves_else_root",
                "e1_valid_routed_fmax_external_scorer",
                "root_only_technical_admission",
            }
            if value.get("schema_version") == E1V2_REAL_GATED_PLAN_SCHEMA
            else {
                "validated_routed_child_if_wns_improves_else_root",
                "e1_valid_routed_fmax_external_scorer",
            }
        )
        or re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", str(value.get("run_id"))
        )
        is None
    ):
        raise RealMutationPlanError("e0_gated_plan_identity_mismatch")
    root = value.get("root")
    if (
        not isinstance(root, dict)
        or set(root) != {"pseudonym", "sha256", "size_bytes"}
        or re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", str(root.get("pseudonym"))
        )
        is None
        or _SHA256_RE.fullmatch(str(root.get("sha256"))) is None
        or not isinstance(root.get("size_bytes"), int)
        or isinstance(root.get("size_bytes"), bool)
        or root["size_bytes"] <= 0
    ):
        raise RealMutationPlanError("e0_gated_plan_root_invalid")
    repository = value.get("repository_constraint")
    if (
        not isinstance(repository, dict)
        or set(repository)
        != {
            "head_commit",
            "head_tree",
            "binary_diff_sha256",
            "tracked_tree_dirty",
            "branch_name",
        }
        or _GIT_OID_RE.fullmatch(str(repository.get("head_commit"))) is None
        or _GIT_OID_RE.fullmatch(str(repository.get("head_tree"))) is None
        or _SHA256_RE.fullmatch(str(repository.get("binary_diff_sha256"))) is None
        or repository.get("tracked_tree_dirty") is not False
        or repository.get("branch_name") != "phase0-policy-evidence-replan"
    ):
        raise RealMutationPlanError("e0_gated_plan_repository_invalid")
    if value.get("expected_tool_versions") != {
        "vivado": EXPECTED_VIVADO_VERSION,
        "python": platform.python_version(),
    }:
        raise RealMutationPlanError("e0_gated_plan_tool_versions_mismatch")
    action = value.get("action")
    expected_action_fields = {
        "level",
        "decision_id",
        "proposal_id",
        "proposal_hash",
        "gate_decision_sha256",
        "causal_evidence_sha256",
        "initial_stage",
        "final_stage",
        "policy_source",
        "model_requests",
        "steps",
    }
    if not isinstance(action, dict) or set(action) != expected_action_fields:
        raise RealMutationPlanError("e0_gated_plan_action_invalid")
    try:
        level = DoFLevel(action["level"])
    except (KeyError, ValueError) as exc:
        raise RealMutationPlanError("e0_gated_plan_level_invalid") from exc
    is_e1v2 = value["schema_version"] == E1V2_REAL_GATED_PLAN_SCHEMA
    if (
        level not in (
            {DoFLevel.L2, DoFLevel.L3, DoFLevel.L4}
            if is_e1v2
            else {DoFLevel.L2, DoFLevel.L3}
        )
        or action.get("initial_stage") != "ROUTED"
        or action.get("policy_source")
        != {
            E0_REAL_GATED_PLAN_SCHEMA: "e0_bound_provider_gate",
            E1_REAL_GATED_PLAN_SCHEMA: "e1_bound_provider_gate",
            E1V2_REAL_GATED_PLAN_SCHEMA: "e1v2_bound_provider_gate",
        }[value["schema_version"]]
        or action.get("model_requests")
        != (0 if value.get("selection_policy") == "root_only_technical_admission" else 1)
        or not isinstance(action.get("steps"), list)
        or not action["steps"]
    ):
        raise RealMutationPlanError("e0_gated_plan_action_semantics_invalid")
    for name in (
        "proposal_hash",
        "gate_decision_sha256",
        "causal_evidence_sha256",
    ):
        if _SHA256_RE.fullmatch(str(action.get(name))) is None:
            raise RealMutationPlanError(f"e0_gated_plan_{name}_invalid")
    for name in ("decision_id", "proposal_id"):
        if (
            re.fullmatch(
                r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", str(action.get(name))
            )
            is None
        ):
            raise RealMutationPlanError(f"e0_gated_plan_{name}_invalid")
    registry = e1v2_tool_call_registry() if is_e1v2 else default_tool_call_registry()
    manifest = (
        real_action_expansion_manifest_v2(registry)
        if is_e1v2
        else real_action_expansion_manifest(registry)
    )
    current_stage = DesignStage.ROUTED
    expanded_steps = []
    exact_step_fields = {
        "step_index",
        "step_id",
        "action_key",
        "parameters",
        "stage_before",
        "stage_after",
    }
    for index, step in enumerate(action["steps"]):
        if (
            not isinstance(step, dict)
            or set(step) != exact_step_fields
            or step.get("step_index") != index
            or step.get("stage_before") != current_stage.value
            or not isinstance(step.get("parameters"), dict)
        ):
            raise RealMutationPlanError("e0_gated_plan_step_invalid")
        try:
            expanded = expand_real_action(
                level,
                step["action_key"],
                step["parameters"],
                current_stage=current_stage,
                registry=registry,
                manifest=manifest,
            )
        except (TypeError, ValueError, ToolCallContractError) as exc:
            raise RealMutationPlanError("e0_gated_plan_step_expansion_invalid") from exc
        if step.get("stage_after") != expanded.final_stage.value:
            raise RealMutationPlanError("e0_gated_plan_step_stage_after_mismatch")
        expanded_steps.append(expanded)
        current_stage = expanded.final_stage
    if action.get("final_stage") != current_stage.value:
        raise RealMutationPlanError("e0_gated_plan_final_stage_mismatch")
    heavy_cap = (
        4
        if value["schema_version"]
        in {E1_REAL_GATED_PLAN_SCHEMA, E1V2_REAL_GATED_PLAN_SCHEMA}
        else 3
    )
    if sum(item.heavy_call_count for item in expanded_steps) > heavy_cap:
        raise RealMutationPlanError("real_gated_heavy_call_cap_exceeded")
    call_plan = technical_gated_plan_call_plan(tuple(expanded_steps), registry)
    bindings = value.get("bindings")
    if not isinstance(bindings, dict) or set(bindings) != {
        "tool_call_registry_hash",
        "real_action_expansion_hash",
        "expanded_plan_hash",
        "declared_call_plan_hash",
        "worker_launch",
    }:
        raise RealMutationPlanError("e0_gated_plan_bindings_invalid")
    expected_hashes = {
        "tool_call_registry_hash": registry.sha256,
        "real_action_expansion_hash": manifest.sha256,
        "expanded_plan_hash": _hash([item.to_dict() for item in expanded_steps]),
        "declared_call_plan_hash": call_plan.sha256,
    }
    if {key: bindings.get(key) for key in expected_hashes} != expected_hashes:
        raise RealMutationPlanError("e0_gated_plan_binding_semantics_mismatch")
    try:
        bindings["worker_launch"] = validate_launch_binding(bindings["worker_launch"])
    except MutationWorkerBindingError as exc:
        raise RealMutationPlanError(str(exc)) from exc
    if value.get("limits") != _real_limits(call_plan, registry):
        raise RealMutationPlanError("e0_gated_plan_limits_mismatch")
    return value | {"plan_sha256": claimed}


def _validate_any_e0_plan(plan: Mapping[str, Any]) -> dict[str, Any]:
    schema = plan.get("schema_version") if isinstance(plan, Mapping) else None
    if schema == E0_REAL_ACTION_PLAN_SCHEMA:
        return validate_e0_real_action_plan(plan)
    if schema == E1_REAL_ACTION_PLAN_SCHEMA:
        return validate_e0_real_action_plan(plan)
    if schema == E0_REAL_GATED_PLAN_SCHEMA:
        return validate_e0_real_gated_plan(plan)
    if schema == E1_REAL_GATED_PLAN_SCHEMA:
        return validate_e0_real_gated_plan(plan)
    if schema == E1V2_REAL_GATED_PLAN_SCHEMA:
        return validate_e0_real_gated_plan(plan)
    raise RealMutationPlanError("e0_dispatch_plan_schema_invalid")


def _expanded_execution(
    plan: Mapping[str, Any],
) -> tuple[tuple[Any, ...], dict[str, Any], Any]:
    level = DoFLevel(plan["action"]["level"])
    is_e1v2 = plan.get("schema_version") == E1V2_REAL_GATED_PLAN_SCHEMA
    registry = e1v2_tool_call_registry() if is_e1v2 else default_tool_call_registry()
    manifest = (
        real_action_expansion_manifest_v2(registry)
        if is_e1v2
        else real_action_expansion_manifest(registry)
    )
    if plan["schema_version"] in {
        E0_REAL_GATED_PLAN_SCHEMA,
        E1_REAL_GATED_PLAN_SCHEMA,
        E1V2_REAL_GATED_PLAN_SCHEMA,
    }:
        current_stage = DesignStage.ROUTED
        expanded_steps = []
        for step in plan["action"]["steps"]:
            expanded = expand_real_action(
                level,
                step["action_key"],
                step["parameters"],
                current_stage=current_stage,
                registry=registry,
                manifest=manifest,
            )
            expanded_steps.append(expanded)
            current_stage = expanded.final_stage
        expanded_tuple = tuple(expanded_steps)
        return (
            expanded_tuple,
            {"steps": [item.to_dict() for item in expanded_tuple]},
            technical_gated_plan_call_plan(expanded_tuple, registry),
        )
    expanded = expand_real_action(
        level,
        plan["action"]["action_key"],
        plan["action"]["parameters"],
        current_stage=DesignStage.ROUTED,
        registry=registry,
        manifest=manifest,
    )
    return (expanded,), expanded.to_dict(), technical_mutation_call_plan(expanded)


SessionFactory = Callable[..., OwnedLineProtocolSession]
FingerprintFunction = Callable[..., Awaitable[tuple[RootFingerprint, OwnedProcessResult]]]
RepositoryObserver = Callable[[str | Path], Any]
RootGuardFactory = Callable[[Path], RootPreservationGuard]


def _runtime_ids(plan: Mapping[str, Any]) -> dict[str, str]:
    if plan.get("schema_version") == REAL_MUTATION_PLAN_SCHEMA:
        return {
            "run": _RUN_ID,
            "scope": _SCOPE_ID,
            "root_artifact": _ROOT_ARTIFACT_ID,
            "root_state": _ROOT_STATE_ID,
            "action": _ACTION_ID,
            "attempt": _ATTEMPT_ID,
            "child_state": _CHILD_STATE_ID,
        }
    token = str(plan["plan_sha256"])[:20]
    return {
        "run": str(plan["run_id"]),
        "scope": f"scope-e0-{token}",
        "root_artifact": f"artifact-e0-root-{token}",
        "root_state": f"state-e0-root-{token}",
        "action": f"action-e0-{token}",
        "attempt": f"attempt-e0-{token}",
        "child_state": f"state-e0-child-{token}",
    }


def execute_real_mutation(
    plan: Mapping[str, Any],
    root_dcp: str | Path,
    run_dir: str | Path,
    *,
    vivado_exec: str | Path,
    python_exec: str | Path,
    repository_anchor: str | Path,
    session_factory: SessionFactory = OwnedLineProtocolSession,
    fingerprint_function: FingerprintFunction = _bounded_fingerprint,
    repository_observer: RepositoryObserver = observe_repository,
    root_guard_factory: RootGuardFactory = RootPreservationGuard,
) -> dict[str, Any]:
    validated = validate_real_mutation_plan(plan)
    return asyncio.run(
        _execute_real_mutation(
            validated,
            Path(root_dcp).resolve(),
            Path(run_dir).resolve(),
            Path(vivado_exec).resolve(),
            # Preserve the venv launcher path.  Resolving the symlink would
            # execute the base interpreter and change the frozen argv.
            Path(python_exec).absolute(),
            repository_anchor,
            session_factory,
            fingerprint_function,
            repository_observer,
            root_guard_factory,
            None,
        )
    )


def execute_e0_real_action(
    plan: Mapping[str, Any],
    root_dcp: str | Path,
    run_dir: str | Path,
    *,
    dispatch_authorization: Mapping[str, Any],
    causal_evidence: Mapping[str, Any],
    vivado_exec: str | Path,
    python_exec: str | Path,
    repository_anchor: str | Path,
    session_factory: SessionFactory = OwnedLineProtocolSession,
    fingerprint_function: FingerprintFunction = _bounded_fingerprint,
    repository_observer: RepositoryObserver = observe_repository,
    root_guard_factory: RootGuardFactory = RootPreservationGuard,
) -> dict[str, Any]:
    validated = validate_e0_real_action_plan(plan)
    validate_e0_dispatch_authorization(
        validated, dispatch_authorization, causal_evidence
    )
    return asyncio.run(
        _execute_real_mutation(
            validated,
            Path(root_dcp).resolve(),
            Path(run_dir).resolve(),
            Path(vivado_exec).resolve(),
            Path(python_exec).absolute(),
            repository_anchor,
            session_factory,
            fingerprint_function,
            repository_observer,
            root_guard_factory,
            dispatch_authorization,
        )
    )


def execute_e0_real_gated_plan(
    plan: Mapping[str, Any],
    root_dcp: str | Path,
    run_dir: str | Path,
    *,
    dispatch_authorization: Mapping[str, Any],
    causal_evidence: Mapping[str, Any],
    vivado_exec: str | Path,
    python_exec: str | Path,
    repository_anchor: str | Path,
    session_factory: SessionFactory = OwnedLineProtocolSession,
    fingerprint_function: FingerprintFunction = _bounded_fingerprint,
    repository_observer: RepositoryObserver = observe_repository,
    root_guard_factory: RootGuardFactory = RootPreservationGuard,
) -> dict[str, Any]:
    validated = validate_e0_real_gated_plan(plan)
    validate_e0_dispatch_authorization(
        validated, dispatch_authorization, causal_evidence
    )
    return asyncio.run(
        _execute_real_mutation(
            validated,
            Path(root_dcp).resolve(),
            Path(run_dir).resolve(),
            Path(vivado_exec).resolve(),
            Path(python_exec).absolute(),
            repository_anchor,
            session_factory,
            fingerprint_function,
            repository_observer,
            root_guard_factory,
            dispatch_authorization,
        )
    )


def write_e0_frozen_plan(plan: Mapping[str, Any], path: str | Path) -> None:
    validated = validate_e0_real_action_plan(plan)
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        raise FileExistsError(target)
    target.write_bytes(json.dumps(validated, sort_keys=True, indent=2).encode() + b"\n")


def write_e0_gated_plan(plan: Mapping[str, Any], path: str | Path) -> None:
    validated = validate_e0_real_gated_plan(plan)
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        raise FileExistsError(target)
    target.write_bytes(json.dumps(validated, sort_keys=True, indent=2).encode() + b"\n")


def load_e0_real_action_plan(path: str | Path) -> dict[str, Any]:
    return validate_e0_real_action_plan(json.loads(Path(path).read_text()))


def load_e0_real_gated_plan(path: str | Path) -> dict[str, Any]:
    return validate_e0_real_gated_plan(json.loads(Path(path).read_text()))


async def _execute_real_mutation(
    plan: dict[str, Any],
    root_dcp: Path,
    run_dir: Path,
    vivado_exec: Path,
    python_exec: Path,
    repository_anchor: str | Path,
    session_factory: SessionFactory,
    fingerprint_function: FingerprintFunction,
    repository_observer: RepositoryObserver,
    root_guard_factory: RootGuardFactory,
    dispatch_authorization: Mapping[str, Any] | None,
) -> dict[str, Any]:
    ids = _runtime_ids(plan)
    run_id = ids["run"]
    scope_id = ids["scope"]
    root_artifact_id = ids["root_artifact"]
    root_state_id = ids["root_state"]
    attempt_id = ids["attempt"]
    child_state_id = ids["child_state"]
    root_spec = plan["root"]
    action_spec = plan["action"]
    level = DoFLevel(action_spec["level"])
    if run_dir.exists():
        raise FileExistsError(run_dir)
    if not vivado_exec.is_file() or not os.access(vivado_exec, os.X_OK):
        raise RealMutationPlanError("vivado_exec_not_executable")
    if not python_exec.is_file() or not os.access(python_exec, os.X_OK):
        raise RealMutationPlanError("python_exec_not_executable")
    root_guard = root_guard_factory(root_dcp)
    if (root_guard.before.sha256, root_guard.before.size_bytes) != (
        root_spec["sha256"],
        root_spec["size_bytes"],
    ):
        raise RealMutationPlanError("execution_root_identity_mismatch")
    repository = repository_observer(repository_anchor)
    constraint = plan["repository_constraint"]
    if repository.tracked_tree_dirty or any(
        getattr(repository, key) != constraint[key]
        for key in ("head_commit", "head_tree", "binary_diff_sha256")
    ):
        raise RealMutationPlanError("execution_repository_constraint_mismatch")
    process_limits = ProcessLimits(**plan["limits"]["process_limits"])
    worker_entry = mutation_worker_entry_path()
    planned_home = run_dir / "private" / "vivado-home"
    frozen_worker_binding = plan["bindings"]["worker_launch"]
    try:
        observed_worker_binding = mutation_worker_launch_binding(
            python_exec=python_exec,
            worker_entry=worker_entry,
            vivado_exec=vivado_exec,
            initialization_timeout_seconds=(
                process_limits.initialization_timeout_seconds
            ),
            run_workspace=run_dir,
            isolated_home=planned_home,
            fake_startup_attestation_sha256=(
                frozen_worker_binding["fake_startup_attestation_sha256"]
            ),
        )
    except MutationWorkerBindingError as exc:
        raise RealMutationPlanError(str(exc)) from exc
    is_e1v2 = plan.get("schema_version") == E1V2_REAL_GATED_PLAN_SCHEMA
    registry = e1v2_tool_call_registry() if is_e1v2 else default_tool_call_registry()
    manifest = (
        real_action_expansion_manifest_v2(registry)
        if is_e1v2
        else real_action_expansion_manifest(registry)
    )
    expanded_steps, expanded_payload, call_plan = _expanded_execution(plan)
    bindings = plan["bindings"]
    observed_bindings = (
        {
            "tool_call_registry_hash": registry.sha256,
            "real_action_expansion_hash": manifest.sha256,
            "expanded_plan_hash": _hash(
                [item.to_dict() for item in expanded_steps]
            ),
            "declared_call_plan_hash": call_plan.sha256,
            "worker_launch": observed_worker_binding,
        }
        if plan["schema_version"]
        in {
            E0_REAL_GATED_PLAN_SCHEMA,
            E1_REAL_GATED_PLAN_SCHEMA,
            E1V2_REAL_GATED_PLAN_SCHEMA,
        }
        else {
            "tool_call_registry_hash": registry.sha256,
            "real_action_expansion_hash": manifest.sha256,
            "expanded_action_hash": _hash(expanded_payload),
            "declared_call_plan_hash": call_plan.sha256,
            "worker_launch": observed_worker_binding,
        }
    )
    if observed_bindings != bindings:
        raise RealMutationPlanError("frozen_binding_mismatch")

    run_dir.mkdir(parents=True, exist_ok=False)
    private_dir = run_dir / "private"
    private_dir.mkdir(mode=0o700)
    home = private_dir / "vivado-home"
    home.mkdir(mode=0o700)
    child_staging = private_dir / "technical-child-staging.dcp"

    writer = ImmutableEvidenceWriter(run_dir, run_id, plan["plan_sha256"])
    sidecars = [
        writer.write_json(
            "evidence/real-mutation-plan.json",
            plan,
            role="real_mutation_plan",
            schema_version=plan["schema_version"],
        ),
        writer.write_json(
            "evidence/repository-provenance.json",
            repository.to_dict() | {"sha256": repository.sha256},
            role="repository_observation",
            schema_version=repository.schema_version,
        ),
        writer.write_json(
            "evidence/call-registry.json",
            registry.to_dict() | {"sha256": registry.sha256},
            role="call_registry_manifest",
            schema_version=registry.schema_version,
        ),
        writer.write_json(
            "evidence/real-action-expansion.json",
            manifest.to_dict() | {"sha256": manifest.sha256},
            role="real_action_expansion",
            schema_version=manifest.schema_version,
        ),
        writer.write_json(
            "evidence/declared-call-plan.json",
            call_plan.to_dict() | {"sha256": call_plan.sha256},
            role="declared_call_plan",
            schema_version=call_plan.schema_version,
        ),
        writer.write_json(
            "evidence/worker-launch-binding.json",
            {
                "schema_version": REAL_MUTATION_WORKER_BINDING_SCHEMA,
                "worker_launch": observed_worker_binding,
            },
            role="worker_launch_binding",
            schema_version=REAL_MUTATION_WORKER_BINDING_SCHEMA,
        ),
    ]
    store = _initialize_store(run_dir, plan, expanded_payload, repository.to_dict())
    trace = writer.open_trace(
        "evidence/backend-calls.jsonl",
        role="backend_call_trace",
        schema_version=REAL_MUTATION_CALL_TRACE_SCHEMA,
    )
    limits = plan["limits"]
    timeouts = limits["call_timeouts_seconds"]
    budget = ActualCallBudget(
        {EventClass(key): value for key, value in limits["event_limits"].items()},
        whole_run_timeout_seconds=limits["whole_run_wall_seconds"],
    )
    session: OwnedLineProtocolSession | None = None
    process_result: OwnedProcessResult | None = None
    hash_results: list[OwnedProcessResult] = []
    transaction_result: ArtifactTransactionResult | None = None
    call_counter = 0
    normal_close = False
    root_before: RootFingerprint | None = None
    root_after: RootFingerprint | None = None
    child_fingerprint: RootFingerprint | None = None
    active_hash_result: OwnedProcessResult | None = None
    phase = "pre"
    report_bytes: dict[str, dict[str, bytes]] = {key: {} for key in ("pre", "post", "child")}
    worker_attestation: dict[str, Any] = {"status": "NOT_OBSERVED"}
    worker_start_attempted = False
    worker_attested = False
    endpoint = "EVIDENCE_INTEGRITY_FAILURE"
    reasons: list[str] = ["run_not_completed"]
    execution_binding_id = _hash(
        {
            "plan_sha256": plan["plan_sha256"],
            "run_id": run_id,
            "kind": "real-runtime",
            "dispatch_authorization_sha256": (
                None
                if dispatch_authorization is None
                else dispatch_authorization["authorization_sha256"]
            ),
        }
    )
    worker_environment = build_mutation_worker_environment(os.environ)
    expected_environment_hash = effective_vivado_environment_hash(
        worker_environment,
        home,
    )
    worker_argv = build_mutation_worker_argv(
        python_exec=python_exec,
        worker_entry=worker_entry,
        vivado_exec=vivado_exec,
        initialization_timeout_seconds=(
            process_limits.initialization_timeout_seconds
        ),
        run_workspace=run_dir,
        isolated_home=home,
    )
    if argv_sha256(worker_argv) != frozen_worker_binding["production_argv_sha256"]:
        raise RealMutationPlanError("production_worker_argv_mismatch")
    close_command = request_bytes(
        "worker.exit", "cleanup", timeout_seconds=timeouts["host.session.close"]
    )

    async def invoke(operation: Any, arguments: Mapping[str, Any]) -> Any:
        nonlocal session, process_result, call_counter, phase, active_hash_result
        nonlocal root_before, root_after, child_fingerprint, transaction_result
        nonlocal worker_attestation, worker_start_attempted, worker_attested
        operation_id = operation.operation_id
        active_hash_result = None
        if operation_id == "host.session.start":
            if session is not None:
                raise RealMutationExecutionError("BACKEND_CONTRACT_MISMATCH", "session_started_twice")
            worker_start_attempted = True
            session = session_factory(
                worker_argv,
                ready_line=READY_LINE,
                close_command=close_command,
                limits=process_limits,
                cwd=run_dir,
                env=worker_environment,
            )
            await session.start()
            startup = session.startup_output
            if startup.truncated:
                raise RealMutationExecutionError(
                    "TOOL_INITIALIZATION_FAILURE",
                    "startup_output_truncated",
                )
            worker_attestation = _verify_worker_attestation(
                startup.data,
                expected_vivado=plan["expected_tool_versions"]["vivado"],
                expected_python=plan["expected_tool_versions"]["python"],
                expected_environment_hash=expected_environment_hash,
                expected_worker=WORKER_VERSION,
                expected_protocol=WORKER_TRANSPORT_PROTOCOL_VERSION,
            )
            worker_attested = True
            return worker_attestation
        if operation_id in {"validation.hash_root", "validation.hash_child"}:
            target = root_dcp if operation_id.endswith("root") else child_staging
            observed, result = await fingerprint_function(
                target,
                python_exec=python_exec,
                cwd=run_dir,
                timeout_seconds=timeouts[operation_id],
                output_limit_bytes=64 * 1024,
                result_sink=hash_results,
            )
            active_hash_result = result
            if operation_id.endswith("root"):
                if observed != root_guard.before:
                    raise RealMutationExecutionError("ROOT_ARTIFACT_MUTATION", "root_fingerprint_changed")
                if arguments.get("phase") == "before":
                    root_before = observed
                else:
                    root_after = observed
            else:
                child_fingerprint = observed
            return {"sha256": observed.sha256, "size_bytes": observed.size_bytes}
        if operation_id == "validation.validate_child":
            validation = _technical_validation(report_bytes["child"])
            if child_fingerprint is None:
                raise RealMutationExecutionError("ARTIFACT_VALIDATION_FAILURE", "child_not_hashed")
            return validation
        if operation_id == "validation.register_child":
            validation = _technical_validation(report_bytes["child"])
            expected = child_fingerprint
            if expected is None:
                raise RealMutationExecutionError("ARTIFACT_VALIDATION_FAILURE", "child_hash_missing")

            def validator(path: Path, digest: str, size_bytes: int) -> ArtifactValidation:
                matches = (digest, size_bytes) == (expected.sha256, expected.size_bytes)
                status = ValidationStatus.CLEAN if validation["valid"] and matches else ValidationStatus.INVALID
                return ArtifactValidation(
                    status,
                    "dof.wp004a-real-child-validator.v1",
                    validation | {"hash_matches_staging": matches},
                )

            transaction = ArtifactTransaction(
                store=store,
                run_dir=run_dir,
                run_id=run_id,
                scope_id=scope_id,
                attempt_id=attempt_id,
                parent_state_id=root_state_id,
                state_id=child_state_id,
            )
            pre_timing = _technical_validation(report_bytes["pre"])["timing"]
            child_timing = validation["timing"]
            pre_wns = pre_timing.get("wns_ns")
            child_wns = child_timing.get("wns_ns")
            e0_selection = (
                plan.get("selection_policy")
                == "validated_routed_child_if_wns_improves_else_root"
            )
            e1_selection = (
                plan.get("selection_policy")
                == "e1_valid_routed_fmax_external_scorer"
            )
            selection_eligible = bool(
                validation["valid"]
                and (
                    e1_selection
                    or (
                        e0_selection
                        and isinstance(pre_wns, (int, float))
                        and isinstance(child_wns, (int, float))
                        and child_wns > pre_wns
                    )
                )
            )
            transaction_result = transaction.execute(
                producer=lambda path: shutil.copyfile(child_staging, path),
                validator=validator,
                metrics={
                    "technical_non_scientific": bool(plan["technical_non_scientific"]),
                    "pilot_non_claim": plan.get("schema_version")
                    in {E0_REAL_ACTION_PLAN_SCHEMA, E0_REAL_GATED_PLAN_SCHEMA},
                    "plan_sha256": plan["plan_sha256"],
                    "pre": _technical_validation(report_bytes["pre"]),
                    "post": _technical_validation(report_bytes["post"]),
                    "child": validation,
                },
                selection_eligible=selection_eligible,
                selection_reason=(
                    "E1 valid routed child admitted for external frozen-Fmax scoring"
                    if e1_selection and selection_eligible
                    else (
                        "E0 validated routed child improved WNS over the immutable root"
                        if selection_eligible
                        else "root retained: child was non-study or did not improve WNS"
                    )
                ),
            )
            return {"endpoint": transaction_result.endpoint, "valid": transaction_result.valid}
        if operation_id == "evidence.finalize":
            lifecycle = verify_artifact_lifecycle(store, run_dir=run_dir)
            return {"safe": lifecycle.safe, "errors": list(lifecycle.errors)}
        if operation_id == "host.session.close":
            if session is None:
                raise RealMutationExecutionError("BACKEND_CONTRACT_MISMATCH", "session_missing")
            process_result = await session.close()
            if not process_result.cleanup_ok or (
                arguments.get("recovery_after_failure") is not True
                and process_result.endpoint is not ProcessEndpoint.SUCCEEDED
            ):
                raise RealMutationExecutionError("PROCESS_CLEANUP_FAILURE", "owned_session_cleanup_failed")
            return process_result.to_dict(include_output_text=False)
        if not operation_id.startswith("vivado.") or session is None:
            raise RealMutationExecutionError("BACKEND_CONTRACT_MISMATCH", "operation_has_no_session")
        token = f"call-{call_counter:03d}"
        call_counter += 1
        worker_arguments: dict[str, Any] = {}
        if operation_id == "vivado.open_checkpoint":
            target = root_dcp if arguments.get("role") == "root" else child_staging
            worker_arguments = {"dcp_path_b64": base64.b64encode(os.fsencode(target)).decode("ascii")}
        elif operation_id == "vivado.write_checkpoint":
            worker_arguments = {"dcp_path_b64": base64.b64encode(os.fsencode(child_staging)).decode("ascii")}
        elif operation_id == GENERATED_OPERATION_ID:
            worker_arguments = validate_generated_program(arguments)
        elif "directive" in arguments:
            worker_arguments = {"directive": arguments["directive"]}
        command = request_bytes(
            operation_id,
            token,
            timeout_seconds=timeouts[operation_id],
            arguments=worker_arguments,
        )
        result = await session.call(
            command,
            terminal_marker=terminal_marker(token),
            timeout_seconds=timeouts[operation_id],
        )
        if result.stdout.truncated:
            raise RealMutationExecutionError("EVIDENCE_INTEGRITY_FAILURE", f"truncated:{operation_id}")
        status, output = parse_tcl_result(token, result.stdout.data)
        if status != 0:
            raise RealMutationExecutionError("TOOL_EXECUTION_FAILURE", f"vivado_failed:{operation_id}")
        if operation_id.startswith("vivado.report_"):
            report_bytes[phase][operation_id] = output
        return {
            "status": "SUCCEEDED",
            "output_sha256": hashlib.sha256(output).hexdigest(),
            "output_size_bytes": len(output),
        }

    observer = ObservedCallExecutor(
        registry=registry,
        plan=call_plan,
        budget=budget,
        invoke=invoke,
        trace_sink=trace.append,
        execution_binding={
            "runtime_binding_id": execution_binding_id,
            "dispatch_authorization_sha256": (
                None
                if dispatch_authorization is None
                else dispatch_authorization["authorization_sha256"]
            ),
        },
    )

    async def observed(
        operation_id: str,
        arguments: Mapping[str, Any] | None = None,
        *,
        current_stage: DesignStage | None = None,
    ) -> Any:
        return await observer.call(
            operation_id,
            dict(arguments or {}),
            timeout_seconds=timeouts[operation_id],
            current_stage=current_stage,
        )

    try:
        await observed("validation.hash_root", {"phase": "before"})
        await observed("host.session.start")
        await observed("vivado.open_checkpoint", {"role": "root"})
        if expanded_steps[0].initial_stage is DesignStage.ROUTED:
            await observed(
                "vivado.report_timing_summary",
                {"phase": "pre"},
                current_stage=DesignStage.ROUTED,
            )
            await observed(
                "vivado.report_route_status",
                {"phase": "pre"},
                current_stage=DesignStage.ROUTED,
            )
        for expanded_step in expanded_steps:
            for expanded_call in expanded_step.calls:
                await observed(
                    expanded_call.operation_id,
                    dict(expanded_call.arguments),
                    current_stage=expanded_call.input_stage,
                )
        phase = "post"
        if expanded_steps[-1].final_stage is DesignStage.ROUTED:
            await observed(
                "vivado.report_timing_summary",
                {"phase": "post"},
                current_stage=DesignStage.ROUTED,
            )
            await observed(
                "vivado.report_route_status",
                {"phase": "post"},
                current_stage=DesignStage.ROUTED,
            )
        await observed("vivado.write_checkpoint", {"role": "technical_child"})
        await observed("vivado.close_design")
        phase = "child"
        await observed("vivado.open_checkpoint", {"role": "technical_child"})
        if expanded_steps[-1].final_stage is DesignStage.ROUTED:
            await observed(
                "vivado.report_timing_summary",
                {"phase": "child"},
                current_stage=DesignStage.ROUTED,
            )
            await observed(
                "vivado.report_route_status",
                {"phase": "child"},
                current_stage=DesignStage.ROUTED,
            )
        await observed("vivado.close_design")
        await observed("host.session.close")
        normal_close = True
        await observed("validation.hash_root", {"phase": "after"})
        await observed("validation.hash_child")
        validation = await observed("validation.validate_child")
        if not validation["valid"]:
            raise RealMutationExecutionError("ARTIFACT_VALIDATION_FAILURE", "child_reports_invalid")
        registered = await observed("validation.register_child")
        if not registered["valid"]:
            raise RealMutationExecutionError("ARTIFACT_VALIDATION_FAILURE", registered["endpoint"])
        await observed("evidence.finalize")
        observer.reconcile_or_raise()
        endpoint = "TECHNICAL_MUTATION_VALID_NONSCIENTIFIC"
        reasons = ["single_fixed_mutation_and_child_validation_completed"]
    except (OwnedProtocolCallTimedOut, asyncio.TimeoutError):
        endpoint = (
            "TOOL_TIMEOUT"
            if worker_attested
            else "TOOL_INITIALIZATION_FAILURE"
        )
        reasons = [
            "bounded_call_timeout"
            if worker_attested
            else "bounded_worker_initialization_timeout"
        ]
    except (BackendContractMismatch, ToolCallContractError) as exc:
        endpoint = "PROVENANCE_OR_CONTRACT_MISMATCH"
        reasons = [type(exc).__name__]
    except (RealMutationExecutionError, ArtifactTransactionError, PreflightExecutionError) as exc:
        endpoint = getattr(exc, "endpoint", "TOOL_EXECUTION_FAILURE")
        reasons = [getattr(exc, "reason", type(exc).__name__)]
    except OwnedProtocolSessionError as exc:
        process_result = exc.result
        if not worker_attested:
            endpoint = "TOOL_INITIALIZATION_FAILURE"
        else:
            endpoint = (
                "PROCESS_CLEANUP_FAILURE"
                if not exc.result.cleanup_ok
                else "TOOL_EXECUTION_FAILURE"
            )
        reasons = [type(exc).__name__]
    except asyncio.CancelledError:
        endpoint = (
            "TOOL_INITIALIZATION_FAILURE"
            if worker_start_attempted and not worker_attested
            else "TOOL_TIMEOUT"
        )
        reasons = [
            "worker_initialization_cancelled"
            if endpoint == "TOOL_INITIALIZATION_FAILURE"
            else "task_cancelled"
        ]
    except Exception as exc:
        endpoint = (
            "TOOL_EXECUTION_FAILURE"
            if worker_attested
            else (
                "TOOL_INITIALIZATION_FAILURE"
                if worker_start_attempted
                else "EVIDENCE_INTEGRITY_FAILURE"
            )
        )
        reasons = [type(exc).__name__]
    finally:
        initialization_was_primary = endpoint == "TOOL_INITIALIZATION_FAILURE"
        if session is not None and not normal_close:
            try:
                await observer.call(
                    "host.session.close",
                    {"recovery_after_failure": True},
                    timeout_seconds=timeouts["host.session.close"],
                    recovery_after_failure=True,
                )
                process_result = session.result
            except BaseException as exc:
                candidate = getattr(exc, "result", None)
                if isinstance(candidate, OwnedProcessResult):
                    process_result = candidate
                if not initialization_was_primary:
                    endpoint = "PROCESS_CLEANUP_FAILURE"
                reasons.append(type(exc).__name__)
        elif session is not None:
            process_result = session.result
        if process_result is not None and not process_result.cleanup_ok:
            if not initialization_was_primary:
                endpoint = "PROCESS_CLEANUP_FAILURE"
            reasons.append("owned_process_cleanup_not_ok")
        if root_after is None and session is not None:
            try:
                observed_root, result = await fingerprint_function(
                    root_dcp,
                    python_exec=python_exec,
                    cwd=run_dir,
                    timeout_seconds=timeouts["validation.hash_root"],
                    output_limit_bytes=64 * 1024,
                    result_sink=hash_results,
                )
                active_hash_result = result
                root_after = observed_root
                if root_after != root_guard.before:
                    endpoint = "ROOT_ARTIFACT_MUTATION"
                    reasons.append("root_fingerprint_changed")
            except BaseException as exc:
                if not initialization_was_primary:
                    endpoint = "EVIDENCE_INTEGRITY_FAILURE"
                reasons.append(type(exc).__name__)

    _record_early_failure(store, endpoint, reasons, attempt_id=attempt_id)
    trace_sidecar = trace.close()
    sidecars.append(trace_sidecar)
    final_repository = repository_observer(repository_anchor)
    repository_match = (
        not final_repository.tracked_tree_dirty
        and final_repository.head_commit == repository.head_commit
        and final_repository.head_tree == repository.head_tree
        and final_repository.binary_diff_sha256 == repository.binary_diff_sha256
    )
    if not repository_match and endpoint != "ROOT_ARTIFACT_MUTATION":
        endpoint = "PROVENANCE_OR_CONTRACT_MISMATCH"
        reasons.append("repository_changed_during_run")
    sidecars.append(
        writer.write_json(
            "evidence/repository-final-check.json",
            {
                "schema_version": REAL_MUTATION_REPOSITORY_FINAL_SCHEMA,
                "status": "MATCH" if repository_match else "MISMATCH",
                "initial_observation_sha256": repository.sha256,
                "final_observation_sha256": final_repository.sha256,
                "tracked_identity_match": repository_match,
            },
            role="repository_final_check",
            schema_version=REAL_MUTATION_REPOSITORY_FINAL_SCHEMA,
        )
    )
    process_payload = {
        "schema_version": REAL_MUTATION_PROCESS_SCHEMA,
        "worker_attestation": worker_attestation,
        "vivado_worker_process": None if process_result is None else process_result.to_dict(include_output_text=False),
        "bounded_hash_processes": [item.to_dict(include_output_text=False) for item in hash_results],
    }
    sidecars.append(
        writer.write_json(
            "evidence/process-lifecycle.json",
            process_payload,
            role="process_lifecycle",
            schema_version=REAL_MUTATION_PROCESS_SCHEMA,
        )
    )
    root_payload = {
        "schema_version": REAL_MUTATION_ROOT_SCHEMA,
        "pseudonym": root_spec["pseudonym"],
        "declared": plan["root"],
        "before": None if root_before is None else {"sha256": root_before.sha256, "size_bytes": root_before.size_bytes},
        "after": None if root_after is None else {"sha256": root_after.sha256, "size_bytes": root_after.size_bytes},
        "unchanged": root_before == root_after == root_guard.before,
    }
    sidecars.append(
        writer.write_json(
            "evidence/root-integrity.json",
            root_payload,
            role="root_integrity",
            schema_version=REAL_MUTATION_ROOT_SCHEMA,
        )
    )
    report_payload = {
        "schema_version": REAL_MUTATION_REPORT_SCHEMA,
        "parser_version": "dof.wp004a-timing-route-parser.v1",
        "raw_reports_included": False,
        "phases": {
            name: {
                "parsed": _technical_validation(values),
                "raw_fingerprints": {
                    key: {"sha256": hashlib.sha256(raw).hexdigest(), "size_bytes": len(raw)}
                    for key, raw in values.items()
                },
            }
            for name, values in report_bytes.items()
        },
    }
    sidecars.append(
        writer.write_json(
            "evidence/technical-reports.json",
            report_payload,
            role="technical_report_summary",
            schema_version=REAL_MUTATION_REPORT_SCHEMA,
            kind=ArtifactKind.REPORT,
        )
    )
    reconciliation = observer.reconciliation().to_dict()
    heavy_starts = sum(
        registry.operation(operation).event_class
        is EventClass.IMPLEMENTATION_HEAVY
        for operation in reconciliation["observed_started_operations"]
    )
    selections = store.selections()
    eligibility = [item for item in store.eligibility() if item.state_id == child_state_id]
    lifecycle = verify_artifact_lifecycle(store, run_dir=run_dir)
    summary = {
        "schema_version": REAL_MUTATION_RESULT_SCHEMA,
        "runner_version": REAL_MUTATION_RUNNER_VERSION,
        "endpoint": endpoint,
        "endpoint_success": endpoint == "TECHNICAL_MUTATION_VALID_NONSCIENTIFIC",
        "reasons": reasons,
        "technical_non_scientific": bool(plan["technical_non_scientific"]),
        "pilot_non_claim": plan.get("schema_version")
        in {E0_REAL_ACTION_PLAN_SCHEMA, E0_REAL_GATED_PLAN_SCHEMA},
        "plan_sha256": plan["plan_sha256"],
        "real_mutation_invocations": heavy_starts,
        "external_model_requests": int(action_spec.get("model_requests", 0)),
        "backend_dispatches": 1 if heavy_starts else 0,
        "worker_start_attempted": worker_start_attempted,
        "worker_attested": worker_attested,
        "worker_launch_binding_sha256": observed_worker_binding[
            "binding_sha256"
        ],
        "root_unchanged": root_payload["unchanged"],
        "call_reconciliation": reconciliation,
        "budget": budget.summary(),
        "transaction": None if transaction_result is None else {
            "endpoint": transaction_result.endpoint,
            "valid": transaction_result.valid,
            "selected": transaction_result.selected,
            "artifact_id": transaction_result.artifact_id,
            "state_id": transaction_result.state_id,
            "sha256": transaction_result.sha256,
            "size_bytes": transaction_result.size_bytes,
            "validation_status": transaction_result.validation_status,
        },
        "canonical_counts": {
            "actions": len(store.actions()),
            "attempts": len(store.attempts()),
            "outcomes": len(store.outcomes()),
            "states": len(store.states()),
            "dcp_artifacts": len([item for item in store.artifacts() if item.kind is ArtifactKind.DCP]),
            "eligibility": len(store.eligibility()),
            "selections": len(selections),
        },
        "root_is_sole_selection": len(selections) == 1 and selections[0].state_id == root_state_id,
        "child_selection_eligible": None if not eligibility else eligibility[0].selection_eligible,
        "artifact_lifecycle": {
            "safe": lifecycle.safe,
            "errors": list(lifecycle.errors),
            "incomplete_attempt_ids": list(lifecycle.incomplete_attempt_ids),
        },
        "repository_tracked_identity_match": repository_match,
        "canonical_schema_version": CURRENT_SCHEMA_VERSION,
    }
    result_sidecar = writer.write_json(
        "evidence/result-summary.json",
        summary,
        role="real_mutation_result",
        schema_version=REAL_MUTATION_RESULT_SCHEMA,
        kind=ArtifactKind.REPORT,
    )
    sidecars.append(result_sidecar)
    registration = register_evidence_and_finalize(
        store,
        run_dir=run_dir,
        sidecars=sidecars,
        run_id=run_id,
        spec_hash=plan["plan_sha256"],
        terminal_status=(RunStatus.FINALIZED if summary["endpoint_success"] else RunStatus.FAILED),
        terminal_detail={
            "endpoint": endpoint,
            "technical_non_scientific": bool(plan["technical_non_scientific"]),
            "pilot_non_claim": plan.get("schema_version")
            in {E0_REAL_ACTION_PLAN_SCHEMA, E0_REAL_GATED_PLAN_SCHEMA},
        },
        scope_id=scope_id,
        required_roles=_REQUIRED_ROLES,
    )
    verification = verify_registered_evidence(
        store,
        run_dir=run_dir,
        expected_run_id=run_id,
        expected_spec_hash=plan["plan_sha256"],
        scope_id=scope_id,
        required_roles=_REQUIRED_ROLES,
    )
    verification.raise_for_errors()
    summary["evidence_set_hash"] = registration.evidence_set_hash
    summary["registered_evidence_roles"] = list(verification.roles)
    summary["sqlite_quick_check"] = store.connection.execute("PRAGMA quick_check").fetchone()[0]
    summary["foreign_key_errors"] = len(store.connection.execute("PRAGMA foreign_key_check").fetchall())
    store.close()
    return summary


def _technical_validation(reports: Mapping[str, bytes]) -> dict[str, Any]:
    timing = _parse_timing(reports.get("vivado.report_timing_summary", b"").decode("utf-8", "replace"))
    route = _parse_route(reports.get("vivado.report_route_status", b"").decode("utf-8", "replace"))
    missing = []
    if timing["wns_ns"] is None or timing["tns_ns"] is None:
        missing.append("timing_summary_not_parsed")
    if route["is_fully_routed"] is not True:
        missing.append("route_status_not_fully_routed")
    return {"valid": not missing, "timing": timing, "route_status": route, "missing_reasons": missing}


def _verify_worker_attestation(
    raw: bytes,
    *,
    expected_vivado: str,
    expected_python: str,
    expected_environment_hash: str,
    expected_worker: str,
    expected_protocol: str,
) -> dict[str, Any]:
    lines = [line.rstrip(b"\r") for line in raw.splitlines()]
    for marker in (INIT_POLICY_LINE, LAUNCHED_LINE, STARTED_LINE, READY_LINE):
        if lines.count(marker.encode("ascii")) != 1:
            raise RealMutationExecutionError("TOOL_INITIALIZATION_FAILURE", "startup_marker_cardinality")

    def value(prefix: str) -> str | None:
        matches = [line[len(prefix):].decode("ascii", "replace") for line in lines if line.startswith(prefix.encode("ascii"))]
        return matches[0] if len(matches) == 1 else None

    vivado = value(VERSION_PREFIX)
    python = value(PYTHON_VERSION_PREFIX)
    environment = value(EFFECTIVE_ENV_PREFIX)
    worker = value(WORKER_VERSION_PREFIX)
    protocol = value(WORKER_PROTOCOL_PREFIX)
    if (vivado, python, environment, worker, protocol) != (
        expected_vivado,
        expected_python,
        expected_environment_hash,
        expected_worker,
        expected_protocol,
    ):
        raise RealMutationExecutionError("TOOL_INITIALIZATION_FAILURE", "startup_attestation_mismatch")
    return {
        "status": "PASS",
        "vivado_version": vivado,
        "python_version": python,
        "effective_environment_sha256": environment,
        "worker_version": worker,
        "transport_protocol_version": protocol,
    }


def _initialize_store(
    run_dir: Path,
    plan: Mapping[str, Any],
    expanded: Mapping[str, Any],
    repository: Mapping[str, Any],
) -> SQLiteRunStore:
    now = datetime.now(timezone.utc)
    ids = _runtime_ids(plan)
    run_id = ids["run"]
    scope_id = ids["scope"]
    root_artifact_id = ids["root_artifact"]
    root_state_id = ids["root_state"]
    action_id = ids["action"]
    attempt_id = ids["attempt"]
    root_spec = plan["root"]
    action_spec = plan["action"]
    token = plan["plan_sha256"][:16]
    store = SQLiteRunStore.create(
        run_dir / "run.db",
        Run(
            run_id,
            (
                "dof_wp004a_real_technical_mutation"
                if plan["schema_version"] == REAL_MUTATION_PLAN_SCHEMA
                else (
                    "dof_e1_known_set_action"
                    if plan["schema_version"]
                    in {
                        E1_REAL_ACTION_PLAN_SCHEMA,
                        E1_REAL_GATED_PLAN_SCHEMA,
                        E1V2_REAL_GATED_PLAN_SCHEMA,
                    }
                    else "dof_e0_real_action_pilot"
                )
            ),
            now,
            {
                "runner_version": REAL_MUTATION_RUNNER_VERSION,
                "spec_hash": plan["plan_sha256"],
                "technical_non_scientific": bool(plan["technical_non_scientific"]),
                "pilot_non_claim": plan["schema_version"]
                in {E0_REAL_ACTION_PLAN_SCHEMA, E0_REAL_GATED_PLAN_SCHEMA},
                "repository_head_commit": repository["head_commit"],
            },
        ),
    )
    with store.transaction():
        store.add_run_event(RunLifecycleEvent(f"run-event-{token}-000", run_id, 0, RunStatus.RUNNING, now))
        store.add_scope(ExecutionScope(scope_id, run_id, ScopeKind.ROOT, "bounded real action trajectory", created_at=now))
        store.add_artifact(
            ArtifactReference(
                root_artifact_id,
                run_id,
                scope_id,
                ArtifactKind.DCP,
                "private-input:" + root_spec["pseudonym"],
                root_spec["sha256"],
                root_spec["size_bytes"],
                now,
                {"role": "immutable_private_root", "pseudonym": root_spec["pseudonym"]},
            )
        )
        store.add_state(DesignState(root_state_id, run_id, scope_id, root_artifact_id, created_at=now, metadata={"stage": "ROUTED"}))
        store.add_validation(ValidationRecord(f"validation-root-{token}", run_id, scope_id, root_state_id, ValidationLevel.CERTIFICATION, ValidationStatus.PASSED, checked_at=now, details={"source": "frozen E1 root" if plan["schema_version"] in {E1_REAL_ACTION_PLAN_SCHEMA, E1_REAL_GATED_PLAN_SCHEMA, E1V2_REAL_GATED_PLAN_SCHEMA} else "frozen E0 root"}))
        store.add_eligibility(StateEligibility(f"eligibility-root-{token}", run_id, root_state_id, True, True, "frozen baseline", now))
        store.add_selection(ResultSelection(f"selection-root-{token}", run_id, scope_id, root_state_id, root_artifact_id, SelectionKind.BASELINE, "immutable root baseline", now))
        if plan["schema_version"] in {
            E0_REAL_GATED_PLAN_SCHEMA,
            E1_REAL_GATED_PLAN_SCHEMA,
            E1V2_REAL_GATED_PLAN_SCHEMA,
        }:
            action_name = "e0.gated_plan"
            action_parameters = {
                "decision_id": action_spec["decision_id"],
                "proposal_hash": action_spec["proposal_hash"],
                "steps": list(action_spec["steps"]),
                "expanded_plan": dict(expanded),
            }
        else:
            action_name = action_spec["action_key"]
            action_parameters = dict(action_spec["parameters"]) | {
                "expanded_action": dict(expanded)
            }
        store.add_action(
            ActionSpecification(
                action_id,
                run_id,
                scope_id,
                action_name,
                action_parameters,
                now,
            )
        )
        store.add_attempt(Attempt(attempt_id, run_id, scope_id, root_state_id, action_id, scheduled_at=now))
        store.add_attempt_event(AttemptLifecycleEvent(f"attempt-event-{token}-000", attempt_id, 0, AttemptEventKind.SCHEDULED, now))
        store.add_attempt_event(AttemptLifecycleEvent(f"attempt-event-{token}-001", attempt_id, 1, AttemptEventKind.STARTED, now))
    return store


def _record_early_failure(
    store: SQLiteRunStore,
    endpoint: str,
    reasons: list[str],
    *,
    attempt_id: str = _ATTEMPT_ID,
) -> None:
    if endpoint == "TECHNICAL_MUTATION_VALID_NONSCIENTIFIC" or store.outcomes():
        return
    events = store.attempt_events(attempt_id=attempt_id)
    if any(event.detail.to_dict().get("transaction_version") for event in events):
        return
    now = datetime.now(timezone.utc)
    with store.transaction():
        token = hashlib.sha256(attempt_id.encode()).hexdigest()[:16]
        store.add_outcome(ExecutionOutcome(f"outcome-failure-{token}", attempt_id, OutcomeStatus.FAILED, events[1].occurred_at, now, error_type=endpoint, error_message=";".join(reasons), metrics={"endpoint": endpoint}))
        store.add_attempt_event(AttemptLifecycleEvent(f"attempt-event-terminal-{token}", attempt_id, len(events), AttemptEventKind.FAILED, now, {"endpoint": endpoint}))


__all__ = [
    "E0_DISPATCH_AUTHORIZATION_SCHEMA",
    "E0_REAL_ACTION_PLAN_SCHEMA",
    "E0_REAL_GATED_PLAN_SCHEMA",
    "E1_REAL_ACTION_PLAN_SCHEMA",
    "E1_REAL_GATED_PLAN_SCHEMA",
    "E1V2_REAL_GATED_PLAN_SCHEMA",
    "EXPECTED_ROOT_PSEUDONYM",
    "EXPECTED_ROOT_SHA256",
    "EXPECTED_ROOT_SIZE",
    "REAL_MUTATION_PLAN_SCHEMA",
    "RealMutationPlanError",
    "build_e0_dispatch_authorization",
    "execute_e0_real_action",
    "execute_e0_real_gated_plan",
    "execute_real_mutation",
    "freeze_e0_real_action_plan",
    "freeze_e0_real_gated_plan",
    "freeze_real_mutation_plan",
    "load_e0_real_action_plan",
    "load_e0_real_gated_plan",
    "load_real_mutation_plan",
    "validate_e0_dispatch_authorization",
    "validate_e0_real_action_plan",
    "validate_e0_real_gated_plan",
    "validate_real_mutation_plan",
    "write_e0_frozen_plan",
    "write_e0_gated_plan",
    "write_frozen_plan",
]
