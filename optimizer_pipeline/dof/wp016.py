"""Promoted compact-protocol and evidence helpers for WP-016.

Frozen schema-version strings retain their accepted R8 provenance; the active
module and controller namespace is WP-016.

This module deliberately has no network or EDA entry point.  It turns immutable
WP-015 records into versioned, provenance-bearing *draft* views and provides the
small deterministic interfaces needed by the fork's unit tests.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
import copy
from dataclasses import dataclass
import hashlib
import json
import math
import random
from typing import Any, Callable

from .single_submission.action_surface_v3 import P_EXCLUSIVE_OPERATIONS
from .single_submission.handles import HandleError, PrivateObject
from .single_submission.language import CompiledProgram
from .named_family import (
    NAMED_FAMILY_SEMANTICS_VERSION, NAMED_FAMILY_SEMANTICS_VERSION_V2,
    named_family_evidence_v1, named_family_evidence_v2,
)


PREFLIGHT_LABEL = "PREFLIGHT_DRYRUN_NOT_FINAL_ANALYSIS"
DERIVATION_VERSION = "wp015-fork01.analysis-v2-draft.1"
RETRIEVAL_VERSION = "wp015-fork01.balanced-retrieval-v0.1"
RETRIEVAL_FRAGMENTATION_VERSION = "wp016a.generic-json-boundary-fragmentation.v1"
SCHEMA_FRAGMENT_TARGET_BYTES = 2_048
RETRIEVAL_FRONTIER_SEMANTICS = "RETRIEVABLE_DESCENDANT_FRONTIER"
ELIGIBILITY_VERSION = "wp015-fork01.eligibility-v0.1"

BOUNDARY_CLASSES = {
    "M_ONLY_EXECUTION",
    "P_DECLARED_NOT_EXERCISED",
    "P_EXCLUSIVE_EXECUTED",
    "G_DECLARED_NOT_EXERCISED",
    "G_EXCLUSIVE_EXECUTED",
    "AMBIGUOUS_BOUNDARY",
}

FUNCTIONAL_STATUSES = {
    "FUNCTIONAL_PASS",
    "FUNCTIONAL_MISMATCH",
    "VALIDATOR_UNSUPPORTED",
    "VALIDATOR_INFRASTRUCTURE_FAILURE",
    "VALIDATOR_TIMEOUT",
    "VALIDATION_NOT_RUN",
    "FUNCTIONAL_UNKNOWN",
}

OUTCOME_KEY_FRAGMENTS = (
    "outcome", "terminal", "delta", "qor", "fmax", "wns", "tns", "score",
    "valid", "winner", "improvement", "child", "execution", "functional",
)


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def sha256(value: bytes | Any) -> str:
    raw = value if isinstance(value, bytes) else canonical_bytes(value)
    return hashlib.sha256(raw).hexdigest()


def proposal_boundary_certificate_v1(
    candidate: Mapping[str, Any], compiled: CompiledProgram,
) -> dict[str, Any]:
    """Certify a *compiled proposal* boundary without implying execution.

    This deliberately requires a source-step-to-host-step path.  Merely
    mentioning a P/G construct, producing an unused binding, or decorating an
    M/P-reachable intervention cannot satisfy the certificate.
    """

    language = candidate.get("language")
    steps = candidate.get("steps")
    steps = steps if isinstance(steps, list) else []
    candidate_steps = {
        str(row.get("step_id")): row for row in steps
        if isinstance(row, Mapping) and isinstance(row.get("step_id"), str)
    }
    bindings = {
        str(row.get("binding_id")): dict(row) for row in compiled.binding_records
        if isinstance(row, Mapping) and row.get("binding_id")
    }
    hosts = [step.public() for step in compiled.host_steps]
    exclusive_hosts = [
        row for row in hosts
        if row.get("source_step_id") in candidate_steps
        and isinstance(row.get("source_construct"), str)
        and row.get("source_construct") != "CALL_MACRO"
    ]
    scoreable = compiled.final_stage == "ROUTED" and compiled.heavy_calls >= 1
    family: str | None = None
    paths: list[dict[str, Any]] = []
    checks: list[dict[str, Any]] = []
    reason: str | None = None

    def step(op: str) -> Mapping[str, Any] | None:
        return next((row for row in steps if isinstance(row, Mapping) and row.get("op") == op), None)

    def ref_at(row: Mapping[str, Any] | None, key: str) -> str | None:
        value = ((row or {}).get("args") or {}).get(key)
        return str(value.get("ref")) if isinstance(value, Mapping) and isinstance(value.get("ref"), str) else None

    valid = False
    if language == "P":
        p_hosts = [
            row for row in exclusive_hosts
            if row.get("source_construct") in P_EXCLUSIVE_OPERATIONS
        ]
        valid = bool(p_hosts and scoreable)
        exclusive_hosts = p_hosts
        family = "P_COMPILED_EXCLUSIVE_HOST_OPERATION" if valid else None
        if not valid:
            reason = "no_compiled_p_exclusive_host_step" if not p_hosts else "compiled_intervention_not_scoreable"
    elif language == "G":
        diff, batch = step("DIFF"), step("BATCH_ROUTE_NETS")
        region, assign = step("CREATE_REGION"), step("ASSIGN_REGION")
        couple, apply = step("COUPLE_SITES"), step("APPLY_ALLOCATION")
        if diff is not None and batch is not None:
            produced = diff.get("bind")
            consumed = ref_at(batch, "typed_nets")
            record = bindings.get(str(produced), {})
            consumer = [
                row for row in hosts
                if row.get("source_step_id") == batch.get("step_id")
                and row.get("source_construct") == "BATCH_ROUTE_NETS"
                and any(item.get("binding_id") == produced for item in row.get("input_bindings", []))
            ]
            cardinality = int(record.get("binding_cardinality") or 0)
            checks.append({"binding_id": produced, "kind": "DIFF_OUTPUT", "observed": cardinality, "minimum": 2,
                           "passed": cardinality >= 2})
            paths.append({"producer_step_id": diff.get("step_id"), "producer_construct": "DIFF",
                          "binding_id": produced, "consumer_step_id": batch.get("step_id"),
                          "consumer_construct": "BATCH_ROUTE_NETS", "exact_binding_consumed": produced == consumed,
                          "compiled_consumer_present": bool(consumer)})
            valid = bool(produced and produced == consumed and cardinality >= 2 and consumer and scoreable)
            family = "NONPREFIX_MULTI_NET_BATCH" if valid else None
            reason = None if valid else "diff_binding_not_consumed_by_compiled_batch"
        elif region is not None and assign is not None:
            produced = region.get("bind")
            consumed = ref_at(assign, "region_handle")
            target = ref_at(assign, "target_handle")
            args = region.get("args") or {}
            coords_ok = all(isinstance(args.get(key), int) and not isinstance(args.get(key), bool)
                            for key in ("x_min", "x_max", "y_min", "y_max"))
            nondegenerate = bool(coords_ok and (
                int(args["x_max"]) > int(args["x_min"])
                or int(args["y_max"]) > int(args["y_min"])
            ))
            consumer = [
                row for row in hosts
                if row.get("source_step_id") == assign.get("step_id")
                and row.get("source_construct") == "ASSIGN_REGION"
                and {item.get("binding_id") for item in row.get("input_bindings", [])} >= {produced, target}
            ]
            checks.append({"binding_id": produced, "kind": "GENERATED_REGION_NONDEGENERATE",
                           "observed": list(args.get(key) for key in ("x_min", "x_max", "y_min", "y_max")),
                           "passed": nondegenerate})
            paths.append({"producer_step_id": region.get("step_id"), "producer_construct": "CREATE_REGION",
                          "binding_id": produced, "consumer_step_id": assign.get("step_id"),
                          "consumer_construct": "ASSIGN_REGION", "exact_binding_consumed": produced == consumed,
                          "target_binding_id": target, "compiled_consumer_present": bool(consumer)})
            valid = bool(produced and target and produced == consumed and nondegenerate and consumer and scoreable)
            family = "SPATIAL_GENERATED_GEOMETRY" if valid else None
            reason = None if valid else "generated_region_not_nondegenerate_and_consumed"
        elif couple is not None and apply is not None:
            produced = couple.get("bind")
            consumed = ref_at(apply, "allocation")
            record = bindings.get(str(produced), {})
            matching = int(record.get("matching_size") or 0)
            consumer = [
                row for row in hosts
                if row.get("source_step_id") == apply.get("step_id")
                and row.get("source_construct") == "APPLY_ALLOCATION"
                and any(item.get("binding_id") == produced for item in row.get("input_bindings", []))
            ]
            checks.append({"binding_id": produced, "kind": "COUPLED_ALLOCATION_MATCHING",
                           "observed": matching, "minimum": 2, "passed": matching >= 2})
            paths.append({"producer_step_id": couple.get("step_id"), "producer_construct": "COUPLE_SITES",
                          "binding_id": produced, "consumer_step_id": apply.get("step_id"),
                          "consumer_construct": "APPLY_ALLOCATION", "exact_binding_consumed": produced == consumed,
                          "compiled_consumer_present": bool(consumer)})
            valid = bool(produced and produced == consumed and matching >= 2 and consumer and scoreable)
            family = "COUPLED_HARDBLOCK_SITE_ALLOCATION" if valid else None
            reason = None if valid else "allocation_not_matched_and_consumed"
        else:
            reason = "no_admitted_sufficient_g_proposal_family"
    else:
        reason = "candidate_language_not_P_or_G"

    core = {
        "schema_version": "wp015-fork01.proposal-boundary-certificate.v1",
        "capability": language,
        "proposal_boundary_valid": bool(valid),
        "proposal_mechanism_family": family,
        "candidate_constructs": [str(row.get("op")) for row in steps if isinstance(row, Mapping)],
        "compiled_exclusive_host_steps": exclusive_hosts,
        "producer_consumer_binding_paths": paths,
        "cardinality_checks": checks,
        "noncertificate_reason": None if valid else reason,
        "candidate_sha256": sha256(candidate),
        "compiled_ir_sha256": compiled.public_sha256,
        "runtime_boundary_exercised": "NOT_MEASURED_IN_C1",
        "functional_validity": "NOT_MEASURED_IN_C1",
        "QoR": "NOT_MEASURED_IN_C1",
    }
    return core | {"certificate_sha256": sha256(core)}


def proposal_boundary_certificate_v2(
    candidate: Mapping[str, Any], compiled: CompiledProgram,
) -> dict[str, Any]:
    """Post-hoc proposal certificate using the shared named-family core."""

    language = candidate.get("language")
    scoreable = compiled.final_stage == "ROUTED" and compiled.heavy_calls >= 1
    hosts = [step.public() for step in compiled.host_steps]
    family = named_family_evidence_v1(candidate, compiled)
    p_hosts = [row for row in hosts if row.get("source_construct") in P_EXCLUSIVE_OPERATIONS
               and row.get("source_construct") != "CALL_MACRO"
               and isinstance(row.get("source_step_id"), str)]
    if language == "P":
        valid = bool(scoreable and p_hosts)
        mechanism = "P_COMPILED_EXCLUSIVE_HOST_OPERATION" if valid else None
        reason = None if valid else (
            "compiled_intervention_not_scoreable" if p_hosts else "no_compiled_p_exclusive_host_step"
        )
        accepted = []
        selected = None
    elif language == "G":
        valid = bool(scoreable and family["named_family_valid"])
        mechanism = family["selected_mechanism_family"] if valid else None
        reason = None if valid else (
            "compiled_intervention_not_scoreable" if family["named_family_valid"]
            else "no_complete_admitted_named_family_path"
        )
        accepted = family["accepted_path_hashes"]
        selected = family["selected_certificate_path_hash"]
    else:
        valid, mechanism, reason, accepted, selected = False, None, "candidate_language_not_P_or_G", [], None
    core = {
        "schema_version": "wp015-fork01.proposal-boundary-certificate.v2",
        "named_family_semantics_version": NAMED_FAMILY_SEMANTICS_VERSION,
        "capability": language,
        "proposal_boundary_valid": valid,
        "proposal_mechanism_family_v2": mechanism,
        "compiled_exclusive_host_steps": p_hosts if language == "P" else [],
        "all_evaluated_paths": family["all_evaluated_paths"] if language == "G" else [],
        "accepted_path_hashes": accepted,
        "rejected_path_reasons": family["rejected_path_reasons"] if language == "G" else [],
        "selected_certificate_path_hash": selected,
        "noncertificate_reason": reason,
        "candidate_sha256": sha256(candidate), "compiled_ir_sha256": compiled.public_sha256,
        "runtime_boundary_exercised": "NOT_MEASURED_IN_C1",
        "physical_validity": "NOT_MEASURED_IN_C1",
        "functional_validity": "NOT_MEASURED_IN_C1", "QoR": "NOT_MEASURED_IN_C1",
    }
    return core | {"certificate_sha256": sha256(core)}


def proposal_boundary_certificate_v3(
    candidate: Mapping[str, Any], compiled: CompiledProgram,
) -> dict[str, Any]:
    """Post-terminal proposal certificate with recomputed v4 family proof."""

    language = candidate.get("language")
    scoreable = compiled.final_stage == "ROUTED" and compiled.heavy_calls >= 1
    hosts = [step.public() for step in compiled.host_steps]
    family = named_family_evidence_v2(candidate, compiled)
    p_hosts = [row for row in hosts if row.get("source_construct") in P_EXCLUSIVE_OPERATIONS
               and row.get("source_construct") != "CALL_MACRO"
               and isinstance(row.get("source_step_id"), str)]
    if language == "P":
        valid = bool(scoreable and p_hosts)
        mechanism = "P_COMPILED_EXCLUSIVE_HOST_OPERATION" if valid else None
        reason = None if valid else (
            "compiled_intervention_not_scoreable" if p_hosts else "no_compiled_p_exclusive_host_step"
        )
    elif language == "G":
        valid = bool(scoreable and family["named_family_valid"])
        mechanism = family["selected_mechanism_family"] if valid else None
        reason = None if valid else (
            "compiled_intervention_not_scoreable" if family["named_family_valid"]
            else "no_complete_recomputed_named_family_path"
        )
    else:
        valid, mechanism, reason = False, None, "candidate_language_not_P_or_G"
    core = {
        "schema_version": "wp015-fork01.proposal-boundary-certificate.v3",
        "named_family_semantics_version": NAMED_FAMILY_SEMANTICS_VERSION_V2,
        "capability": language,
        "proposal_named_family_valid_commit_v4": valid,
        "proposal_boundary_valid": valid,
        "proposal_mechanism_family_v4": mechanism,
        "compiled_exclusive_host_steps": p_hosts if language == "P" else [],
        "all_evaluated_paths": family["all_evaluated_paths"] if language == "G" else [],
        "accepted_path_hashes": family["accepted_path_hashes"] if language == "G" else [],
        "rejected_path_reasons": family["rejected_path_reasons"] if language == "G" else [],
        "selected_certificate_path_hash": family["selected_certificate_path_hash"] if language == "G" else None,
        "noncertificate_reason": reason,
        "candidate_sha256": sha256(candidate), "compiled_ir_sha256": compiled.public_sha256,
        "certificate_timing": "POST_TERMINAL_ONLY_NOT_MODEL_VISIBLE_PRECOMMIT",
        "runtime_boundary_exercised": "NOT_MEASURED_IN_C1",
        "physical_validity": "NOT_MEASURED_IN_C1",
        "functional_validity": "NOT_MEASURED_IN_C1", "QoR": "NOT_MEASURED_IN_C1",
    }
    return core | {"certificate_sha256": sha256(core)}


def provenance(
    value: Any,
    *,
    rule_id: str,
    source_event_ids: Sequence[str],
    source_hashes: Sequence[str],
    confidence: str,
) -> dict[str, Any]:
    return {
        "value": value,
        "provenance_class": "DERIVED",
        "derivation_rule_id": rule_id,
        "derivation_rule_version": DERIVATION_VERSION,
        "source_event_ids": list(source_event_ids),
        "source_hashes": list(source_hashes),
        "confidence_or_ambiguity": confidence,
        "label": PREFLIGHT_LABEL,
    }


def _expected_worker_intersection(job: Mapping[str, Any]) -> set[str]:
    evidence = job.get("boundary_evidence")
    if not isinstance(evidence, Mapping):
        return set()
    expected = set(evidence.get("expected_exclusive_worker_operations") or ())
    observed = set(evidence.get("observed_worker_operations") or ())
    return expected & observed


def strict_boundary_class(job: Mapping[str, Any]) -> tuple[str, str]:
    """Reconstruct exclusive use without trusting condition labels or prose.

    A strict G certificate is the strongest existing causal record.  P requires
    an observed worker operation matching the pre-dispatch exclusive-operation
    expectation.  Wrapper invocation, language assignment, or a selector-only
    construct is not sufficient.
    """

    language = job.get("selected_language")
    certificate_v4 = job.get("strict_boundary_certificate_v4")
    if language == "G" and isinstance(certificate_v4, Mapping):
        if certificate_v4.get("G_CAUSAL_RUNTIME_EXECUTED") is True:
            return "G_EXCLUSIVE_EXECUTED", "strict_g_v4_causal_runtime_certificate"
        return "G_DECLARED_NOT_EXERCISED", "strict_g_v4_no_causal_runtime_execution"
    certificate = job.get("strict_boundary_certificate")
    if language == "G":
        if isinstance(certificate, Mapping) and certificate.get("strict_boundary_exercised") is True:
            return "G_EXCLUSIVE_EXECUTED", "strict_g_certificate"
        if job.get("boundary_runtime_exercised") is True and not isinstance(certificate, Mapping):
            return "AMBIGUOUS_BOUNDARY", "runtime_flag_without_strict_g_certificate"
        return "G_DECLARED_NOT_EXERCISED", "no_strict_g_certificate"
    if language == "P":
        intersection = _expected_worker_intersection(job)
        if intersection:
            return "P_EXCLUSIVE_EXECUTED", "observed_expected_exclusive_worker_operation"
        evidence = job.get("boundary_evidence")
        observed = evidence.get("observed_worker_operations") if isinstance(evidence, Mapping) else None
        if job.get("boundary_runtime_exercised") is True and observed:
            return "AMBIGUOUS_BOUNDARY", "runtime_worker_evidence_not_in_expected_set"
        return "P_DECLARED_NOT_EXERCISED", "no_exclusive_worker_crossing"
    if language == "M":
        return "M_ONLY_EXECUTION", "selected_m"
    return "AMBIGUOUS_BOUNDARY", "missing_or_unknown_selected_language"


def named_family_class_v4(job: Mapping[str, Any]) -> tuple[str, str]:
    """Classify named-family evidence without conflating causal reachability."""

    certificate = job.get("strict_boundary_certificate_v4")
    if not isinstance(certificate, Mapping):
        return "G_NAMED_FAMILY_UNKNOWN", "v4_certificate_missing"
    if certificate.get("G_NAMED_FAMILY_PROVEN_V4") is True:
        return "G_NAMED_FAMILY_PROVEN_V4", "recomputed_named_family_certificate"
    if certificate.get("G_CAUSAL_NAMED_FAMILY_UNPROVEN") is True:
        return "G_CAUSAL_NAMED_FAMILY_UNPROVEN", "causal_runtime_without_narrow_family_proof"
    return "G_NAMED_FAMILY_NOT_PROVEN", "v4_named_family_and_causal_evidence_absent"


def boundary_stage_and_class(job: Mapping[str, Any]) -> tuple[str, str | None, str]:
    """Separate absence of an executable submission from evidence ambiguity."""

    if job.get("selected_language") is None and job.get("canonical_terminal") in {
        "NO_VALID_SUBMISSION", "PROVIDER_FAILURE",
    }:
        return "NO_EXECUTABLE_SUBMISSION", None, "no_selected_language"
    klass, reason = strict_boundary_class(job)
    return "EXECUTABLE_SUBMISSION", klass, reason


ASSIGNMENT_FUNNEL_ENDPOINTS = (
    "valid_executable_submission",
    "context_or_limit_terminal",
    "boundary_not_reached",
    "any_classified_execution",
    "M_only_execution",
    "P_exclusive_realisation",
    "G_exclusive_realisation",
    "execution_attempted",
    "tool_successful_completion",
    "tool_failure",
    "timeout",
    "artifact_produced",
    "physical_validity",
    "preparation_complete",
    "precheck_complete",
    "functional_validity",
)


def assignment_funnel_endpoints(
    derived_job: Mapping[str, Any],
    source_job: Mapping[str, Any],
    validation_receipt: Mapping[str, Any] | None,
) -> dict[str, bool | None]:
    """Return assignment-level events without conflating submission and execution.

    The function is intentionally descriptive.  In particular, a selected
    language or terminal label alone cannot establish an execution attempt;
    runtime evidence must contain an attempt/process/call trace.  Functional
    validity remains unknown until the frozen five-seed validator completes.
    """

    boundary = derived_job.get("boundary_execution_class")
    stage = derived_job.get("boundary_stage_status")
    terminal = str(derived_job.get("canonical_terminal") or "")
    turns = source_job.get("turns") if isinstance(source_job.get("turns"), Sequence) else ()
    failures = " ".join(
        str(turn.get("failure_class") or turn.get("error") or "")
        for turn in turns if isinstance(turn, Mapping)
    ).upper()
    execution = source_job.get("execution") if isinstance(source_job.get("execution"), Mapping) else {}
    attempted = bool(
        execution.get("attempt_id")
        or execution.get("process")
        or execution.get("call_trace")
        or execution.get("outer_exit_code") is not None
    )
    artifact = bool(
        execution.get("child")
        or execution.get("child_path")
        or execution.get("output_artifact_id")
    )
    preparation = bool(
        validation_receipt and validation_receipt.get("preparation_status") == "COMPLETE"
    )
    precheck = bool(
        validation_receipt and validation_receipt.get("precheck_status") == "PASS"
    )
    functional = None
    if validation_receipt:
        status = normalize_functional_status(validation_receipt.get("functional_validation_status"))
        if status == "FUNCTIONAL_PASS":
            functional = True
        elif status == "FUNCTIONAL_MISMATCH":
            functional = False
    return {
        "valid_executable_submission": stage == "EXECUTABLE_SUBMISSION",
        "context_or_limit_terminal": any(token in failures for token in ("CONTEXT", "LIMIT", "TOKEN")),
        "boundary_not_reached": stage != "EXECUTABLE_SUBMISSION",
        "any_classified_execution": boundary in {
            "M_ONLY_EXECUTION", "P_EXCLUSIVE_EXECUTED", "G_EXCLUSIVE_EXECUTED",
        },
        "M_only_execution": boundary == "M_ONLY_EXECUTION",
        "P_exclusive_realisation": boundary == "P_EXCLUSIVE_EXECUTED",
        "G_exclusive_realisation": boundary == "G_EXCLUSIVE_EXECUTED",
        "execution_attempted": attempted,
        "tool_successful_completion": terminal == "VALID_COMPLETION",
        "tool_failure": terminal == "TOOL_FAILURE",
        "timeout": terminal == "TIMEOUT",
        "artifact_produced": artifact,
        "physical_validity": terminal == "VALID_COMPLETION",
        "preparation_complete": preparation,
        "precheck_complete": precheck,
        "functional_validity": functional,
    }


def normalize_functional_status(value: Any) -> str:
    aliases = {
        "MULTISEED_FUNCTIONAL_PASS": "FUNCTIONAL_PASS",
        "FUNCTIONAL_PRECHECK_MISMATCH": "FUNCTIONAL_MISMATCH",
        "FUNCTIONAL_VALIDATION_INFRASTRUCTURE_FAILURE": "VALIDATOR_INFRASTRUCTURE_FAILURE",
        "VALIDATOR_INFRASTRUCTURE_FAILURE": "VALIDATOR_INFRASTRUCTURE_FAILURE",
        "PENDING_V1": "FUNCTIONAL_UNKNOWN",
        None: "VALIDATION_NOT_RUN",
    }
    status = aliases.get(value, value)
    return status if status in FUNCTIONAL_STATUSES else "FUNCTIONAL_UNKNOWN"


DELTA_DERIVATION_VERSION = "wp015.canonical-timing-delta.v2"


def finite_number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def fmax_from_period_wns(period: Any, wns: Any) -> float | None:
    period_value, wns_value = finite_number(period), finite_number(wns)
    if period_value is None or wns_value is None or period_value - wns_value <= 0:
        return None
    return 1000.0 / (period_value - wns_value)


def canonical_timing(job: Mapping[str, Any], root: Mapping[str, Any]) -> dict[str, Any]:
    """Use one root-relative scorer regime for both root and child.

    ``constraint_period_ns`` describes the implemented clock constraint, while
    ``effective_scoring_period_ns`` describes the frozen scorer conversion.
    They are normally equal, but Titan23's direct scorer receipts establish a
    4x relation.  A per-job pre-timing report is deliberately *not* used as the
    root baseline: post-intervention wrappers may emit an intermediate pre
    report that is already different from the frozen root scorer state.
    """

    execution = job.get("execution") if isinstance(job.get("execution"), Mapping) else {}
    pre = execution.get("pre_timing") if isinstance(execution.get("pre_timing"), Mapping) else {}
    post = execution.get("post_timing") if isinstance(execution.get("post_timing"), Mapping) else {}
    initial_wns = finite_number(root.get("observation_baseline_wns_ns"))
    initial_wns = initial_wns if initial_wns is not None else finite_number(root.get("baseline_wns_ns"))
    reported_initial_wns = finite_number(execution.get("initial_wns_ns"))
    initial_wns = initial_wns if initial_wns is not None else reported_initial_wns
    final_wns = finite_number(execution.get("final_wns_ns"))
    initial_wns = initial_wns if initial_wns is not None else finite_number(pre.get("wns"))
    final_wns = final_wns if final_wns is not None else finite_number(post.get("wns"))
    constraint_period = finite_number(root.get("constraint_period_ns"))
    constraint_period = constraint_period if constraint_period is not None else finite_number(root.get("target_period_ns"))
    constraint_period = constraint_period if constraint_period is not None else finite_number(root.get("period_ns"))
    effective_period = finite_number(root.get("effective_scoring_period_ns"))
    effective_period = effective_period if effective_period is not None else constraint_period
    reported_initial = finite_number(execution.get("initial_fmax_mhz"))
    reported_final = finite_number(execution.get("final_fmax_mhz"))
    initial = finite_number(root.get("root_scorer_baseline_fmax_mhz"))
    initial = initial if initial is not None else reported_initial
    initial = initial if initial is not None else finite_number(root.get("baseline_fmax_mhz"))
    initial = initial if initial is not None else fmax_from_period_wns(effective_period, initial_wns)
    final = reported_final if reported_final is not None else fmax_from_period_wns(effective_period, final_wns)
    score_source = (
        "REPORTED_ROOT_AND_CHILD_SCORER"
        if reported_initial is not None and reported_final is not None
        else "DERIVED_ROOT_AND_CHILD_FROM_EFFECTIVE_SCORER_PERIOD"
    )
    timing_contract = {
        "root_id": root.get("root_id"), "target_clock": root.get("target_clock"),
        "constraint_period_ns": constraint_period,
        "effective_scoring_period_ns": effective_period,
        "clock_scale_or_parallelism_factor": root.get("clock_scale_or_parallelism_factor"),
        "clock_scale_semantics": root.get("clock_scale_semantics"),
        "target_period_evidence": root.get("target_period_evidence"),
        "source_evidence_sha256": root.get("timing_contract_evidence_sha256"),
        "baseline_observation_sha256": root.get("baseline_observation_sha256"),
    }
    timing_evidence = {
        "initial_fmax_mhz": initial, "final_fmax_mhz": final,
        "root_scorer_baseline_fmax_mhz": initial, "child_scorer_fmax_mhz": final,
        "reported_initial_fmax_mhz": reported_initial, "reported_final_fmax_mhz": reported_final,
        "initial_wns_ns": initial_wns, "final_wns_ns": final_wns,
        "pre_timing": pre, "post_timing": post,
    }
    return {
        **{key: timing_evidence[key] for key in (
            "initial_fmax_mhz", "final_fmax_mhz", "initial_wns_ns", "final_wns_ns"
        )},
        "delta_fmax_mhz": final - initial if initial is not None and final is not None else None,
        "scorer_contract_source": score_source,
        "constraint_period_ns": constraint_period,
        "effective_scoring_period_ns": effective_period,
        "root_scorer_baseline_fmax_mhz": initial,
        "child_scorer_fmax_mhz": final,
        "timing_contract_hash": sha256(timing_contract),
        "timing_evidence_hash": sha256(timing_evidence),
        "delta_derivation_rule_version": DELTA_DERIVATION_VERSION,
    }


def scientific_delta(
    job: Mapping[str, Any], root: Mapping[str, Any] | None = None, *, tolerance_mhz: float = 1e-9,
) -> dict[str, Any]:
    root_hash = job.get("root_sha256")
    child_hash = job.get("resolved_child_sha256")
    execution = job.get("execution")
    reported = finite_number(execution.get("fmax_improvement_mhz")) if isinstance(execution, Mapping) else None
    identity = bool(root_hash and child_hash and root_hash == child_hash)
    physical_valid = job.get("canonical_terminal") == "VALID_COMPLETION"
    root_fallback = identity and not physical_valid
    timing = canonical_timing(job, root) if root is not None else None
    derived = timing.get("delta_fmax_mhz") if timing else reported
    preliminary = None
    drift = None
    exact = False
    source = "UNAVAILABLE_WITH_REASON"
    unavailable_reason = "NOT_PHYSICAL_VALID" if not physical_valid else "MISSING_TIMING_EVIDENCE"
    if physical_valid and derived is not None:
        preliminary = float(derived)
        unavailable_reason = None
        if reported is not None:
            disagreement = abs(float(reported) - preliminary)
            if disagreement > tolerance_mhz:
                raise ValueError(f"UNEXPLAINED_REPORTED_TIMING_DELTA_DISAGREEMENT:{job.get('job_id')}:{disagreement}")
            source = "REPORTED_DERIVED_CROSSCHECKED"
        else:
            source = (
                str(timing.get("scorer_contract_source"))
                if timing is not None else "DERIVED_ROOT_AND_CHILD_FROM_EFFECTIVE_SCORER_PERIOD"
            )
    if physical_valid and identity:
        if derived is None:
            raise ValueError(f"EXACT_ZERO_WITHOUT_RAW_TIMING_DELTA:{job.get('job_id')}")
        preliminary = 0.0
        drift = derived
        exact = True
        source = "EXACT_ZERO_OVERRIDE"
    return {
        "artifact_identity_equal": identity,
        "root_fallback_identity": root_fallback,
        "physical_signed_delta_preliminary": preliminary,
        "scorer_remeasurement_drift": drift,
        "exact_zero_scientific_delta_applied": exact,
        "physical_delta_source": source,
        "raw_remeasured_delta_before_exact_zero": derived if physical_valid else None,
        "timing_contract_hash": timing.get("timing_contract_hash") if timing else None,
        "timing_evidence_hash": timing.get("timing_evidence_hash") if timing else None,
        "delta_derivation_rule_version": DELTA_DERIVATION_VERSION,
        "physical_delta_unavailable_reason": unavailable_reason,
    }


def utility_estimands(
    job: Mapping[str, Any],
    *,
    functional_status: str | None = None,
    root: Mapping[str, Any] | None = None,
    intact_root_fallback_proven: bool = False,
    post_score_fallback_allowed: bool = False,
) -> dict[str, Any]:
    status = normalize_functional_status(
        functional_status if functional_status is not None else job.get("functional_terminal")
    )
    delta = scientific_delta(job, root)["physical_signed_delta_preliminary"]
    passed = status == "FUNCTIONAL_PASS" and isinstance(delta, (int, float))
    known = status in {"FUNCTIONAL_PASS", "FUNCTIONAL_MISMATCH"}
    signed = float(delta) if passed else None
    blind = None
    if known and intact_root_fallback_proven:
        blind = float(delta) if passed else 0.0
    oracle = None
    if known and post_score_fallback_allowed:
        oracle = max(0.0, float(delta)) if passed else 0.0
    return {
        "functional_validation_status": status,
        "scientific_child_effect_signed": signed,
        "blind_commit_policy_utility": blind,
        "oracle_fallback_deployment_utility": oracle,
        "oracle_is_optimistic_post_score_estimand": True,
    }


def _fragment_token(value: Any) -> str:
    return str(value).replace("~", "~0").replace("/", "~1")


def _retrieval_result(content_id: str, entry: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "status": "RETRIEVAL_OK",
        "content_id": content_id,
        "content": entry["content"],
        "content_sha256": entry["content_sha256"],
    }


def fragment_retrieval_registry(
    registry: Mapping[str, Any], *, protocol: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Apply one generic JSON object/path fragmentation rule.

    Existing public entries remain globally discoverable.  Recursively derived
    schema children are model-usable but discovered hierarchically from their
    parent, preventing a flat schema index from consuming the low-reserve arm.
    No trajectory-, model-, capability-, or QoR-specific split is used.
    """

    result = copy.deepcopy(dict(registry))
    entries = result.get("entries")
    if not isinstance(entries, dict):
        raise ValueError("retrieval_registry_entries_required")

    # Rebuilding an already-normalised registry is deterministic: discard only
    # fragments created by this generic rule, while retaining pre-existing
    # reviewed public leaves.
    for content_id in list(entries):
        entry = entries[content_id]
        if isinstance(entry, Mapping) and entry.get("fragment_parent_content_id"):
            del entries[content_id]

    if protocol is not None:
        protocol_ids = {
            f"SCHEMA/M/protocol/{direction}/{label}"
            for direction, group in (
                ("request", protocol["request_schemas"]),
                ("response", protocol["response_schemas_by_status"]),
            )
            for label in group
        }
        for content_id in list(entries):
            if content_id.startswith((
                "SCHEMA/M/protocol/request/", "SCHEMA/M/protocol/response/",
            )) and content_id not in protocol_ids:
                del entries[content_id]
        for direction, group in (
            ("request", protocol["request_schemas"]),
            ("response", protocol["response_schemas_by_status"]),
        ):
            for label, schema in group.items():
                content_id = f"SCHEMA/M/protocol/{direction}/{label}"
                entries[content_id] = {
                    "capability": "M", "content": copy.deepcopy(schema),
                    "content_sha256": sha256(schema), "model_usable": True,
                    "discoverable": True,
                }

    seeds = [
        (content_id, copy.deepcopy(entry["content"]))
        for content_id, entry in list(entries.items())
        if content_id.startswith("SCHEMA/")
        and isinstance(entry, Mapping)
        and entry.get("model_usable", True) is True
        and "content" in entry
    ]

    def visit(root_id: str, parent_id: str, node: Any) -> None:
        parent_entry = entries[parent_id]
        parent_size = len(canonical_bytes(_retrieval_result(parent_id, parent_entry)))
        if parent_size <= SCHEMA_FRAGMENT_TARGET_BYTES:
            return
        children: Iterable[tuple[Any, Any]]
        if isinstance(node, Mapping):
            children = node.items()
        elif isinstance(node, list):
            children = enumerate(node)
        else:
            return
        for key, child in children:
            child_id = parent_id + "/" + _fragment_token(key)
            existing = entries.get(child_id)
            if existing is not None:
                if existing.get("content_sha256") != sha256(child):
                    raise ValueError("schema_fragment_content_collision:" + child_id)
                existing.setdefault("discoverable", True)
                existing.setdefault("model_usable", True)
            else:
                entries[child_id] = {
                    "capability": entries[root_id]["capability"],
                    "content": copy.deepcopy(child),
                    "content_sha256": sha256(child),
                    "model_usable": True,
                    "discoverable": False,
                    "fragment_root_content_id": root_id,
                    "fragment_parent_content_id": parent_id,
                }
            visit(root_id, child_id, child)

    for content_id, content in seeds:
        entries[content_id].setdefault("discoverable", True)
        entries[content_id].setdefault("model_usable", True)
        visit(content_id, content_id, content)

    for content_id, entry in entries.items():
        if not isinstance(entry, dict) or "content" not in entry:
            continue
        entry.setdefault("model_usable", True)
        entry.setdefault("discoverable", True)
        entry["retrieval_size_bytes"] = len(canonical_bytes(_retrieval_result(content_id, entry)))

    for content_id, entry in entries.items():
        if not content_id.startswith("SCHEMA/") or not isinstance(entry, dict):
            continue
        prefix = content_id + "/"
        children = sorted(
            child_id for child_id, child in entries.items()
            if child_id.startswith(prefix)
            and "/" not in child_id[len(prefix):]
            and isinstance(child, Mapping)
            and child.get("model_usable") is True
        )
        entry["child_fragment_ids"] = children

    result["schema_version"] = "wp016a.closed-registry.v2"
    result["fragmentation"] = {
        "version": RETRIEVAL_FRAGMENTATION_VERSION,
        "rule": "size-aware recursive JSON mapping-key/list-index boundary",
        "target_fragment_bytes": SCHEMA_FRAGMENT_TARGET_BYTES,
        "generated_children_globally_discoverable": False,
        "rejected_oversize_items_are_partially_injected": False,
    }
    result.pop("registry_sha256", None)
    result["registry_sha256"] = sha256(result)
    return result


