"""Frozen Protocol E1-v2 bundle and complete-job execution."""

from __future__ import annotations

from dataclasses import dataclass, replace
import base64
import json
import os
from pathlib import Path
import time
from typing import Any, Mapping, Sequence

from .artifacts import fingerprint_file
from .catalogues import filtered_nested_action_catalogue, nested_action_catalogue
from .common_observation import CommonPolicyContext, build_common_policy_context
from .contracts import DesignStage, DoFLevel, EventClass, UnknownRegime
from .e0_job import (
    _gated_step_rows,
    _hash,
    _write_json,
    capture_real_observation,
)
from .e1 import (
    E1_L1_ACTIONS,
    action_aware_history_row,
    repeated_action_diagnostic,
)
from .e1_job import _choose_best, _l1_causal, _reconcile_l1_actual_heavy_calls, _score_child
from .e1v2 import (
    E1V2_BOUND_BUNDLE_SCHEMA,
    E1V2_JOB_SPEC_SCHEMA,
    E1V2_TRAJECTORY_RESULT_SCHEMA,
    UNKNOWN_PROVIDER_REQUEST_RESERVE_USD,
    WP012_EXCLUDED_ACTIONS,
    canonical_bytes,
    content_hash,
    protocol_manifest,
)
from .evidence import ImmutableEvidenceWriter
from .gate import ActionGate, EndpointCode, GateDecision, ProposalKind
from .g_diagnostic import (
    GCue,
    G_DIAGNOSTIC_PROTOCOL,
    G_DIAGNOSTIC_RESULT_SCHEMA,
    build_g_diagnostic_policy_context,
    diagnostic_catalogue,
    validate_diagnostic_spec,
)
from .generated import GENERATED_ACTION_KEY, generated_program_evidence
from .manifests import capability_manifest_v2, interface_fingerprints_v2
from .masks import create_mask_rules
from .mutation_worker_binding import verified_fake_attestation_evidence_sha256
from .openrouter import (
    OPENROUTER_ADAPTER_VERSION,
    OPENROUTER_E1_PROFILES,
    OPENROUTER_PROVIDER_ERROR_CATEGORIES,
    OpenRouterChatCompletionsTransport,
    build_openrouter_outbound,
)
from .provider import (
    DoFProviderObserver,
    ProviderDecision,
    ProviderAttemptStatus,
    ProviderRequestBinding,
    ProviderRunLedger,
    ProviderSettings,
    verify_provider_attempts,
    write_provider_attempt_trace,
)
from .provenance import observe_repository
from .real_mutation import (
    build_e0_dispatch_authorization,
    execute_e0_real_action,
    execute_e0_real_gated_plan,
    freeze_e0_real_action_plan,
    freeze_e0_real_gated_plan,
    validate_e0_dispatch_authorization,
    write_e0_frozen_plan,
    write_e0_gated_plan,
)


PROVIDER_TRANSPORT_FAILURE_ENDPOINT = "PROVIDER_TRANSPORT_FAILURE"
PROVIDER_REJECTION_ENDPOINT = "PROVIDER_REJECTION"
_IMMEDIATE_GLOBAL_PROVIDER_CATEGORIES = frozenset(
    {
        "AUTHENTICATION_OR_KEY",
        "PAYMENT_OR_BUDGET",
        "PERMISSION_OR_WORKSPACE",
        "UNCLASSIFIED_4XX",
    }
)
_ORDINARY_TRANSPORT_FAILURE_CLASSES = frozenset(
    {
        "RESPONSE_NOT_OBSERVED",
        "HTTP_ERROR",
        "RESPONSE_EVIDENCE_INVALID",
        "ACCOUNTING_UNAVAILABLE",
        "TRANSPORT_TIMEOUT",
        "TRANSPORT_ERROR",
        "CLEANUP_FAILURE",
    }
)
_PROPOSAL_BINDING_REASONS = frozenset(
    {
        "proposal_decision_id_mismatch",
        "proposal_catalogue_version_mismatch",
        "proposal_catalogue_hash_mismatch",
    }
)


def _apply_reconciled_accounting(
    decision: ProviderDecision,
    binding: ProviderRequestBinding,
    transport: Any,
) -> ProviderDecision:
    accounting = getattr(transport, "reconciled_accounting", None)
    if decision.accepted or not isinstance(accounting, Mapping) or not decision.attempts:
        return decision
    last = decision.attempts[-1]
    if last.cost_usd is not None:
        raise RuntimeError("generation_accounting_conflicts_with_actual_cost")
    cost = float(accounting["cost_usd"])
    if cost > UNKNOWN_PROVIDER_REQUEST_RESERVE_USD + 1e-12:
        raise RuntimeError("generation_accounting_exceeds_frozen_reserve")
    reasons = tuple(
        reason
        for reason in last.rejection_reasons
        if not reason.startswith("unknown_provider_cost")
    ) + ("generation_accounting_reconciled",)
    reconciled = replace(
        last,
        observed_request_contract_hash=binding.request_contract_hash,
        actual_provider="openrouter",
        provider_request_id=str(accounting["generation_id"]),
        returned_model_id=str(accounting["returned_model"]),
        returned_model_revision=str(accounting["upstream_provider"]),
        input_tokens=int(accounting["input_tokens"]),
        output_tokens=int(accounting["output_tokens"]),
        total_tokens=int(accounting["total_tokens"]),
        cost_usd=cost,
        accounted_cost_usd=cost,
        global_accounted_cost_usd_after_attempt=cost,
        cost_source=str(accounting["cost_source"]),
        rejection_reasons=reasons,
    )
    return ProviderDecision(
        False,
        decision.endpoint,
        (*decision.attempts[:-1], reconciled),
        reasons=tuple(
            reason
            for reason in decision.reasons
            if not reason.startswith("unknown_provider_cost")
        )
        + ("generation_accounting_reconciled",),
    )


