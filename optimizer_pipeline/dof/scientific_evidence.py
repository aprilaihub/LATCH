"""Canonical scientific evidence rules shared by WP-014E and later runs."""

from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
from statistics import median
from typing import Any, Mapping, Sequence

from .single_submission.action_surface_v3 import (
    G_EXCLUSIVE_NODES,
    P_EXCLUSIVE_OPERATIONS,
    P_OPERATION_REGISTRY,
)
from .named_family import (
    NAMED_FAMILY_SEMANTICS_VERSION, NAMED_FAMILY_SEMANTICS_VERSION_V2,
    named_family_evidence_v1, named_family_evidence_v2,
)


SCIENTIFIC_EVIDENCE_VERSION = "dof.scientific-evidence.v2"
CAUSAL_LINEAGE_VERSION = "dof.g-causal-lineage.v2"
STRICT_FUNCTIONAL_PASS = "FUNCTIONAL_PASS_200_VECTOR"


def _canonical_hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()


def _references(value: Any) -> tuple[str, ...]:
    """Return the exact generated binding names consumed by one argument tree."""

    found: list[str] = []
    if isinstance(value, Mapping):
        if set(value) == {"ref"} and isinstance(value.get("ref"), str):
            found.append(value["ref"])
        else:
            for key in sorted(value):
                found.extend(_references(value[key]))
    elif isinstance(value, list):
        for item in value:
            found.extend(_references(item))
    return tuple(found)


def _reference_slots(value: Any, prefix: str = "args") -> tuple[tuple[str, str], ...]:
    found: list[tuple[str, str]] = []
    if isinstance(value, Mapping):
        if set(value) == {"ref"} and isinstance(value.get("ref"), str):
            found.append((prefix, str(value["ref"])))
        else:
            for key in sorted(value):
                found.extend(_reference_slots(value[key], f"{prefix}.{key}"))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            found.extend(_reference_slots(item, f"{prefix}[{index}]"))
    return tuple(found)