def make_retrieval_registry(full_contract: Mapping[str, Any]) -> dict[str, Any]:
    """Losslessly split the frozen L4 contract into content-addressed entries."""

    entries: dict[str, Any] = {}

    def add_schema_fragments(capability: str, name: str, value: Any) -> None:
        def visit(path: tuple[str, ...], node: Any) -> None:
            content_id = "/".join(("SCHEMA", capability, name, *path))
            entries[content_id] = {
                "capability": capability,
                "content": node,
                "content_sha256": sha256(node),
            }
            if isinstance(node, Mapping):
                for key, child in node.items():
                    visit((*path, _fragment_token(key)), child)
            elif isinstance(node, list):
                for index, child in enumerate(node):
                    visit((*path, str(index)), child)
        visit((), value)

    for capability in ("M", "P", "G"):
        if capability not in full_contract:
            continue
        content = full_contract[capability]
        entries[f"CAPABILITY/{capability}"] = {
            "capability": capability,
            "content": content,
            "content_sha256": sha256(content),
        }
        cards = content.get("cards") if isinstance(content, Mapping) else None
        if isinstance(cards, Mapping) and cards.get("encoding") == "ordered_rows.v1":
            fields, rows = cards.get("field_order"), cards.get("rows")
            if isinstance(fields, list) and isinstance(rows, list):
                for row in rows:
                    card = dict(zip(fields, row, strict=True))
                    action_id = card.get("key")
                    if not action_id:
                        # P/G cards use the operation/node name as the first field.
                        action_id = row[0]
                    entries[f"ACTION/{capability}/{action_id}"] = {
                        "capability": capability,
                        "content": card,
                        "content_sha256": sha256(card),
                    }
        for name in ("candidate_schema", "program_schema", "$defs"):
            fragment = content.get(name) if isinstance(content, Mapping) else None
            if fragment is not None:
                add_schema_fragments(capability, name, fragment)
    examples = full_contract.get("examples") or {}
    example_rows = examples.items() if isinstance(examples, Mapping) else enumerate(examples)
    for label, example in example_rows:
        candidate = example.get("candidate") if isinstance(example, Mapping) else None
        capability = str(candidate.get("language", "M")) if isinstance(candidate, Mapping) else "M"
        entries[f"EXAMPLE/{capability}/{label}"] = {
            "capability": capability,
            "content": example,
            "content_sha256": sha256(example),
        }
    registry = {
        "schema_version": RETRIEVAL_VERSION,
        "semantics_source": "contract_for_level(L4)",
        "entries": entries,
    }
    return fragment_retrieval_registry(registry)


