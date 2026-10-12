"""Fake-host technical mutation harness for WP-004 Gates 1 and 2.

The harness exercises the production expansion, observed-call, budget,
artifact-transaction, schema-v2, and immutable-evidence boundaries without
starting Vivado or contacting an external provider.  It is deliberately not a
scientific experiment runner.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from typing import Any

from optimizer_pipeline.core.domain import (
    ActionSpecification,
    ArtifactKind,
    ArtifactReference,
    Attempt,
    AttemptEventKind,
    AttemptLifecycleEvent,
    DesignState,
    ExecutionScope,
    ExecutionOutcome,
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
    ArtifactFailurePoint,
    ArtifactTransaction,
    ArtifactTransactionError,
    ArtifactTransactionResult,
    ArtifactValidation,
    RootPreservationGuard,
    verify_artifact_lifecycle,
)
from .catalogues import strict_l3_catalogue
from .contracts import DesignStage, DoFLevel, EventClass
from .evidence import (
    ImmutableEvidenceWriter,
    register_evidence_and_finalize,
    verify_registered_evidence,
)
from .expansions import (
    ExpandedAction,
    expand_real_action,
    real_action_expansion_manifest,
)
from .gate import ACTION_PROPOSAL_SCHEMA_VERSION, ActionGate, GateDecision
from .masks import create_mask_rules
from .provider import STRICT_PROVIDER_PARSER_VERSION, parse_exact_action_proposal
from .toolcalls import (
    ActualCallBudget,
    CallRule,
    CallStatus,
    DeclaredCallPlan,
    InfrastructureEventClass,
    ObservedCallExecutor,
    ToolCallRegistry,
    ToolCallContractError,
    default_tool_call_registry,
)


TECHNICAL_CALL_PLAN_VERSION = "dof.technical-mutation-call-plan.v1"
TECHNICAL_FAKE_RUNNER_VERSION = "dof.technical-fake-mutation.v1"
TECHNICAL_RESULT_SCHEMA = "dof.technical-mutation-result.v1"
TECHNICAL_TRACE_SCHEMA = "dof.technical-backend-call-trace.v1"
TECHNICAL_EXPANSION_EVIDENCE_SCHEMA = "dof.technical-expansion-evidence.v1"
TECHNICAL_PLAN_EVIDENCE_SCHEMA = "dof.technical-call-plan-evidence.v1"
TECHNICAL_COMPOSED_RUNNER_VERSION = "dof.technical-composed-l3-fake.v1"
TECHNICAL_COMPOSED_RESULT_SCHEMA = "dof.technical-composed-l3-result.v1"
TECHNICAL_DECISION_BINDING_SCHEMA = "dof.technical-decision-binding.v1"
TECHNICAL_STEP_TRACE_SCHEMA = "dof.technical-plan-step-trace.v1"

_RUN_ID = "wp004-fake-technical-mutation"
_COMPOSED_RUN_ID = "wp004a-fake-composed-l3"
_SCOPE_ID = "scope-root"
_ROOT_ARTIFACT_ID = "artifact-root"
_ROOT_STATE_ID = "state-root"
_ACTION_ID = "action-technical"
_ATTEMPT_ID = "attempt-technical-001"
_CHILD_STATE_ID = "state-technical-child"
_NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)

_COMPOSED_DECISION_ID = "wp004a-composed-l3-decision"
_COMPOSED_PROPOSAL_ID = "wp004a-composed-l3-plan"
_COMPOSED_STEPS: tuple[tuple[str, str, Mapping[str, Any]], ...] = (
    ("step-unroute", "manual.unroute_all", {}),
    ("step-unplace", "manual.unplace_all", {}),
    ("step-place", "manual.place_design", {"directive": "Explore"}),
    ("step-route", "manual.route_design", {"directive": "Default"}),
    (
        "step-phys-opt",
        "manual.phys_opt_design",
        {"directive": "AddRetime"},
    ),
)


class TechnicalSmokeError(RuntimeError):
    def __init__(self, endpoint: str, reason: str) -> None:
        self.endpoint = str(endpoint)
        self.reason = str(reason)
        super().__init__(self.reason)


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def _spec_hash(value: Any) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def technical_mutation_call_plan(
    expanded: ExpandedAction,
    registry: ToolCallRegistry | None = None,
) -> DeclaredCallPlan:
    """Return the closed common transaction around one expanded action."""

    return _technical_call_plan(
        operation_ids=tuple(item.operation_id for item in expanded.calls),
        initial_stage=expanded.initial_stage,
        final_stage=expanded.final_stage,
        plan_id="technical-mutation:" + _spec_hash(expanded.to_dict())[:24],
        registry=registry,
    )


def technical_gated_plan_call_plan(
    expanded_steps: tuple[ExpandedAction, ...],
    registry: ToolCallRegistry | None = None,
) -> DeclaredCallPlan:
    """Close an already-gated serial action plan inside one transaction.

    Each expansion must start at the stage produced by its predecessor.  The
    caller remains responsible for binding the expansions to the exact gate
    decision; this helper only creates the closed host-call declaration.
    """

    if not expanded_steps:
        raise TechnicalSmokeError(
            "BACKEND_CONTRACT_MISMATCH", "gated plan has no expanded steps"
        )
    for previous, current in zip(expanded_steps, expanded_steps[1:]):
        if previous.final_stage is not current.initial_stage:
            raise TechnicalSmokeError(
                "BACKEND_CONTRACT_MISMATCH",
                "gated plan expansion stages are not contiguous",
            )
    operation_ids = tuple(
        call.operation_id
        for expanded in expanded_steps
        for call in expanded.calls
    )
    plan_hash = _spec_hash([item.to_dict() for item in expanded_steps])
    return _technical_call_plan(
        operation_ids=operation_ids,
        initial_stage=expanded_steps[0].initial_stage,
        final_stage=expanded_steps[-1].final_stage,
        plan_id="e0-gated-plan:" + plan_hash[:24],
        registry=registry,
    )


def _technical_call_plan(
    *,
    operation_ids: tuple[str, ...],
    initial_stage: DesignStage,
    final_stage: DesignStage,
    plan_id: str,
    registry: ToolCallRegistry | None = None,
) -> DeclaredCallPlan:
    """Close one or more admitted mutations inside the common transaction."""

    registry = registry or default_tool_call_registry()
    rules: list[CallRule] = [
        CallRule("validation.hash_root"),
        CallRule("host.session.start"),
        CallRule("vivado.open_checkpoint"),
    ]
    if initial_stage is DesignStage.ROUTED:
        rules.extend(
            (
                CallRule("vivado.report_timing_summary"),
                CallRule("vivado.report_route_status"),
            )
        )
    rules.extend(CallRule(operation_id) for operation_id in operation_ids)
    if final_stage is DesignStage.ROUTED:
        rules.extend(
            (
                CallRule("vivado.report_timing_summary"),
                CallRule("vivado.report_route_status"),
            )
        )
    rules.extend(
        (
            CallRule("vivado.write_checkpoint"),
            CallRule("vivado.close_design"),
            CallRule("vivado.open_checkpoint"),
        )
    )
    if final_stage is DesignStage.ROUTED:
        rules.extend(
            (
                CallRule("vivado.report_timing_summary"),
                CallRule("vivado.report_route_status"),
            )
        )
    rules.extend(
        (
            CallRule("vivado.close_design"),
            CallRule("host.session.close"),
            CallRule("validation.hash_root"),
            CallRule("validation.hash_child"),
            CallRule("validation.validate_child"),
            CallRule("validation.register_child"),
            CallRule("evidence.finalize"),
        )
    )
    plan = DeclaredCallPlan(
        TECHNICAL_CALL_PLAN_VERSION,
        plan_id,
        registry.sha256,
        tuple(rules),
        l0_read_only=False,
    )
    plan.validate_against(registry)
    return plan


def _fixed_composed_l3_decision() -> tuple[bytes, GateDecision]:
    """Strictly parse and gate the fixed non-scientific five-step L3 plan."""

    catalogue = strict_l3_catalogue()
    proposal_payload = {
        "schema_version": ACTION_PROPOSAL_SCHEMA_VERSION,
        "proposal_id": _COMPOSED_PROPOSAL_ID,
        "decision_id": _COMPOSED_DECISION_ID,
        "catalogue_version": catalogue.version,
        "catalogue_hash": catalogue.sha256,
        "kind": "PLAN",
        "steps": [
            {
                "step_id": step_id,
                "action_key": action_key,
                "parameters": dict(parameters),
            }
            for step_id, action_key, parameters in _COMPOSED_STEPS
        ],
    }
    proposal_bytes = _canonical_bytes(proposal_payload)
    parsed = parse_exact_action_proposal(proposal_bytes)
    mask = create_mask_rules(version="dof.wp004a-composed-l3-mask.v1")
    decision = ActionGate().evaluate(
        parsed,
        level=DoFLevel.L3,
        expected_decision_id=_COMPOSED_DECISION_ID,
        catalogue=catalogue,
        expected_catalogue_hash=catalogue.sha256,
        current_stage=DesignStage.ROUTED,
        budget_remaining={event_class: 8 for event_class in EventClass},
        mask=mask,
    )
    if not decision.accepted or len(decision.gated_steps) != len(_COMPOSED_STEPS):
        raise TechnicalSmokeError(
            "CAPABILITY_ACTION_REJECTION",
            "fixed composed L3 proposal did not pass the common action gate:"
            + ";".join(decision.reasons),
        )
    if decision.final_stage is not DesignStage.ROUTED:
        raise TechnicalSmokeError(
            "CAPABILITY_ACTION_REJECTION",
            "fixed composed L3 proposal did not return to ROUTED",
        )
    return proposal_bytes, decision


def _composed_l3_call_plan(
    decision: GateDecision,
) -> DeclaredCallPlan:
    """Declare the closed host stream without eagerly expanding runtime steps."""

    manifest = real_action_expansion_manifest()
    operation_ids: list[str] = []
    for gated_step in decision.gated_steps:
        declared = manifest.action(gated_step.action_key)
        if len(declared.steps) != 1:
            raise TechnicalSmokeError(
                "BACKEND_CONTRACT_MISMATCH",
                f"composed L3 step is not one registered operation:{gated_step.action_key}",
            )
        operation_ids.append(declared.steps[0].operation_id)
    decision_hash = _spec_hash(decision.to_dict())
    return _technical_call_plan(
        operation_ids=tuple(operation_ids),
        initial_stage=DesignStage.ROUTED,
        final_stage=DesignStage.ROUTED,
        plan_id="wp004a-technical-composed-l3:" + decision_hash[:24],
    )


def _event_limits(plan: DeclaredCallPlan) -> dict[EventClass, int]:
    registry = default_tool_call_registry()
    result = {event_class: 0 for event_class in EventClass}
    for rule in plan.rules:
        operation = registry.operation(rule.operation_id)
        if isinstance(operation.event_class, EventClass):
            result[operation.event_class] += rule.max_calls
    return result


def run_fake_technical_mutation(
    run_dir: str | Path,
    *,
    level: DoFLevel,
    action_key: str,
    parameters: Mapping[str, Any],
    initial_stage: DesignStage,
    child_mode: str = "clean",
    fail_operation: str | None = None,
    fail_at: ArtifactFailurePoint | None = None,
    inject_extra_operation: str | None = None,
    mutate_root: bool = False,
    selection_eligible: bool = False,
) -> dict[str, Any]:
    """Execute one deterministic fake technical run with immutable evidence."""

    return _run_fake_technical_mutation(
        run_dir,
        run_id=_RUN_ID,
        runner_version=TECHNICAL_FAKE_RUNNER_VERSION,
        result_schema=TECHNICAL_RESULT_SCHEMA,
        level=level,
        action_key=action_key,
        parameters=parameters,
        initial_stage=initial_stage,
        child_mode=child_mode,
        fail_operation=fail_operation,
        fail_at=fail_at,
        inject_extra_operation=inject_extra_operation,
        mutate_root=mutate_root,
        selection_eligible=selection_eligible,
    )


def run_fake_composed_l3_mutation(
    run_dir: str | Path,
    *,
    fail_step_index: int | None = None,
) -> dict[str, Any]:
    """Run the fixed parser/gate-bound five-step L3 technical plan.

    The only injected failure admitted by this WP-004A seam is the third
    proposal step.  Keeping that seam closed prevents this fake harness from
    becoming an alternate general execution surface.
    """

    if fail_step_index not in {None, 2}:
        raise ValueError("composed L3 fake failure may target only step index 2")
    proposal_bytes, decision = _fixed_composed_l3_decision()
    return _run_fake_technical_mutation(
        run_dir,
        run_id=_COMPOSED_RUN_ID,
        runner_version=TECHNICAL_COMPOSED_RUNNER_VERSION,
        result_schema=TECHNICAL_COMPOSED_RESULT_SCHEMA,
        level=DoFLevel.L3,
        action_key="technical.composed_l3_plan",
        parameters={},
        initial_stage=DesignStage.ROUTED,
        child_mode="clean",
        fail_operation=None,
        fail_at=None,
        inject_extra_operation=None,
        mutate_root=False,
        selection_eligible=False,
        composed_proposal_bytes=proposal_bytes,
        composed_decision=decision,
        fail_step_index=fail_step_index,
    )


def _run_fake_technical_mutation(
    run_dir: str | Path,
    *,
    run_id: str,
    runner_version: str,
    result_schema: str,
    level: DoFLevel,
    action_key: str,
    parameters: Mapping[str, Any],
    initial_stage: DesignStage,
    child_mode: str,
    fail_operation: str | None,
    fail_at: ArtifactFailurePoint | None,
    inject_extra_operation: str | None,
    mutate_root: bool,
    selection_eligible: bool,
    composed_proposal_bytes: bytes | None = None,
    composed_decision: GateDecision | None = None,
    fail_step_index: int | None = None,
) -> dict[str, Any]:
    """Execute one closed fake technical action or gate-bound plan."""

    if selection_eligible:
        raise TechnicalSmokeError(
            "TECHNICAL_SELECTION_REJECTION",
            "technical child cannot be selection eligible",
        )
    if child_mode not in {"clean", "warning", "missing", "truncated", "invalid", "tampered"}:
        raise ValueError("unknown child_mode")
    output = Path(run_dir).resolve()
    output.mkdir(parents=True, exist_ok=False)
    artifacts = output / "artifacts"
    artifacts.mkdir()
    root_path = artifacts / "root.mockdcp"
    root_bytes = b"WP004 deterministic fake routed root\n"
    root_path.write_bytes(root_bytes)
    root_guard = RootPreservationGuard(root_path)

    manifest = real_action_expansion_manifest()
    is_composed = composed_decision is not None
    if is_composed != (composed_proposal_bytes is not None):
        raise ValueError("composed proposal bytes and decision must be provided together")
    expanded: ExpandedAction | None
    if composed_decision is None:
        expanded = expand_real_action(
            level,
            action_key,
            parameters,
            current_stage=initial_stage,
            manifest=manifest,
        )
        plan = technical_mutation_call_plan(expanded)
        decision_binding_payload: dict[str, Any] | None = None
    else:
        expanded = None
        plan = _composed_l3_call_plan(composed_decision)
        assert composed_proposal_bytes is not None
        decision_dict = composed_decision.to_dict()
        parsed_proposal = composed_decision.proposal
        if parsed_proposal is None:
            raise TechnicalSmokeError(
                "CAPABILITY_ACTION_REJECTION",
                "accepted composed L3 decision has no parsed proposal",
            )
        parser_binding = {
            "parser_version": STRICT_PROVIDER_PARSER_VERSION,
            "proposal_schema_version": ACTION_PROPOSAL_SCHEMA_VERSION,
            "raw_proposal_sha256": hashlib.sha256(composed_proposal_bytes).hexdigest(),
            "parsed_proposal_hash": parsed_proposal.proposal_hash,
        }
        gate_binding = {
            "gate_type": f"{ActionGate.__module__}.{ActionGate.__qualname__}",
            "level": DoFLevel.L3.value,
            "expected_decision_id": _COMPOSED_DECISION_ID,
            "catalogue_version": parsed_proposal.catalogue_version,
            "catalogue_hash": parsed_proposal.catalogue_hash,
            "initial_stage": DesignStage.ROUTED.value,
            "mask": create_mask_rules(
                version="dof.wp004a-composed-l3-mask.v1"
            ).to_dict(),
            "budget_remaining": {
                event_class.value: 8 for event_class in EventClass
            },
            "gate_decision_hash": _spec_hash(decision_dict),
        }
        decision_binding_payload = {
            "schema_version": TECHNICAL_DECISION_BINDING_SCHEMA,
            "parser_version": STRICT_PROVIDER_PARSER_VERSION,
            "proposal_schema_version": ACTION_PROPOSAL_SCHEMA_VERSION,
            "raw_proposal_utf8": composed_proposal_bytes.decode("utf-8"),
            "raw_proposal_sha256": hashlib.sha256(composed_proposal_bytes).hexdigest(),
            "parsed_proposal_hash": parsed_proposal.proposal_hash,
            "expected_decision_id": _COMPOSED_DECISION_ID,
            "decision_id": parsed_proposal.decision_id,
            "catalogue_version": parsed_proposal.catalogue_version,
            "catalogue_hash": parsed_proposal.catalogue_hash,
            "gate_decision": decision_dict,
            "gate_decision_hash": _spec_hash(decision_dict),
            "parser_binding_hash": _spec_hash(parser_binding),
            "gate_binding_hash": _spec_hash(gate_binding),
            "parser_binding": parser_binding,
            "gate_binding": gate_binding,
            "gate_accepted": composed_decision.accepted,
            "fallback_used": False,
            "technical_non_scientific": True,
        }
    spec_payload = {
        "runner_version": runner_version,
        "level": level.value,
        "action_key": action_key,
        "parameters": dict(parameters),
        "initial_stage": initial_stage.value,
        "expansion_manifest_hash": manifest.sha256,
        "call_plan_hash": plan.sha256,
        "child_mode": child_mode,
        "fail_operation": fail_operation,
        "fail_at": None if fail_at is None else fail_at.value,
        "inject_extra_operation": inject_extra_operation,
        "mutate_root": mutate_root,
        "technical_non_scientific": True,
    }
    if decision_binding_payload is not None:
        spec_payload.update(
            {
                "fail_step_index": fail_step_index,
                "decision_binding_hash": _spec_hash(decision_binding_payload),
            }
        )
    spec_hash = _spec_hash(spec_payload)
    writer = ImmutableEvidenceWriter(output, run_id, spec_hash)
    expansion_sidecar = writer.write_json(
        "evidence/real_action_expansion.json",
        manifest.to_dict() | {"sha256": manifest.sha256},
        role="real_action_expansion",
        schema_version=TECHNICAL_EXPANSION_EVIDENCE_SCHEMA,
    )
    plan_payload = plan.to_dict() | {"sha256": plan.sha256}
    if expanded is not None:
        plan_payload["expanded_action"] = expanded.to_dict()
    else:
        assert composed_decision is not None
        plan_payload["gate_decision"] = composed_decision.to_dict()
        plan_payload["jit_step_expansion"] = True
    plan_sidecar = writer.write_json(
        "evidence/declared_call_plan.json",
        plan_payload,
        role="declared_call_plan",
        schema_version=TECHNICAL_PLAN_EVIDENCE_SCHEMA,
    )
    decision_sidecar = (
        None
        if decision_binding_payload is None
        else writer.write_json(
            "evidence/technical_decision_binding.json",
            decision_binding_payload,
            role="technical_decision_binding",
            schema_version=TECHNICAL_DECISION_BINDING_SCHEMA,
        )
    )
    trace = writer.open_trace(
        "evidence/backend_calls.jsonl",
        role="backend_call_trace",
        schema_version=TECHNICAL_TRACE_SCHEMA,
    )
    step_trace = (
        None
        if not is_composed
        else writer.open_trace(
            "evidence/technical_plan_steps.jsonl",
            role="technical_plan_step_trace",
            schema_version=TECHNICAL_STEP_TRACE_SCHEMA,
        )
    )
    action_payload = (
        {
            "expanded_action": expanded.to_dict(),
            "technical_non_scientific": True,
        }
        if expanded is not None
        else {
            "proposal_id": _COMPOSED_PROPOSAL_ID,
            "decision_id": _COMPOSED_DECISION_ID,
            "parsed_proposal_hash": decision_binding_payload[
                "parsed_proposal_hash"
            ],
            "parser_binding_hash": decision_binding_payload[
                "parser_binding_hash"
            ],
            "gate_binding_hash": decision_binding_payload["gate_binding_hash"],
            "gate_decision_hash": decision_binding_payload[
                "gate_decision_hash"
            ],
            "decision_binding_hash": _spec_hash(decision_binding_payload),
            "decision_binding_sidecar_hash": decision_sidecar.sha256,
            "call_plan_hash": plan.sha256,
            "jit_step_expansion": True,
            "technical_non_scientific": True,
        }
    )
    store = _initialize_store(
        output,
        root_path,
        root_bytes,
        run_id,
        action_key,
        action_payload,
        initial_stage,
        manifest.sha256,
        runner_version,
        spec_hash,
        expansion_sidecar.sha256,
        plan_sidecar.sha256,
    )

    registry = default_tool_call_registry()
    budget = ActualCallBudget(
        _event_limits(plan),
        whole_run_timeout_seconds=30.0,
    )
    stage = initial_stage
    child_payload: bytes | None = None
    child_validation: ArtifactValidation | None = None
    transaction_result: ArtifactTransactionResult | None = None
    endpoint = "TECHNICAL_MUTATION_VALID_NONSCIENTIFIC"
    reasons = ["fake_observed_host_completed"]
    active_step_index: int | None = None
    active_step_id: str | None = None
    active_step_action: str | None = None
    executed_expansions: list[dict[str, Any]] = []
    step_lifecycle: list[dict[str, Any]] = []
    stage_progression = [initial_stage.value]

    def validator(path: Path, digest: str, size_bytes: int) -> ArtifactValidation:
        if child_mode == "tampered":
            path.write_bytes(path.read_bytes() + b"tamper")
            return ArtifactValidation(
                ValidationStatus.CLEAN,
                "dof.fake-technical-validator.v1",
                {"injected_tamper": True},
            )
        status = {
            "clean": ValidationStatus.CLEAN,
            "warning": ValidationStatus.WARNING,
            "truncated": ValidationStatus.INVALID,
            "invalid": ValidationStatus.INVALID,
        }.get(child_mode, ValidationStatus.FAILED)
        return ArtifactValidation(
            status,
            "dof.fake-technical-validator.v1",
            {
                "digest": digest,
                "size_bytes": size_bytes,
                "final_stage": stage.value,
                "technical_non_scientific": True,
            },
        )

    async def invoke(operation: Any, arguments: Mapping[str, Any]) -> Any:
        nonlocal child_payload, child_validation, transaction_result
        if (
            fail_step_index is not None
            and active_step_index == fail_step_index
            and operation.event_class
            in {EventClass.MUTATION_LIGHT, EventClass.IMPLEMENTATION_HEAVY}
        ):
            raise TechnicalSmokeError(
                "TOOL_EXECUTION_FAILURE",
                "technical_plan_step_failed:"
                f"{active_step_index}:{active_step_id}:{active_step_action}",
            )
        if operation.operation_id == fail_operation:
            raise RuntimeError("injected fake host failure")
        if operation.operation_id == "validation.hash_root":
            observed = root_guard.verify_unchanged()
            return {
                "sha256": observed.sha256,
                "size_bytes": observed.size_bytes,
            }
        if operation.operation_id in {
            "host.session.start",
            "host.session.close",
            "vivado.open_checkpoint",
            "vivado.close_design",
        }:
            return {"status": "SUCCEEDED"}
        if operation.operation_id.startswith("vivado.report_"):
            return {
                "status": "SUCCEEDED",
                "stage": stage.value,
                "report_hash": _spec_hash(
                    {"operation": operation.operation_id, "stage": stage.value}
                ),
            }
        if operation.event_class in {
            EventClass.MUTATION_LIGHT,
            EventClass.IMPLEMENTATION_HEAVY,
        }:
            return {
                "status": "SUCCEEDED",
                "arguments_hash": _spec_hash(dict(arguments)),
            }
        if operation.operation_id == "vivado.write_checkpoint":
            if child_mode == "missing":
                child_payload = None
            elif child_mode == "truncated":
                child_payload = b"x"
            else:
                execution_payload = (
                    expanded.to_dict()
                    if expanded is not None
                    else {
                        "decision_binding_hash": _spec_hash(
                            decision_binding_payload
                        ),
                        "executed_expansions": executed_expansions,
                        "stage_progression": stage_progression,
                    }
                )
                child_payload = root_bytes + b"expanded:" + _canonical_bytes(
                    execution_payload
                )
            return {
                "status": "SUCCEEDED",
                "child_bytes_available": child_payload is not None,
            }
        if operation.operation_id == "validation.hash_child":
            if child_payload is None:
                raise TechnicalSmokeError(
                    "TECHNICAL_MUTATION_NO_VALID_CHILD",
                    "fake child payload is missing",
                )
            return {
                "sha256": hashlib.sha256(child_payload).hexdigest(),
                "size_bytes": len(child_payload),
            }
        if operation.operation_id == "validation.validate_child":
            if child_payload is None:
                raise TechnicalSmokeError(
                    "ARTIFACT_VALIDATION_FAILURE", "child is missing"
                )
            probe_path = output / "tmp-child-validation.dcp"
            probe_path.write_bytes(child_payload)
            try:
                child_validation = validator(
                    probe_path,
                    hashlib.sha256(child_payload).hexdigest(),
                    len(child_payload),
                )
            finally:
                if probe_path.exists():
                    probe_path.unlink()
            return {
                "status": child_validation.status.value,
                "valid": child_validation.valid,
            }
        if operation.operation_id == "validation.register_child":
            if child_payload is None:
                raise TechnicalSmokeError(
                    "ARTIFACT_VALIDATION_FAILURE", "child is missing"
                )
            transaction = ArtifactTransaction(
                store=store,
                run_dir=output,
                run_id=run_id,
                scope_id=_SCOPE_ID,
                attempt_id=_ATTEMPT_ID,
                parent_state_id=_ROOT_STATE_ID,
                state_id=_CHILD_STATE_ID,
                started_at=_NOW,
                now=lambda: _NOW,
            )
            transaction_result = transaction.execute(
                producer=lambda path: path.write_bytes(child_payload or b""),
                validator=validator,
                metrics={
                    "technical_non_scientific": True,
                    "expansion_manifest_hash": manifest.sha256,
                    "call_plan_hash": plan.sha256,
                    "final_stage": stage.value,
                },
                selection_eligible=False,
                selection_reason="WP-004 technical child is never selectable",
                fail_at=fail_at,
            )
            return {
                "endpoint": transaction_result.endpoint,
                "valid": transaction_result.valid,
                "selected": transaction_result.selected,
            }
        if operation.operation_id == "evidence.finalize":
            lifecycle = verify_artifact_lifecycle(store, run_dir=output)
            return {
                "artifact_lifecycle_safe": lifecycle.safe,
                "artifact_lifecycle_errors": list(lifecycle.errors),
            }
        raise TechnicalSmokeError(
            "BACKEND_CONTRACT_MISMATCH",
            f"fake host has no binding:{operation.operation_id}",
        )

    observer = ObservedCallExecutor(
        registry=registry,
        plan=plan,
        budget=budget,
        invoke=invoke,
        trace_sink=trace.append,
        execution_binding={"runtime_binding_id": spec_hash},
    )

    async def execute() -> None:
        nonlocal stage, active_step_index, active_step_id, active_step_action

        async def observed(
            operation_id: str,
            arguments: Mapping[str, Any] | None = None,
            *,
            current_stage: DesignStage | None = None,
        ) -> Any:
            return await observer.call(
                operation_id,
                dict(arguments or {}),
                current_stage=current_stage,
            )

        await observed("validation.hash_root", {"phase": "before"})
        await observed("host.session.start")
        await observed("vivado.open_checkpoint", {"role": "root"})
        if stage is DesignStage.ROUTED:
            await observed("vivado.report_timing_summary", current_stage=stage)
            await observed("vivado.report_route_status", current_stage=stage)
        if inject_extra_operation is not None:
            await observed(inject_extra_operation, current_stage=stage)
        if composed_decision is None:
            assert expanded is not None
            for call in expanded.calls:
                await observed(
                    call.operation_id,
                    dict(call.arguments),
                    current_stage=stage,
                )
                # The stage changes only after the observed terminal call succeeds.
                stage = call.output_stage
        else:
            for gated_step in composed_decision.gated_steps:
                if gated_step.stage_before is not stage:
                    raise TechnicalSmokeError(
                        "BACKEND_CONTRACT_MISMATCH",
                        "gate/runtime stage mismatch before JIT expansion:"
                        f"{gated_step.step_index}:{stage.value}",
                    )
                # This is intentionally inside the sequential loop.  No later
                # step is expanded until the prior observed call has a
                # SUCCEEDED terminal row and advances ``stage`` below.
                step_expansion = expand_real_action(
                    DoFLevel.L3,
                    gated_step.action_key,
                    gated_step.parameters,
                    current_stage=stage,
                    manifest=manifest,
                )
                if (
                    len(step_expansion.calls) != 1
                    or step_expansion.final_stage is not gated_step.stage_after
                ):
                    raise TechnicalSmokeError(
                        "BACKEND_CONTRACT_MISMATCH",
                        "JIT expansion disagrees with gate decision:"
                        f"{gated_step.step_index}:{gated_step.action_key}",
                    )
                expansion_dict = step_expansion.to_dict()
                expansion_hash = _spec_hash(expansion_dict)
                expansion_record = {
                    "record_type": "PLAN_STEP_EXPANDED",
                    "step_index": gated_step.step_index,
                    "step_id": gated_step.step_id,
                    "action_key": gated_step.action_key,
                    "proposal_id": gated_step.proposal_id,
                    "proposal_hash": gated_step.proposal_hash,
                    "decision_id": _COMPOSED_DECISION_ID,
                    "stage_before": stage.value,
                    "expected_stage_after": gated_step.stage_after.value,
                    "expanded_action": expansion_dict,
                    "expanded_action_hash": expansion_hash,
                    "prior_terminal_success_count": len(step_lifecycle),
                    "jit_after_prior_success": all(
                        item["status"] == "SUCCEEDED"
                        for item in step_lifecycle
                    ),
                    "schema_version": TECHNICAL_STEP_TRACE_SCHEMA,
                }
                executed_expansions.append(expansion_dict)
                assert step_trace is not None
                step_trace.append(expansion_record)
                active_step_index = gated_step.step_index
                active_step_id = gated_step.step_id
                active_step_action = gated_step.action_key
                try:
                    call = step_expansion.calls[0]
                    await observed(
                        call.operation_id,
                        dict(call.arguments),
                        current_stage=stage,
                    )
                except Exception as exc:
                    terminal = {
                        "record_type": "PLAN_STEP_TERMINAL",
                        "step_index": gated_step.step_index,
                        "step_id": gated_step.step_id,
                        "action_key": gated_step.action_key,
                        "proposal_id": gated_step.proposal_id,
                        "proposal_hash": gated_step.proposal_hash,
                        "decision_id": _COMPOSED_DECISION_ID,
                        "status": "FAILED",
                        "stage_before": stage.value,
                        "stage_after": stage.value,
                        "expanded_action_hash": expansion_hash,
                        "error_class": type(exc).__name__,
                        "schema_version": TECHNICAL_STEP_TRACE_SCHEMA,
                    }
                    step_lifecycle.append(terminal)
                    step_trace.append(terminal)
                    raise
                else:
                    stage = step_expansion.final_stage
                    stage_progression.append(stage.value)
                    terminal = {
                        "record_type": "PLAN_STEP_TERMINAL",
                        "step_index": gated_step.step_index,
                        "step_id": gated_step.step_id,
                        "action_key": gated_step.action_key,
                        "proposal_id": gated_step.proposal_id,
                        "proposal_hash": gated_step.proposal_hash,
                        "decision_id": _COMPOSED_DECISION_ID,
                        "status": "SUCCEEDED",
                        "stage_before": step_expansion.initial_stage.value,
                        "stage_after": stage.value,
                        "expanded_action_hash": expansion_hash,
                        "schema_version": TECHNICAL_STEP_TRACE_SCHEMA,
                    }
                    step_lifecycle.append(terminal)
                    step_trace.append(terminal)
                finally:
                    active_step_index = None
                    active_step_id = None
                    active_step_action = None
        if mutate_root:
            root_path.write_bytes(root_path.read_bytes() + b"root-drift")
        if stage is DesignStage.ROUTED:
            await observed("vivado.report_timing_summary", current_stage=stage)
            await observed("vivado.report_route_status", current_stage=stage)
        await observed("vivado.write_checkpoint", {"role": "technical_child"})
        await observed("vivado.close_design")
        await observed("vivado.open_checkpoint", {"role": "technical_child"})
        if stage is DesignStage.ROUTED:
            await observed("vivado.report_timing_summary", current_stage=stage)
            await observed("vivado.report_route_status", current_stage=stage)
        await observed("vivado.close_design")
        await observed("host.session.close")
        await observed("validation.hash_root", {"phase": "after"})
        await observed("validation.hash_child")
        await observed("validation.validate_child")
        await observed("validation.register_child")
        await observed("evidence.finalize")

    try:
        asyncio.run(execute())
        reconciliation = observer.reconcile_or_raise().to_dict()
        if transaction_result is None or not transaction_result.valid:
            endpoint = (
                "ARTIFACT_VALIDATION_FAILURE"
                if transaction_result is None
                else transaction_result.endpoint
            )
            reasons = [
                "no valid technical child"
                if transaction_result is None
                else transaction_result.endpoint
            ]
    except TechnicalSmokeError as exc:
        endpoint = exc.endpoint
        reasons = [exc.reason]
        reconciliation = observer.reconciliation().to_dict()
    except ArtifactTransactionError as exc:
        endpoint = exc.endpoint
        reasons = [exc.reason]
        reconciliation = observer.reconciliation().to_dict()
    except ToolCallContractError as exc:
        endpoint = "BACKEND_CONTRACT_MISMATCH"
        reasons = [str(exc)]
        reconciliation = observer.reconciliation().to_dict()
    except Exception as exc:
        endpoint = "TOOL_EXECUTION_FAILURE"
        reasons = [type(exc).__name__]
        reconciliation = observer.reconciliation().to_dict()

    _record_terminal_failure_if_needed(store, endpoint, reasons)

    trace_sidecar = trace.close()
    step_trace_sidecar = None if step_trace is None else step_trace.close()
    lifecycle = verify_artifact_lifecycle(store, run_dir=output)
    selected = store.selections()
    eligibility = store.eligibility()
    child_eligibility = [
        item for item in eligibility if item.state_id == _CHILD_STATE_ID
    ]
    try:
        root_guard.verify_unchanged()
        root_unchanged = True
    except ArtifactTransactionError:
        root_unchanged = False
    budget_summary = budget.summary()
    mutation_operations = {
        "vivado.unroute_all",
        "vivado.unplace_all",
        "vivado.place_design",
        "vivado.route_design",
        "vivado.phys_opt_design",
    }
    observed_mutation_operations = [
        operation_id
        for operation_id in reconciliation["observed_started_operations"]
        if operation_id in mutation_operations
    ]
    technical_plan_summary: dict[str, Any] | None = None
    if composed_decision is not None:
        assert decision_binding_payload is not None
        planned_steps = list(composed_decision.gated_steps)
        terminal_step_indexes = {
            int(item["step_index"]) for item in step_lifecycle
        }
        technical_plan_summary = {
            "proposal_id": composed_decision.proposal.proposal_id,
            "proposal_hash": composed_decision.proposal.proposal_hash,
            "decision_id": composed_decision.proposal.decision_id,
            "parser_version": STRICT_PROVIDER_PARSER_VERSION,
            "parser_binding_hash": decision_binding_payload["parser_binding_hash"],
            "gate_binding_hash": decision_binding_payload["gate_binding_hash"],
            "gate_decision_hash": decision_binding_payload["gate_decision_hash"],
            "decision_binding_hash": _spec_hash(decision_binding_payload),
            "gate_accepted": composed_decision.accepted,
            "call_plan_id": plan.plan_id,
            "call_plan_hash": plan.sha256,
            "jit_step_expansion": True,
            "planned_step_count": len(planned_steps),
            "expanded_step_count": len(executed_expansions),
            "terminal_step_count": len(step_lifecycle),
            "step_lifecycle": step_lifecycle,
            "stage_progression": stage_progression,
            "final_observed_stage": stage.value,
            "expected_full_stage_progression": [
                "ROUTED",
                "PLACED",
                "UNPLACED",
                "PLACED",
                "ROUTED",
                "ROUTED",
            ],
            "observed_mutation_operations": observed_mutation_operations,
            "actual_light_started": budget_summary["used"][
                EventClass.MUTATION_LIGHT.value
            ],
            "actual_heavy_started": budget_summary["used"][
                EventClass.IMPLEMENTATION_HEAVY.value
            ],
            "undispatched_step_ids": [
                item.step_id
                for item in planned_steps
                if item.step_index not in terminal_step_indexes
            ],
            "no_later_step_dispatched": (
                len(observed_mutation_operations) == len(step_lifecycle)
            ),
            "failed_step_index": next(
                (
                    int(item["step_index"])
                    for item in step_lifecycle
                    if item["status"] == "FAILED"
                ),
                None,
            ),
            "failed_step_id": next(
                (
                    str(item["step_id"])
                    for item in step_lifecycle
                    if item["status"] == "FAILED"
                ),
                None,
            ),
            "failed_action_key": next(
                (
                    str(item["action_key"])
                    for item in step_lifecycle
                    if item["status"] == "FAILED"
                ),
                None,
            ),
        }
    summary = {
        "schema_version": result_schema,
        "runner_version": runner_version,
        "endpoint": endpoint,
        "endpoint_success": endpoint == "TECHNICAL_MUTATION_VALID_NONSCIENTIFIC",
        "reasons": reasons,
        "technical_non_scientific": True,
        "spec_hash": spec_hash,
        "level": level.value,
        "expanded_action": None if expanded is None else expanded.to_dict(),
        "expansion_manifest_hash": manifest.sha256,
        "call_plan_hash": plan.sha256,
        "call_reconciliation": reconciliation,
        "budget": budget_summary,
        "root_unchanged": root_unchanged,
        "transaction": (
            None
            if transaction_result is None
            else {
                "endpoint": transaction_result.endpoint,
                "valid": transaction_result.valid,
                "selected": transaction_result.selected,
                "state_id": transaction_result.state_id,
                "artifact_id": transaction_result.artifact_id,
                "sha256": transaction_result.sha256,
                "size_bytes": transaction_result.size_bytes,
                "validation_status": transaction_result.validation_status,
            }
        ),
        "canonical_counts": {
            "actions": len(store.actions()),
            "attempts": len(store.attempts()),
            "outcomes": len(store.outcomes()),
            "states": len(store.states()),
            "selections": len(selected),
        },
        "root_is_sole_selection": (
            len(selected) == 1 and selected[0].state_id == _ROOT_STATE_ID
        ),
        "child_selection_eligible": (
            None
            if not child_eligibility
            else child_eligibility[0].selection_eligible
        ),
        "artifact_lifecycle": {
            "safe": lifecycle.safe,
            "errors": list(lifecycle.errors),
            "incomplete_attempt_ids": list(lifecycle.incomplete_attempt_ids),
        },
        "canonical_schema_version": CURRENT_SCHEMA_VERSION,
    }
    if technical_plan_summary is not None:
        summary["technical_plan"] = technical_plan_summary
    result_sidecar = writer.write_json(
        "evidence/result_summary.json",
        summary,
        role="technical_result_summary",
        schema_version=result_schema,
        kind=ArtifactKind.REPORT,
    )
    terminal_status = (
        RunStatus.FINALIZED
        if summary["endpoint_success"]
        else RunStatus.FAILED
    )
    sidecars = [expansion_sidecar, plan_sidecar, trace_sidecar, result_sidecar]
    required_roles = [
        "real_action_expansion",
        "declared_call_plan",
        "backend_call_trace",
        "technical_result_summary",
    ]
    if decision_sidecar is not None:
        sidecars.append(decision_sidecar)
        required_roles.append("technical_decision_binding")
    if step_trace_sidecar is not None:
        sidecars.append(step_trace_sidecar)
        required_roles.append("technical_plan_step_trace")
    registration = register_evidence_and_finalize(
        store,
        run_dir=output,
        sidecars=tuple(sidecars),
        run_id=run_id,
        spec_hash=spec_hash,
        terminal_status=terminal_status,
        terminal_detail={
            "endpoint": endpoint,
            "technical_non_scientific": True,
        },
        required_roles=tuple(required_roles),
    )
    verification = verify_registered_evidence(
        store,
        run_dir=output,
        expected_run_id=run_id,
        expected_spec_hash=spec_hash,
        required_roles=tuple(required_roles),
    )
    verification.raise_for_errors()
    summary["evidence_set_hash"] = registration.evidence_set_hash
    summary["registered_evidence_roles"] = list(verification.roles)
    summary["sqlite_quick_check"] = store.connection.execute(
        "PRAGMA quick_check"
    ).fetchone()[0]
    summary["foreign_key_errors"] = len(
        store.connection.execute("PRAGMA foreign_key_check").fetchall()
    )
    store.close()
    return summary


def _initialize_store(
    run_dir: Path,
    root_path: Path,
    root_bytes: bytes,
    run_id: str,
    action_key: str,
    action_payload: Mapping[str, Any],
    initial_stage: DesignStage,
    expansion_manifest_hash: str,
    runner_version: str,
    spec_hash: str,
    expansion_sidecar_hash: str,
    plan_sidecar_hash: str,
) -> SQLiteRunStore:
    store = SQLiteRunStore.create(
        run_dir / "run.db",
        Run(
            run_id,
            "dof_technical_fake_mutation",
            _NOW,
            {
                "runner_version": runner_version,
                "spec_hash": spec_hash,
                "technical_non_scientific": True,
                "expansion_manifest_hash": expansion_manifest_hash,
                "expansion_sidecar_hash": expansion_sidecar_hash,
                "call_plan_sidecar_hash": plan_sidecar_hash,
            },
        ),
    )
    with store.transaction():
        store.add_run_event(
            RunLifecycleEvent(
                "run-event-000",
                run_id,
                0,
                RunStatus.RUNNING,
                _NOW,
                {"runner_version": runner_version},
            )
        )
        store.add_scope(
            ExecutionScope(
                _SCOPE_ID,
                run_id,
                ScopeKind.ROOT,
                "WP-004 fake technical mutation",
                created_at=_NOW,
            )
        )
        store.add_artifact(
            ArtifactReference(
                _ROOT_ARTIFACT_ID,
                run_id,
                _SCOPE_ID,
                ArtifactKind.DCP,
                root_path.relative_to(run_dir).as_posix(),
                hashlib.sha256(root_bytes).hexdigest(),
                len(root_bytes),
                _NOW,
                {"role": "fake_technical_root", "immutable_input": True},
            )
        )
        store.add_state(
            DesignState(
                _ROOT_STATE_ID,
                run_id,
                _SCOPE_ID,
                _ROOT_ARTIFACT_ID,
                created_at=_NOW,
                metadata={
                    "stage": initial_stage.value,
                    "technical_non_scientific": True,
                },
            )
        )
        store.add_validation(
            ValidationRecord(
                "validation-root",
                run_id,
                _SCOPE_ID,
                _ROOT_STATE_ID,
                ValidationLevel.CERTIFICATION,
                ValidationStatus.PASSED,
                checked_at=_NOW,
                details={"validator": "fake-technical-root.v1"},
            )
        )
        store.add_eligibility(
            StateEligibility(
                "eligibility-root",
                run_id,
                _ROOT_STATE_ID,
                True,
                True,
                "frozen technical baseline",
                _NOW,
            )
        )
        store.add_selection(
            ResultSelection(
                "selection-root",
                run_id,
                _SCOPE_ID,
                _ROOT_STATE_ID,
                _ROOT_ARTIFACT_ID,
                SelectionKind.BASELINE,
                "root remains sole technical baseline",
                _NOW,
            )
        )
        store.add_action(
            ActionSpecification(
                _ACTION_ID,
                run_id,
                _SCOPE_ID,
                action_key,
                dict(action_payload),
                _NOW,
            )
        )
        store.add_attempt(
            Attempt(
                _ATTEMPT_ID,
                run_id,
                _SCOPE_ID,
                _ROOT_STATE_ID,
                _ACTION_ID,
                scheduled_at=_NOW,
            )
        )
        store.add_attempt_event(
            AttemptLifecycleEvent(
                "attempt-event-000",
                _ATTEMPT_ID,
                0,
                AttemptEventKind.SCHEDULED,
                _NOW,
            )
        )
        store.add_attempt_event(
            AttemptLifecycleEvent(
                "attempt-event-001",
                _ATTEMPT_ID,
                1,
                AttemptEventKind.STARTED,
                _NOW,
            )
        )
    return store


def _record_terminal_failure_if_needed(
    store: SQLiteRunStore,
    endpoint: str,
    reasons: list[str],
) -> None:
    """Close an attempt that failed before ArtifactTransaction owned it."""

    if endpoint == "TECHNICAL_MUTATION_VALID_NONSCIENTIFIC":
        return
    existing_outcomes = store.outcomes()
    existing_events = store.attempt_events(attempt_id=_ATTEMPT_ID)
    artifact_transaction_started = any(
        event.detail.to_dict().get("transaction_version") is not None
        for event in existing_events
    )
    if existing_outcomes or artifact_transaction_started:
        return
    with store.transaction():
        store.add_outcome(
            ExecutionOutcome(
                "outcome-technical-failure",
                _ATTEMPT_ID,
                OutcomeStatus.FAILED,
                _NOW,
                _NOW,
                error_type=endpoint,
                error_message=";".join(reasons),
                metrics={
                    "endpoint": endpoint,
                    "technical_non_scientific": True,
                },
            )
        )
        store.add_attempt_event(
            AttemptLifecycleEvent(
                "attempt-event-terminal-failure",
                _ATTEMPT_ID,
                len(existing_events),
                AttemptEventKind.FAILED,
                _NOW,
                {
                    "endpoint": endpoint,
                    "technical_non_scientific": True,
                },
            )
        )


__all__ = [
    "TECHNICAL_CALL_PLAN_VERSION",
    "TECHNICAL_COMPOSED_RUNNER_VERSION",
    "TECHNICAL_FAKE_RUNNER_VERSION",
    "TechnicalSmokeError",
    "run_fake_composed_l3_mutation",
    "run_fake_technical_mutation",
    "technical_mutation_call_plan",
]