def freeze_e1v2_job_bundle(
    roots: Mapping[str, str | Path],
    root_audit: Mapping[str, Any],
    provider_discovery: Mapping[str, Any],
    *,
    matrix: str,
    repository_anchor: str | Path,
    fake_startup_attestation: str | Path,
) -> dict[str, Any]:
    root_names = tuple(roots)
    protocol = protocol_manifest(matrix=matrix, roots=root_names)
    repository = observe_repository(repository_anchor)
    if repository.tracked_tree_dirty:
        raise RuntimeError("tracked_product_tree_not_clean")
    if root_audit.get("all_accepted") is not True:
        raise RuntimeError("SCORER_OR_ROOT_IDENTITY_BLOCKER")
    model_discovery = provider_discovery.get("models", {})
    endpoint_discovery = provider_discovery.get("endpoints", {})
    reserve = provider_discovery.get(
        "unknown_cost_reserve_verification",
        provider_discovery.get("reserve_4096"),
    )
    reserve_accepted = isinstance(reserve, Mapping) and (
        reserve.get("all_models_accepted") is True
        or reserve.get("old_reserve_all_models_conservative") is True
    )
    profiles_available = provider_discovery.get("all_profiles_available") is True or (
        isinstance(model_discovery, Mapping)
        and model_discovery.get("all_required_available") is True
        and isinstance(endpoint_discovery, Mapping)
        and endpoint_discovery.get("all_profiles_available") is True
        and reserve_accepted
    )
    if not profiles_available:
        raise RuntimeError("MODEL_AVAILABILITY_BLOCKER")
    if not reserve_accepted:
        raise RuntimeError("UNKNOWN_PROVIDER_COST_RESERVE_BLOCKER")
    required_models = {row["model"] for row in protocol["models"]}
    available_models = {
        str(model)
        for model, available in (
            model_discovery.get("required_models", {}).items()
            if isinstance(model_discovery, Mapping)
            and isinstance(model_discovery.get("required_models"), Mapping)
            else ()
        )
        if available is True
    }
    endpoint_models = (
        set(endpoint_discovery.get("models", {}))
        if isinstance(endpoint_discovery, Mapping)
        and isinstance(endpoint_discovery.get("models"), Mapping)
        else set()
    )
    if not required_models <= available_models or not required_models <= endpoint_models:
        raise RuntimeError("MODEL_AVAILABILITY_BLOCKER")
    rows = {row["benchmark"]: row for row in root_audit.get("rows", ())}
    if set(rows) != set(root_names):
        raise ValueError("e1v2_root_audit_set_mismatch")
    root_rows: dict[str, Any] = {}
    for name in root_names:
        observed = fingerprint_file(Path(roots[name]).resolve())
        row = rows[name]
        if row.get("artifact") != {
            "sha256": observed.sha256,
            "size_bytes": observed.size_bytes,
        }:
            raise RuntimeError("e1v2_root_audit_identity_mismatch")
        root_rows[name] = {
            "pseudonym": name,
            "sha256": observed.sha256,
            "size_bytes": observed.size_bytes,
            "score": row["score"],
            "validation_sha256": row.get("validation_sha256"),
        }
    attestation_sha = verified_fake_attestation_evidence_sha256(fake_startup_attestation)
    frozen = {
        "protocol_sha256": protocol["protocol_sha256"],
        "repository": {
            "branch_name": repository.branch_name,
            "head_commit": repository.head_commit,
            "head_tree": repository.head_tree,
            "binary_diff_sha256": repository.binary_diff_sha256,
        },
        "root_audit_sha256": root_audit["audit_sha256"],
        "provider_discovery_sha256": content_hash(provider_discovery),
        "unknown_cost_reserve_verification_sha256": reserve.get(
            "verification_sha256"
        ),
        "fake_startup_attestation_evidence_sha256": attestation_sha,
        "roots": root_rows,
    }
    freeze_sha = content_hash(frozen)
    stochastic_jobs = sum(job["level"] in {"L2", "L3", "L4"} for job in protocol["jobs"])
    per_job_cost = (
        float(protocol["budgets"]["per_trajectory_provider_cost_cap_usd"])
        if "per_trajectory_provider_cost_cap_usd" in protocol["budgets"]
        else float(protocol["budgets"]["aggregate_cost_usd"]) / stochastic_jobs
    )
    specs = []
    for job in protocol["jobs"]:
        declared_heavy_calls = (
            0
            if job["level"] == "L0"
            else (
                len(job["l1_actions"])
                if job["level"] == "L1" and "l1_actions" in job
                else 4
            )
        )
        core = {
            "schema_version": E1V2_JOB_SPEC_SCHEMA,
            "protocol_version": protocol["protocol_version"],
            "matrix": matrix,
            "study_status": protocol["study_status"],
            "job": job,
            "root": root_rows[job["benchmark"]],
            "protocol_sha256": protocol["protocol_sha256"],
            "freeze_sha256": freeze_sha,
            "budgets": {
                "heavy_calls": declared_heavy_calls,
                "model_requests": 4 if job["level"] in {"L2", "L3", "L4"} else 0,
                "trajectory_wall_seconds": 3700,
                "provider_cost_usd": per_job_cost if job["model"] else 0.0,
            },
        }
        specs.append(core | {"job_spec_sha256": content_hash(core)})
    payload = {
        "schema_version": E1V2_BOUND_BUNDLE_SCHEMA,
        "protocol": protocol,
        "freeze": frozen,
        "freeze_sha256": freeze_sha,
        "jobs": specs,
    }
    return payload | {"bundle_sha256": content_hash(payload)}


def validate_e1v2_job_bundle(bundle: Mapping[str, Any]) -> dict[str, Any]:
    value = json.loads(canonical_bytes(dict(bundle)))
    claimed = value.pop("bundle_sha256", None)
    if value.get("schema_version") != E1V2_BOUND_BUNDLE_SCHEMA:
        raise ValueError("e1v2_bundle_schema_mismatch")
    if claimed != content_hash(value):
        raise ValueError("e1v2_bundle_hash_mismatch")
    protocol = value.get("protocol", {})
    expected = protocol_manifest(
        matrix=protocol.get("matrix"), roots=tuple(protocol.get("roots", ()))
    )
    if protocol != expected:
        raise ValueError("e1v2_protocol_binding_mismatch")
    if len(value.get("jobs", ())) != protocol["job_count"]:
        raise ValueError("e1v2_job_count_mismatch")
    ids: set[str] = set()
    for spec in value["jobs"]:
        core = {key: item for key, item in spec.items() if key != "job_spec_sha256"}
        if (
            spec.get("schema_version") != E1V2_JOB_SPEC_SCHEMA
            or spec.get("job_spec_sha256") != content_hash(core)
            or spec.get("protocol_sha256") != protocol["protocol_sha256"]
        ):
            raise ValueError("e1v2_job_spec_invalid")
        ids.add(spec["job"]["job_id"])
    if len(ids) != len(value["jobs"]):
        raise ValueError("e1v2_job_ids_not_unique")
    return value | {"bundle_sha256": claimed}


