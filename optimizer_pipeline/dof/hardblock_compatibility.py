"""Exact root-bound hardblock-to-site compatibility evidence for WP-015B."""

from __future__ import annotations

import hashlib
from typing import Any, Iterable, Mapping, Sequence

from .single_submission.specs import canonical_json_bytes


RELATION_SCHEMA = "dof.wp015b-hardblock-compatibility-relation.v1"
ALLOCATION_SCHEMA = "dof.wp015b-hardblock-allocation.v1"
MATCHING_ALGORITHM = "deterministic-augmenting-path-lexical-v1"


def digest(value: Any) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def pair_hash(target_handle: str, site_handle: str) -> str:
    return digest({"target_handle": target_handle, "site_handle": site_handle})


def feasibility_query_hash(evidence: Mapping[str, Any]) -> str:
    """Bind the parent target query and every target-specific site query."""

    targets = evidence.get("targets")
    if not isinstance(targets, list):
        raise ValueError("compatibility_targets_invalid")
    return digest({
        "hardblock_query_sha256": evidence.get("hardblock_query_sha256"),
        "target_site_query_sha256": {
            str(row.get("target_handle")): row.get("target_site_query_sha256")
            for row in targets if isinstance(row, Mapping)
        },
    })


def _ordered_sites(sites: Iterable[str], strategy: str) -> tuple[str, ...]:
    ordered = sorted(set(sites))
    if strategy == "REVERSE":
        ordered.reverse()
    elif strategy == "ROTATE_ONE" and ordered:
        ordered = ordered[1:] + ordered[:1]
    elif strategy != "FORWARD":
        raise ValueError("allocation_strategy_invalid")
    return tuple(ordered)


def maximum_matching(
    relation: Mapping[str, Sequence[str]],
    *,
    target_order: Sequence[str] | None = None,
    strategy: str = "FORWARD",
) -> tuple[tuple[str, str], ...]:
    """Return a deterministic maximum matching, including greedy-trap graphs."""

    targets = tuple(target_order) if target_order is not None else tuple(sorted(relation))
    if len(targets) != len(set(targets)) or set(targets) - set(relation):
        raise ValueError("matching_target_order_invalid")
    edges = {target: _ordered_sites(relation[target], strategy) for target in targets}
    site_owner: dict[str, str] = {}

    def augment(target: str, seen_sites: set[str], seen_targets: set[str]) -> bool:
        if target in seen_targets:
            return False
        seen_targets.add(target)
        for site in edges[target]:
            if site in seen_sites:
                continue
            seen_sites.add(site)
            previous = site_owner.get(site)
            if previous is None or augment(previous, seen_sites, seen_targets):
                site_owner[site] = target
                return True
        return False

    for target in targets:
        augment(target, set(), set())
    by_target = {target: site for site, target in site_owner.items()}
    return tuple((target, by_target[target]) for target in targets if target in by_target)


def build_relation(
    *,
    root_sha256: str,
    stage: str,
    target_rows: Sequence[Mapping[str, Any]],
    target_query_sha256: str,
    target_site_query_sha256: Mapping[str, str],
    strategy: str = "FORWARD",
) -> dict[str, Any]:
    if len(root_sha256) != 64 or stage not in {"PLACED", "ROUTED"}:
        raise ValueError("relation_root_or_stage_invalid")
    relation: dict[str, tuple[str, ...]] = {}
    target_records = []
    for row in target_rows:
        target = str(row.get("handle") or "")
        sites = tuple(sorted(set(str(item) for item in row.get("compatible_site_handles", ()))))
        query_sha = target_site_query_sha256.get(target)
        if not target or not query_sha:
            raise ValueError("target_relation_evidence_missing")
        relation[target] = sites
        target_records.append({
            "target_handle": target,
            "target_identity_sha256": str(row.get("target_identity_sha256") or ""),
            "resource_type": str(row.get("resource_type") or "ANY"),
            "compatible_site_handles": list(sites),
            "target_site_query_sha256": query_sha,
        })
    matching = maximum_matching(relation, strategy=strategy)
    relation_core = {
        "schema_version": RELATION_SCHEMA,
        "root_sha256": root_sha256,
        "stage": stage,
        "hardblock_query_sha256": target_query_sha256,
        "targets": sorted(target_records, key=lambda row: row["target_handle"]),
        "qor_fields_used": [],
    }
    relation_sha = digest(relation_core)
    matching_core = {
        "algorithm": MATCHING_ALGORITHM,
        "strategy": strategy,
        "pairs": [
            {
                "target_handle": target,
                "site_handle": site,
                "target_site_pair_sha256": pair_hash(target, site),
            }
            for target, site in matching
        ],
    }
    return relation_core | {
        "compatibility_relation_sha256": relation_sha,
        "matching": matching_core | {
            "matching_size": len(matching),
            "matching_certificate_sha256": digest(matching_core),
        },
        "compatible_target_count": sum(bool(sites) for sites in relation.values()),
        "distinct_compatible_site_count": len({site for sites in relation.values() for site in sites}),
        "feasible": len(target_rows) >= 2 and len(matching) >= 2,
    }