def retrieve(
    registry: Mapping[str, Any], capability_mask: Sequence[str], content_id: str,
) -> dict[str, Any]:
    entries = registry.get("entries")
    if not isinstance(entries, Mapping) or content_id not in entries:
        return {"status": "RETRIEVAL_NOT_FOUND", "content_id": content_id}
    entry = entries[content_id]
    capability = entry.get("capability") if isinstance(entry, Mapping) else None
    if capability not in set(capability_mask):
        return {"status": "CAPABILITY_UNAVAILABLE", "content_id": content_id}
    return {
        "status": "RETRIEVAL_OK",
        "content_id": content_id,
        "content": entry["content"],
        "content_sha256": entry["content_sha256"],
    }


COMPACT_TERMINAL_STATUSES = (
    "MALFORMED_RETRIEVAL",
    "RETRIEVAL_NOT_FOUND",
    "CAPABILITY_UNAVAILABLE",
    "SEMANTIC_RESERVE_EXHAUSTED",
    "QUERY_COUNT_EXHAUSTED",
    "QUERY_RESPONSE_TRUNCATED",
    "TURN_LIMIT_EXHAUSTED",
    "OUTPUT_LIMIT_EXCEEDED",
    "MALFORMED_OUTPUT_ACCOUNTING",
    "MULTIPLE_COMMIT_REJECTED",
    "PROGRAM_VALIDATION_REQUIRED",
)