@dataclass(frozen=True, slots=True)
class V2BoundDecision:
    binding: ProviderRequestBinding
    decision: Any
    gate: Any
    result: Mapping[str, Any]
    common_context: CommonPolicyContext


def _condition_runtime(
    spec: Mapping[str, Any], level: DoFLevel
) -> tuple[UnknownRegime, Any, Any]:
    """Bind a job's pre-frozen regime to its catalogue and audit-only mask."""

    job = spec["job"]
    regime = UnknownRegime(str(job.get("regime", UnknownRegime.KNOWN.value)))
    excluded = tuple(job.get("excluded_action_keys", ()))
    if regime is UnknownRegime.KNOWN:
        if excluded:
            raise ValueError("known_regime_cannot_exclude_actions")
        catalogue = nested_action_catalogue(level)
        mask = create_mask_rules(version="dof.e1v2-known-empty-mask.v1")
    elif regime is UnknownRegime.FORCED_UNKNOWN:
        if excluded != WP012_EXCLUDED_ACTIONS:
            raise ValueError("forced_unknown_excluded_action_contract_mismatch")
        catalogue = filtered_nested_action_catalogue(
            level, excluded_action_keys=excluded
        )
        mask = create_mask_rules(
            version="dof.wp012-neutral-mask.v1",
            hidden_macro_families=("placement_routing",),
            forbidden_sentinels=(str(job["benchmark"]),),
        )
    else:
        if excluded:
            raise ValueError("natural_unknown_cannot_exclude_actions")
        catalogue = nested_action_catalogue(level)
        mask = create_mask_rules(version="dof.wp012-natural-empty-mask.v1")
    return regime, catalogue, mask


