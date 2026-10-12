from __future__ import annotations

import pytest
from optimizer_pipeline.dof.scientific_evidence import strict_boundary_certificate

from optimizer_pipeline.dof.hardblock_compatibility import (
    build_allocation,
    build_relation,
    maximum_matching,
    pair_hash,
    validate_allocation,
    validate_relation,
)


def test_maximum_matching_escapes_greedy_trap() -> None:
    relation = {"hardblock_a": ["site_x", "site_y"], "hardblock_b": ["site_x"]}
    assert maximum_matching(relation) == (
        ("hardblock_a", "site_y"),
        ("hardblock_b", "site_x"),
    )


def test_exact_two_target_relation_is_feasible() -> None:
    evidence = build_relation(
        root_sha256="a" * 64,
        stage="ROUTED",
        target_rows=[
            {
                "handle": "hardblock_a",
                "target_identity_sha256": "1" * 64,
                "resource_type": "BRAM",
                "compatible_site_handles": ["site_x"],
            },
            {
                "handle": "hardblock_b",
                "target_identity_sha256": "2" * 64,
                "resource_type": "BRAM",
                "compatible_site_handles": ["site_y"],
            },
        ],
        target_query_sha256="b" * 64,
        target_site_query_sha256={
            "hardblock_a": "c" * 64,
            "hardblock_b": "d" * 64,
        },
    )
    validate_relation(evidence)
    assert evidence["feasible"] is True
    assert evidence["matching"]["matching_size"] == 2


def test_empty_target_relation_is_not_feasible() -> None:
    evidence = build_relation(
        root_sha256="a" * 64,
        stage="ROUTED",
        target_rows=[
            {
                "handle": "hardblock_a",
                "target_identity_sha256": "1" * 64,
                "resource_type": "BRAM",
                "compatible_site_handles": ["site_x"],
            },
            {
                "handle": "hardblock_b",
                "target_identity_sha256": "2" * 64,
                "resource_type": "BRAM",
                "compatible_site_handles": [],
            },
        ],
        target_query_sha256="b" * 64,
        target_site_query_sha256={
            "hardblock_a": "c" * 64,
            "hardblock_b": "d" * 64,
        },
    )
    assert evidence["feasible"] is False
    assert evidence["matching"]["matching_size"] == 1


def test_manifest_count_cannot_override_relation_rows() -> None:
    evidence = build_relation(
        root_sha256="a" * 64,
        stage="ROUTED",
        target_rows=[{
            "handle": "hardblock_a",
            "target_identity_sha256": "1" * 64,
            "resource_type": "BRAM",
            "compatible_site_handles": ["site_x"],
        }],
        target_query_sha256="b" * 64,
        target_site_query_sha256={"hardblock_a": "c" * 64},
    )
    tampered = dict(evidence)
    tampered["targets"] = list(evidence["targets"]) + [dict(evidence["targets"][0])]
    with pytest.raises(ValueError, match="duplicate_or_invalid|hash_mismatch"):
        validate_relation(tampered)


def _two_target_relation() -> dict:
    return build_relation(
        root_sha256="a" * 64, stage="ROUTED",
        target_rows=[
            {"handle": "hardblock_a", "target_identity_sha256": "1" * 64, "resource_type": "BRAM", "compatible_site_handles": ["site_x", "site_y"]},
            {"handle": "hardblock_b", "target_identity_sha256": "2" * 64, "resource_type": "BRAM", "compatible_site_handles": ["site_x"]},
        ],
        target_query_sha256="b" * 64,
        target_site_query_sha256={"hardblock_a": "c" * 64, "hardblock_b": "d" * 64},
    )


def test_exact_allocation_escapes_greedy_trap_and_binds_all_evidence() -> None:
    relation = _two_target_relation()
    allocation = build_allocation(
        relation=relation, target_handles=["hardblock_a", "hardblock_b"],
        submitted_site_handles=["site_x", "site_y"], strategy="FORWARD",
        root_sha256="a" * 64, stage="ROUTED",
    )
    assert allocation["site_handles"] == ["site_y", "site_x"]
    assert allocation["target_site_pair_hashes"] == [
        pair_hash("hardblock_a", "site_y"), pair_hash("hardblock_b", "site_x"),
    ]
    validate_allocation(allocation, relation, root_sha256="a" * 64, stage="ROUTED")


def test_resource_compatible_but_nonmember_site_is_rejected() -> None:
    relation = _two_target_relation()
    with pytest.raises(ValueError, match="matching_unavailable"):
        build_allocation(
            relation=relation, target_handles=["hardblock_a", "hardblock_b"],
            submitted_site_handles=["site_y", "site_z"], strategy="FORWARD",
            root_sha256="a" * 64, stage="ROUTED",
        )


def test_duplicate_site_submission_is_rejected() -> None:
    with pytest.raises(ValueError, match="duplicate_or_empty"):
        build_allocation(
            relation=_two_target_relation(),
            target_handles=["hardblock_a", "hardblock_b"],
            submitted_site_handles=["site_x", "site_x"], strategy="FORWARD",
            root_sha256="a" * 64, stage="ROUTED",
        )


