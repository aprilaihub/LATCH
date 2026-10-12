"""Canonical common O/Q construction for L2, L3 and L4."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
import re
import time
from types import MappingProxyType
from typing import Any, Iterable, Mapping

from .handles import TrialHandleRegistry
from .specs import COMMON_OBSERVATION_O_V1, COMMON_QUERY_Q_V1, canonical_json_bytes


_LEVELS = frozenset({"L2", "L3", "L4"})
_QUERY_TYPES = {
    "critical_path_details": "path",
    "critical_object_locations": "cell",
    "high_fanout_net_details": "net",
    "congested_region_details": "region",
    "critical_hardblock_candidates": "hardblock",
    "pblock_constraint_details": "pblock",
    "placement_summary": "region",
    "route_summary": "net",
}
_PUBLIC_FIELDS = frozenset(
    {
        "object_type",
        "resource_type",
        "path_member",
        "path_rank",
        "slack_ns",
        "delay_ns",
        "fanout",
        "slr_id",
        "clock_region_id",
        "congestion_class",
        "site_class",
        "compatible_region_handles",
        "compatible_site_handles",
        "startpoint_handle",
        "endpoint_handle",
        "location_site_handle",
        "location_region_handle",
        "location_status",
        "route_status",
        "row_semantics",
        "bounds",
        "count",
        "status",
    }
)
_PATHISH = re.compile(r"[/\\]|(?:\.dcp|\.rpt|\.log|\.tcl)(?:$|\s)", re.I)


class ObservationError(ValueError):
    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


def _validate_json(value: Any, label: str = "value") -> None:
    if value is None or isinstance(value, (str, bool, int)):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ObservationError(f"{label}_nonfinite")
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            _validate_json(item, f"{label}[{index}]")
        return
    if isinstance(value, Mapping):
        for key, item in value.items():
            if not isinstance(key, str):
                raise ObservationError(f"{label}_key_not_string")
            _validate_json(item, f"{label}.{key}")
        return
    raise ObservationError(f"{label}_not_json")


def _assert_public_safe(value: Any, label: str = "value") -> None:
    if isinstance(value, str) and _PATHISH.search(value):
        raise ObservationError(f"{label}_path_leakage")
    if isinstance(value, list):
        for index, item in enumerate(value):
            _assert_public_safe(item, f"{label}[{index}]")
    elif isinstance(value, Mapping):
        for key, item in value.items():
            _assert_public_safe(item, f"{label}.{key}")


@dataclass(frozen=True, slots=True)
class CommonObservation:
    payload: Mapping[str, Any]
    canonical_bytes: bytes
    sha256: str

    def for_level(self, level: str) -> bytes:
        if level not in _LEVELS:
            raise ObservationError("common_observation_level_not_l2_l3_l4")
        return self.canonical_bytes


def build_common_observation(
    *,
    root_pseudonym: str,
    target_clock: Mapping[str, Any],
    timing: Mapping[str, Any],
    utilization: Mapping[str, Any],
    implementation: Mapping[str, Any],
    constraints: Mapping[str, Any],
    geometry: Mapping[str, Any],
    congestion: Mapping[str, Any],
    critical_paths: Iterable[Mapping[str, Any]],
    budget: Mapping[str, Any],
) -> CommonObservation:
    if not re.fullmatch(r"root-[0-9a-f]{16}", root_pseudonym):
        raise ObservationError("root_pseudonym_invalid")
    paths = list(critical_paths)
    if len(paths) > 8:
        raise ObservationError("critical_path_summary_exceeds_8")
    payload = {
        "schema_version": COMMON_OBSERVATION_O_V1,
        "root_pseudonym": root_pseudonym,
        "target_clock": dict(target_clock),
        "timing": dict(timing),
        "utilization": dict(utilization),
        "implementation": dict(implementation),
        "constraints": dict(constraints),
        "geometry": dict(geometry),
        "congestion": dict(congestion),
        "critical_paths": paths,
        "budget_remaining": dict(budget),
    }
    _validate_json(payload)
    _assert_public_safe(payload)
    raw = canonical_json_bytes(payload)
    return CommonObservation(MappingProxyType(payload), raw, hashlib.sha256(raw).hexdigest())


@dataclass(frozen=True, slots=True)
class QueryResult:
    query_name: str
    rows: tuple[Mapping[str, Any], ...]
    canonical_bytes: bytes
    sha256: str
    private_raw_sha256: str
    latency_seconds: float
    truncated: bool
    missing_reason: str | None
    tool_call_ids: tuple[str, ...]

    def public_payload(self) -> dict[str, Any]:
        return json.loads(self.canonical_bytes)


class CommonQuerySurface:
    def __init__(self, registry: TrialHandleRegistry, *, max_queries: int = 8) -> None:
        if max_queries != 8:
            raise ValueError("wp013_query_limit_must_equal_8")
        self.registry = registry
        self.max_queries = max_queries
        self._results: dict[tuple[str, int], QueryResult] = {}
        self._query_count = 0

    @property
    def catalogue(self) -> dict[str, Any]:
        return {
            "schema_version": COMMON_QUERY_Q_V1,
            "queries": sorted(_QUERY_TYPES),
            "max_queries": self.max_queries,
        }

    def materialize(
        self,
        *,
        query_name: str,
        top_k: int,
        private_rows: Iterable[Mapping[str, Any]],
        tool_call_ids: Iterable[str] = (),
        latency_seconds: float | None = None,
        missing_reason: str | None = None,
    ) -> QueryResult:
        if query_name not in _QUERY_TYPES:
            raise ObservationError("unknown_query")
        if not isinstance(top_k, int) or isinstance(top_k, bool) or not 1 <= top_k <= 64:
            raise ObservationError("query_top_k_out_of_range")
        key = (query_name, top_k)
        existing = self._results.get(key)
        rows = [dict(row) for row in private_rows]
        raw_hash = hashlib.sha256(canonical_json_bytes(rows)).hexdigest()
        if existing is not None:
            if existing.private_raw_sha256 != raw_hash:
                raise ObservationError("query_result_changed_within_trial")
            return existing
        if self._query_count >= self.max_queries:
            raise ObservationError("query_budget_exhausted")
        started = time.monotonic()
        expected_type = _QUERY_TYPES[query_name]
        normalized: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
        for index, row in enumerate(rows):
            identity = row.pop("canonical_identity", None)
            object_type = str(row.pop("handle_type", expected_type))
            source_tool = str(row.pop("source_tool", "vivado"))
            if not isinstance(identity, str) or not identity:
                raise ObservationError(f"query_row_{index}_identity_missing")
            unknown = sorted(set(row) - _PUBLIC_FIELDS)
            if unknown:
                raise ObservationError("query_row_forbidden_fields:" + ",".join(unknown))
            handle = self.registry.register(
                object_type=object_type,
                canonical_identity=identity,
                attributes=row,
                source_tool=source_tool,
            )
            public = {"handle": handle, "handle_type": object_type} | row
            _validate_json(public, f"query_row_{index}")
            _assert_public_safe(public, f"query_row_{index}")
            metric = _sort_metric(query_name, public)
            normalized.append((metric + (identity,), public))
        normalized.sort(key=lambda item: item[0])
        selected = [MappingProxyType(row) for _, row in normalized[:top_k]]
        truncated = len(normalized) > top_k
        payload = {
            "schema_version": COMMON_QUERY_Q_V1,
            "query_name": query_name,
            "top_k": top_k,
            "rows": [dict(row) for row in selected],
            "returned": len(selected),
            "truncated": truncated,
            "missing_reason": missing_reason,
        }
        raw = canonical_json_bytes(payload)
        measured = max(0.0, time.monotonic() - started) if latency_seconds is None else float(latency_seconds)
        if not math.isfinite(measured) or measured < 0:
            raise ObservationError("query_latency_invalid")
        result = QueryResult(
            query_name,
            tuple(selected),
            raw,
            hashlib.sha256(raw).hexdigest(),
            raw_hash,
            measured,
            truncated,
            missing_reason,
            tuple(tool_call_ids),
        )
        self._results[key] = result
        self._query_count += 1
        return result


def _sort_metric(query_name: str, row: Mapping[str, Any]) -> tuple[Any, ...]:
    if query_name == "critical_path_details":
        return (_number(row.get("slack_ns"), math.inf), _number(row.get("path_rank"), math.inf))
    if query_name == "high_fanout_net_details":
        return (-_number(row.get("fanout"), -math.inf),)
    if query_name == "critical_hardblock_candidates":
        return (_number(row.get("path_rank"), math.inf), _number(row.get("slack_ns"), math.inf))
    if query_name == "congested_region_details":
        order = {"high": 0, "medium": 1, "low": 2}
        return (order.get(str(row.get("congestion_class", "")), 3),)
    return (_number(row.get("path_rank"), math.inf),)


def _number(value: Any, default: float) -> float:
    if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(float(value)):
        return float(value)
    return default


__all__ = ["CommonObservation", "CommonQuerySurface", "ObservationError", "QueryResult", "build_common_observation"]
