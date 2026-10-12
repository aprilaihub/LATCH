"""Configuration-driven, mock-only common DoF experiment runner.

This is the sole WP-002 end-to-end path.  It is intentionally unable to load a
real backend, an external model provider, a contest profile, or the L4
reference implementation.  Every L1--L3 dispatchable step must be produced by
``ExperimentActionGate`` before the mock dispatcher accepts it.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone
import hashlib
import os
from pathlib import Path
import platform
from typing import Any

from optimizer_pipeline.core.domain import (
    ActionSpecification,
    ArtifactKind,
    ArtifactReference,
    Attempt,
    AttemptEventKind,
    AttemptLifecycleEvent,
    BudgetEvent,
    BudgetEventKind,
    DesignState,
    ExecutionOutcome,
    ExecutionScope,
    OutcomeStatus,
    ParentUseMode,
    Proposal,
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

from .catalogues import (
    CATALOGUE_SCHEMA_VERSION,
    L4_REFERENCE_CATALOGUE_HASH,
    L4_REFERENCE_CATALOGUE_VERSION,
    NO_ACTION_CATALOGUE_HASH,
    NO_ACTION_CATALOGUE_VERSION,
    ActionCatalogueManifest,
    clean_macro_catalogue,
    strict_l3_catalogue,
)
from .contracts import DesignStage, DoFLevel, EndpointCode, EventClass
from .evidence import (
    ImmutableEvidenceWriter,
    ImmutableSidecar,
    register_evidence_and_finalize,
    verify_registered_evidence,
)
from .gate import ACTION_PROPOSAL_SCHEMA_VERSION, ExperimentActionGate, ProposalKind
from .manifests import (
    action_interface_schema,
    capability_manifest,
    context_interface_schema,
    interface_fingerprints,
    prompt_text,
)
from .masks import MASK_MANIFEST_SCHEMA_VERSION
from .projector import DecisionContextProjector
from .provenance import (
    REPOSITORY_PROVENANCE_SCHEMA_VERSION,
    RepositoryObservation,
    RepositoryProvenanceError,
    enforce_repository_constraint,
    observe_repository,
    tracked_repository_observations_match,
)
from .runtime import (
    BudgetExceeded,
    DeterministicMockClock,
    EventBudgetLedger,
    MockDispatchResult,
    MockGatedDispatcher,
    TraceEventRecorder,
)
from .spec import ExperimentSpec, load_experiment_spec


RUNNER_VERSION = "dof.mock-runner.v3"
EXPERIMENT_MANIFEST_SCHEMA = "dof.experiment-manifest.v2"
PROMPT_MODEL_MANIFEST_SCHEMA = "dof.prompt-model-manifest.v1"
TOOL_ENVIRONMENT_MANIFEST_SCHEMA = "dof.tool-environment-manifest.v2"
RESULT_SUMMARY_SCHEMA = "dof.result-summary.v2"
DECISION_EVENT_SCHEMA = "dof.decision-event.v1"
_ROOT_SCOPE_ID = "scope-root"
_ROOT_STATE_ID = "state-root"
_ROOT_ARTIFACT_ID = "artifact-root"
_DETERMINISTIC_ANCHOR = datetime(2026, 1, 1, tzinfo=timezone.utc)
_UNBOUNDED_GATE_REMAINING = 2**31 - 1


def run_mock_experiment(spec_path: Path, run_dir: Path) -> dict[str, Any]:
    """Execute one strict spec and return its verified result summary.

    Controlled policy/capability/mask/budget rejections and L4 reference
    pending are finalized evidence outcomes.  Invalid specs, root fingerprints,
    or evidence corruption raise before a misleading terminal claim is made.
    """

    spec_source = Path(spec_path).resolve()
    spec = load_experiment_spec(spec_source)
    repository_observation = observe_repository(Path(__file__))
    enforce_repository_constraint(
        spec.provenance.repository_constraint.to_dict(),
        repository_observation,
    )
    root_source = spec.resolve_root(spec_source)
    root_bytes = _verified_root_bytes(root_source, spec.root_dcp.sha256)
    output_root = _prepare_run_dir(run_dir)

    catalogue, catalogue_payload = _catalogue_for(spec.level)
    capability = capability_manifest(spec.level)
    _verify_spec_bindings(spec, capability.sha256)

    clock = DeterministicMockClock(anchor=_DETERMINISTIC_ANCHOR)
    effective_pricing_policy = _effective_pricing_policy(
        spec.budget.unknown_pricing_policy
    )
    ledger = EventBudgetLedger(
        spec.budget.limits_by_event_class(),
        wall_time_seconds=spec.budget.whole_run_wall_time_seconds,
        max_usd=spec.budget.max_usd,
        unknown_pricing_policy=effective_pricing_policy,
        clock=clock,
    )

    writer = ImmutableEvidenceWriter(output_root, spec.run_id, spec.spec_hash)
    manifest_sidecars = _write_static_sidecars(
        writer=writer,
        spec=spec,
        capability_payload=capability.to_dict() | {"sha256": capability.sha256},
        catalogue_payload=catalogue_payload,
        root_size=len(root_bytes),
        effective_pricing_policy=effective_pricing_policy,
        repository_observation=repository_observation,
    )
    repository_sidecar = next(
        sidecar
        for sidecar in manifest_sidecars
        if sidecar.role == "repository_provenance"
    )
    repository_binding = _sidecar_binding(repository_sidecar)

    root_artifact_path = output_root / "artifacts" / "root.mockdcp"
    _write_once(root_artifact_path, root_bytes)
    store = _initialize_store(
        output_root,
        spec,
        root_artifact_path,
        root_bytes,
        manifest_sidecars,
        clock,
    )

    trace_handle = writer.open_trace(
        "evidence/decision_events.jsonl",
        schema_version=DECISION_EVENT_SCHEMA,
    )
    recorder = TraceEventRecorder(trace_handle, ledger)
    endpoint = EndpointCode.L4_REFERENCE_PENDING
    reasons: list[str] = []
    decision_context_hash: str | None = None
    proposal_hash: str | None = None
    accepted_steps = 0
    selected_state_id = _ROOT_STATE_ID
    selected_artifact_id = _ROOT_ARTIFACT_ID
    final_stage = DesignStage.ROUTED
    parent_state_id = _ROOT_STATE_ID
    parent_bytes = root_bytes
    parent_metrics = _root_metrics(spec)
    attempt_ids: list[str] = []

    try:
        trace_handle.append(
            {
                "record_type": "RUN_START",
                "runner_version": RUNNER_VERSION,
                "level": spec.level.value,
                "unknown_regime": spec.unknown_regime.value,
                "schema_version": CURRENT_SCHEMA_VERSION,
                "catalogue_hash": spec.catalogue.catalogue_hash,
                "capability_hash": spec.catalogue.capability_hash,
                "mask_hash": spec.mask.hash,
            }
        )

        initial_budget_reason = _initial_budget_preflight(spec, ledger)
        if initial_budget_reason is None and spec.level is not DoFLevel.L4:
            recorder.consume_event(
                EventClass.READ_ONLY,
                "mock.measure_root",
                detail={"source": "current_root_state", "real_tool": False},
            )
            recorder.consume_event(
                EventClass.VALIDATION,
                "mock.validate_root",
                detail={"validation_id": spec.validation.validation_id},
            )
            store.add_validation(
                ValidationRecord(
                    "validation-root-mock",
                    spec.run_id,
                    _ROOT_SCOPE_ID,
                    _ROOT_STATE_ID,
                    ValidationLevel.CERTIFICATION,
                    ValidationStatus.PASSED,
                    checked_at=clock.now(),
                    details={
                        "validator": "mock",
                        "validation_id": spec.validation.validation_id,
                        "scoring_id": spec.validation.scoring_id,
                    },
                )
            )

        if initial_budget_reason is not None:
            endpoint = EndpointCode.BUDGET_REJECTION
            reasons = [initial_budget_reason]
            recorder.append_decision(
                decision_id="decision-platform-preflight",
                actor="platform",
                endpoint=endpoint.value,
                reasons=reasons,
                gated_steps=[],
                fallback_used=False,
            )
        elif spec.level is DoFLevel.L0:
            endpoint = EndpointCode.VALID_COMPLETION
            reasons = ["l0_exact_root_baseline"]
            recorder.append_decision(
                decision_id="decision-baseline",
                actor="none",
                endpoint=endpoint.value,
                reasons=reasons,
                selected_state_id=_ROOT_STATE_ID,
                fallback_used=False,
            )
        elif spec.level is DoFLevel.L4:
            endpoint = EndpointCode.L4_REFERENCE_PENDING
            reasons = ["l4_reference_pending", "no_execution_surface"]
            recorder.append_decision(
                decision_id="decision-reference-pending",
                actor="unavailable",
                endpoint=endpoint.value,
                reasons=reasons,
                gated_steps=[],
                fallback_used=False,
            )
        else:
            assert catalogue is not None
            decision_id = f"decision-{spec.spec_hash[:16]}"
            projected = None
            if spec.level in {DoFLevel.L2, DoFLevel.L3}:
                source = spec.mock_observation.to_projector_source()
                source["budget_remaining"] = _projector_remaining(ledger)
                projected = DecisionContextProjector().project(
                    level=spec.level,
                    regime=spec.unknown_regime,
                    source=source,
                    catalogue=catalogue,
                    mask=spec.mask,
                )
                decision_context_hash = projected.context_hash
                trace_handle.append(
                    {
                        "record_type": "MODEL_CONTEXT",
                        "decision_id": decision_id,
                        "context_schema_version": projected.context.get(
                            "schema_version"
                        ),
                        "context_hash": projected.context_hash,
                        "serialized_context": projected.serialized,
                        "action_interface": action_interface_schema(spec.level),
                        "prompt_hash": spec.interface.prompt_hash,
                    }
                )
                recorder.consume_event(
                    EventClass.LLM_REQUEST,
                    "mock.policy_request",
                    detail={
                        "provider": spec.model.provider,
                        "model_id": spec.model.model_id,
                        "mock": True,
                    },
                    cost_usd=0.0,
                )

            policy_payload = _policy_envelope(spec, decision_id)
            gate = ExperimentActionGate().evaluate(
                policy_payload,
                level=spec.level,
                expected_decision_id=decision_id,
                catalogue=catalogue,
                expected_catalogue_hash=spec.catalogue.catalogue_hash,
                current_stage=DesignStage.ROUTED,
                budget_remaining=_gate_remaining(ledger),
                mask=spec.mask,
                infrastructure_events_per_step={
                    EventClass.OUTER_ATTEMPT: 1,
                    EventClass.VALIDATION: 1,
                },
            )
            proposal_hash = gate.proposal.proposal_hash if gate.proposal else None
            dispatch_budget_reason = (
                _dispatch_budget_preflight(gate.gated_steps, ledger)
                if gate.accepted and gate.gated_steps
                else None
            )
            if not gate.accepted:
                dispatch_preflight_evidence = {
                    "status": "NOT_RUN",
                    "reason": "action_gate_rejected",
                }
            elif not gate.gated_steps:
                dispatch_preflight_evidence = {
                    "status": "NOT_APPLICABLE",
                    "reason": "proposal_has_no_dispatch_steps",
                }
            elif dispatch_budget_reason is not None:
                dispatch_preflight_evidence = {
                    "status": "REJECTED",
                    "reason": dispatch_budget_reason,
                }
            else:
                dispatch_preflight_evidence = {
                    "status": "ACCEPTED",
                    "reason": None,
                }
            recorder.append_decision(
                decision_id=decision_id,
                actor=spec.policy.mode,
                policy_config_hash=spec.policy.config_hash,
                raw_policy_output=_plain_json(spec.policy.mock_response),
                compiled_policy_output=policy_payload,
                projected_context_hash=(
                    projected.context_hash if projected is not None else None
                ),
                gate=gate.to_dict(),
                dispatch_budget_preflight=dispatch_preflight_evidence,
                fallback_used=False,
            )
            if dispatch_budget_reason is not None:
                endpoint = EndpointCode.BUDGET_REJECTION
                reasons = [dispatch_budget_reason]
            elif not gate.accepted:
                endpoint = gate.endpoint or EndpointCode.CAPABILITY_ACTION_REJECTION
                reasons = list(gate.reasons)
            elif gate.proposal is not None and gate.proposal.kind is ProposalKind.STOP:
                endpoint = EndpointCode.NO_IMPROVEMENT
                reasons = ["policy_stop"]
            else:
                dispatch = MockGatedDispatcher()
                last_result: MockDispatchResult | None = None
                for step in gate.gated_steps:
                    attempt_id = f"attempt-{step.step_index:03d}"
                    attempt_ids.append(attempt_id)
                    _record_attempt_start(
                        store,
                        spec,
                        step,
                        parent_state_id,
                        attempt_id,
                        clock,
                    )
                    started_at = clock.now()
                    for event in step.events:
                        if event.event_class is EventClass.VALIDATION:
                            continue
                        recorder.consume_event(
                            event.event_class,
                            event.operation,
                            units=event.units,
                            outer_attempt_id=attempt_id,
                            detail={
                                "action_key": step.action_key,
                                "step_id": step.step_id,
                                "declared_by_gate": True,
                            },
                        )
                    result = dispatch.dispatch(
                        step,
                        parent_bytes=parent_bytes,
                        parent_metrics=parent_metrics,
                    )
                    for event in step.events:
                        if event.event_class is not EventClass.VALIDATION:
                            continue
                        recorder.consume_event(
                            event.event_class,
                            event.operation,
                            units=event.units,
                            outer_attempt_id=attempt_id,
                            detail={
                                "action_key": step.action_key,
                                "validation_status": result.validation_status,
                                "declared_by_gate": True,
                            },
                        )
                    finished_at = clock.now()
                    produced = _record_attempt_finish(
                        store,
                        output_root,
                        spec,
                        step,
                        attempt_id,
                        parent_state_id,
                        result,
                        started_at,
                        finished_at,
                        clock,
                    )
                    accepted_steps += 1
                    last_result = result
                    trace_handle.append(
                        {
                            "record_type": "ATTEMPT_ENDPOINT",
                            "outer_attempt_id": attempt_id,
                            "step_id": step.step_id,
                            "action_key": step.action_key,
                            "endpoint": result.endpoint.value,
                            "valid": result.valid,
                            "improved": result.improved,
                            "validation_status": result.validation_status,
                            "output_state_id": produced[0],
                            "output_artifact_id": produced[1],
                        }
                    )
                    if produced[0] is not None and result.output_bytes is not None:
                        parent_state_id = produced[0]
                        parent_bytes = result.output_bytes
                        parent_metrics = dict(result.metrics)
                        final_stage = result.stage_after or final_stage
                    if produced[2]:
                        selected_state_id = produced[0] or selected_state_id
                        selected_artifact_id = produced[1] or selected_artifact_id
                    if result.endpoint is not EndpointCode.VALID_COMPLETION:
                        endpoint = result.endpoint
                        reasons = [result.error_message or "mock_dispatch_failure"]
                        break

                if not reasons:
                    if last_result is None:
                        endpoint = EndpointCode.NO_IMPROVEMENT
                        reasons = ["accepted_proposal_had_no_steps"]
                    elif not last_result.valid:
                        endpoint = EndpointCode.INVALID_ARTIFACT
                        reasons = ["final_plan_state_not_routed_or_valid"]
                    elif selected_state_id == _ROOT_STATE_ID:
                        endpoint = EndpointCode.NO_IMPROVEMENT
                        reasons = ["no_valid_improving_successor"]
                    else:
                        endpoint = EndpointCode.VALID_COMPLETION
                        reasons = ["valid_improving_successor_selected"]

        trace_handle.append(
            {
                "record_type": "RUN_ENDPOINT",
                "endpoint": endpoint.value,
                "reasons": reasons,
                "selected_state_id": selected_state_id,
                "accepted_steps": accepted_steps,
                "fallback_used": False,
                "budget": ledger.summary(),
            }
        )
        trace_sidecar = trace_handle.close()

        final_repository_observation = observe_repository(Path(__file__))
        enforce_repository_constraint(
            spec.provenance.repository_constraint.to_dict(),
            final_repository_observation,
        )
        if not tracked_repository_observations_match(
            repository_observation,
            final_repository_observation,
        ):
            raise RepositoryProvenanceError("repository_changed_during_run")

        pre_registration_integrity = list(store.integrity_check())
        if pre_registration_integrity:
            raise RuntimeError(
                "canonical store integrity failed: "
                + "; ".join(pre_registration_integrity)
            )
        summary = {
            "schema_version": RESULT_SUMMARY_SCHEMA,
            "runner_version": RUNNER_VERSION,
            "run_id": spec.run_id,
            "spec_hash": spec.spec_hash,
            "experiment_id": spec.experiment_id,
            "condition_id": spec.condition_id,
            "repeat_id": spec.repeat_id,
            "level": spec.level.value,
            "level_status": spec.status.value,
            "unknown_regime": spec.unknown_regime.value,
            "endpoint": endpoint.value,
            "endpoint_success": endpoint is EndpointCode.VALID_COMPLETION,
            "reasons": reasons,
            "selected_state_id": selected_state_id,
            "selected_artifact_id": selected_artifact_id,
            "final_stage": final_stage.value,
            "decision_context_hash": decision_context_hash,
            "proposal_hash": proposal_hash,
            "accepted_steps": accepted_steps,
            "outer_attempt_ids": attempt_ids,
            "budget": ledger.summary(),
            "canonical_schema_version": store.schema_version,
            "canonical_integrity_errors": pre_registration_integrity,
            "fallback_used": False,
            "real_tools_started": False,
            "external_model_requests": 0,
            "repository_provenance": repository_binding,
        }
        result_sidecar = writer.write_json(
            "evidence/result_summary.json",
            summary,
            role="result_summary",
            schema_version=RESULT_SUMMARY_SCHEMA,
            kind=ArtifactKind.REPORT,
        )
        sidecars = [*manifest_sidecars, trace_sidecar, result_sidecar]
        registration = register_evidence_and_finalize(
            store,
            run_dir=output_root,
            sidecars=sidecars,
            run_id=spec.run_id,
            spec_hash=spec.spec_hash,
            terminal_status=RunStatus.FINALIZED,
            terminal_detail={
                "endpoint": endpoint.value,
                "selected_state_id": selected_state_id,
                "fallback_used": False,
            },
        )
        verification = verify_registered_evidence(
            store,
            run_dir=output_root,
            expected_run_id=spec.run_id,
            expected_spec_hash=spec.spec_hash,
        )
        verification.raise_for_errors()
        final_integrity = list(store.integrity_check())
        if final_integrity:
            raise RuntimeError(
                "canonical store integrity failed after registration: "
                + "; ".join(final_integrity)
            )
        return summary | {
            "evidence_set_hash": registration.evidence_set_hash,
            "evidence_verification": "PASS",
            "run_dir": str(output_root),
        }
    finally:
        if not trace_handle.closed:
            trace_handle.close()
        store.close()


def _prepare_run_dir(run_dir: Path) -> Path:
    path = Path(run_dir).resolve()
    if path.exists() and any(path.iterdir()):
        raise FileExistsError(f"run directory must be new or empty: {path}")
    path.mkdir(parents=True, exist_ok=True)
    return path


def _verified_root_bytes(path: Path, expected_sha256: str) -> bytes:
    if not path.is_file():
        raise FileNotFoundError(f"mock root artifact is missing: {path}")
    data = path.read_bytes()
    actual = hashlib.sha256(data).hexdigest()
    if actual != expected_sha256:
        raise ValueError(
            f"root artifact sha256 mismatch: expected {expected_sha256}, got {actual}"
        )
    return data


def _catalogue_for(
    level: DoFLevel,
) -> tuple[ActionCatalogueManifest | None, dict[str, Any]]:
    if level in {DoFLevel.L1, DoFLevel.L2}:
        catalogue = clean_macro_catalogue(level)
        return catalogue, catalogue.to_dict() | {"sha256": catalogue.sha256}
    if level is DoFLevel.L3:
        catalogue = strict_l3_catalogue()
        return catalogue, catalogue.to_dict() | {"sha256": catalogue.sha256}
    if level is DoFLevel.L0:
        payload = {
            "schema_version": CATALOGUE_SCHEMA_VERSION,
            "version": NO_ACTION_CATALOGUE_VERSION,
            "level_contract": DoFLevel.L0.value,
            "actions": [],
        }
        return None, payload | {"sha256": NO_ACTION_CATALOGUE_HASH}
    payload = {
        "schema_version": CATALOGUE_SCHEMA_VERSION,
        "version": L4_REFERENCE_CATALOGUE_VERSION,
        "level_contract": "REFERENCE_PENDING",
        "actions": [],
    }
    return None, payload | {"sha256": L4_REFERENCE_CATALOGUE_HASH}


def _verify_spec_bindings(spec: ExperimentSpec, capability_hash: str) -> None:
    reasons: list[str] = []
    capability = capability_manifest(spec.level)
    if spec.catalogue.capability_version != capability.version:
        reasons.append("capability_version_mismatch")
    if spec.catalogue.capability_hash != capability_hash:
        reasons.append("capability_hash_mismatch")
    expected_interface = interface_fingerprints(spec.level)
    supplied_interface = spec.interface.to_dict()
    for key, expected in expected_interface.items():
        if supplied_interface.get(key) != expected:
            reasons.append(f"interface_binding_mismatch:{key}")
    if reasons:
        raise ValueError("; ".join(reasons))


def _effective_pricing_policy(declared: str) -> str:
    if declared in {"reject", "stop_before_unpriced"}:
        return "reject"
    if declared in {"allow_not_run", "allow_unpriced", "not_applicable"}:
        return "allow_not_run"
    raise ValueError(f"unsupported unknown pricing policy: {declared}")


def _write_static_sidecars(
    *,
    writer: ImmutableEvidenceWriter,
    spec: ExperimentSpec,
    capability_payload: Mapping[str, Any],
    catalogue_payload: Mapping[str, Any],
    root_size: int,
    effective_pricing_policy: str,
    repository_observation: RepositoryObservation,
) -> list[ImmutableSidecar]:
    repository_sidecar = writer.write_json(
        "evidence/repository_provenance.json",
        repository_observation.to_dict(),
        role="repository_provenance",
        schema_version=REPOSITORY_PROVENANCE_SCHEMA_VERSION,
    )
    repository_binding = _sidecar_binding(repository_sidecar)
    experiment_payload = {
        "schema_version": EXPERIMENT_MANIFEST_SCHEMA,
        "runner_version": RUNNER_VERSION,
        "spec": spec.to_dict(),
        "root_verification": {
            "internal_ref": spec.root_dcp.internal_ref,
            "sha256": spec.root_dcp.sha256,
            "size_bytes": root_size,
            "model_visible_pseudonym": spec.root_dcp.model_pseudonym,
            "verified": True,
        },
        "canonical_store_schema_version": CURRENT_SCHEMA_VERSION,
        "real_execution": False,
        "repository_provenance": repository_binding,
    }
    prompt_payload = {
        "schema_version": PROMPT_MODEL_MANIFEST_SCHEMA,
        "level": spec.level.value,
        "model": spec.model.to_dict(),
        "policy": spec.policy.to_dict(),
        "interface_binding": spec.interface.to_dict(),
        "prompt": {
            "status": (
                "ACTIVE"
                if spec.level in {DoFLevel.L2, DoFLevel.L3}
                else "NOT_RUN"
            ),
            "content": prompt_text(spec.level),
            "content_hash": spec.interface.prompt_hash,
        },
        "action_interface_schema": (
            action_interface_schema(spec.level)
            if spec.level in {DoFLevel.L2, DoFLevel.L3}
            else None
        ),
        "context_interface_schema": (
            context_interface_schema()
            if spec.level in {DoFLevel.L2, DoFLevel.L3}
            else None
        ),
        "external_model_requests": 0,
    }
    environment_payload = {
        "schema_version": TOOL_ENVIRONMENT_MANIFEST_SCHEMA,
        "execution_mode": "mock",
        "runner_version": RUNNER_VERSION,
        "repository_constraint": spec.provenance.repository_constraint.to_dict(),
        "repository_provenance": repository_binding,
        "declared_environment_manifest_version": (
            spec.provenance.environment_manifest_version
        ),
        "declared_environment_manifest_hash": spec.provenance.environment_manifest_hash,
        "declared_tool_versions": [
            item.to_dict() for item in spec.provenance.tool_versions
        ],
        "observed_mock_runtime": {
            "python": platform.python_version(),
            "implementation": platform.python_implementation(),
            "platform_system": platform.system(),
            "platform_machine": platform.machine(),
        },
        "vivado": "NOT_RUN",
        "rapidwright": "NOT_RUN",
        "mcp": "NOT_RUN",
        "external_model_api": "NOT_RUN",
        "declared_unknown_pricing_policy": spec.budget.unknown_pricing_policy,
        "effective_mock_pricing_policy": effective_pricing_policy,
    }
    definitions = (
        (
            "evidence/experiment_manifest.json",
            experiment_payload,
            "experiment_manifest",
            EXPERIMENT_MANIFEST_SCHEMA,
        ),
        (
            "evidence/capability_manifest.json",
            dict(capability_payload),
            "capability_manifest",
            str(capability_payload["schema_version"]),
        ),
        (
            "evidence/catalogue_manifest.json",
            dict(catalogue_payload),
            "catalogue_manifest",
            str(catalogue_payload["schema_version"]),
        ),
        (
            "evidence/mask_manifest.json",
            spec.mask.to_manifest_dict(),
            "mask_manifest",
            MASK_MANIFEST_SCHEMA_VERSION,
        ),
        (
            "evidence/prompt_model_manifest.json",
            prompt_payload,
            "prompt_model_manifest",
            PROMPT_MODEL_MANIFEST_SCHEMA,
        ),
        (
            "evidence/tool_environment_manifest.json",
            environment_payload,
            "tool_environment_manifest",
            TOOL_ENVIRONMENT_MANIFEST_SCHEMA,
        ),
    )
    return [repository_sidecar, *[
        writer.write_json(uri, payload, role=role, schema_version=schema)
        for uri, payload, role, schema in definitions
    ]]


def _sidecar_binding(sidecar: ImmutableSidecar) -> dict[str, str]:
    return {
        "artifact_id": sidecar.artifact_id,
        "schema_version": sidecar.schema_version,
        "sha256": sidecar.sha256,
    }


def _initialize_store(
    run_dir: Path,
    spec: ExperimentSpec,
    root_path: Path,
    root_bytes: bytes,
    sidecars: list[ImmutableSidecar],
    clock: DeterministicMockClock,
) -> SQLiteRunStore:
    metadata = {
        "artifact_contract": "run.db.v2+immutable-dof-sidecars",
        "runner_version": RUNNER_VERSION,
        "spec_hash": spec.spec_hash,
        "level": spec.level.value,
        "unknown_regime": spec.unknown_regime.value,
        "declared_manifest_hashes": {
            sidecar.role: sidecar.sha256 for sidecar in sidecars
        },
    }
    store = SQLiteRunStore.create(
        run_dir / "run.db",
        Run(spec.run_id, "dof_mock", clock.now(), metadata),
    )
    scope = ExecutionScope(
        _ROOT_SCOPE_ID,
        spec.run_id,
        ScopeKind.ROOT,
        "common DoF mock condition",
        created_at=clock.now(),
        metadata={
            "condition_id": spec.condition_id,
            "repeat_id": spec.repeat_id,
        },
    )
    root_artifact = ArtifactReference(
        _ROOT_ARTIFACT_ID,
        spec.run_id,
        _ROOT_SCOPE_ID,
        ArtifactKind.DCP,
        root_path.relative_to(run_dir).as_posix(),
        sha256=hashlib.sha256(root_bytes).hexdigest(),
        size_bytes=len(root_bytes),
        created_at=clock.now(),
        metadata={"role": "frozen_mock_root", "real_dcp": False},
    )
    root_state = DesignState(
        _ROOT_STATE_ID,
        spec.run_id,
        _ROOT_SCOPE_ID,
        _ROOT_ARTIFACT_ID,
        created_at=clock.now(),
        metadata={
            "stage": DesignStage.ROUTED.value,
            "metrics": _root_metrics(spec),
            "pseudonym": spec.root_dcp.model_pseudonym,
        },
    )
    with store.transaction():
        store.add_run_event(
            RunLifecycleEvent(
                "run-event-000",
                spec.run_id,
                0,
                RunStatus.RUNNING,
                occurred_at=clock.now(),
                detail={"runner_version": RUNNER_VERSION},
            )
        )
        store.add_scope(scope)
        store.add_artifact(root_artifact)
        store.add_state(root_state)
        store.add_validation(
            ValidationRecord(
                "validation-root",
                spec.run_id,
                _ROOT_SCOPE_ID,
                _ROOT_STATE_ID,
                ValidationLevel.CERTIFICATION,
                ValidationStatus.NOT_RUN,
                checked_at=clock.now(),
                details={
                    "validator": "not_run_at_registration",
                    "reference_pending": spec.level is DoFLevel.L4,
                },
            )
        )
        store.add_eligibility(
            StateEligibility(
                "eligibility-root",
                spec.run_id,
                _ROOT_STATE_ID,
                True,
                True,
                "frozen baseline is always the safe selection",
                recorded_at=clock.now(),
            )
        )
        store.add_selection(
            ResultSelection(
                "selection-baseline",
                spec.run_id,
                _ROOT_SCOPE_ID,
                _ROOT_STATE_ID,
                _ROOT_ARTIFACT_ID,
                SelectionKind.BASELINE,
                "safe frozen mock baseline",
                selected_at=clock.now(),
            )
        )
    return store


def _root_metrics(spec: ExperimentSpec) -> dict[str, Any]:
    source = spec.mock_observation.to_projector_source()
    timing = source["state"]["measurements"].get("timing", {})
    return {
        "estimated_fmax_mhz": float(timing.get("estimated_fmax_mhz", 0.0) or 0.0),
        "wns_ns": float(timing.get("wns_ns", 0.0) or 0.0),
        "mock": True,
    }


def _projector_remaining(ledger: EventBudgetLedger) -> dict[str, Any]:
    return {
        key: value
        for key, value in ledger.remaining().items()
        if value is not None
    }


def _initial_budget_preflight(
    spec: ExperimentSpec,
    ledger: EventBudgetLedger,
) -> str | None:
    if spec.level is DoFLevel.L4:
        return None
    events: list[tuple[EventClass, int, float | None]] = [
        (EventClass.READ_ONLY, 1, 0.0),
        (EventClass.VALIDATION, 1, 0.0),
    ]
    if spec.level in {DoFLevel.L2, DoFLevel.L3}:
        events.append((EventClass.LLM_REQUEST, 1, 0.0))
    try:
        ledger.preflight_batch(events)
    except BudgetExceeded as exc:
        return f"platform_budget_preflight:{exc.reason}"
    return None


def _dispatch_budget_preflight(steps: Any, ledger: EventBudgetLedger) -> str | None:
    events = [
        (event.event_class, event.units, 0.0)
        for step in steps
        for event in step.events
    ]
    if not events:
        return None
    try:
        ledger.preflight_batch(events)
    except BudgetExceeded as exc:
        return f"dispatch_budget_preflight:{exc.reason}"
    return None


def _gate_remaining(ledger: EventBudgetLedger) -> dict[EventClass, int]:
    remaining = ledger.remaining()
    return {
        event_class: (
            _UNBOUNDED_GATE_REMAINING
            if remaining[event_class.value] is None
            else int(remaining[event_class.value])
        )
        for event_class in EventClass
    }


def _policy_envelope(spec: ExperimentSpec, decision_id: str) -> dict[str, Any]:
    raw = _plain_json(spec.policy.mock_response)
    if "raw_text" in raw:
        return raw
    steps = [
        {
            "step_id": f"step-{index:03d}",
            "action_key": item["action_key"],
            "parameters": dict(item["parameters"]),
        }
        for index, item in enumerate(raw.get("steps", []))
    ]
    return {
        "schema_version": ACTION_PROPOSAL_SCHEMA_VERSION,
        "proposal_id": f"policy-proposal-{spec.spec_hash[:16]}",
        "decision_id": decision_id,
        "catalogue_version": spec.catalogue.catalogue_version,
        "catalogue_hash": spec.catalogue.catalogue_hash,
        "kind": raw.get("kind"),
        "steps": steps,
    }


def _record_attempt_start(
    store: SQLiteRunStore,
    spec: ExperimentSpec,
    step: Any,
    parent_state_id: str,
    attempt_id: str,
    clock: DeterministicMockClock,
) -> None:
    suffix = f"{step.step_index:03d}"
    action_id = f"action-{suffix}"
    canonical_proposal_id = f"proposal-{suffix}"
    with store.transaction():
        store.add_action(
            ActionSpecification(
                action_id,
                spec.run_id,
                _ROOT_SCOPE_ID,
                step.action_key,
                {
                    "parameters": _plain_json(step.parameters),
                    "catalogue_hash": step.catalogue_hash,
                    "proposal_hash": step.proposal_hash,
                    "declared_events": [event.to_dict() for event in step.events],
                },
                created_at=clock.now(),
            )
        )
        store.add_proposal(
            Proposal(
                canonical_proposal_id,
                spec.run_id,
                _ROOT_SCOPE_ID,
                parent_state_id,
                action_id,
                spec.policy.mode,
                proposed_at=clock.now(),
                policy_metadata={
                    "policy_proposal_id": step.proposal_id,
                    "proposal_hash": step.proposal_hash,
                    "step_id": step.step_id,
                    "step_index": step.step_index,
                    "policy_config_hash": spec.policy.config_hash,
                },
            )
        )
        store.add_attempt(
            Attempt(
                attempt_id,
                spec.run_id,
                _ROOT_SCOPE_ID,
                parent_state_id,
                action_id,
                canonical_proposal_id,
                ParentUseMode.NORMAL,
                scheduled_at=clock.now(),
            )
        )
        store.add_attempt_event(
            AttemptLifecycleEvent(
                f"{attempt_id}:event:000",
                attempt_id,
                0,
                AttemptEventKind.SCHEDULED,
                occurred_at=clock.now(),
                detail={"gate_accepted": True},
            )
        )
        store.add_attempt_event(
            AttemptLifecycleEvent(
                f"{attempt_id}:event:001",
                attempt_id,
                1,
                AttemptEventKind.STARTED,
                occurred_at=clock.now(),
                detail={"mock": True},
            )
        )
        store.add_budget_event(
            BudgetEvent(
                f"{attempt_id}:budget:reserved",
                spec.run_id,
                _ROOT_SCOPE_ID,
                attempt_id,
                BudgetEventKind.RESERVED,
                occurred_at=clock.now(),
            )
        )


def _record_attempt_finish(
    store: SQLiteRunStore,
    run_dir: Path,
    spec: ExperimentSpec,
    step: Any,
    attempt_id: str,
    parent_state_id: str,
    result: MockDispatchResult,
    started_at: datetime,
    finished_at: datetime,
    clock: DeterministicMockClock,
) -> tuple[str | None, str | None, bool]:
    suffix = f"{step.step_index:03d}"
    artifact_id: str | None = None
    state_id: str | None = None
    if result.output_bytes is not None:
        artifact_id = f"artifact-{suffix}"
        state_id = f"state-{suffix}"
        relative_uri = f"artifacts/{attempt_id}.mockdcp"
        _write_once(run_dir / relative_uri, result.output_bytes)
    else:
        relative_uri = ""

    succeeded = result.output_bytes is not None and result.endpoint is EndpointCode.VALID_COMPLETION
    outcome_status = _outcome_status(result.endpoint, succeeded)
    validation_status = _validation_status(result)
    select = bool(succeeded and result.valid and result.improved and state_id)
    event_kind = AttemptEventKind.COMPLETED if succeeded else _attempt_event_kind(result.endpoint)
    with store.transaction():
        if artifact_id is not None and state_id is not None and result.output_bytes is not None:
            store.add_artifact(
                ArtifactReference(
                    artifact_id,
                    spec.run_id,
                    _ROOT_SCOPE_ID,
                    ArtifactKind.DCP,
                    relative_uri,
                    sha256=hashlib.sha256(result.output_bytes).hexdigest(),
                    size_bytes=len(result.output_bytes),
                    created_at=clock.now(),
                    metadata={
                        "attempt_id": attempt_id,
                        "mock": True,
                        "stage": (
                            result.stage_after.value if result.stage_after else None
                        ),
                    },
                )
            )
        store.add_outcome(
            ExecutionOutcome(
                f"outcome-{suffix}",
                attempt_id,
                outcome_status,
                started_at,
                finished_at,
                output_artifact_id=artifact_id if succeeded else None,
                error_type=(None if succeeded else result.endpoint.value),
                error_message=result.error_message,
                metrics={
                    **dict(result.metrics),
                    "endpoint": result.endpoint.value,
                    "valid": result.valid,
                    "improved": result.improved,
                    "elapsed_seconds": (
                        finished_at - started_at
                    ).total_seconds(),
                },
            )
        )
        if state_id is not None and artifact_id is not None:
            store.add_state(
                DesignState(
                    state_id,
                    spec.run_id,
                    _ROOT_SCOPE_ID,
                    artifact_id,
                    parent_state_id,
                    attempt_id,
                    created_at=clock.now(),
                    metadata={
                        "stage": (
                            result.stage_after.value if result.stage_after else None
                        ),
                        "metrics": dict(result.metrics),
                        "scratch": not result.valid,
                    },
                )
            )
        subject_id = state_id or attempt_id
        store.add_validation(
            ValidationRecord(
                f"validation-{suffix}",
                spec.run_id,
                _ROOT_SCOPE_ID,
                subject_id,
                ValidationLevel.ATTEMPT,
                validation_status,
                attempt_id,
                checked_at=clock.now(),
                details={
                    "endpoint": result.endpoint.value,
                    "validator": "mock",
                    "valid_successor": result.valid,
                },
            )
        )
        if state_id is not None:
            store.add_eligibility(
                StateEligibility(
                    f"eligibility-{suffix}",
                    spec.run_id,
                    state_id,
                    succeeded,
                    select,
                    (
                        "valid improving routed mock successor"
                        if select
                        else "scratch or non-improving mock successor"
                    ),
                    recorded_at=clock.now(),
                )
            )
        if select and state_id is not None and artifact_id is not None:
            store.add_selection(
                ResultSelection(
                    f"selection-{suffix}",
                    spec.run_id,
                    _ROOT_SCOPE_ID,
                    state_id,
                    artifact_id,
                    SelectionKind.BEST_VALID,
                    "valid improving mock successor",
                    selected_at=clock.now(),
                )
            )
        store.add_attempt_event(
            AttemptLifecycleEvent(
                f"{attempt_id}:event:002",
                attempt_id,
                2,
                event_kind,
                occurred_at=clock.now(),
                detail={"endpoint": result.endpoint.value},
            )
        )
        store.add_budget_event(
            BudgetEvent(
                f"{attempt_id}:budget:done",
                spec.run_id,
                _ROOT_SCOPE_ID,
                attempt_id,
                (
                    BudgetEventKind.COMPLETED
                    if succeeded
                    else BudgetEventKind.FAILED
                ),
                occurred_at=clock.now(),
            )
        )
    return state_id, artifact_id, select


def _outcome_status(endpoint: EndpointCode, succeeded: bool) -> OutcomeStatus:
    if succeeded:
        return OutcomeStatus.SUCCEEDED
    if endpoint is EndpointCode.TIMEOUT:
        return OutcomeStatus.TIMED_OUT
    if endpoint is EndpointCode.CANCELLATION:
        return OutcomeStatus.CANCELLED
    return OutcomeStatus.FAILED


def _attempt_event_kind(endpoint: EndpointCode) -> AttemptEventKind:
    if endpoint is EndpointCode.CANCELLATION:
        return AttemptEventKind.CANCELLED
    return AttemptEventKind.FAILED


def _validation_status(result: MockDispatchResult) -> ValidationStatus:
    if result.endpoint is EndpointCode.INVALID_ARTIFACT:
        return ValidationStatus.INVALID
    if result.endpoint is not EndpointCode.VALID_COMPLETION:
        return ValidationStatus.FAILED
    if result.validation_status == "CLEAN":
        return ValidationStatus.CLEAN
    return ValidationStatus.WARNING


def _write_once(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())


def _plain_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _plain_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_plain_json(item) for item in value]
    if isinstance(value, list):
        return [_plain_json(item) for item in value]
    return value