def test_stale_root_or_stage_is_rejected_at_build_and_revalidation() -> None:
    relation = _two_target_relation()
    with pytest.raises(ValueError, match="root_or_stage_stale"):
        build_allocation(
            relation=relation, target_handles=["hardblock_a", "hardblock_b"],
            submitted_site_handles=["site_x", "site_y"], strategy="FORWARD",
            root_sha256="f" * 64, stage="ROUTED",
        )
    allocation = build_allocation(
        relation=relation, target_handles=["hardblock_a", "hardblock_b"],
        submitted_site_handles=["site_x", "site_y"], strategy="FORWARD",
        root_sha256="a" * 64, stage="ROUTED",
    )
    with pytest.raises(ValueError, match="root_or_stage_stale"):
        validate_allocation(allocation, relation, root_sha256="a" * 64, stage="PLACED")


def test_allocation_pair_tampering_is_rejected() -> None:
    relation = _two_target_relation()
    allocation = build_allocation(
        relation=relation, target_handles=["hardblock_a", "hardblock_b"],
        submitted_site_handles=["site_x", "site_y"], strategy="FORWARD",
        root_sha256="a" * 64, stage="ROUTED",
    )
    tampered = dict(allocation) | {"target_site_pair_hashes": ["0" * 64, "0" * 64]}
    with pytest.raises(ValueError, match="hash_or_schema_mismatch"):
        validate_allocation(tampered, relation, root_sha256="a" * 64, stage="ROUTED")


def test_every_target_row_is_materialized_not_only_the_first() -> None:
    relation = build_relation(
        root_sha256="a" * 64, stage="ROUTED",
        target_rows=[
            {"handle": "hardblock_a", "target_identity_sha256": "1" * 64, "resource_type": "BRAM", "compatible_site_handles": ["site_x"]},
            {"handle": "hardblock_b", "target_identity_sha256": "2" * 64, "resource_type": "BRAM", "compatible_site_handles": ["site_y"]},
            {"handle": "hardblock_c", "target_identity_sha256": "3" * 64, "resource_type": "BRAM", "compatible_site_handles": ["site_z"]},
        ],
        target_query_sha256="b" * 64,
        target_site_query_sha256={
            "hardblock_a": "c" * 64, "hardblock_b": "d" * 64,
            "hardblock_c": "e" * 64,
        },
    )
    assert [len(row["compatible_site_handles"]) for row in relation["targets"]] == [1, 1, 1]
    assert relation["matching"]["matching_size"] == 3


def test_strict_certificate_is_bound_to_relation_and_dispatch_pair_proof() -> None:
    candidate = {"language": "G", "steps": [
        {"step_id": "s1", "op": "SELECT_WHERE", "args": {"source_type": "hardblock", "predicate": {"op": "COMPARE", "field": "resource_type", "operator": "EQ", "value": "BRAM"}, "top_k": 2}, "bind": "hardblocks"},
        {"step_id": "s2", "op": "SELECT_WHERE", "args": {"source_type": "site", "predicate": {"op": "COMPARE", "field": "object_type", "operator": "EQ", "value": "site"}, "top_k": 2}, "bind": "sites"},
        {"step_id": "s3", "op": "COUPLE_SITES", "args": {"hardblocks": {"ref": "hardblocks"}, "sites": {"ref": "sites"}, "strategy": "FORWARD"}, "bind": "allocation"},
        {"step_id": "s4", "op": "UNROUTE", "args": {}},
        {"step_id": "s5", "op": "APPLY_ALLOCATION", "args": {"allocation": {"ref": "allocation"}}},
        {"step_id": "s6", "op": "ROUTE", "args": {"directive": "Default"}},
    ]}
    relation_hash = "a" * 64
    pair_hashes = ["b" * 64, "c" * 64]
    execution = {
        "selected_count": 2, "transformed_count": 2,
        "child": {"sha256": "d" * 64},
        "binding_records": [{
            "binding_id": "allocation", "binding_type": "allocation",
            "binding_cardinality": 1, "binding_content_hash": "e" * 64,
            "matching_size": 2, "compatibility_relation_sha256": relation_hash,
            "feasibility_query_sha256": "f" * 64,
            "target_site_pair_hashes": pair_hashes,
            "allocation_sha256": "1" * 64,
        }],
        "call_trace": [{
            "operation": "vivado.wp014h_apply_allocation", "source_step_id": "s5",
            "worker_dispatch_id": "wp015b-005", "actual_target_identity_hash": "2" * 64,
            "pair_membership_revalidated": True,
            "compatibility_relation_sha256": relation_hash,
            "target_site_pair_hashes": pair_hashes,
            "allocation_sha256": "1" * 64,
        }],
        "mechanism_state_deltas": [{
            "state_delta_id": "allocation", "mechanism_family": "hardblock_allocation",
            "changed_count": 2, "changed_identity_hash": "3" * 64,
            "evidence_strength": "PROPERTY_BEFORE_AFTER",
        }],
    }
    passed = strict_boundary_certificate(
        candidate=candidate, execution_started=True, execution=execution,
    )
    assert passed["strict_boundary_exercised"] is True
    tampered = dict(execution) | {"call_trace": [dict(execution["call_trace"][0]) | {"compatibility_relation_sha256": "9" * 64}]}
    failed = strict_boundary_certificate(
        candidate=candidate, execution_started=True, execution=tampered,
    )
    assert failed["strict_boundary_exercised"] is False
    assert failed["noncertificate_reason"] == "EXACT_COMPATIBILITY_RELATION_OR_DISPATCH_PROOF_MISSING"