COMPACT_RECOVERABLE_STATUSES = (
    "RETRIEVAL_ITEM_TOO_LARGE",
)

NO_VALID_SUBMISSION_REASONS = (
    "INSUFFICIENT_VISIBLE_EVIDENCE",
    "NO_CAPABILITY_ELIGIBLE_TARGET",
    "CANNOT_CONSTRUCT_SCHEMA_VALID_PROGRAM",
    "DYNAMIC_BUDGET_EXHAUSTED",
)


def _closed_schema(properties: Mapping[str, Any], required: Sequence[str]) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": dict(properties),
        "required": list(required),
        "additionalProperties": False,
    }


def compact_protocol_definition() -> dict[str, Any]:
    """Canonical compact protocol used by both presentation and validation.

    P/G program schemas remain content-addressed registry documents.  The
    endpoint schemas refer to those executable definitions rather than copying
    a divergent prose summary into the initial compact packet.
    """

    endpoint = {"type": "string"}
    def id_schema(prefix: str) -> dict[str, Any]:
        return {"type": "string", "pattern": f"^{prefix}/(M|P|G)/.+$"}
    program = {
        "type": "object",
        "description": (
            "Must validate against the executable registry schema identified by "
            "SCHEMA/P/program_schema or SCHEMA/G/program_schema."
        ),
    }
    requests = {
        "LIST_CAPABILITIES": _closed_schema({"endpoint": {"const": "LIST_CAPABILITIES"}}, ["endpoint"]),
        "GET_ACTION_SPEC": _closed_schema(
            {"endpoint": {"const": "GET_ACTION_SPEC"}, "content_id": id_schema("ACTION")},
            ["endpoint", "content_id"],
        ),
        "GET_SCHEMA_FRAGMENT": _closed_schema(
            {"endpoint": {"const": "GET_SCHEMA_FRAGMENT"}, "content_id": id_schema("SCHEMA")},
            ["endpoint", "content_id"],
        ),
        "GET_EXAMPLE": _closed_schema(
            {"endpoint": {"const": "GET_EXAMPLE"}, "content_id": id_schema("EXAMPLE")},
            ["endpoint", "content_id"],
        ),
        "QUERY_DESIGN": _closed_schema(
            {
                "endpoint": {"const": "QUERY_DESIGN"},
                "query_id": {"type": "string"},
                "arguments": {"type": "object"},
            },
            ["endpoint", "query_id", "arguments"],
        ),
        "VALIDATE_PROGRAM": _closed_schema(
            {"endpoint": {"const": "VALIDATE_PROGRAM"}, "program": program},
            ["endpoint", "program"],
        ),
        "COMMIT": _closed_schema(
            {"endpoint": {"const": "COMMIT"}, "program": program},
            ["endpoint", "program"],
        ),
        "NO_VALID_SUBMISSION": _closed_schema(
            {
                "endpoint": {"const": "NO_VALID_SUBMISSION"},
                "reason_code": {"enum": list(NO_VALID_SUBMISSION_REASONS)},
            },
            ["endpoint", "reason_code"],
        ),
    }
    common_response = {
        "status": {"type": "string"},
        "remaining_bytes": {"type": "integer", "minimum": 0},
        "remaining_queries": {"type": "integer", "minimum": 0},
        "remaining_turns": {"type": "integer", "minimum": 0},
    }
    response_schemas = {
        "OK": _closed_schema(
            common_response | {
                "consumed_bytes": {"type": "integer", "minimum": 0},
                "content": {}, "content_id": {"type": "string"},
                "content_sha256": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
            },
            ["status", "consumed_bytes", "remaining_bytes", "remaining_queries", "remaining_turns"],
        ),
        "COMMIT_RECEIVED": _closed_schema(
            common_response | {
                "program": program,
                "program_sha256": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
            },
            ["status", "program", "program_sha256", "remaining_bytes", "remaining_queries", "remaining_turns"],
        ),
        "NO_VALID_SUBMISSION_RECEIVED": _closed_schema(
            common_response | {"reason_code": {"enum": list(NO_VALID_SUBMISSION_REASONS)}},
            ["status", "reason_code", "remaining_bytes", "remaining_queries", "remaining_turns"],
        ),
    }
    response_schemas["RETRIEVAL_OK"] = response_schemas["OK"] | {
        "properties": response_schemas["OK"]["properties"] | {"status": {"const": "RETRIEVAL_OK"}},
    }
    error_schema = _closed_schema(
        common_response | {
            "reason": {"type": "string"}, "endpoint": endpoint,
            "content_id": {"type": "string"}, "query_id": {"type": "string"},
            "required_bytes": {"type": "integer", "minimum": 0},
            "cap_bytes": {"type": "integer", "minimum": 0},
            "query_count": {"type": "integer", "minimum": 0},
            "query_cap": {"type": "integer", "minimum": 0},
            "turn_count": {"type": "integer", "minimum": 0},
            "turn_cap": {"type": "integer", "minimum": 0},
            "observed_tokens": {"type": "integer", "minimum": 0},
            "output_cap_tokens": {"type": "integer", "minimum": 0},
            "current_turn_output_tokens": {"type": "integer", "minimum": 0},
            "cumulative_output_tokens": {"type": "integer", "minimum": 0},
            "per_request_output_cap": {"type": "integer", "minimum": 0},
            "session_output_cap": {"type": "integer", "minimum": 0},
            "commit_count": {"type": "integer", "minimum": 0},
            "terminal_already_recorded": {"type": "string"},
            "program_sha256": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
        },
        ["status", "remaining_bytes", "remaining_queries", "remaining_turns"],
    )
    for status in COMPACT_TERMINAL_STATUSES:
        response_schemas[status] = error_schema | {
            "properties": error_schema["properties"] | {"status": {"const": status}},
        }
    response_schemas["RETRIEVAL_ITEM_TOO_LARGE"] = _closed_schema(
        common_response | {
            "status": {"const": "RETRIEVAL_ITEM_TOO_LARGE"},
            "frontier_semantics": {"const": RETRIEVAL_FRONTIER_SEMANTICS},
            "requested_content_id": {"type": "string"},
            "requested_size_bytes": {"type": "integer", "minimum": 1},
            "requested_content_sha256": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
            "maximum_retrievable_bytes": {"type": "integer", "minimum": 0},
            "requested_content_bytes_charged": {"const": 0},
            "frontier_metadata_bytes": {"type": "integer", "minimum": 1},
            "consumed_bytes": {"type": "integer", "minimum": 1},
            "available_child_fragments": {
                "type": "array",
                "items": _closed_schema({
                    "content_id": {"type": "string"},
                    "size_bytes": {"type": "integer", "minimum": 1},
                    "retrievable_now": {"type": "boolean"},
                }, ["content_id", "size_bytes", "retrievable_now"]),
            },
        },
        [
            "status", "frontier_semantics", "requested_content_id", "requested_size_bytes",
            "requested_content_sha256", "maximum_retrievable_bytes",
            "requested_content_bytes_charged", "frontier_metadata_bytes",
            "consumed_bytes", "available_child_fragments", "remaining_bytes",
            "remaining_queries", "remaining_turns",
        ],
    )
    return {
        "schema_version": "wp016a.compact-protocol.v3",
        "optimisation_objective": (
            "Preserve mandatory validity and maximise final valid Fmax under one committed intervention."
        ),
        "capability_mask_semantics": (
            "Only advertised content within capability_mask may be retrieved or submitted; "
            "inherited M does not satisfy P-exclusive and P-reachable does not satisfy strict G."
        ),
        "content_id_grammar": {
            "action": "ACTION/<capability>/<key>",
            "schema": "SCHEMA/<capability>/<path>",
            "example": "EXAMPLE/<capability>/<name>",
            "discovery": (
                "LIST_CAPABILITIES returns the globally discoverable content index with exact "
                "retrieval sizes; oversize schema responses return a metered, lexicographically "
                "ordered RETRIEVABLE_DESCENDANT_FRONTIER."
            ),
            "schema_fragmentation_rule": (
                "recursively split fragments larger than 2048 bytes at direct JSON "
                "mapping-key/list-index boundaries"
            ),
            "capability_spec_endpoint": None,
        },
        "request_schemas": requests,
        "response_schemas_by_status": response_schemas,
        "endpoint_success_status": {
            "LIST_CAPABILITIES": "OK", "GET_ACTION_SPEC": "RETRIEVAL_OK",
            "GET_SCHEMA_FRAGMENT": "RETRIEVAL_OK", "GET_EXAMPLE": "RETRIEVAL_OK",
            "QUERY_DESIGN": "OK", "VALIDATE_PROGRAM": "OK",
            "COMMIT": "COMMIT_RECEIVED",
            "NO_VALID_SUBMISSION": "NO_VALID_SUBMISSION_RECEIVED",
        },
        "recoverable_statuses": list(COMPACT_RECOVERABLE_STATUSES),
        "reachable_error_statuses": [
            *COMPACT_RECOVERABLE_STATUSES, *COMPACT_TERMINAL_STATUSES,
        ],
        "limits": {
            "turn_cap": 4, "query_cap": 8, "history_cap_bytes": 8192,
            "query_result_cap_bytes": 4096, "output_tokens_per_request": 4096,
            "output_tokens_per_session": 16384,
            "terminal_cap": 1, "provider_guard_bytes": 65_536,
        },
        "terminal_rule": {
            "exactly_one_of": ["COMMIT", "NO_VALID_SUBMISSION"],
            "post_terminal_request_accepted": False,
            "post_commit_repair": False,
        },
        "firewall": {
            "single_submission": True, "precommit_qor_feedback": False,
            "offline_construction_feedback_only": True, "backend_calls": False,
        },
    }