def relation_map(evidence: Mapping[str, Any]) -> dict[str, tuple[str, ...]]:
    return {
        str(row["target_handle"]): tuple(str(site) for site in row.get("compatible_site_handles", ()))
        for row in evidence.get("targets", ())
        if isinstance(row, Mapping) and row.get("target_handle")
    }


def validate_relation(evidence: Mapping[str, Any]) -> None:
    core = {
        "schema_version": evidence.get("schema_version"),
        "root_sha256": evidence.get("root_sha256"),
        "stage": evidence.get("stage"),
        "hardblock_query_sha256": evidence.get("hardblock_query_sha256"),
        "targets": evidence.get("targets"),
        "qor_fields_used": evidence.get("qor_fields_used"),
    }
    if evidence.get("schema_version") != RELATION_SCHEMA:
        raise ValueError("compatibility_relation_schema_mismatch")
    if evidence.get("qor_fields_used") != []:
        raise ValueError("compatibility_relation_qor_contamination")
    if not isinstance(evidence.get("root_sha256"), str) or len(str(evidence["root_sha256"])) != 64:
        raise ValueError("compatibility_relation_root_invalid")
    if evidence.get("stage") not in {"PLACED", "ROUTED"}:
        raise ValueError("compatibility_relation_stage_invalid")
    targets = evidence.get("targets")
    if not isinstance(targets, list):
        raise ValueError("compatibility_targets_invalid")
    target_handles = [str(row.get("target_handle")) for row in targets if isinstance(row, Mapping)]
    if len(target_handles) != len(targets) or len(target_handles) != len(set(target_handles)):
        raise ValueError("compatibility_target_rows_duplicate_or_invalid")
    for row in targets:
        sites = row.get("compatible_site_handles")
        if not isinstance(sites, list) or len(sites) != len(set(map(str, sites))):
            raise ValueError("compatibility_site_rows_duplicate_or_invalid")
        if not isinstance(row.get("target_site_query_sha256"), str) or len(row["target_site_query_sha256"]) != 64:
            raise ValueError("compatibility_target_query_hash_invalid")
    if digest(core) != evidence.get("compatibility_relation_sha256"):
        raise ValueError("compatibility_relation_hash_mismatch")
    relation = relation_map(evidence)
    matching = maximum_matching(
        relation,
        strategy=str(evidence.get("matching", {}).get("strategy") or "FORWARD"),
    )
    recorded = evidence.get("matching", {})
    if int(recorded.get("matching_size") or 0) != len(matching):
        raise ValueError("compatibility_matching_size_mismatch")
    recorded_pairs = [
        (str(row.get("target_handle")), str(row.get("site_handle")))
        for row in recorded.get("pairs", ()) if isinstance(row, Mapping)
    ]
    if recorded_pairs != list(matching):
        raise ValueError("compatibility_matching_pairs_mismatch")
    for row, (target, site) in zip(recorded.get("pairs", ()), matching):
        if row.get("target_site_pair_sha256") != pair_hash(target, site):
            raise ValueError("compatibility_matching_pair_hash_mismatch")
    matching_core = {
        "algorithm": MATCHING_ALGORITHM,
        "strategy": str(recorded.get("strategy") or "FORWARD"),
        "pairs": list(recorded.get("pairs", ())),
    }
    if recorded.get("algorithm") != MATCHING_ALGORITHM:
        raise ValueError("compatibility_matching_algorithm_mismatch")
    if recorded.get("matching_certificate_sha256") != digest(matching_core):
        raise ValueError("compatibility_matching_certificate_hash_mismatch")
    compatible_target_count = sum(bool(sites) for sites in relation.values())
    distinct_site_count = len({site for sites in relation.values() for site in sites})
    feasible = len(targets) >= 2 and len(matching) >= 2 and distinct_site_count >= 2
    if int(evidence.get("compatible_target_count") or 0) != compatible_target_count:
        raise ValueError("compatibility_target_count_mismatch")
    if int(evidence.get("distinct_compatible_site_count") or 0) != distinct_site_count:
        raise ValueError("compatibility_site_count_mismatch")
    if evidence.get("feasible") is not feasible:
        raise ValueError("compatibility_feasibility_flag_mismatch")