def run_bound_provider_decision_v2(
    spec: Mapping[str, Any],
    observation: Mapping[str, Any],
    decision_dir: str | Path,
    *,
    decision_index: int,
    heavy_remaining: int,
    remaining_cost_usd: float,
    credential: str | None = None,
    transport_factory: Any = OpenRouterChatCompletionsTransport,
) -> V2BoundDecision:
    if observation.get("origin") != "real_read_only_vivado":
        raise ValueError("synthetic_observation_for_real_decision_rejected")
    if observation.get("job_spec_sha256") != spec.get("job_spec_sha256"):
        raise ValueError("observation_job_binding_mismatch")
    if observation.get("observation_sha256") != _hash(
        {key: item for key, item in observation.items() if key != "observation_sha256"}
    ):
        raise ValueError("observation_hash_mismatch")
    level = DoFLevel(spec["job"]["level"])
    regime, catalogue, mask = _condition_runtime(spec, level)
    diagnostic_context = None
    if "diagnostic" in spec:
        cue = validate_diagnostic_spec(spec)
        if level is not DoFLevel.L4 or regime is not UnknownRegime.KNOWN:
            raise ValueError("g_diagnostic_requires_known_l4")
        diagnostic_context = build_g_diagnostic_policy_context(
            source=observation["source"],
            current_artifact_sha256=observation["current_artifact"]["sha256"],
            decision_index=decision_index,
            cue=cue,
        )
        catalogue = diagnostic_catalogue(cue, decision_index)
        common = diagnostic_context.common
    else:
        common = build_common_policy_context(
            level=level,
            source=observation["source"],
            current_artifact_sha256=observation["current_artifact"]["sha256"],
            decision_index=decision_index,
            regime=regime,
            catalogue=catalogue,
            mask=mask,
        )
    interfaces = interface_fingerprints_v2(level)
    binding = ProviderRequestBinding(
        run_id="e1v2-" + _hash((spec["job_spec_sha256"], decision_index))[:24],
        spec_hash=spec["job_spec_sha256"],
        experiment_id=str(spec["matrix"]).lower(),
        condition_id=spec["job"]["job_id"],
        repeat_id=f"repeat-{spec['job']['repeat']}",
        decision_id=common.decision_id,
        context_bytes=common.provider_context_bytes,
        prompt_bytes=common.provider_prompt_bytes,
        prompt_version=(
            interfaces["prompt_version"]
            if diagnostic_context is None
            else G_DIAGNOSTIC_PROTOCOL + "." + diagnostic_context.cue.value.lower()
        ),
        action_interface_version=interfaces["action_schema_version"],
        action_interface_hash=interfaces["action_schema_hash"],
        context_interface_version=interfaces["context_version"],
        context_interface_hash=interfaces["context_hash"],
        catalogue_version=catalogue.version,
        catalogue_hash=catalogue.sha256,
        capability_hash=capability_manifest_v2(level).sha256,
        mask_evidence_hash=mask.hash,
        provider_role=(
            "e1v2_policy" if diagnostic_context is None else "g_diagnostic_policy"
        ),
        provider="openrouter",
        model_id=spec["job"]["model"],
        adapter_version=OPENROUTER_ADAPTER_VERSION,
        settings=ProviderSettings(
            temperature=None,
            max_tokens=4096,
            seed=None,
            timeout_seconds=120.0,
            max_retries=0,
            retry_on=(),
            max_requests=1,
            max_usd=remaining_cost_usd,
            max_cost_per_request_usd=UNKNOWN_PROVIDER_REQUEST_RESERVE_USD,
            unknown_cost_policy="reject",
            unknown_usage_policy="reject",
        ),
    )
    output = Path(decision_dir).resolve()
    output.mkdir(parents=True, exist_ok=True)
    writer = ImmutableEvidenceWriter(output, binding.run_id, binding.spec_hash)
    outbound = build_openrouter_outbound(binding)
    writer.write_json(
        "evidence/real-observation-binding.json",
        observation,
        role="e1v2_real_observation",
        schema_version=str(observation["schema_version"]),
    )
    writer.write_json(
        "evidence/common-context.json",
        common.evidence(),
        role="e1v2_common_context",
        schema_version="dof.e1v2-common-context-evidence.v1",
    )
    if diagnostic_context is not None:
        writer.write_json(
            "evidence/g-diagnostic-context.json",
            diagnostic_context.evidence(),
            role="g_diagnostic_context",
            schema_version="dof.g-diagnostic-context-evidence.v1",
        )
    writer.write_json(
        "evidence/provider-request.json",
        {
            "schema_version": "dof.e1v2-bound-provider-request.v1",
            "binding": binding.to_evidence_dict(),
            "outbound": outbound.to_dict(),
        },
        role="e1v2_provider_request",
        schema_version="dof.e1v2-bound-provider-request.v1",
    )
    key = credential or os.environ.get("OPENROUTER_API_KEY")
    if not key:
        raise RuntimeError("openrouter_credential_unavailable")
    transport = transport_factory(api_key=key, expected_outbound_binding=outbound)
    decision = DoFProviderObserver(
        run_ledger=ProviderRunLedger.for_binding(binding)
    ).request(binding, transport)
    decision = _apply_reconciled_accounting(decision, binding, transport)
    errors = verify_provider_attempts(decision.attempts, binding)
    if errors:
        raise RuntimeError("provider_attempt_evidence_invalid:" + ";".join(errors))
    exchange = transport.last_exchange
    route = None if exchange is None else exchange.get("route")
    transport_shape_valid = (
        transport.physical_requests == 1 and transport.send_attempts == 1
    )
    success_route_valid = (
        isinstance(route, Mapping)
        and route.get("requested_model") == binding.model_id
        and route.get("returned_model") == binding.model_id
        and route.get("fallback_allowed") is False
        and route.get("attempt") == 1
    )
    failure_class = (
        exchange.get("failure_class") if isinstance(exchange, Mapping) else None
    )
    provider_error = (
        exchange.get("provider_error") if isinstance(exchange, Mapping) else None
    )
    provider_error_category = (
        provider_error.get("category")
        if isinstance(provider_error, Mapping)
        and provider_error.get("category") in OPENROUTER_PROVIDER_ERROR_CATEGORIES
        else None
    )
    transport_terminal = (
        not decision.accepted
        and bool(decision.attempts)
        and decision.attempts[-1].status
        in {
            ProviderAttemptStatus.PROVIDER_ERROR,
            ProviderAttemptStatus.TIMED_OUT,
            ProviderAttemptStatus.CANCELLED,
        }
        and transport_shape_valid
        and failure_class in _ORDINARY_TRANSPORT_FAILURE_CLASSES
    )
    guardrail_terminal = (
        transport_terminal
        and provider_error_category == "GUARDRAIL_OR_PROMPT_BLOCK"
    )
    pre_request_budget_rejection = (
        not decision.accepted
        and decision.endpoint == "BUDGET_REJECTION"
        and not decision.attempts
        and transport.physical_requests == 0
        and transport.send_attempts == 0
    )
    policy_output_terminal = (
        not decision.accepted
        and bool(decision.attempts)
        and decision.attempts[-1].status
        is ProviderAttemptStatus.POLICY_OUTPUT_REJECTED
        and transport_shape_valid
        and success_route_valid
    )
    proposal_binding_terminal = (
        policy_output_terminal
        and bool(decision.reasons)
        and set(decision.reasons).issubset(_PROPOSAL_BINDING_REASONS)
    )
    write_provider_attempt_trace(writer, decision.attempts, binding=binding)
    private_response = getattr(transport, "private_response_bytes", None)
    if isinstance(private_response, bytes):
        private_dir = output / "private"
        private_dir.mkdir(parents=True, exist_ok=True)
        private_path = private_dir / "openrouter-error-response.bin"
        with private_path.open("xb") as handle:
            handle.write(private_response)
            handle.flush()
            os.fsync(handle.fileno())
    writer.write_json(
        "evidence/openrouter-exchange.json",
        exchange if exchange is not None else {"status": "NOT_AVAILABLE"},
        role=(
            "openrouter_transport_terminal"
            if transport_terminal
            else "openrouter_exchange"
        ),
        schema_version=(
            str(exchange.get("schema_version"))
            if isinstance(exchange, Mapping)
            and isinstance(exchange.get("schema_version"), str)
            else "dof.openrouter-exchange.v1"
        ),
    )
    if failure_class == "MODEL_OR_ROUTE_DRIFT":
        raise RuntimeError("openrouter_model_or_route_drift")
    if (
        decision.accepted
        and (not transport_shape_valid or not success_route_valid)
    ) or (
        not decision.accepted
        and not transport_terminal
        and not pre_request_budget_rejection
        and not policy_output_terminal
    ):
        raise RuntimeError("openrouter_transport_evidence_invalid")
    gate = None
    if decision.accepted and decision.bound_proposal is not None:
        proposal = decision.bound_proposal.verify_for_dispatch(binding)
        gate = ActionGate().evaluate(
            proposal,
            level=level,
            expected_decision_id=binding.decision_id,
            catalogue=catalogue,
            expected_catalogue_hash=catalogue.sha256,
            current_stage=DesignStage.ROUTED,
            budget_remaining={
                event: (heavy_remaining if event is EventClass.IMPLEMENTATION_HEAVY else 8)
                for event in EventClass
            },
            mask=mask,
            capability_contract="e1v2",
        )
        if (
            diagnostic_context is not None
            and diagnostic_context.cue is GCue.C4
            and decision_index == 0
            and proposal.kind is not ProposalKind.STOP
            and (
                proposal.kind is not ProposalKind.ACTION
                or len(proposal.steps) != 1
                or proposal.steps[0].action_key != GENERATED_ACTION_KEY
            )
        ):
            gate = GateDecision(
                accepted=False,
                endpoint=EndpointCode.CAPABILITY_ACTION_REJECTION,
                reasons=("g_diagnostic_c4_requires_exactly_one_g_action_step",),
                proposal=proposal,
            )
    last = decision.attempts[-1] if decision.attempts else None
    known_cost_usd = sum(
        float(attempt.cost_usd)
        for attempt in decision.attempts
        if attempt.cost_usd is not None
    )
    reserved_unknown_cost_usd = sum(
        float(attempt.accounted_cost_usd)
        for attempt in decision.attempts
        if attempt.cost_usd is None
    )
    unknown_usage_attempt_count = sum(
        attempt.total_tokens is None for attempt in decision.attempts
    )
    decision_endpoint = (
        PROVIDER_REJECTION_ENDPOINT
        if guardrail_terminal
        else (
            PROVIDER_TRANSPORT_FAILURE_ENDPOINT
            if transport_terminal
            else decision.endpoint
        )
    )
    diagnostic_proposal = (
        decision.bound_proposal.proposal
        if decision.bound_proposal is not None
        else None
    )
    raw_policy_bytes = b""
    if last is not None:
        try:
            raw_policy_bytes = base64.b64decode(last.raw_response_bytes_b64, validate=True)
        except (ValueError, TypeError):
            raw_policy_bytes = b""
    g_proposed = bool(
        diagnostic_proposal is not None
        and any(step.action_key == GENERATED_ACTION_KEY for step in diagnostic_proposal.steps)
    )
    result = {
        "schema_version": "dof.e1v2-bound-provider-decision.v1",
        "decision_id": binding.decision_id,
        "request_contract_hash": binding.request_contract_hash,
        "base_observation_hash": common.base_observation_hash,
        "common_prompt_core_hash": common.common_prompt_core_hash,
        "capability_appendix_hash": common.capability_appendix_hash,
        "context_sha256": binding.context_hash,
        "prompt_sha256": binding.prompt_hash,
        "endpoint": decision_endpoint,
        "policy_output_subtype": (
            "PROPOSAL_BINDING_MISMATCH" if proposal_binding_terminal else None
        ),
        "reasons": list(decision.reasons),
        "provider_accepted": decision.accepted,
        "provider_transport_accounting_accepted": bool(
            decision.accepted or policy_output_terminal
        ),
        "proposal_dispatchable": decision.accepted,
        "gate": None if gate is None else gate.to_dict(),
        "raw_response_sha256": None if last is None else last.raw_response_hash,
        "cost_usd": None if last is None else last.cost_usd,
        "actual_cost_usd": None if last is None else last.cost_usd,
        "reserved_unknown_cost_usd": reserved_unknown_cost_usd,
        "known_cost_usd": known_cost_usd,
        "unknown_cost_attempt_count": sum(
            attempt.cost_usd is None for attempt in decision.attempts
        ),
        "cost_lower_bound_usd": known_cost_usd,
        "cost_upper_bound_usd": known_cost_usd + reserved_unknown_cost_usd,
        "input_tokens": None if last is None else last.input_tokens,
        "output_tokens": None if last is None else last.output_tokens,
        "total_tokens": None if last is None else last.total_tokens,
        "known_token_total": sum(
            int(attempt.total_tokens or 0) for attempt in decision.attempts
        ),
        "unknown_usage_attempt_count": unknown_usage_attempt_count,
        "usage_status": (
            "NOT_APPLICABLE"
            if last is None
            else ("UNKNOWN" if last.total_tokens is None else "EXACT")
        ),
        "cost_status": (
            "NOT_APPLICABLE"
            if last is None
            else ("UNKNOWN_RESERVED" if last.cost_usd is None else "EXACT")
        ),
        "transport_failure_class": failure_class,
        "provider_error_category": provider_error_category,
        "provider_global_breaker": (
            provider_error_category in _IMMEDIATE_GLOBAL_PROVIDER_CATEGORIES
        ),
        "provider_route": OPENROUTER_E1_PROFILES[binding.model_id]["endpoint_tag"],
        "transport_terminal": transport_terminal,
        "proposal_created": decision.bound_proposal is not None,
        "action_gate_entered": gate is not None,
        "vivado_dispatches_after_decision": 0,
        "route": route,
        "physical_requests": transport.physical_requests,
        "retries": 0,
        "fallbacks": 0,
        "repairs": 0,
    }
    if diagnostic_context is not None:
        result["g_diagnostic"] = diagnostic_context.evidence() | {
            "g_mentioned": GENERATED_ACTION_KEY.encode("utf-8") in raw_policy_bytes,
            "g_proposed": g_proposed,
            "proposal_schema_and_binding_valid": decision.accepted,
            "g_gate_admitted": bool(
                g_proposed and gate is not None and gate.accepted
            ),
            "zero_backend_dispatch_at_provider_boundary": True,
        }
    result = result | {"decision_result_sha256": content_hash(result)}
    writer.write_json(
        "evidence/provider-decision.json",
        result,
        role="e1v2_bound_provider_decision",
        schema_version="dof.e1v2-bound-provider-decision.v1",
    )
    return V2BoundDecision(binding, decision, gate, result, common)