def _candidate_rows(candidate: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    rows = candidate.get("steps")
    if not isinstance(rows, list):
        rows = candidate.get("statements")
    return [row for row in rows if isinstance(row, Mapping)] if isinstance(rows, list) else []


def _row_identity(row: Mapping[str, Any], index: int) -> tuple[str, str]:
    return (
        str(row.get("step_id") or row.get("statement_id") or f"row{index}"),
        str(row.get("op") or row.get("node") or "UNKNOWN"),
    )


def _consumer_runtime_operations(construct: str) -> tuple[str, ...]:
    if construct == "ASSIGN_REGION":
        return ("vivado.wp014c_create_pblock", "vivado.wp014c_assign_pblock")
    if construct == "BATCH_ROUTE_NETS":
        return ("vivado.wp014h_batch_route_nets",)
    if construct == "APPLY_ALLOCATION":
        return ("vivado.wp014h_apply_allocation",)
    row = P_OPERATION_REGISTRY.get(construct)
    if row and row.get("worker_operation"):
        return (str(row["worker_operation"]),)
    return ()


def causal_lineage(
    *,
    candidate: Mapping[str, Any] | None,
    execution_started: bool,
    execution: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Build a fail-closed producer→consumer→runtime lineage certificate.

    This deliberately derives no edge from aggregate execution counts.  A G
    producer counts only when a source-level reference reaches a concrete
    consumer whose exact worker operation was observed.  Mechanism-specific
    probes may attach a stronger ``state_deltas`` record; legacy executions
    receive a DCP-level delta certificate and are labelled accordingly.
    """

    candidate = candidate or {}
    execution = execution or {}
    rows = _candidate_rows(candidate)
    producers: dict[str, dict[str, Any]] = {}
    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, Any]] = []
    consumers: dict[str, dict[str, Any]] = {}
    binding_records = {
        str(row.get("binding_id")): dict(row)
        for row in execution.get("binding_records", ())
        if isinstance(row, Mapping) and row.get("binding_id")
    }

    for index, row in enumerate(rows, start=1):
        node_id, construct = _row_identity(row, index)
        bind = row.get("bind")
        args = row.get("args") if isinstance(row.get("args"), Mapping) else {}
        ref_slots = _reference_slots(args)
        refs = tuple(binding for _slot, binding in ref_slots)
        role = "PRODUCER" if construct in G_EXCLUSIVE_NODES and isinstance(bind, str) else "CONSUMER"
        node = {
            "node_id": node_id,
            "node_role": role,
            "construct": construct,
            "boundary_level": "G" if construct in G_EXCLUSIVE_NODES else "P" if construct in P_EXCLUSIVE_OPERATIONS else "M",
            "input_binding_ids": list(refs),
            "output_binding_id": bind if isinstance(bind, str) else None,
            "output_binding_type": binding_records.get(str(bind), {}).get("binding_type") if isinstance(bind, str) else None,
            "output_binding_content_hash": binding_records.get(str(bind), {}).get("binding_content_hash") if isinstance(bind, str) else None,
            "public_args_sha256": _canonical_hash(args),
        }
        nodes.append(node)
        if role == "PRODUCER":
            producers[bind] = node
        operations = _consumer_runtime_operations(construct)
        if operations:
            consumers[node_id] = {
                "consumer_node_id": node_id,
                "construct": construct,
                "runtime_operations": list(operations),
            }
        for slot, ref in ref_slots:
            producer = producers.get(ref)
            if producer is None:
                continue
            edges.append({
                "producer_node_id": producer["node_id"],
                "output_handle_id": ref,
                "output_handle_type": binding_records.get(ref, {}).get("binding_type", "typed_binding"),
                "output_binding_content_hash": binding_records.get(ref, {}).get("binding_content_hash"),
                "consumer_node_id": node_id,
                "consumer_argument_path": slot,
            })

    trace = execution.get("call_trace")
    trace = trace if isinstance(trace, list) else []
    observed = [str(row.get("operation")) for row in trace if isinstance(row, Mapping) and row.get("operation")]
    runtime_operations: list[dict[str, Any]] = []
    causal_paths: list[dict[str, Any]] = []
    for edge in edges:
        consumer = consumers.get(edge["consumer_node_id"])
        if consumer is None:
            continue
        matched_rows = [
            row for row in trace if isinstance(row, Mapping)
            and row.get("operation") in consumer["runtime_operations"]
            and row.get("source_step_id") in {None, edge["consumer_node_id"]}
        ]
        for trace_row in matched_rows:
            operation = str(trace_row["operation"])
            dispatch_id = str(trace_row.get("worker_dispatch_id") or f"legacy:{operation}")
            runtime_id = f"{edge['consumer_node_id']}:{dispatch_id}"
            exact_step = trace_row.get("source_step_id") == edge["consumer_node_id"]
            runtime_operations.append({
                "runtime_operation_id": runtime_id,
                "consumer_node_id": edge["consumer_node_id"],
                "worker_operation": operation,
                "worker_dispatch_id": dispatch_id,
                "actual_target_identity_hash": trace_row.get("actual_target_identity_hash"),
                "source_step_exact": exact_step,
                "endpoint": "OBSERVED" if execution_started else "NOT_STARTED",
            })
            causal_paths.append(edge | {
                "runtime_operation_id": runtime_id,
                "worker_dispatch_id": dispatch_id,
                "actual_target_identity_hash": trace_row.get("actual_target_identity_hash"),
                "source_step_exact": exact_step,
            })

    selected = int(execution.get("selected_count") or 0)
    transformed = int(execution.get("transformed_count") or 0)
    child = execution.get("child")
    child_sha = child.get("sha256") if isinstance(child, Mapping) else None
    nonempty_delta = bool(child_sha and transformed > 0)
    supplied_deltas = execution.get("mechanism_state_deltas")
    state_deltas = [
        dict(row) for row in supplied_deltas
        if isinstance(row, Mapping)
    ] if isinstance(supplied_deltas, list) else []
    mechanism_delta = any(
        row.get("evidence_strength") in {
            "MECHANISM_EXACT", "OBJECT_IDENTITY", "PROPERTY_BEFORE_AFTER",
            "ROUTE_BEFORE_AFTER", "TOPOLOGY_COUNT_BEFORE_AFTER",
        }
        and int(row.get("changed_count") or 0) > 0
        and bool(row.get("changed_identity_hash"))
        for row in state_deltas
    )
    if child_sha and not state_deltas:
        state_deltas.append({
            "state_delta_id": "child-dcp",
            "mechanism_family": "DCP_LEVEL_FALLBACK",
            "before_hash": None,
            "after_hash": child_sha,
            "changed_count": transformed,
            "changed_identity_hash": _canonical_hash({
                "child_sha256": child_sha,
                "observed_operations": observed,
                "transformed_count": transformed,
            }),
            "evidence_strength": "DCP_LEVEL_ONLY",
        })
    exercised = bool(
        execution_started and causal_paths and selected > 0
        and (mechanism_delta or nonempty_delta)
    )
    if not producers:
        reason = "NO_G_PRODUCER"
    elif not edges:
        reason = "G_OUTPUT_NOT_CONSUMED"
    elif not causal_paths:
        reason = "BOUND_CONSUMER_RUNTIME_NOT_OBSERVED"
    elif selected <= 0:
        reason = "EMPTY_SELECTED_OBJECT_SET"
    elif not (mechanism_delta or nonempty_delta):
        reason = "STATE_DELTA_NOT_EVIDENCED"
    else:
        reason = None
    core = {
        "schema_version": CAUSAL_LINEAGE_VERSION,
        "program_sha256": _canonical_hash(candidate),
        "nodes": nodes,
        "edges": edges,
        "runtime_operations": runtime_operations,
        "state_deltas": state_deltas,
        "selected_object_count": selected,
        "selected_object_identity_hash": execution.get("selected_object_identity_hash"),
        "transformed_object_count": transformed,
        "transformed_object_identity_hash": execution.get("transformed_object_identity_hash"),
        "boundary_declared": candidate.get("language") == "G",
        "boundary_compiled": bool(producers),
        "boundary_reached": bool(execution_started and causal_paths),
        "boundary_runtime_exercised": exercised,
        "causal_paths": causal_paths,
        "nonexercise_reason": reason,
    }
    return core | {"lineage_sha256": _canonical_hash(core)}


def strict_boundary_certificate(
    *,
    candidate: Mapping[str, Any] | None,
    execution_started: bool,
    execution: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Return a conservative sufficient, never-necessary G strict certificate."""

    candidate = candidate or {}
    execution = execution or {}
    lineage = causal_lineage(
        candidate=candidate,
        execution_started=execution_started,
        execution=execution,
    )
    rows = _candidate_rows(candidate)
    constructs = [str(row.get("op") or row.get("node")) for row in rows]
    records = {
        str(row.get("binding_id")): dict(row)
        for row in execution.get("binding_records", ())
        if isinstance(row, Mapping) and row.get("binding_id")
    }
    family = None
    sufficient_rule = None
    required_binding = None
    if "CREATE_REGION" in constructs and "ASSIGN_REGION" in constructs:
        family = "SPATIAL_GENERATED_GEOMETRY"
        sufficient_rule = "generated geometry is not one of P's published predefined regions"
        region_rows = [row for row in rows if row.get("op") == "CREATE_REGION" or row.get("node") == "CREATE_REGION"]
        required_binding = str(region_rows[0].get("bind")) if region_rows else None
        region_args = region_rows[0].get("args", {}) if region_rows else {}
        generated_region_nonpredefined = bool(
            isinstance(region_args, Mapping)
            and all(isinstance(region_args.get(key), int) for key in ("x_min", "x_max", "y_min", "y_max"))
            and (
                int(region_args["x_max"]) > int(region_args["x_min"])
                or int(region_args["y_max"]) > int(region_args["y_min"])
            )
        )
    elif "DIFF" in constructs and "BATCH_ROUTE_NETS" in constructs:
        family = "NONPREFIX_MULTI_NET_BATCH"
        sufficient_rule = "DIFF-produced multi-net collection is consumed by one batch operation unavailable to P"
        diff_rows = [row for row in rows if row.get("op") == "DIFF" or row.get("node") == "DIFF"]
        required_binding = str(diff_rows[0].get("bind")) if diff_rows else None
    elif "COUPLE_SITES" in constructs and "APPLY_ALLOCATION" in constructs:
        family = "COUPLED_HARDBLOCK_SITE_ALLOCATION"
        sufficient_rule = "generated one-to-one multi-object allocation is consumed atomically and unavailable to P"
        allocation_rows = [row for row in rows if row.get("op") == "COUPLE_SITES" or row.get("node") == "COUPLE_SITES"]
        required_binding = str(allocation_rows[0].get("bind")) if allocation_rows else None

    record = records.get(required_binding or "", {})
    if family == "SPATIAL_GENERATED_GEOMETRY":
        cardinality_ok = bool(record) and generated_region_nonpredefined
    elif family == "NONPREFIX_MULTI_NET_BATCH":
        cardinality_ok = int(record.get("binding_cardinality") or 0) >= 2
    elif family == "COUPLED_HARDBLOCK_SITE_ALLOCATION":
        relation_hash = record.get("compatibility_relation_sha256")
        pair_hashes = record.get("target_site_pair_hashes")
        matching_size = int(record.get("matching_size") or 0)
        cardinality_ok = (
            matching_size >= 2
            and int(execution.get("selected_count") or 0) >= 2
            and int(execution.get("transformed_count") or 0) >= 2
        )
    else:
        cardinality_ok = bool(record)
    exact_paths = [row for row in lineage["causal_paths"] if row.get("source_step_exact")]
    relevant_paths = [row for row in exact_paths if row.get("output_handle_id") == required_binding]
    hardblock_dispatch_rows = [
        row for row in execution.get("call_trace", ())
        if isinstance(row, Mapping)
        and row.get("operation") == "vivado.wp014h_apply_allocation"
        and row.get("source_step_id") in {path.get("consumer_node_id") for path in relevant_paths}
    ]
    hardblock_relation_ok = True
    if family == "COUPLED_HARDBLOCK_SITE_ALLOCATION":
        hardblock_relation_ok = bool(
            isinstance(relation_hash, str) and len(relation_hash) == 64
            and isinstance(pair_hashes, list) and len(pair_hashes) == matching_size
            and hardblock_dispatch_rows
            and all(
                row.get("pair_membership_revalidated") is True
                and row.get("compatibility_relation_sha256") == relation_hash
                and row.get("target_site_pair_hashes") == pair_hashes
                and row.get("allocation_sha256") == record.get("allocation_sha256")
                for row in hardblock_dispatch_rows
            )
        )
    deltas = [
        dict(row) for row in execution.get("mechanism_state_deltas", ())
        if isinstance(row, Mapping)
        and int(row.get("changed_count") or 0) > 0
        and row.get("changed_identity_hash")
    ]
    child = execution.get("child") if isinstance(execution.get("child"), Mapping) else {}
    strict = bool(
        family
        and lineage["boundary_runtime_exercised"]
        and relevant_paths
        and cardinality_ok
        and hardblock_relation_ok
        and deltas
        and child.get("sha256")
        and all(row.get("worker_dispatch_id") and row.get("actual_target_identity_hash") for row in relevant_paths)
    )
    reason = None
    if not family:
        reason = "NO_SUFFICIENT_STRICT_RULE"
    elif not lineage["boundary_runtime_exercised"]:
        reason = lineage.get("nonexercise_reason") or "G_NOT_CAUSALLY_EXERCISED"
    elif not relevant_paths:
        reason = "EXACT_PRODUCER_CONSUMER_DISPATCH_LINEAGE_MISSING"
    elif not cardinality_ok:
        reason = "STRICT_CARDINALITY_REQUIREMENT_NOT_MET"
    elif not hardblock_relation_ok:
        reason = "EXACT_COMPATIBILITY_RELATION_OR_DISPATCH_PROOF_MISSING"
    elif not deltas:
        reason = "MECHANISM_SPECIFIC_DELTA_MISSING"
    elif not child.get("sha256"):
        reason = "CHILD_HASH_MISSING"
    elif not all(row.get("worker_dispatch_id") and row.get("actual_target_identity_hash") for row in relevant_paths):
        reason = "EXACT_RUNTIME_IDENTITY_MISSING"
    certificates = []
    if strict:
        delta = deltas[0]
        for path in relevant_paths:
            certificates.append({
                "producer_step_id": path["producer_node_id"],
                "output_binding_id": path["output_handle_id"],
                "binding_type": path["output_handle_type"],
                "binding_content_hash": path["output_binding_content_hash"],
                "consumer_step_id": path["consumer_node_id"],
                "consumer_input_slot": path["consumer_argument_path"],
                "worker_dispatch_id": path["worker_dispatch_id"],
                "actual_target_identity_hash": path["actual_target_identity_hash"],
                "selected_object_count": int(execution.get("selected_count") or 0),
                "transformed_object_count": int(execution.get("transformed_count") or 0),
                "mechanism_delta_id": delta.get("state_delta_id"),
                "mechanism_delta_kind": delta.get("mechanism_family"),
                "child_artifact_hash": child.get("sha256"),
                "compatibility_relation_sha256": record.get("compatibility_relation_sha256"),
                "feasibility_query_sha256": record.get("feasibility_query_sha256"),
                "target_site_pair_hashes": record.get("target_site_pair_hashes"),
                "allocation_sha256": record.get("allocation_sha256"),
            })
    core = {
        "schema_version": "dof.g-strict-boundary-certificate.v2",
        "event": "G_STRICT_BOUNDARY_EXERCISED" if strict else "G_CAUSAL_BUT_STRICT_UNPROVEN" if lineage["boundary_runtime_exercised"] else "G_NOT_CAUSALLY_EXERCISED",
        "strict_boundary_exercised": strict,
        "mechanism_family": family,
        "sufficient_rule": sufficient_rule,
        "required_binding_id": required_binding,
        "noncertificate_reason": reason,
        "certificates": certificates,
        "causal_lineage_sha256": lineage["lineage_sha256"],
    }
    return core | {"certificate_sha256": _canonical_hash(core)}


def strict_boundary_certificate_v3(
    *, candidate: Mapping[str, Any] | None, compiled: Any,
    execution_started: bool, execution: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Runtime named-family certificate using the proposal-v2 semantic core."""

    candidate = candidate or {}
    execution = execution or {}
    evidence = named_family_evidence_v1(
        candidate, compiled, runtime=execution if execution_started else None,
    )
    lineage = causal_lineage(
        candidate=candidate, execution_started=execution_started, execution=execution,
    )
    if not execution_started:
        status = "RAW_EVIDENCE_INSUFFICIENT"
        exercised = False
        reason = "runtime_execution_not_started"
    elif evidence["named_family_valid"]:
        status = "PASS_NAMED_FAMILY_V3"
        exercised = True
        reason = None
    elif lineage.get("boundary_runtime_exercised") is True:
        status = "G_CAUSAL_BUT_NAMED_STRICT_FAMILY_UNPROVEN"
        exercised = False
        reason = "causal_runtime_path_present_but_named_family_sufficient_rule_unproven"
    else:
        status = "RAW_EVIDENCE_INSUFFICIENT"
        exercised = False
        reason = "proposal_named_family_path_unproven"
    core = {
        "schema_version": "dof.g-strict-boundary-certificate.v3",
        "named_family_semantics_version": NAMED_FAMILY_SEMANTICS_VERSION,
        "v3_named_family_status": status,
        "strict_boundary_exercised": exercised,
        "mechanism_family": evidence["selected_mechanism_family"],
        "all_evaluated_paths": evidence["all_evaluated_paths"],
        "accepted_path_hashes": evidence["accepted_path_hashes"],
        "rejected_path_reasons": evidence["rejected_path_reasons"],
        "selected_certificate_path_hash": evidence["selected_certificate_path_hash"],
        "noncertificate_reason": reason,
        "candidate_sha256": _canonical_hash(candidate),
        "execution_evidence_sha256": _canonical_hash(execution),
        "raw_evidence_unchanged": True,
    }
    return core | {"certificate_sha256": _canonical_hash(core)}


def strict_boundary_certificate_v4(
    *, candidate: Mapping[str, Any] | None, compiled: Any,
    execution_started: bool, execution: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Separate causal runtime reachability from recomputed family proof."""

    candidate, execution = candidate or {}, execution or {}
    evidence = named_family_evidence_v2(
        candidate, compiled, runtime=execution if execution_started else None,
    )
    lineage = causal_lineage(
        candidate=candidate, execution_started=execution_started, execution=execution,
    )
    causal = lineage.get("boundary_runtime_exercised") is True
    named = evidence["named_family_valid"] is True
    unproven = bool(causal and not named)
    if named:
        status, reason = "PASS_NAMED_FAMILY_V4", None
    elif unproven:
        status = "G_CAUSAL_BUT_NAMED_FAMILY_UNPROVEN"
        reason = "causal_runtime_path_present_but_recomputed_named_family_rule_unproven"
    else:
        status, reason = "RAW_EVIDENCE_INSUFFICIENT", "causal_or_named_family_runtime_evidence_missing"
    core = {
        "schema_version": "dof.g-strict-boundary-certificate.v4",
        "named_family_semantics_version": NAMED_FAMILY_SEMANTICS_VERSION_V2,
        "v4_named_family_status": status,
        "G_CAUSAL_RUNTIME_EXECUTED": causal,
        "G_NAMED_FAMILY_PROVEN_V4": named,
        "G_CAUSAL_NAMED_FAMILY_UNPROVEN": unproven,
        "strict_boundary_exercised": named,
        "mechanism_family": evidence["selected_mechanism_family"],
        "all_evaluated_paths": evidence["all_evaluated_paths"],
        "accepted_path_hashes": evidence["accepted_path_hashes"],
        "rejected_path_reasons": evidence["rejected_path_reasons"],
        "selected_certificate_path_hash": evidence["selected_certificate_path_hash"],
        "noncertificate_reason": reason,
        "candidate_sha256": _canonical_hash(candidate),
        "execution_evidence_sha256": _canonical_hash(execution),
        "raw_v2_v3_evidence_overwritten": False,
    }
    return core | {"certificate_sha256": _canonical_hash(core)}


def file_sha256(path: str | Path | None) -> str | None:
    if path is None:
        return None
    candidate = Path(path)
    if not candidate.is_file():
        return None
    digest = hashlib.sha256()
    with candidate.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def freeze_epsilon_fmax(same_dcp_measurements_mhz: Sequence[float]) -> dict[str, Any]:
    """Freeze a conservative scorer tolerance from repeated same-DCP scores."""

    values = tuple(float(value) for value in same_dcp_measurements_mhz)
    if len(values) < 2 or any(not math.isfinite(value) for value in values):
        raise ValueError("same_dcp_repeatability_requires_two_finite_measurements")
    centre = median(values)
    epsilon_fmax = max(abs(value - centre) for value in values)
    return {
        "schema_version": SCIENTIFIC_EVIDENCE_VERSION,
        "method": "max_absolute_deviation_from_same_dcp_median",
        "sample_count": len(values),
        "median_fmax_mhz": centre,
        "epsilon_fmax": epsilon_fmax,
        "range_fmax_mhz": max(values) - min(values),
    }


def score_fmax_delta(
    *,
    root_fmax_mhz: float | None,
    child_fmax_mhz: float | None,
    root_sha256: str | None,
    child_sha256: str | None,
    epsilon_fmax: float,
) -> dict[str, Any]:
    epsilon = float(epsilon_fmax)
    if not math.isfinite(epsilon) or epsilon < 0:
        raise ValueError("epsilon_fmax_must_be_finite_and_nonnegative")
    byte_identical = bool(root_sha256 and child_sha256 and root_sha256 == child_sha256)
    if root_fmax_mhz is None or child_fmax_mhz is None:
        raw_delta = effective_delta = None
    else:
        raw_delta = float(child_fmax_mhz) - float(root_fmax_mhz)
        if not math.isfinite(raw_delta):
            raise ValueError("fmax_delta_nonfinite")
        effective_delta = 0.0 if byte_identical or abs(raw_delta) <= epsilon else raw_delta
    return {
        "schema_version": SCIENTIFIC_EVIDENCE_VERSION,
        "root_sha256": root_sha256,
        "child_sha256": child_sha256,
        "byte_identical_child": byte_identical,
        "epsilon_fmax": epsilon,
        "measurement_delta_fmax_mhz": raw_delta,
        "delta_fmax_mhz": effective_delta,
    }


def classify_functional_validation(
    report: Mapping[str, Any] | None,
    *,
    returncode: int | None,
    timed_out: bool,
    required_vectors: int = 200,
) -> str:
    if timed_out:
        return "FUNCTIONAL_TIMEOUT"
    if report is None:
        return "FUNCTIONAL_INFRASTRUCTURE_FAILURE"
    if report.get("phase2_skipped"):
        return "FUNCTIONAL_UNSUPPORTED_SKIPPED"
    if report.get("infrastructure_failure"):
        return "FUNCTIONAL_INFRASTRUCTURE_FAILURE"
    if not report.get("phase1_passed"):
        return "FUNCTIONAL_STRUCTURAL_REJECTION"
    simulation = report.get("simulation_report")
    if not isinstance(simulation, Mapping):
        return "FUNCTIONAL_INCOMPLETE"
    mismatch = int(simulation.get("mismatch_count") or 0)
    protocol = int(simulation.get("protocol_mismatch_count") or 0)
    cycles = simulation.get("cycles_simulated")
    vectors = report.get("num_vectors")
    if (
        report.get("phase2_passed") is True
        and vectors == required_vectors
        and cycles == required_vectors
        and mismatch == 0
        and protocol == 0
        and not simulation.get("result_fail_seen")
        and returncode in (0, None)
    ):
        return STRICT_FUNCTIONAL_PASS
    if mismatch or protocol or simulation.get("result_fail_seen"):
        return "FUNCTIONAL_MISMATCH"
    if returncode not in (0, None):
        return "FUNCTIONAL_INFRASTRUCTURE_FAILURE"
    return "FUNCTIONAL_INCOMPLETE"


def boundary_evidence(
    *,
    candidate: Mapping[str, Any] | None,
    execution_started: bool,
    execution: Mapping[str, Any] | None,
) -> dict[str, Any]:
    candidate = candidate or {}
    language = candidate.get("language")
    rows = candidate.get("steps") if language in {"P", "G"} else None
    if language == "G" and not isinstance(rows, list):
        rows = candidate.get("statements")
    rows = rows if isinstance(rows, list) else []
    constructs = [
        str(row.get("op") or row.get("node"))
        for row in rows if isinstance(row, Mapping)
    ]
    p_constructs = sorted(set(constructs) & set(P_EXCLUSIVE_OPERATIONS))
    g_constructs = sorted(set(constructs) & set(G_EXCLUSIVE_NODES))
    compiled_boundary = "G" if g_constructs else "P" if p_constructs else "M" if language == "M" or "CALL_MACRO" in constructs else None

    execution = execution or {}
    trace = execution.get("call_trace")
    trace = trace if isinstance(trace, list) else []
    observed_operations = {
        str(row.get("operation")) for row in trace
        if isinstance(row, Mapping)
    }
    expected_worker_ops = {
        str(P_OPERATION_REGISTRY[key].get("worker_operation"))
        for key in p_constructs
        if P_OPERATION_REGISTRY[key].get("worker_operation")
    }
    p_runtime = bool(expected_worker_ops & observed_operations)
    lineage = causal_lineage(
        candidate=candidate,
        execution_started=execution_started,
        execution=execution,
    )
    macro_runtime = bool(execution.get("actual_heavy_calls")) and (
        language == "M" or "CALL_MACRO" in constructs
    )
    exercised = (
        lineage["boundary_runtime_exercised"]
        if language == "G"
        else bool(execution_started and (p_runtime or macro_runtime))
    )
    return {
        "schema_version": SCIENTIFIC_EVIDENCE_VERSION,
        "declared_language": language,
        "compiled_boundary": compiled_boundary,
        "p_exclusive_constructs": p_constructs,
        "g_exclusive_constructs": g_constructs,
        "execution_started": bool(execution_started),
        "expected_exclusive_worker_operations": sorted(expected_worker_ops),
        "observed_worker_operations": sorted(observed_operations),
        "selected_count": int(execution.get("selected_count") or 0),
        "transformed_count": int(execution.get("transformed_count") or 0),
        "boundary_runtime_reached": (
            lineage["boundary_reached"]
            if language == "G" else bool(execution_started and compiled_boundary)
        ),
        "boundary_runtime_exercised": exercised,
        "producer_consumer_lineage": lineage,
    }


def canonical_scientific_outcome(
    *,
    physical_endpoint: str,
    functional_terminal: str | None,
    delta: Mapping[str, Any],
) -> dict[str, Any]:
    physical_valid = physical_endpoint == "VALID_COMPLETION"
    functional_pass = functional_terminal == STRICT_FUNCTIONAL_PASS
    conditional = delta.get("delta_fmax_mhz") if physical_valid and functional_pass else None
    return {
        "schema_version": SCIENTIFIC_EVIDENCE_VERSION,
        "physical_endpoint": physical_endpoint,
        "functional_terminal": functional_terminal,
        "physically_valid_scoreable": physical_valid,
        "functional_pass": functional_pass,
        "valid_child_conditional_delta_fmax_mhz": conditional,
        "root_fallback_deployment_delta_fmax_mhz": conditional if conditional is not None else 0.0,
    }


__all__ = [
    "CAUSAL_LINEAGE_VERSION", "SCIENTIFIC_EVIDENCE_VERSION", "STRICT_FUNCTIONAL_PASS",
    "boundary_evidence", "canonical_scientific_outcome",
    "causal_lineage",
    "strict_boundary_certificate", "strict_boundary_certificate_v3",
    "strict_boundary_certificate_v4",
    "classify_functional_validation", "file_sha256", "freeze_epsilon_fmax",
    "score_fmax_delta",
]