def build_allocation(
    *,
    relation: Mapping[str, Any],
    target_handles: Sequence[str],
    submitted_site_handles: Sequence[str],
    strategy: str,
    root_sha256: str,
    stage: str,
) -> dict[str, Any]:
    """Build a complete exact-relation allocation or fail closed."""

    validate_relation(relation)
    targets = tuple(map(str, target_handles))
    submitted = tuple(map(str, submitted_site_handles))
    if not 2 <= len(targets) <= 16 or len(targets) != len(set(targets)):
        raise ValueError("allocation_target_cardinality_invalid")
    if not submitted or len(submitted) != len(set(submitted)):
        raise ValueError("allocation_submitted_sites_duplicate_or_empty")
    if root_sha256 != relation.get("root_sha256") or stage != relation.get("stage"):
        raise ValueError("allocation_relation_root_or_stage_stale")
    exact = relation_map(relation)
    if any(target not in exact for target in targets):
        raise ValueError("allocation_target_relation_row_missing")
    submitted_set = set(submitted)
    restricted = {
        target: tuple(site for site in exact[target] if site in submitted_set)
        for target in targets
    }
    matched = maximum_matching(restricted, target_order=targets, strategy=strategy)
    if len(matched) != len(targets):
        raise ValueError("allocation_exact_compatible_matching_unavailable")
    pairs = [
        {
            "target_handle": target,
            "site_handle": site,
            "target_site_pair_sha256": pair_hash(target, site),
        }
        for target, site in matched
    ]
    core = {
        "schema_version": ALLOCATION_SCHEMA,
        "target_handles": [row["target_handle"] for row in pairs],
        "site_handles": [row["site_handle"] for row in pairs],
        "target_site_pair_hashes": [row["target_site_pair_sha256"] for row in pairs],
        "compatibility_relation_sha256": relation["compatibility_relation_sha256"],
        "feasibility_query_sha256": feasibility_query_hash(relation),
        "matching_size": len(pairs),
        "strategy": strategy,
        "root_sha256": root_sha256,
        "stage": stage,
    }
    return core | {"allocation_sha256": digest(core)}


def validate_allocation(
    allocation: Mapping[str, Any], relation: Mapping[str, Any], *,
    root_sha256: str, stage: str,
) -> None:
    """Revalidate the immutable allocation immediately before dispatch."""

    validate_relation(relation)
    core = {key: allocation.get(key) for key in (
        "schema_version", "target_handles", "site_handles",
        "target_site_pair_hashes", "compatibility_relation_sha256",
        "feasibility_query_sha256", "matching_size", "strategy",
        "root_sha256", "stage",
    )}
    if allocation.get("schema_version") != ALLOCATION_SCHEMA or allocation.get("allocation_sha256") != digest(core):
        raise ValueError("allocation_hash_or_schema_mismatch")
    if allocation.get("compatibility_relation_sha256") != relation.get("compatibility_relation_sha256"):
        raise ValueError("allocation_relation_hash_mismatch")
    if allocation.get("feasibility_query_sha256") != feasibility_query_hash(relation):
        raise ValueError("allocation_feasibility_query_hash_mismatch")
    if allocation.get("root_sha256") != root_sha256 or allocation.get("stage") != stage:
        raise ValueError("allocation_root_or_stage_stale")
    targets = allocation.get("target_handles")
    sites = allocation.get("site_handles")
    hashes = allocation.get("target_site_pair_hashes")
    if not all(isinstance(value, list) for value in (targets, sites, hashes)):
        raise ValueError("allocation_pair_lists_invalid")
    if not 2 <= len(targets) <= 16 or len(targets) != len(sites) or len(targets) != len(hashes):
        raise ValueError("allocation_pair_cardinality_invalid")
    if len(targets) != len(set(map(str, targets))) or len(sites) != len(set(map(str, sites))):
        raise ValueError("allocation_duplicate_target_or_site")
    exact = relation_map(relation)
    for target, site, observed_hash in zip(targets, sites, hashes):
        if str(target) not in exact or str(site) not in exact[str(target)]:
            raise ValueError("allocation_pair_not_in_exact_relation")
        if observed_hash != pair_hash(str(target), str(site)):
            raise ValueError("allocation_pair_hash_mismatch")
    if int(allocation.get("matching_size") or 0) != len(targets):
        raise ValueError("allocation_matching_size_mismatch")


__all__ = [
    "ALLOCATION_SCHEMA", "MATCHING_ALGORITHM", "RELATION_SCHEMA",
    "build_allocation", "build_relation", "digest", "feasibility_query_hash",
    "maximum_matching", "pair_hash", "relation_map", "validate_allocation",
    "validate_relation",
]