def _causal_evidence(
    spec: Mapping[str, Any], observation: Mapping[str, Any], bound: V2BoundDecision
) -> dict[str, Any]:
    if bound.gate is None or not bound.gate.accepted or not bound.gate.gated_steps:
        raise ValueError("accepted_gated_steps_required")
    proposal = bound.decision.bound_proposal
    if proposal is None:
        raise ValueError("bound_provider_proposal_required")
    payload = {
        "schema_version": "dof.e1v2-causal-decision-evidence.v1",
        "policy_source": "openrouter_common_observation",
        "job_spec_sha256": spec["job_spec_sha256"],
        "current_artifact": observation["current_artifact"],
        "observation_sha256": observation["observation_sha256"],
        "base_observation_hash": bound.common_context.base_observation_hash,
        "common_prompt_core_hash": bound.common_context.common_prompt_core_hash,
        "context_sha256": bound.binding.context_hash,
        "decision_id": bound.binding.decision_id,
        "request_contract_hash": bound.binding.request_contract_hash,
        "raw_response_sha256": proposal.raw_response_hash,
        "proposal_sha256": proposal.parsed_proposal_hash,
        "gate_decision_sha256": _hash(bound.gate.to_dict()),
    }
    return payload | {"causal_evidence_sha256": content_hash(payload)}


