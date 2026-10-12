"""QoR-blind named-family evidence shared by proposal and runtime certificates."""

from __future__ import annotations

import hashlib
import json
from typing import Any, Mapping, Sequence


NAMED_FAMILY_SEMANTICS_VERSION = "dof.named-family-evidence.v1"
NAMED_FAMILY_SEMANTICS_VERSION_V2 = "dof.named-family-evidence.v2"


def _bytes(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def _hash(value: Any) -> str:
    return hashlib.sha256(value if isinstance(value, bytes) else _bytes(value)).hexdigest()


def _ref(row: Mapping[str, Any], key: str) -> str | None:
    value = (row.get("args") or {}).get(key)
    return str(value.get("ref")) if isinstance(value, Mapping) and isinstance(value.get("ref"), str) else None


def _compiled(value: Any) -> dict[str, Any]:
    if hasattr(value, "public_payload"):
        return dict(value.public_payload())
    return dict(value or {})


def named_family_evidence_v1(
    candidate: Mapping[str, Any], compiled: Any, *, runtime: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Enumerate every admitted named-family path and certify deterministically."""

    plan = _compiled(compiled)
    steps = [row for row in candidate.get("steps", ()) if isinstance(row, Mapping)]
    hosts = [row for row in plan.get("host_steps", ()) if isinstance(row, Mapping)]
    records = {str(row.get("binding_id")): dict(row) for row in plan.get("binding_records", ())
               if isinstance(row, Mapping) and row.get("binding_id")}
    runtime = dict(runtime or {})
    trace = [row for row in runtime.get("call_trace", ()) if isinstance(row, Mapping)]
    deltas = [row for row in runtime.get("mechanism_state_deltas", ()) if isinstance(row, Mapping)]
    child = runtime.get("child") if isinstance(runtime.get("child"), Mapping) else {}
    runtime_requested = bool(runtime)
    paths: list[dict[str, Any]] = []

    def emit(core: dict[str, Any], reasons: Sequence[str], expected_ops: set[str]) -> None:
        proposal_valid = not reasons
        path_hash = _hash(core | {"proposal_rejection_reasons": list(reasons)})
        dispatches = []
        if runtime_requested and proposal_valid:
            dispatches = [row for row in trace if row.get("source_step_id") == core.get("consumer_step_id")
                          and row.get("source_construct") == core.get("consumer_construct")
                          and row.get("operation") in expected_ops
                          and any(item.get("binding_id") == core.get("output_binding_id")
                                  for item in row.get("consumer_input_slots", ())) ]
        family_marker = str(core["mechanism_family"]).lower()
        matching_deltas = [row for row in deltas
                           if str(row.get("mechanism_family") or row.get("state_delta_id")) == family_marker
                           and int(row.get("changed_count") or 0) > 0
                           and row.get("before_hash") != row.get("after_hash")]
        runtime_reasons = []
        if runtime_requested and proposal_valid:
            observed_dispatch_ops = {str(row.get("operation")) for row in dispatches}
            if not expected_ops <= observed_dispatch_ops:
                runtime_reasons.append("exact_worker_dispatch_missing")
            if not any(isinstance(row.get("actual_target_identity_hash"), str) for row in dispatches):
                runtime_reasons.append("actual_target_identity_missing")
            if not matching_deltas:
                runtime_reasons.append("mechanism_specific_state_delta_missing")
            if not isinstance(child.get("sha256"), str):
                runtime_reasons.append("child_artifact_hash_missing")
        value = core | {
            "path_hash": path_hash,
            "proposal_path_valid": proposal_valid,
            "proposal_rejection_reasons": list(reasons),
            "runtime_path_valid": proposal_valid and runtime_requested and not runtime_reasons,
            "runtime_rejection_reasons": runtime_reasons,
            "runtime_dispatch_ids": sorted(str(row.get("worker_dispatch_id")) for row in dispatches),
            "runtime_actual_target_identity_hashes": sorted(
                str(row["actual_target_identity_hash"]) for row in dispatches
                if isinstance(row.get("actual_target_identity_hash"), str)
            ),
            "runtime_state_delta_hashes": sorted(_hash(row) for row in matching_deltas),
            "child_artifact_hash": child.get("sha256"),
        }
        paths.append(value)

    diffs = [row for row in steps if row.get("op") == "DIFF"]
    batches = [row for row in steps if row.get("op") == "BATCH_ROUTE_NETS"]
    for producer in diffs:
        output = str(producer.get("bind") or "")
        record = records.get(output, {})
        left, removed = _ref(producer, "left"), _ref(producer, "right")
        matching = [row for row in batches if _ref(row, "typed_nets") == output] or [None]
        for consumer in matching:
            consumer_id = None if consumer is None else consumer.get("step_id")
            compiled_consumers = [row for row in hosts if row.get("source_step_id") == consumer_id
                                  and row.get("source_construct") == "BATCH_ROUTE_NETS"
                                  and any(item.get("binding_id") == output for item in row.get("input_bindings", ()))]
            left_n = int(record.get("left_cardinality") or 0)
            removed_n = int(record.get("removed_cardinality") or 0)
            result_n = int(record.get("binding_cardinality") or 0)
            indices = record.get("selected_source_indices")
            expected_indices = list(range(removed_n, left_n))
            reasons = []
            checks = (
                (bool(left and removed and output), "diff_binding_path_incomplete"),
                (record.get("derivation") == "DIFF", "diff_derivation_record_missing"),
                (record.get("left_binding_id") == left and record.get("right_binding_id") == removed,
                 "diff_operand_binding_mismatch"),
                (record.get("right_is_nonempty_proper_prefix_of_left") is True,
                 "removed_binding_not_nonempty_proper_prefix"),
                (record.get("result_is_exact_left_minus_right") is True, "result_not_exact_left_minus_prefix"),
                (result_n >= 2, "result_cardinality_below_two"),
                (indices == expected_indices, "privacy_safe_source_index_proof_failed"),
                (bool(compiled_consumers), "compiled_batch_consumer_missing"),
            )
            reasons.extend(reason for passed, reason in checks if not passed)
            emit({
                "mechanism_family": "NONPREFIX_MULTI_NET_BATCH",
                "producer_step_id": producer.get("step_id"), "producer_construct": "DIFF",
                "consumer_step_id": consumer_id, "consumer_construct": "BATCH_ROUTE_NETS",
                "output_binding_id": output, "left_binding_id": left, "removed_prefix_binding_id": removed,
                "left_content_hash": record.get("left_content_hash"),
                "removed_prefix_content_hash": record.get("removed_content_hash"),
                "result_content_hash": record.get("binding_content_hash"),
                "left_cardinality": left_n, "removed_prefix_cardinality": removed_n,
                "result_cardinality": result_n, "selected_source_indices": indices,
            }, reasons, {"vivado.wp014h_batch_route_nets"})

    regions = [row for row in steps if row.get("op") == "CREATE_REGION"]
    assignments = [row for row in steps if row.get("op") == "ASSIGN_REGION"]
    for producer in regions:
        output = str(producer.get("bind") or "")
        record = records.get(output, {})
        matching = [row for row in assignments if _ref(row, "region_handle") == output] or [None]
        for consumer in matching:
            consumer_id = None if consumer is None else consumer.get("step_id")
            target = None if consumer is None else _ref(consumer, "target_handle")
            target_record = records.get(str(target), {})
            compiled_consumers = [row for row in hosts if row.get("source_step_id") == consumer_id
                                  and row.get("source_construct") == "ASSIGN_REGION"
                                  and {item.get("binding_id") for item in row.get("input_bindings", ())} >= {output, target}]
            bounds = record.get("generated_bounds")
            integer = isinstance(bounds, list) and len(bounds) == 4 and all(
                isinstance(value, int) and not isinstance(value, bool) for value in bounds)
            nondegenerate = bool(integer and (bounds[1] > bounds[0] or bounds[3] > bounds[2]))
            reasons = []
            checks = (
                (record.get("derivation") == "CREATE_REGION", "generated_region_record_missing"),
                (integer, "generated_bounds_not_integer"),
                (nondegenerate, "generated_bounds_degenerate"),
                (record.get("generated_bounds_in_device") is True, "generated_bounds_out_of_device"),
                (record.get("generated_bounds_not_predefined") is True, "generated_bounds_equal_predefined_region"),
                (int(record.get("predefined_geometry_count") or 0) == len(record.get("predefined_geometry_bounds") or []),
                 "predefined_geometry_set_incomplete"),
                (int(target_record.get("binding_cardinality") or 0) > 0, "target_binding_empty_or_missing"),
                (bool(compiled_consumers), "compiled_spatial_assignment_missing"),
            )
            reasons.extend(reason for passed, reason in checks if not passed)
            emit({
                "mechanism_family": "SPATIAL_GENERATED_GEOMETRY",
                "producer_step_id": producer.get("step_id"), "producer_construct": "CREATE_REGION",
                "consumer_step_id": consumer_id, "consumer_construct": "ASSIGN_REGION",
                "output_binding_id": output, "target_binding_id": target,
                "generated_bounds": bounds, "generated_bounds_hash": record.get("generated_bounds_hash"),
                "predefined_geometry_set_hash": record.get("predefined_geometry_set_hash"),
                "predefined_geometry_count": record.get("predefined_geometry_count"),
                "predefined_geometry_bounds": record.get("predefined_geometry_bounds"),
                "nonmembership_proof": record.get("generated_bounds_not_predefined"),
            }, reasons, {"vivado.wp014c_create_pblock", "vivado.wp014c_assign_pblock"})

    couples = [row for row in steps if row.get("op") == "COUPLE_SITES"]
    applications = [row for row in steps if row.get("op") == "APPLY_ALLOCATION"]
    for producer in couples:
        output = str(producer.get("bind") or "")
        record = records.get(output, {})
        matching = [row for row in applications if _ref(row, "allocation") == output] or [None]
        for consumer in matching:
            consumer_id = None if consumer is None else consumer.get("step_id")
            compiled_consumers = [row for row in hosts if row.get("source_step_id") == consumer_id
                                  and row.get("source_construct") == "APPLY_ALLOCATION"
                                  and any(item.get("binding_id") == output for item in row.get("input_bindings", ()))]
            matching_size = int(record.get("matching_size") or 0)
            reasons = []
            checks = ((matching_size >= 2, "matching_cardinality_below_two"),
                      (isinstance(record.get("compatibility_relation_sha256"), str), "compatibility_relation_missing"),
                      (bool(compiled_consumers), "compiled_allocation_consumer_missing"))
            reasons.extend(reason for passed, reason in checks if not passed)
            emit({
                "mechanism_family": "COUPLED_HARDBLOCK_SITE_ALLOCATION",
                "producer_step_id": producer.get("step_id"), "producer_construct": "COUPLE_SITES",
                "consumer_step_id": consumer_id, "consumer_construct": "APPLY_ALLOCATION",
                "output_binding_id": output, "matching_size": matching_size,
                "compatibility_relation_sha256": record.get("compatibility_relation_sha256"),
                "allocation_sha256": record.get("allocation_sha256"),
            }, reasons, {"vivado.wp014h_apply_allocation"})

    paths.sort(key=lambda row: row["path_hash"])
    accepted_proposal = sorted(row["path_hash"] for row in paths if row["proposal_path_valid"])
    accepted_runtime = sorted(row["path_hash"] for row in paths if row["runtime_path_valid"])
    primary = (accepted_runtime if runtime_requested else accepted_proposal)
    return {
        "schema_version": NAMED_FAMILY_SEMANTICS_VERSION,
        "runtime_evidence_requested": runtime_requested,
        "all_evaluated_paths": paths,
        "accepted_path_hashes": accepted_runtime if runtime_requested else accepted_proposal,
        "rejected_path_reasons": [
            {"path_hash": row["path_hash"], "proposal": row["proposal_rejection_reasons"],
             "runtime": row["runtime_rejection_reasons"]}
            for row in paths if row["path_hash"] not in primary
        ],
        "selected_certificate_path_hash": primary[0] if primary else None,
        "selected_mechanism_family": next(
            (row["mechanism_family"] for row in paths if row["path_hash"] == primary[0]), None,
        ) if primary else None,
        "named_family_valid": bool(primary),
    }


def named_family_evidence_v2(
    candidate: Mapping[str, Any], compiled: Any, *, runtime: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Recompute v4 family facts from privacy-safe primitive evidence.

    V1 is retained for historical reproduction.  V2 deliberately distrusts
    every published conclusion Boolean and every published spatial hash.
    """

    prior = named_family_evidence_v1(candidate, compiled, runtime=runtime)
    plan = _compiled(compiled)
    records = {str(row.get("binding_id")): dict(row) for row in plan.get("binding_records", ())
               if isinstance(row, Mapping) and row.get("binding_id")}
    runtime_value = dict(runtime or {})
    trace = [row for row in runtime_value.get("call_trace", ()) if isinstance(row, Mapping)]
    deltas = [row for row in runtime_value.get("mechanism_state_deltas", ()) if isinstance(row, Mapping)]
    child = runtime_value.get("child") if isinstance(runtime_value.get("child"), Mapping) else {}
    runtime_requested = bool(runtime)
    paths: list[dict[str, Any]] = []

    for old in prior["all_evaluated_paths"]:
        family = str(old["mechanism_family"])
        record = records.get(str(old.get("output_binding_id")), {})
        reasons: list[str] = []
        proof: dict[str, Any] = {}
        if family == "NONPREFIX_MULTI_NET_BATCH":
            left_n = int(record.get("left_cardinality") or 0)
            right_n = int(record.get("removed_cardinality") or 0)
            result_n = int(record.get("binding_cardinality") or 0)
            right_indices = record.get("right_source_indices")
            result_indices = record.get("result_source_indices")
            selected_indices = record.get("selected_source_indices")
            if (
                isinstance(result_indices, list)
                and isinstance(selected_indices, list)
                and result_indices != selected_indices
            ):
                reasons.append("redundant_result_selected_indices_mismatch")
            if not isinstance(right_indices, list):
                right_indices = []
            if not isinstance(result_indices, list):
                result_indices = selected_indices
            if not isinstance(result_indices, list):
                result_indices = []
            index_shape = all(isinstance(value, int) and not isinstance(value, bool)
                              for value in [*right_indices, *result_indices])
            indices_in_range = index_shape and all(0 <= value < left_n
                                                   for value in [*right_indices, *result_indices])
            right_unique = len(set(right_indices)) == len(right_indices)
            result_unique = len(set(result_indices)) == len(result_indices)
            proper_prefix = bool(
                index_shape and 0 < right_n < left_n
                and len(right_indices) == right_n
                and right_indices == list(range(right_n))
            )
            exact_suffix = bool(
                proper_prefix and len(result_indices) == result_n
                and result_indices == list(range(right_n, left_n))
                and right_n + result_n == left_n
            )
            result_not_prefix = bool(result_indices != list(range(result_n)))
            checks = (
                (record.get("derivation") == "DIFF", "diff_derivation_record_missing"),
                (record.get("left_binding_id") == old.get("left_binding_id")
                 and record.get("right_binding_id") == old.get("removed_prefix_binding_id"),
                 "diff_operand_binding_mismatch"),
                (index_shape and indices_in_range and right_unique and result_unique,
                 "privacy_safe_index_shape_invalid"),
                (proper_prefix, "removed_binding_not_recomputed_proper_prefix"),
                (exact_suffix, "result_not_recomputed_exact_left_minus_prefix"),
                (result_not_prefix, "diff_result_is_source_prefix"),
                (result_n >= 2, "result_cardinality_below_two"),
                (record.get("right_is_nonempty_proper_prefix_of_left") is proper_prefix,
                 "stored_proper_prefix_boolean_inconsistent"),
                (record.get("result_is_exact_left_minus_right") is exact_suffix,
                 "stored_exact_difference_boolean_inconsistent"),
                (record.get("result_is_source_prefix") is (not result_not_prefix),
                 "stored_result_prefix_boolean_inconsistent"),
                ("compiled_batch_consumer_missing" not in old.get("proposal_rejection_reasons", ()),
                 "compiled_batch_consumer_missing"),
            )
            reasons.extend(reason for passed, reason in checks if not passed)
            proof = {
                "left_binding_identity_hash": _hash(old.get("left_binding_id")),
                "right_binding_identity_hash": _hash(old.get("removed_prefix_binding_id")),
                "result_binding_identity_hash": _hash(old.get("output_binding_id")),
                "left_cardinality": left_n, "right_cardinality": right_n,
                "result_cardinality": result_n, "right_source_indices": right_indices,
                "result_source_indices": result_indices,
                "proper_prefix_recomputed": proper_prefix,
                "exact_left_minus_right_recomputed": exact_suffix,
                "result_not_source_prefix_recomputed": result_not_prefix,
            }
        elif family == "SPATIAL_GENERATED_GEOMETRY":
            bounds = record.get("generated_bounds")
            device = record.get("device_geometry_bounds")
            predefined = record.get("predefined_geometry_bounds")
            integer = isinstance(bounds, list) and len(bounds) == 4 and all(
                isinstance(value, int) and not isinstance(value, bool) for value in bounds)
            device_integer = isinstance(device, list) and len(device) == 4 and all(
                isinstance(value, int) and not isinstance(value, bool) for value in device)
            predefined_shape = isinstance(predefined, list) and all(
                isinstance(row, list) and len(row) == 4 and all(
                    isinstance(value, int) and not isinstance(value, bool) for value in row)
                for row in predefined
            )
            ordered_predefined = (
                [list(row) for row in sorted({tuple(row) for row in predefined})]
                if predefined_shape else []
            )
            generated_hash = _hash(bounds) if integer else None
            set_hash = _hash(ordered_predefined) if predefined_shape else None
            nondegenerate = bool(integer and (bounds[1] > bounds[0] or bounds[3] > bounds[2]))
            in_device = bool(integer and device_integer and
                device[0] <= bounds[0] <= bounds[1] <= device[1] and
                device[2] <= bounds[2] <= bounds[3] <= device[3])
            nonmember = bool(integer and predefined_shape and bounds not in ordered_predefined)
            checks = (
                (record.get("derivation") == "CREATE_REGION", "generated_region_record_missing"),
                (integer, "generated_bounds_not_integer"),
                (device_integer, "device_geometry_bounds_missing_or_invalid"),
                (nondegenerate, "generated_bounds_degenerate"),
                (in_device, "generated_bounds_out_of_device"),
                (predefined_shape, "predefined_geometry_set_shape_invalid"),
                (int(record.get("predefined_geometry_count") or -1) == len(ordered_predefined),
                 "predefined_geometry_count_inconsistent"),
                (record.get("generated_bounds_hash") == generated_hash,
                 "generated_bounds_hash_inconsistent"),
                (record.get("predefined_geometry_set_hash") == set_hash,
                 "predefined_geometry_set_hash_inconsistent"),
                (nonmember, "generated_bounds_equal_predefined_region"),
                (record.get("generated_bounds_not_predefined") is nonmember,
                 "stored_nonmembership_boolean_inconsistent"),
                ("target_binding_empty_or_missing" not in old.get("proposal_rejection_reasons", ()),
                 "target_binding_empty_or_missing"),
                ("compiled_spatial_assignment_missing" not in old.get("proposal_rejection_reasons", ()),
                 "compiled_spatial_assignment_missing"),
            )
            reasons.extend(reason for passed, reason in checks if not passed)
            proof = {
                "generated_bounds": bounds, "device_geometry_bounds": device,
                "generated_bounds_sha256_recomputed": generated_hash,
                "predefined_geometry_bounds": ordered_predefined,
                "predefined_geometry_set_sha256_recomputed": set_hash,
                "predefined_geometry_count_recomputed": len(ordered_predefined),
                "integer_coordinates_recomputed": integer,
                "nondegenerate_recomputed": nondegenerate,
                "in_device_recomputed": in_device, "nonmembership_recomputed": nonmember,
            }
        else:
            reasons.extend(old.get("proposal_rejection_reasons", ()))
            proof = {
                "matching_size": old.get("matching_size"),
                "compatibility_relation_sha256": old.get("compatibility_relation_sha256"),
                "allocation_sha256": old.get("allocation_sha256"),
            }

        semantic = {
            "mechanism_family": family,
            "producer_step_id": old.get("producer_step_id"),
            "producer_construct": old.get("producer_construct"),
            "consumer_step_id": old.get("consumer_step_id"),
            "consumer_construct": old.get("consumer_construct"),
            "consumer_binding_identity_hash": _hash(old.get("output_binding_id")),
            "privacy_safe_proof": proof,
        }
        path_hash = _hash(semantic)
        proposal_valid = not reasons
        expected_ops = {
            "NONPREFIX_MULTI_NET_BATCH": {"vivado.wp014h_batch_route_nets"},
            "SPATIAL_GENERATED_GEOMETRY": {
                "vivado.wp014c_create_pblock", "vivado.wp014c_assign_pblock",
            },
            "COUPLED_HARDBLOCK_SITE_ALLOCATION": {"vivado.wp014h_apply_allocation"},
        }[family]
        dispatches = [row for row in trace
                      if row.get("source_step_id") == old.get("consumer_step_id")
                      and row.get("source_construct") == old.get("consumer_construct")
                      and row.get("operation") in expected_ops
                      and any(item.get("binding_id") == old.get("output_binding_id")
                              for item in row.get("consumer_input_slots", ()))]
        matching_deltas = [row for row in deltas
                           if str(row.get("mechanism_family") or row.get("state_delta_id")) == family.lower()
                           and int(row.get("changed_count") or 0) > 0
                           and row.get("before_hash") != row.get("after_hash")]
        runtime_reasons: list[str] = []
        if runtime_requested and proposal_valid:
            if not expected_ops <= {str(row.get("operation")) for row in dispatches}:
                runtime_reasons.append("exact_worker_dispatch_missing")
            if not all(isinstance(row.get("worker_dispatch_id"), str) for row in dispatches):
                runtime_reasons.append("worker_dispatch_id_missing")
            if not dispatches or not all(isinstance(row.get("actual_target_identity_hash"), str)
                                         for row in dispatches):
                runtime_reasons.append("actual_target_identity_missing")
            if not matching_deltas:
                runtime_reasons.append("mechanism_specific_state_delta_missing")
            if not isinstance(child.get("sha256"), str):
                runtime_reasons.append("child_artifact_hash_missing")
        paths.append(semantic | {
            "path_hash": path_hash, "proposal_path_valid": proposal_valid,
            "proposal_rejection_reasons": sorted(set(reasons)),
            "runtime_path_valid": bool(proposal_valid and runtime_requested and not runtime_reasons),
            "runtime_rejection_reasons": runtime_reasons,
            "runtime_binding": {
                "consumer_source_step_id": old.get("consumer_step_id"),
                "worker_operations": sorted(str(row.get("operation")) for row in dispatches),
                "worker_dispatch_ids": sorted(str(row.get("worker_dispatch_id")) for row in dispatches),
                "consumer_binding_identity_hash": _hash(old.get("output_binding_id")),
                "actual_target_identity_hashes": sorted(str(row.get("actual_target_identity_hash")) for row in dispatches),
                "mechanism_state_delta_hashes": sorted(_hash(row) for row in matching_deltas),
                "child_artifact_hash": child.get("sha256"),
                "state_delta_path_specificity": (
                    "NOT_REQUESTED" if not runtime_requested else
                    "FAMILY_LEVEL_UNAMBIGUOUS_SINGLE_PATH_LIMITATION"
                ),
            },
        })

    # A family-level delta cannot distinguish multiple otherwise-valid paths.
    for family in {row["mechanism_family"] for row in paths}:
        valid = [row for row in paths if row["mechanism_family"] == family and row["proposal_path_valid"]]
        if runtime_requested and len(valid) > 1:
            for row in valid:
                row["runtime_path_valid"] = False
                row["runtime_rejection_reasons"].append("family_level_state_delta_ambiguous_across_paths")
                row["runtime_binding"]["state_delta_path_specificity"] = "AMBIGUOUS_MULTIPLE_PROPOSAL_PATHS"

    paths.sort(key=lambda row: row["path_hash"])
    accepted = sorted(
        row["path_hash"] for row in paths
        if (row["runtime_path_valid"] if runtime_requested else row["proposal_path_valid"])
    )
    selected = accepted[0] if accepted else None
    return {
        "schema_version": NAMED_FAMILY_SEMANTICS_VERSION_V2,
        "runtime_evidence_requested": runtime_requested,
        "all_evaluated_paths": paths,
        "accepted_path_hashes": accepted,
        "rejected_path_reasons": [{"path_hash": row["path_hash"],
            "proposal": row["proposal_rejection_reasons"], "runtime": row["runtime_rejection_reasons"]}
            for row in paths if row["path_hash"] not in accepted],
        "selected_certificate_path_hash": selected,
        "selected_mechanism_family": next((row["mechanism_family"] for row in paths
                                           if row["path_hash"] == selected), None),
        "named_family_valid": bool(selected),
    }


__all__ = [
    "NAMED_FAMILY_SEMANTICS_VERSION", "NAMED_FAMILY_SEMANTICS_VERSION_V2",
    "named_family_evidence_v1", "named_family_evidence_v2",
]
