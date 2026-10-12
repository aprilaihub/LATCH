"""Honest WP-013A common-Q catalogue and semantic audit."""

from __future__ import annotations

import hashlib
from typing import Any, Iterable, Mapping

from .contract_v2 import QUERY_NAMES
from .handles import TrialHandleRegistry
from .observation import CommonQuerySurface, ObservationError
from .specs import canonical_json_bytes


QUERY_Q_V2 = "dof.single-submission.query-q.v2"
QUERY_SEMANTICS: dict[str, dict[str, Any]] = {
    "critical_path_details": {
        "row_type": "path",
        "returns": ["handle", "path_rank", "slack_ns", "delay_ns", "startpoint_handle", "endpoint_handle"],
        "ordering": "slack_ns ascending, then path_rank, then private canonical identity",
        "empty_policy": "error on E0 roots; otherwise explicit no_timing_paths_returned",
    },
    "critical_object_locations": {
        "row_type": "cell",
        "returns": ["handle", "path_rank", "resource_type", "location_status", "location_site_handle", "location_region_handle"],
        "ordering": "path_rank then private canonical identity",
        "empty_policy": "explicit no_critical_cells_returned",
    },
    "high_fanout_net_details": {
        "row_type": "net",
        "returns": ["handle", "fanout"],
        "ordering": "fanout descending then private canonical identity",
        "empty_policy": "explicit no_nonclock_nets_returned",
    },
    "critical_hardblock_candidates": {
        "row_type": "hardblock",
        "returns": ["handle", "path_rank", "resource_type", "compatible_site_handles"],
        "ordering": "path_rank then slack_ns then private canonical identity",
        "empty_policy": "explicit no_critical_hardblocks_returned",
    },
    "placement_summary": {
        "row_type": "region",
        "returns": ["handle", "bounds", "count"],
        "ordering": "private canonical region identity",
        "empty_policy": "explicit no_clock_regions_returned",
    },
    "route_summary": {
        "row_type": "net",
        "returns": ["handle", "route_status", "count"],
        "ordering": "one root-bound summary row",
        "empty_policy": "error; read-only route evidence is required",
    },
}


class CommonQuerySurfaceV2(CommonQuerySurface):
    def __init__(self, registry: TrialHandleRegistry) -> None:
        super().__init__(registry, max_queries=8)

    @property
    def catalogue(self) -> dict[str, Any]:
        core = {
            "schema_version": QUERY_Q_V2,
            "max_queries": 8,
            "queries": [
                {"name": name, **QUERY_SEMANTICS[name]} for name in QUERY_NAMES
            ],
        }
        return core | {
            "catalogue_sha256": hashlib.sha256(canonical_json_bytes(core)).hexdigest()
        }

    def materialize(self, *, query_name: str, **kwargs: Any):  # type: ignore[override]
        if query_name not in QUERY_NAMES:
            raise ObservationError("query_not_advertised_in_v2")
        return super().materialize(query_name=query_name, **kwargs)


def audit_query_semantics(
    rows: Mapping[str, Iterable[Mapping[str, Any]]],
    missing_reasons: Mapping[str, str | None],
    *,
    require_e0_paths: bool,
) -> dict[str, Any]:
    row_lists = {name: [dict(row) for row in rows.get(name, ())] for name in QUERY_NAMES}
    checks: dict[str, bool] = {
        "catalogue_exact": set(row_lists) == set(QUERY_NAMES),
        "every_empty_has_reason": all(
            values or bool(missing_reasons.get(name)) for name, values in row_lists.items()
        ),
        "path_rows_nonempty_when_required": bool(row_lists["critical_path_details"]) if require_e0_paths else True,
        "path_rows_have_typed_links": all(
            {
                "canonical_identity", "handle_type", "path_rank", "slack_ns",
                "delay_ns", "startpoint_handle", "endpoint_handle",
            } <= set(row)
            for row in row_lists["critical_path_details"]
        ),
        "location_rows_are_honest": all(
            {
                "canonical_identity", "handle_type", "path_rank", "location_status",
                "location_site_handle", "location_region_handle",
            } <= set(row)
            for row in row_lists["critical_object_locations"]
        ),
        "route_summary_nonempty": bool(row_lists["route_summary"]),
        "names_match_runtime": set(QUERY_SEMANTICS) == set(QUERY_NAMES),
    }
    payload = {
        "schema_version": "dof.wp013a-query-semantic-audit.v1",
        "query_counts": {name: len(values) for name, values in row_lists.items()},
        "missing_reasons": {name: missing_reasons.get(name) for name in QUERY_NAMES},
        "checks": checks,
        "accepted": all(checks.values()),
    }
    payload["audit_sha256"] = hashlib.sha256(canonical_json_bytes(payload)).hexdigest()
    return payload


__all__ = [
    "CommonQuerySurfaceV2", "QUERY_Q_V2", "QUERY_SEMANTICS", "audit_query_semantics",
]