def _g_diagnostic_utilisation(
    decisions: Sequence[Mapping[str, Any]], cue: GCue
) -> dict[str, Any]:
    sequence: list[dict[str, Any]] = []
    g_decisions: list[int] = []
    g_executed_decisions: list[int] = []
    g_valid_child_decisions: list[int] = []
    g_programs: list[dict[str, Any]] = []
    g_mentioned = False
    g_proposed = False
    g_admitted = False
    for index, row in enumerate(decisions):
        provider = row.get("provider", {})
        diagnostic = provider.get("g_diagnostic", {}) if isinstance(provider, Mapping) else {}
        g_mentioned = g_mentioned or diagnostic.get("g_mentioned") is True
        g_proposed = g_proposed or diagnostic.get("g_proposed") is True
        g_admitted = g_admitted or diagnostic.get("g_gate_admitted") is True
        gate = provider.get("gate") if isinstance(provider, Mapping) else None
        actions = []
        if isinstance(gate, Mapping):
            for step in gate.get("gated_steps", ()):
                if isinstance(step, Mapping):
                    actions.append(
                        {
                            "action_key": step.get("action_key"),
                            "parameters": step.get("parameters"),
                        }
                    )
        sequence.append(
            {
                "decision_index": index,
                "actions": actions,
                "endpoint": provider.get("endpoint") if isinstance(provider, Mapping) else None,
                "actual_heavy_calls": int(row.get("actual_heavy_calls", 0) or 0),
                "actual_light_mutation_calls": int(
                    row.get("actual_light_mutation_calls", 0) or 0
                ),
                "valid_child": bool(
                    isinstance(row.get("action_result"), Mapping)
                    and isinstance(row["action_result"].get("transaction"), Mapping)
                    and row["action_result"]["transaction"].get("valid") is True
                ),
                "scored_child": isinstance(row.get("child_score"), Mapping),
            }
        )
        generated = row.get("generated_interventions", ())
        if generated:
            g_decisions.append(index)
            g_programs.extend(dict(item) for item in generated if isinstance(item, Mapping))
            if int(row.get("actual_light_mutation_calls", 0) or 0) > 0:
                g_executed_decisions.append(index)
            action_result = row.get("action_result")
            transaction = (
                action_result.get("transaction", {})
                if isinstance(action_result, Mapping)
                else {}
            )
            if isinstance(transaction, Mapping) and transaction.get("valid") is True:
                g_valid_child_decisions.append(index)
    first_g = min(g_decisions) if g_decisions else None
    implementation_after_g = []
    scored_after_g = []
    if first_g is not None:
        implementation_after_g = [
            item["decision_index"]
            for item in sequence
            if item["decision_index"] > first_g and item["actual_heavy_calls"] > 0
        ]
        scored_after_g = [
            item["decision_index"]
            for item in sequence
            if item["decision_index"] >= first_g and item["scored_child"]
        ]
    required = cue in {GCue.C3, GCue.C4}
    payload = {
        "schema_version": "dof.g-diagnostic-utilisation.v1",
        "cue": cue.value,
        "g_available": True,
        "g_mentioned": g_mentioned,
        "g_proposed": g_proposed,
        "g_gate_admitted": g_admitted,
        "g_executed": bool(g_executed_decisions),
        "g_valid_child": bool(g_valid_child_decisions),
        "g_followed_by_implementation": bool(implementation_after_g),
        "g_scored_outcome": bool(scored_after_g),
        "first_g_decision_index": first_g,
        "first_g_decision_number": None if first_g is None else first_g + 1,
        "g_decision_indices": g_decisions,
        "g_executed_decision_indices": g_executed_decisions,
        "g_valid_child_decision_indices": g_valid_child_decisions,
        "implementation_after_g_decision_indices": implementation_after_g,
        "scored_after_g_decision_indices": scored_after_g,
        "g_programs": g_programs,
        "action_sequence": sequence,
        "instruction_compliance_required": required,
        "instruction_complied": None if not required else g_admitted,
    }
    return payload | {"utilisation_sha256": content_hash(payload)}