def compact_model_visible_protocol(
    definition: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Lossless index projection of the canonical executable definition.

    The large schemas are frozen in ``compact-protocol-schema.json`` and
    content-addressed.  The initial packet exposes their exact hashes, endpoint
    names, terminal semantics, and retrieval locations, avoiding a second
    hand-written protocol summary.
    """

    source = dict(definition or compact_protocol_definition())
    return {
        "schema_version": "wp016a.model-visible-protocol.v3",
        "canonical_definition_sha256": sha256(source),
        "optimisation_objective": source["optimisation_objective"],
        "capability_mask_semantics": source["capability_mask_semantics"],
        "content_id_grammar": source["content_id_grammar"],
        "endpoints": source["endpoint_success_status"],
        "request_schema_index_rule": "SCHEMA/M/protocol/request/<endpoint>",
        "response_schema_index_rule": "SCHEMA/M/protocol/response/<status>",
        "reachable_error_statuses": source["reachable_error_statuses"],
        "recoverable_statuses": source["recoverable_statuses"],
        "limits": source["limits"],
        "terminal_rule": source["terminal_rule"],
        "firewall": source["firewall"],
    }


@dataclass(slots=True)
class RetrievalBudgetState:
    """Fail-closed matched dynamic-context state for one offline transcript."""

    registry: Mapping[str, Any]
    capability_mask: tuple[str, ...]
    query_results: Mapping[str, Any]
    reserve_bytes: int
    history_cap_bytes: int
    query_response_cap_bytes: int
    program_validator: Callable[[Mapping[str, Any]], Mapping[str, Any]] | None = None
    max_turns: int = 4
    max_queries: int = 8
    per_request_output_cap: int = 4_096
    session_output_cap: int = 16_384
    # Compatibility input for older offline fixtures.  New C1 code must use
    # the two explicit caps above.
    max_output_tokens: int | None = None
    retrieval_bytes: int = 0
    query_result_bytes: int = 0
    history_bytes: int = 0
    schema_feedback_bytes: int = 0
    turn_count: int = 0
    query_count: int = 0
    current_turn_output_tokens: int = 0
    cumulative_output_tokens: int = 0
    output_tokens: int = 0
    commit_count: int = 0
    terminal: str | None = None
    validated_program_sha256: str | None = None

    def __post_init__(self) -> None:
        if self.max_output_tokens is not None:
            self.per_request_output_cap = self.max_output_tokens
            self.session_output_cap = self.max_output_tokens

    @property
    def remaining_turns(self) -> int:
        return max(0, self.max_turns - self.turn_count)

    @property
    def remaining_queries(self) -> int:
        return max(0, self.max_queries - self.query_count)

    def _response(self, status: str, **fields: Any) -> dict[str, Any]:
        """Return the one canonical model-visible response envelope."""

        return {
            "status": status,
            **fields,
            "remaining_bytes": self.remaining_reserve_bytes,
            "remaining_queries": self.remaining_queries,
            "remaining_turns": self.remaining_turns,
        }

    @property
    def spent_bytes(self) -> int:
        return self.retrieval_bytes + self.query_result_bytes + self.history_bytes + self.schema_feedback_bytes

    @property
    def remaining_reserve_bytes(self) -> int:
        return max(0, self.reserve_bytes - self.spent_bytes)

    def _consume(self, field: str, payload: Any) -> dict[str, Any]:
        amount = len(canonical_bytes(payload))
        if amount > self.remaining_reserve_bytes:
            self.terminal = "SEMANTIC_RESERVE_EXHAUSTED"
            return self._response(self.terminal, required_bytes=amount)
        setattr(self, field, getattr(self, field) + amount)
        return self._response("OK", consumed_bytes=amount)

    def _advertised_content_ids(self) -> list[str]:
        closed = self.registry.get("closed_world") is True
        return sorted(
            key for key, value in self.registry["entries"].items()
            if value.get("capability") in self.capability_mask
            and (
                (closed and key.startswith(("ACTION/", "SCHEMA/", "EXAMPLE/"))
                 and value.get("model_usable", False) is True
                 and value.get("discoverable", True) is True)
                or (not closed and key.startswith(("CAPABILITY/", "ACTION/", "EXAMPLE/")))
            )
        )

    def _advertised_content_index(self) -> list[dict[str, Any]]:
        return [{
            "content_id": content_id,
            "size_bytes": int(self.registry["entries"][content_id].get(
                "retrieval_size_bytes",
                len(canonical_bytes(_retrieval_result(
                    content_id, self.registry["entries"][content_id],
                ))),
            )),
        } for content_id in self._advertised_content_ids()]

    def _descendant_frontier_candidates(self, content_id: str) -> list[dict[str, Any]]:
        """Return the generic fragment-threshold frontier, independent of budget."""

        rows: list[dict[str, Any]] = []
        def visit(child_id: str) -> None:
            child = self.registry["entries"].get(child_id)
            if (
                not isinstance(child, Mapping)
                or child.get("model_usable") is not True
                or child.get("capability") not in self.capability_mask
            ):
                return
            size = int(child.get(
                "retrieval_size_bytes",
                len(canonical_bytes(_retrieval_result(child_id, child))),
            ))
            grandchildren = child.get("child_fragment_ids", ())
            if size > SCHEMA_FRAGMENT_TARGET_BYTES and grandchildren:
                for grandchild_id in grandchildren:
                    visit(grandchild_id)
                return
            rows.append({"content_id": child_id, "size_bytes": size})
        entry = self.registry["entries"][content_id]
        for child_id in entry.get("child_fragment_ids", ()):
            visit(child_id)
        return sorted(rows, key=lambda row: row["content_id"])

    def _metered_descendant_frontier(
        self, *, content_id: str, requested_size: int, content_sha256: str,
    ) -> tuple[dict[str, Any], int] | None:
        """Build a stable frontier whose booleans use the post-charge balance.

        Starting with the smallest (all-true) encoding and monotonically
        changing rows to false gives a deterministic fixed point.  The
        frontier selection itself depends only on the generic 2,048-byte
        fragmentation threshold, never on the treatment balance.
        """

        candidates = self._descendant_frontier_candidates(content_id)
        flags = [True] * len(candidates)
        before = self.remaining_reserve_bytes
        for _ in range(len(candidates) + 2):
            rows = [dict(row, retrievable_now=flag)
                    for row, flag in zip(candidates, flags, strict=True)]
            frontier = {
                "frontier_semantics": RETRIEVAL_FRONTIER_SEMANTICS,
                "requested_content_id": content_id,
                "requested_size_bytes": requested_size,
                "requested_content_sha256": content_sha256,
                "maximum_retrievable_bytes": before,
                "available_child_fragments": rows,
            }
            amount = len(canonical_bytes(frontier))
            if amount > before:
                return None
            after = before - amount
            updated = [row["size_bytes"] <= after for row in candidates]
            if updated == flags:
                return frontier, amount
            # Starting all-true makes this transition monotone true -> false.
            if any(new and not old for old, new in zip(flags, updated, strict=True)):
                raise RuntimeError("retrieval_frontier_fixed_point_nonmonotone")
            flags = updated
        raise RuntimeError("retrieval_frontier_fixed_point_not_reached")

    def _default_validate_program(self, program: Mapping[str, Any]) -> dict[str, Any]:
        language = program.get("language")
        errors: list[str] = []
        if language not in self.capability_mask:
            errors.append("language_not_in_capability_mask")
        if language not in {"P", "G"}:
            errors.append("language_must_be_P_or_G")
        steps = program.get("steps")
        if not isinstance(steps, list) or not steps:
            errors.append("nonempty_steps_required")
        return {
            "accepted": not errors,
            "errors": errors,
            "canonical_ir_sha256": sha256(program) if not errors else None,
            "strict_boundary_certificate": {
                "capability": language,
                "boundary_valid": language in {"P", "G"} and not errors,
                "certificate_source": "offline_schema_and_capability_precheck",
            },
        }

    def dispatch(self, request: Mapping[str, Any]) -> dict[str, Any]:
        if self.terminal is not None:
            endpoint = request.get("endpoint") if isinstance(request, Mapping) else None
            if endpoint in {"COMMIT", "NO_VALID_SUBMISSION"}:
                return self._response(
                    "MULTIPLE_COMMIT_REJECTED", terminal_already_recorded=self.terminal,
                )
            return self._response(self.terminal, reason="terminal_state_is_fail_closed")
        if not isinstance(request, Mapping) or not isinstance(request.get("endpoint"), str):
            return self._response("MALFORMED_RETRIEVAL")
        endpoint = request["endpoint"]
        if endpoint == "LIST_CAPABILITIES":
            if set(request) != {"endpoint"}:
                return self._response("MALFORMED_RETRIEVAL")
            content = {
                "capability_mask": list(self.capability_mask),
                "content_index": self._advertised_content_index(),
                "schema_id_form": "SCHEMA/<capability>/<path>",
                "size_semantics": "exact canonical RETRIEVAL_OK payload bytes",
            }
            budget = self._consume("retrieval_bytes", content)
            return budget | ({"content": content} if budget["status"] == "OK" else {})
        if endpoint in {"GET_ACTION_SPEC", "GET_SCHEMA_FRAGMENT", "GET_EXAMPLE"}:
            expected = {"GET_ACTION_SPEC": "ACTION/", "GET_SCHEMA_FRAGMENT": "SCHEMA/", "GET_EXAMPLE": "EXAMPLE/"}[endpoint]
            content_id = request.get("content_id")
            if set(request) != {"endpoint", "content_id"} or not isinstance(content_id, str) or not content_id.startswith(expected):
                return self._response("MALFORMED_RETRIEVAL")
            if self.registry.get("closed_world") is True:
                entries = self.registry.get("entries", {})
                entry = entries.get(content_id)
                if isinstance(entry, Mapping) and entry.get("capability") not in self.capability_mask:
                    return self._response("CAPABILITY_UNAVAILABLE", content_id=content_id)
                if not isinstance(entry, Mapping) or entry.get("model_usable") is not True:
                    return self._response("RETRIEVAL_NOT_FOUND", content_id=content_id)
            result = retrieve(self.registry, self.capability_mask, content_id)
            if result["status"] != "RETRIEVAL_OK":
                return self._response(result["status"], content_id=content_id)
            requested_size = len(canonical_bytes(result))
            if requested_size > self.remaining_reserve_bytes:
                metered = self._metered_descendant_frontier(
                    content_id=content_id, requested_size=requested_size,
                    content_sha256=result["content_sha256"],
                )
                if metered is None:
                    self.terminal = "SEMANTIC_RESERVE_EXHAUSTED"
                    return self._response(
                        self.terminal, reason="retrieval_frontier_cannot_fit",
                    )
                frontier, frontier_bytes = metered
                budget = self._consume("retrieval_bytes", frontier)
                if budget["status"] != "OK" or budget["consumed_bytes"] != frontier_bytes:
                    raise RuntimeError("retrieval_frontier_atomic_charge_drift")
                return self._response(
                    "RETRIEVAL_ITEM_TOO_LARGE",
                    **frontier,
                    requested_content_bytes_charged=0,
                    frontier_metadata_bytes=frontier_bytes,
                    consumed_bytes=frontier_bytes,
                )
            budget = self._consume("retrieval_bytes", result)
            if budget["status"] != "OK":  # Defensive: exact precheck above must dominate.
                raise RuntimeError("retrieval_budget_precheck_drift")
            return {**budget, "status": "RETRIEVAL_OK",
                "content_id": content_id,
                "content": result["content"],
                "content_sha256": result["content_sha256"],
            }
        if endpoint == "QUERY_DESIGN":
            if self.query_count >= self.max_queries:
                self.terminal = "QUERY_COUNT_EXHAUSTED"
                return self._response(self.terminal, query_count=self.query_count, query_cap=self.max_queries)
            query_id = request.get("query_id")
            if set(request) != {"endpoint", "query_id", "arguments"} or not isinstance(query_id, str) or not isinstance(request.get("arguments"), Mapping):
                return self._response("MALFORMED_RETRIEVAL")
            if query_id not in self.query_results:
                return self._response("RETRIEVAL_NOT_FOUND", query_id=query_id)
            self.query_count += 1
            content = {"query_id": query_id, "result": self.query_results[query_id]}
            amount = len(canonical_bytes(content))
            if amount > self.query_response_cap_bytes:
                self.terminal = "QUERY_RESPONSE_TRUNCATED"
                return self._response(self.terminal, required_bytes=amount, cap_bytes=self.query_response_cap_bytes)
            budget = self._consume("query_result_bytes", content)
            return budget | ({"content": content} if budget["status"] == "OK" else {})
        if endpoint == "VALIDATE_PROGRAM":
            if set(request) != {"endpoint", "program"} or not isinstance(request.get("program"), Mapping):
                return self._response("MALFORMED_RETRIEVAL")
            validator = self.program_validator or self._default_validate_program
            validation = dict(validator(request["program"]))
            validation.setdefault("accepted", False)
            validation.setdefault("errors", [])
            validation["canonical_program_sha256"] = sha256(request["program"])
            if validation["accepted"]:
                self.validated_program_sha256 = validation["canonical_program_sha256"]
            budget = self._consume("schema_feedback_bytes", validation)
            return budget | ({"content": validation} if budget["status"] == "OK" else {})
        if endpoint == "COMMIT":
            if set(request) != {"endpoint", "program"} or not isinstance(request.get("program"), Mapping):
                return self._response("MALFORMED_RETRIEVAL")
            if self.commit_count:
                self.terminal = "MULTIPLE_COMMIT_REJECTED"
                return self._response(self.terminal, commit_count=self.commit_count)
            program_hash = sha256(request["program"])
            if request["program"].get("language") != "M" and self.validated_program_sha256 != program_hash:
                return self._response("PROGRAM_VALIDATION_REQUIRED", program_sha256=program_hash)
            self.commit_count = 1
            self.terminal = "COMMIT_RECEIVED"
            return self._response("COMMIT_RECEIVED", program=request["program"], program_sha256=program_hash)
        if endpoint == "NO_VALID_SUBMISSION":
            reasons = {
                "INSUFFICIENT_VISIBLE_EVIDENCE", "NO_CAPABILITY_ELIGIBLE_TARGET",
                "CANNOT_CONSTRUCT_SCHEMA_VALID_PROGRAM", "DYNAMIC_BUDGET_EXHAUSTED",
            }
            reason = request.get("reason_code")
            if set(request) != {"endpoint", "reason_code"} or reason not in reasons:
                return self._response("MALFORMED_RETRIEVAL")
            self.terminal = "NO_VALID_SUBMISSION_RECEIVED"
            return self._response(self.terminal, reason_code=reason)
        return self._response("MALFORMED_RETRIEVAL", endpoint=endpoint)

    def add_history(self, payload: Any) -> dict[str, Any]:
        amount = len(canonical_bytes(payload))
        if self.history_bytes + amount > self.history_cap_bytes:
            self.terminal = "SEMANTIC_RESERVE_EXHAUSTED"
            return self._response(
                self.terminal,
                reason="history_cap",
                required_bytes=self.history_bytes + amount,
                cap_bytes=self.history_cap_bytes,
            )
        return self._consume("history_bytes", payload)

    def add_schema_feedback(self, payload: Any) -> dict[str, Any]:
        return self._consume("schema_feedback_bytes", payload)

    def begin_turn(self) -> dict[str, Any]:
        if self.terminal is not None:
            return self._response(self.terminal)
        if self.turn_count >= self.max_turns:
            self.terminal = "TURN_LIMIT_EXHAUSTED"
            return self._response(self.terminal, turn_count=self.turn_count, turn_cap=self.max_turns)
        self.turn_count += 1
        self.current_turn_output_tokens = 0
        return self._response("TURN_STARTED", turn_count=self.turn_count)

    def record_output_tokens(self, tokens: int) -> dict[str, Any]:
        if self.terminal is not None:
            return self._response(self.terminal)
        if not isinstance(tokens, int) or isinstance(tokens, bool) or tokens < 0:
            return self._response("MALFORMED_OUTPUT_ACCOUNTING")
        next_turn = self.current_turn_output_tokens + tokens
        next_session = self.cumulative_output_tokens + tokens
        if next_turn > self.per_request_output_cap or next_session > self.session_output_cap:
            self.terminal = "OUTPUT_LIMIT_EXCEEDED"
            return self._response(
                self.terminal,
                current_turn_output_tokens=next_turn,
                cumulative_output_tokens=next_session,
                per_request_output_cap=self.per_request_output_cap,
                session_output_cap=self.session_output_cap,
            )
        self.current_turn_output_tokens = next_turn
        self.cumulative_output_tokens = next_session
        self.output_tokens = next_session
        return self._response(
            "OUTPUT_ACCOUNTED",
            current_turn_output_tokens=self.current_turn_output_tokens,
            cumulative_output_tokens=self.cumulative_output_tokens,
        )


def balanced_initial_packets(
    *, common_shell: Mapping[str, Any], observation: Mapping[str, Any], target_bytes: int | None = None,
) -> dict[str, dict[str, Any]]:
    masks = {"M": ["M"], "P": ["M", "P"], "G": ["M", "P", "G"]}
    base = {
        key: {
            "schema_version": RETRIEVAL_VERSION,
            "common_shell": common_shell,
            "observation": observation,
            "capability_mask": mask,
            "padding": "",
        }
        for key, mask in masks.items()
    }
    minimum = max(len(canonical_bytes(row)) for row in base.values())
    target = target_bytes if target_bytes is not None else minimum + 256
    if target < minimum:
        raise ValueError("target_bytes_below_minimum")
    for row in base.values():
        deficit = target - len(canonical_bytes(row))
        row["padding"] = "0" * deficit
        if len(canonical_bytes(row)) != target:
            raise AssertionError("canonical_padding_accounting_failed")
    return base


def context_ledger_row(
    condition: str,
    packet: Mapping[str, Any],
    *,
    prompt_bytes: int,
    dynamic_reserve_bytes: int,
    provider_limit: int = 65_536,
) -> dict[str, Any]:
    context_size = len(canonical_bytes(packet))
    guarded = prompt_bytes + context_size
    return {
        "condition": condition,
        "system_instruction_bytes": prompt_bytes,
        "task_prompt_bytes": 0,
        "observation_bytes": len(canonical_bytes(packet.get("observation"))),
        "action_index_bytes": len(canonical_bytes(packet.get("capability_mask"))),
        "action_spec_bytes": 0,
        "example_bytes": 0,
        "query_response_bytes": 0,
        "conversation_history_bytes": 0,
        "schema_feedback_bytes": 0,
        "total_input_bytes": guarded,
        "provider_guard_quantity": guarded,
        "remaining_dynamic_reserve_bytes": dynamic_reserve_bytes,
        "estimated_tokens_by_model_system": round(guarded / 4),
        "provider_limit_bytes": provider_limit,
    }


MECHANISM_RULES: Mapping[str, tuple[str, Any]] = {
    "FANOUT_REPLICATION": ("hard structural fanout target is not enumerated in the frozen public manifest", None),
    "LUT_CONE": ("typed cell handles are not enumerated in the frozen public manifest", None),
    "NONPREFIX_MULTI_NET_BATCH": ("typed net cardinality is private/hash-only in the frozen public manifest", None),
    "SPATIAL_GENERATED_GEOMETRY": ("requires at least two regions and non-degenerate geometry", "spatial"),
    "COUPLED_HARDBLOCK_SITE_ALLOCATION": ("requires at least two hardblocks and compatible sites", "hardblock"),
    "P_EXCLUSIVE_WORKER_DATAFLOW": ("routed roots admit stage-compatible worker operations", "routed"),
}


def classify_mechanism(root_id: str, observation: Mapping[str, Any], mechanism: str) -> dict[str, Any]:
    manifest = observation.get("handle_manifest")
    counts = manifest.get("object_counts", {}) if isinstance(manifest, Mapping) else {}
    payload = observation.get("payload")
    implementation = payload.get("implementation", {}) if isinstance(payload, Mapping) else {}
    geometry = observation.get("geometry_bounds")
    reason, kind = MECHANISM_RULES[mechanism]
    status = "UNKNOWN"
    object_ids: list[str] = []
    confidence = "LOW"
    if kind == "spatial":
        regions = int(counts.get("region", 0) or 0)
        if regions >= 2 and isinstance(geometry, list) and len(geometry) == 4 and geometry[1] >= geometry[0] and geometry[3] >= geometry[2]:
            status, confidence = "ELIGIBLE", "HIGH"
            object_ids = list(observation.get("predefined_regions") or ())[:2]
        else:
            status, confidence = "INSUFFICIENT_TARGET", "HIGH"
    elif kind == "hardblock":
        hardblocks, sites = int(counts.get("hardblock", 0) or 0), int(counts.get("site", 0) or 0)
        if hardblocks >= 2 and sites >= 2:
            status, confidence = "ELIGIBLE", "MEDIUM"
            handles = manifest.get("handles", []) if isinstance(manifest, Mapping) else []
            object_ids = [h for h in handles if h.startswith(("hardblock_", "site_"))][:4]
        else:
            status, confidence = "INSUFFICIENT_TARGET", "HIGH"
    elif kind == "routed":
        if implementation.get("route_state") == "ROUTED":
            status, confidence = "ELIGIBLE", "HIGH"
        else:
            status, confidence = "STAGE_INCOMPATIBLE", "HIGH"
    return {
        "root_id": root_id,
        "mechanism": mechanism,
        "classification": status,
        "evidence_source": "scientific-bundle-v2.observations pre-treatment structural fields",
        "evidence_object_ids": object_ids,
        "eligibility_rule_id": f"eligibility.{mechanism.lower()}.v1",
        "rule_version": ELIGIBILITY_VERSION,
        "confidence": confidence,
        "explanation": reason,
    }


def assert_outcome_blind_rows(rows: Iterable[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    checked = []
    for row in rows:
        offending = [
            key for key in row
            if any(fragment in str(key).lower() for fragment in OUTCOME_KEY_FRAGMENTS)
        ]
        if offending:
            raise ValueError("OUTCOME_COLUMNS_ACCESSED_DURING_ROOT_SELECTION:" + ",".join(sorted(offending)))
        checked.append(row)
    return checked


def outcome_blind_stratified_sample(
    rows: Iterable[Mapping[str, Any]], *, n: int, seed: int,
) -> list[Mapping[str, Any]]:
    pool = list(assert_outcome_blind_rows(rows))
    if n > len(pool):
        raise ValueError("sample_size_exceeds_population")
    by_stratum: dict[tuple[str, str], list[Mapping[str, Any]]] = {}
    for row in pool:
        key = (str(row.get("resource_class", "UNKNOWN")), str(row.get("timing_stratum", "UNKNOWN")))
        by_stratum.setdefault(key, []).append(row)
    rng = random.Random(seed)
    for group in by_stratum.values():
        group.sort(key=lambda row: str(row.get("root_id")))
        rng.shuffle(group)
    result: list[Mapping[str, Any]] = []
    keys = sorted(by_stratum)
    while len(result) < n:
        progressed = False
        for key in keys:
            if by_stratum[key] and len(result) < n:
                result.append(by_stratum[key].pop())
                progressed = True
        if not progressed:
            break
    return result


@dataclass(frozen=True, slots=True)
class CanonicalProviderIR:
    model_id: str
    provider: str
    role_ordered_messages: tuple[tuple[str, str], ...]
    capability_assignment: str
    input_budget_bytes: int
    output_budget_tokens: int
    max_requests: int = 1
    retries: int = 0
    fallbacks: int = 0
    response_repairs: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": "wp015-fork01.provider-ir.v1",
            "model_id": self.model_id,
            "provider": self.provider,
            "messages": [{"role": r, "content": c} for r, c in self.role_ordered_messages],
            "capability_assignment": self.capability_assignment,
            "budgets": {"input_bytes": self.input_budget_bytes, "output_tokens": self.output_budget_tokens},
            "transport": {
                "max_requests": self.max_requests,
                "retries": self.retries,
                "fallbacks": self.fallbacks,
                "response_repairs": self.response_repairs,
            },
            "expected_stop_semantics": ["COMMIT", "NO_VALID_SUBMISSION", "PROVIDER_FAILURE"],
        }


class PublicEvidenceHandleRegistry:
    """Offline compiler resolver for exact pseudonymous handles in public receipts.

    It never reconstructs private Vivado identities. Existing handles resolve to
    evidence-hash identities; compiler-generated objects are deterministic and
    remain confined to the preflight process.
    """

    def __init__(self, *, root_sha256: str, rows: Sequence[Mapping[str, Any]]) -> None:
        self._root_sha256 = root_sha256
        self._objects: dict[str, PrivateObject] = {}
        for row in rows:
            handle = str(row.get("handle") or "")
            object_type = str(row.get("handle_type") or handle.split("_h", 1)[0])
            if not handle or not object_type:
                continue
            attributes = {key: value for key, value in row.items() if key not in {"handle", "handle_type"}}
            self._objects[handle] = PrivateObject(
                handle, object_type, "public-evidence-sha256:" + sha256({"handle": handle, "attributes": attributes}),
                attributes, "frozen_public_query_evidence",
            )

    @property
    def root_sha256(self) -> str:
        return self._root_sha256

    def resolve(self, handle: str, *, expected_type: str | None = None) -> PrivateObject:
        try:
            value = self._objects[handle]
        except KeyError as exc:
            raise HandleError("unknown_or_stale_handle") from exc
        if expected_type is not None and value.object_type != expected_type:
            raise HandleError("handle_type_mismatch")
        return value

    def objects(self, *, object_type: str | None = None) -> tuple[PrivateObject, ...]:
        return tuple(sorted(
            (row for row in self._objects.values() if object_type is None or row.object_type == object_type),
            key=lambda row: (row.object_type, row.handle),
        ))

    def register(self, *, object_type: str, canonical_identity: str,
                 attributes: Mapping[str, Any] | None = None, source_tool: str = "compiler") -> str:
        token = sha256({"root": self._root_sha256, "type": object_type, "identity": canonical_identity})[:24]
        handle = f"{object_type}_h{token}"
        current = self._objects.get(handle)
        if current is None:
            self._objects[handle] = PrivateObject(handle, object_type, canonical_identity, attributes or {}, source_tool)
        return handle


__all__ = [
    "BOUNDARY_CLASSES", "FUNCTIONAL_STATUSES", "CanonicalProviderIR", "PublicEvidenceHandleRegistry", "PREFLIGHT_LABEL",
    "COMPACT_RECOVERABLE_STATUSES", "COMPACT_TERMINAL_STATUSES",
    "NO_VALID_SUBMISSION_REASONS", "RETRIEVAL_FRAGMENTATION_VERSION",
    "RETRIEVAL_FRONTIER_SEMANTICS",
    "SCHEMA_FRAGMENT_TARGET_BYTES",
    "RetrievalBudgetState", "boundary_stage_and_class", "compact_model_visible_protocol",
    "compact_protocol_definition",
    "proposal_boundary_certificate_v1", "proposal_boundary_certificate_v2",
    "proposal_boundary_certificate_v3",
    "normalize_functional_status",
    "assert_outcome_blind_rows", "balanced_initial_packets", "canonical_bytes",
    "classify_mechanism", "context_ledger_row", "fragment_retrieval_registry",
    "make_retrieval_registry",
    "canonical_timing", "finite_number", "fmax_from_period_wns",
    "outcome_blind_stratified_sample", "provenance", "retrieve", "scientific_delta",
    "sha256", "strict_boundary_class", "utility_estimands",
    "named_family_class_v4",
]