def execute_real_e1v2_job(
    spec: Mapping[str, Any],
    root_dcp: str | Path,
    output_dir: str | Path,
    *,
    vivado_exec: str | Path,
    python_exec: str | Path,
    repository_anchor: str | Path,
    scorer_script: str | Path,
    fake_startup_attestation_sha256: str,
    credential: str | None = None,
    transport_factory: Any = OpenRouterChatCompletionsTransport,
) -> dict[str, Any]:
    start = time.monotonic()
    output = Path(output_dir).resolve()
    output.mkdir(parents=True, exist_ok=False)
    level = DoFLevel(spec["job"]["level"])
    root = fingerprint_file(Path(root_dcp).resolve())
    if (root.sha256, root.size_bytes) != (
        spec["root"]["sha256"],
        spec["root"]["size_bytes"],
    ):
        raise RuntimeError("ROOT_IDENTITY_BLOCKER")
    root_candidate = {
        "source": "root",
        "artifact": {"sha256": root.sha256, "size_bytes": root.size_bytes},
        "score": spec["root"]["score"],
        "valid": True,
        "heavy_runtime_seconds": 0.0,
        "completion_order": 0,
    }
    candidates = [root_candidate]
    decisions: list[dict[str, Any]] = []
    history: list[dict[str, Any]] = []
    heavy_used = 0
    light_mutation_used = 0
    design_mutated = False
    model_requests = 0
    provider_known_cost = 0.0
    provider_reserved_unknown_cost = 0.0
    provider_global_breaker = False
    provider_error_category: str | None = None
    provider_route: str | None = None
    unknown_cost_attempts = 0
    known_token_total = 0
    unknown_usage_attempts = 0
    endpoint = "VALID_COMPLETION"
    policy_output_subtype: str | None = None
    reasons: list[str] = []
    if level is DoFLevel.L0:
        pass
    elif level is DoFLevel.L1:
        observation = capture_real_observation(
            spec,
            root_dcp,
            output / "root-observation",
            decision_index=0,
            history=[],
            heavy_remaining=int(spec["budgets"]["heavy_calls"]),
            model_requests_remaining=0,
            vivado_exec=vivado_exec,
            python_exec=python_exec,
            repository_anchor=repository_anchor,
            frozen_timing_score=spec["root"]["score"],
            e1_protocol=True,
        )
        l1_actions = tuple(spec["job"].get("l1_actions", E1_L1_ACTIONS))
        if not l1_actions or not set(l1_actions) <= set(E1_L1_ACTIONS):
            raise ValueError("invalid_l1_action_portfolio")
        if spec["job"].get("regime") == "forced_unknown" and (
            set(l1_actions) & set(WP012_EXCLUDED_ACTIONS)
        ):
            raise ValueError("forced_unknown_l1_contains_excluded_action")
        for completion_order, action_key in enumerate(l1_actions, start=1):
            arm_dir = output / "l1-arms" / action_key.replace(".", "-")
            run_dir = arm_dir / "action-run"
            causal = _l1_causal(spec, observation, action_key)
            plan = freeze_e0_real_action_plan(
                root_dcp,
                root_pseudonym=spec["job"]["benchmark"],
                level=level,
                action_key=action_key,
                parameters={},
                run_id="e1v2-l1-" + _hash((spec["job_spec_sha256"], action_key))[:20],
                repository_anchor=repository_anchor,
                python_exec=python_exec,
                vivado_exec=vivado_exec,
                run_dir=run_dir,
                fake_startup_attestation_sha256=fake_startup_attestation_sha256,
                protocol="e1",
            )
            write_e0_frozen_plan(plan, arm_dir / "materialized-plan.json")
            authorization = build_e0_dispatch_authorization(plan, causal)
            _write_json(arm_dir / "causal-evidence.json", causal)
            _write_json(arm_dir / "dispatch-authorization.json", authorization)
            arm_start = time.monotonic()
            mutation = execute_e0_real_action(
                plan,
                root_dcp,
                run_dir,
                dispatch_authorization=authorization,
                causal_evidence=causal,
                vivado_exec=vivado_exec,
                python_exec=python_exec,
                repository_anchor=repository_anchor,
            )
            arm_wall = time.monotonic() - arm_start
            heavy = int(mutation.get("real_mutation_invocations", 0))
            light = int(
                mutation.get("budget", {}).get("used", {}).get("MUTATION_LIGHT", 0)
            )
            heavy_used += heavy
            light_mutation_used += light
            design_mutated = design_mutated or bool(heavy or light)
            transaction = mutation.get("transaction") or {}
            valid = bool(mutation.get("endpoint_success") and transaction.get("valid"))
            row: dict[str, Any] = {
                "action_key": action_key,
                "causal_evidence_sha256": causal["causal_evidence_sha256"],
                "authorization_sha256": authorization["authorization_sha256"],
                "action_result": mutation,
                "actual_heavy_calls": heavy,
                "actual_light_mutation_calls": light,
                "wall_seconds": arm_wall,
                "valid": valid,
            }
            if valid:
                child = run_dir / "artifacts" / "sha256" / f"{transaction['sha256']}.dcp"
                scored = _score_child(
                    child,
                    spec,
                    arm_dir / "child-score",
                    vivado_exec=vivado_exec,
                    scorer_script=scorer_script,
                )
                row["child_score"] = scored
                candidates.append(
                    {
                        "source": action_key,
                        "artifact": scored["artifact"],
                        "score": scored["score"],
                        "valid": True,
                        "heavy_runtime_seconds": arm_wall,
                        "completion_order": completion_order,
                    }
                )
            decisions.append(row)
        if l1_actions == E1_L1_ACTIONS:
            shortfall = _reconcile_l1_actual_heavy_calls(decisions, heavy_used)
        else:
            expected_heavy = len(l1_actions)
            if heavy_used > expected_heavy or (
                all(decision.get("valid") is True for decision in decisions)
                and heavy_used != expected_heavy
            ):
                raise RuntimeError("wp012_l1_actual_heavy_call_count_mismatch")
            shortfall = (
                None
                if heavy_used == expected_heavy
                else "l1_declared_heavy_calls_short_circuited_by_arm_failure"
            )
        if shortfall:
            reasons.append(shortfall)
    else:
        current_dcp = Path(root_dcp).resolve()
        current_score = spec["root"]["score"]
        for decision_index in range(4):
            if time.monotonic() - start >= float(spec["budgets"]["trajectory_wall_seconds"]):
                endpoint = "TRAJECTORY_TIMEOUT"
                reasons.append("trajectory_wall_budget_exhausted")
                break
            decision_root = output / f"decision-{decision_index:02d}"
            observation = capture_real_observation(
                spec,
                current_dcp,
                decision_root / "observation",
                decision_index=decision_index,
                history=history,
                heavy_remaining=max(0, 4 - heavy_used),
                model_requests_remaining=max(0, 4 - model_requests),
                vivado_exec=vivado_exec,
                python_exec=python_exec,
                repository_anchor=repository_anchor,
                frozen_timing_score=current_score,
                e1_protocol=True,
            )
            bound = run_bound_provider_decision_v2(
                spec,
                observation,
                decision_root / "provider",
                decision_index=decision_index,
                heavy_remaining=max(0, 4 - heavy_used),
                remaining_cost_usd=max(
                    0.0,
                    float(spec["budgets"]["provider_cost_usd"])
                    - provider_known_cost
                    - provider_reserved_unknown_cost,
                ),
                credential=credential,
                transport_factory=transport_factory,
            )
            model_requests += int(bound.result["physical_requests"])
            provider_known_cost += float(bound.result["known_cost_usd"])
            provider_reserved_unknown_cost += float(
                bound.result["reserved_unknown_cost_usd"]
            )
            unknown_cost_attempts += int(
                bound.result["unknown_cost_attempt_count"]
            )
            known_token_total += int(bound.result["known_token_total"])
            unknown_usage_attempts += int(
                bound.result["unknown_usage_attempt_count"]
            )
            provider_global_breaker = provider_global_breaker or bool(
                bound.result.get("provider_global_breaker")
            )
            provider_error_category = bound.result.get("provider_error_category")
            provider_route = bound.result.get("provider_route")
            decision_row: dict[str, Any] = {
                "provider": dict(bound.result),
                "common_context": bound.common_context.evidence(),
                "dispatches": 0,
            }
            decisions.append(decision_row)
            if not bound.decision.accepted:
                endpoint = str(bound.result["endpoint"])
                policy_output_subtype = bound.result.get("policy_output_subtype")
                reasons.extend(bound.decision.reasons)
                break
            proposal = bound.decision.bound_proposal.proposal
            if proposal.kind is ProposalKind.STOP:
                endpoint = "POLICY_STOP"
                reasons.append("provider_returned_stop")
                break
            if bound.gate is None or not bound.gate.accepted or not bound.gate.gated_steps:
                endpoint = "CAPABILITY_ACTION_REJECTION"
                reasons.extend(() if bound.gate is None else bound.gate.reasons)
                break
            causal = _causal_evidence(spec, observation, bound)
            canonical_actions = [
                {"action_key": step.action_key, "parameters": dict(step.parameters)}
                for step in bound.gate.gated_steps
            ]
            decision_row["repeated_action_diagnostic"] = repeated_action_diagnostic(
                history, canonical_actions
            )
            generated_rows = [
                generated_program_evidence(action["parameters"])
                for action in canonical_actions
                if action["action_key"] == GENERATED_ACTION_KEY
            ]
            if generated_rows:
                decision_row["generated_interventions"] = generated_rows
                _write_json(
                    decision_root / "dispatch" / "generated-interventions.json",
                    {
                        "schema_version": "dof.e1v2-generated-interventions.v1",
                        "rows": generated_rows,
                    },
                )
            plan_dir = decision_root / "dispatch"
            run_dir = decision_root / "action-run"
            plan = freeze_e0_real_gated_plan(
                current_dcp,
                root_pseudonym=spec["job"]["benchmark"],
                level=level,
                decision_id=bound.binding.decision_id,
                proposal_id=proposal.proposal_id,
                proposal_hash=proposal.proposal_hash,
                gate_decision_sha256=_hash(bound.gate.to_dict()),
                gated_steps=_gated_step_rows(bound.gate),
                causal_evidence_sha256=causal["causal_evidence_sha256"],
                run_id="e1v2-action-"
                + _hash((spec["job_spec_sha256"], decision_index, proposal.proposal_hash))[:20],
                repository_anchor=repository_anchor,
                python_exec=python_exec,
                vivado_exec=vivado_exec,
                run_dir=run_dir,
                fake_startup_attestation_sha256=fake_startup_attestation_sha256,
                protocol="e1v2",
            )
            write_e0_gated_plan(plan, plan_dir / "materialized-plan.json")
            authorization = build_e0_dispatch_authorization(plan, causal)
            _write_json(plan_dir / "causal-evidence.json", causal)
            _write_json(plan_dir / "dispatch-authorization.json", authorization)
            validate_e0_dispatch_authorization(plan, authorization, causal)
            action_start = time.monotonic()
            mutation = execute_e0_real_gated_plan(
                plan,
                current_dcp,
                run_dir,
                dispatch_authorization=authorization,
                causal_evidence=causal,
                vivado_exec=vivado_exec,
                python_exec=python_exec,
                repository_anchor=repository_anchor,
            )
            action_wall = time.monotonic() - action_start
            heavy = int(mutation.get("real_mutation_invocations", 0))
            light = int(
                mutation.get("budget", {}).get("used", {}).get("MUTATION_LIGHT", 0)
            )
            heavy_used += heavy
            light_mutation_used += light
            design_mutated = design_mutated or bool(heavy or light)
            decision_row.update(
                {
                    "dispatches": int(bool(mutation.get("backend_dispatches"))),
                    "causal_evidence_sha256": causal["causal_evidence_sha256"],
                    "authorization_sha256": authorization["authorization_sha256"],
                    "action_result": mutation,
                    "actual_heavy_calls": heavy,
                    "actual_light_mutation_calls": light,
                    "action_wall_seconds": action_wall,
                }
            )
            transaction = mutation.get("transaction") or {}
            valid = bool(mutation.get("endpoint_success") and transaction.get("valid"))
            child_score = None
            if valid:
                child = run_dir / "artifacts" / "sha256" / f"{transaction['sha256']}.dcp"
                child_score = _score_child(
                    child,
                    spec,
                    decision_root / "child-score",
                    vivado_exec=vivado_exec,
                    scorer_script=scorer_script,
                )
                decision_row["child_score"] = child_score
            history.append(
                action_aware_history_row(
                    decision_index=decision_index,
                    actions=canonical_actions,
                    parent_qor=current_score,
                    child_qor={} if child_score is None else child_score["score"],
                    valid=valid,
                    endpoint=mutation.get("endpoint", "UNKNOWN"),
                    actual_heavy_calls=heavy,
                )
            )
            if not valid:
                endpoint = mutation.get("endpoint", "TOOL_EXECUTION_FAILURE")
                reasons.extend(mutation.get("reasons", []))
                break
            assert child_score is not None
            current_dcp = child
            current_score = child_score["score"]
            candidates.append(
                {
                    "source": f"decision-{decision_index:02d}",
                    "artifact": child_score["artifact"],
                    "score": child_score["score"],
                    "valid": True,
                    "heavy_runtime_seconds": action_wall,
                    "completion_order": decision_index + 1,
                }
            )
            if heavy_used >= 4 or model_requests >= 4:
                reasons.append("trajectory_budget_exhausted")
                break
    best = _choose_best(candidates)
    final_root = fingerprint_file(Path(root_dcp).resolve())
    root_unchanged = (final_root.sha256, final_root.size_bytes) == (root.sha256, root.size_bytes)
    if not root_unchanged:
        raise RuntimeError("ROOT_MUTATED_IN_PLACE")
    diagnostic_cue = (
        validate_diagnostic_spec(spec) if "diagnostic" in spec else None
    )
    result = {
        "schema_version": (
            E1V2_TRAJECTORY_RESULT_SCHEMA
            if diagnostic_cue is None
            else G_DIAGNOSTIC_RESULT_SCHEMA
        ),
        "protocol_version": spec["protocol_version"],
        "matrix": spec["matrix"],
        "study_status": spec["study_status"],
        "job": spec["job"],
        "job_spec_sha256": spec["job_spec_sha256"],
        "endpoint": endpoint,
        "policy_output_subtype": policy_output_subtype,
        "reasons": reasons,
        "decisions": decisions,
        "history": history,
        "model_requests": model_requests,
        "provider_cost_usd": provider_known_cost,
        "known_cost_usd": provider_known_cost,
        "unknown_cost_attempt_count": unknown_cost_attempts,
        "reserved_unknown_cost_usd": provider_reserved_unknown_cost,
        "cost_lower_bound_usd": provider_known_cost,
        "cost_upper_bound_usd": (
            provider_known_cost + provider_reserved_unknown_cost
        ),
        "known_token_total": known_token_total,
        "unknown_usage_attempt_count": unknown_usage_attempts,
        "provider_global_breaker": provider_global_breaker,
        "provider_error_category": provider_error_category,
        "provider_route": provider_route,
        "heavy_calls": heavy_used,
        "light_mutation_calls": light_mutation_used,
        "design_mutated": design_mutated,
        "selection_candidates": candidates,
        "best_valid_selection": best,
        "root_unchanged": root_unchanged,
        "wall_seconds": time.monotonic() - start,
    }
    if diagnostic_cue is not None:
        result["diagnostic"] = {
            "protocol": G_DIAGNOSTIC_PROTOCOL,
            "cue": diagnostic_cue.value,
            "primary_aggregate_eligible": False,
            "sealed_holdouts_accessed": False,
            "utilisation": _g_diagnostic_utilisation(decisions, diagnostic_cue),
        }
    result = result | {"result_sha256": content_hash(result)}
    _write_json(output / "trajectory-result.json", result)
    return result


__all__ = [
    "V2BoundDecision",
    "execute_real_e1v2_job",
    "freeze_e1v2_job_bundle",
    "run_bound_provider_decision_v2",
    "validate_e1v2_job_bundle",
]
