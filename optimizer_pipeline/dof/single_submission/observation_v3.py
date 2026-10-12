"""WP-013B bounded, mechanically materialised common-Q surface."""

from __future__ import annotations

import hashlib
import math
import time
from types import MappingProxyType
from typing import Any, Iterable, Mapping

from .contract_v3 import (
    QUERY_NAMES, QUERY_RESULT_MAX_BYTES, QUERY_SESSION_MAX_BYTES,
    QUERY_SESSION_MAX_ROWS, QUERY_TOP_K_MAX,
)
from .handles import TrialHandleRegistry
from .observation import (
    CommonQuerySurface, ObservationError, QueryResult, _PUBLIC_FIELDS,
    _assert_public_safe, _sort_metric, _validate_json,
)
from .observation_v2 import QUERY_SEMANTICS
from .specs import canonical_json_bytes


QUERY_Q_V3 = "dof.single-submission.query-q.v3"


class CommonQuerySurfaceV3(CommonQuerySurface):
    individual_result_max_bytes = QUERY_RESULT_MAX_BYTES
    session_result_max_bytes = QUERY_SESSION_MAX_BYTES
    session_result_max_rows = QUERY_SESSION_MAX_ROWS

    def __init__(self, registry: TrialHandleRegistry) -> None:
        super().__init__(registry, max_queries=8)
        self._session_result_bytes = 0
        self._session_result_rows = 0

    @property
    def catalogue(self) -> dict[str, Any]:
        core = {
            "schema_version": QUERY_Q_V3,
            "max_queries": 8,
            "top_k_max": QUERY_TOP_K_MAX,
            "individual_result_max_bytes": self.individual_result_max_bytes,
            "session_result_max_bytes": self.session_result_max_bytes,
            "session_result_max_rows": self.session_result_max_rows,
            "queries": [{"name": name, **QUERY_SEMANTICS[name]} for name in QUERY_NAMES],
        }
        return core | {"catalogue_sha256": hashlib.sha256(canonical_json_bytes(core)).hexdigest()}

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
        if query_name not in QUERY_NAMES:
            raise ObservationError("query_not_advertised_in_v3")
        if not isinstance(top_k, int) or isinstance(top_k, bool) or not 1 <= top_k <= QUERY_TOP_K_MAX:
            raise ObservationError("query_top_k_out_of_range")
        key = (query_name, top_k)
        source_rows = [dict(row) for row in private_rows]
        raw_hash = hashlib.sha256(canonical_json_bytes(source_rows)).hexdigest()
        existing = self._results.get(key)
        if existing is not None:
            if existing.private_raw_sha256 != raw_hash:
                raise ObservationError("query_result_changed_within_trial")
            return existing
        if self._query_count >= self.max_queries:
            raise ObservationError("query_budget_exhausted")

        started = time.monotonic()
        expected_type = QUERY_SEMANTICS[query_name]["row_type"]
        normalized: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
        for index, original in enumerate(source_rows):
            row = dict(original)
            identity = row.pop("canonical_identity", None)
            object_type = str(row.pop("handle_type", expected_type))
            source_tool = str(row.pop("source_tool", "vivado"))
            if not isinstance(identity, str) or not identity:
                raise ObservationError(f"query_row_{index}_identity_missing")
            unknown = sorted(set(row) - _PUBLIC_FIELDS)
            if unknown:
                raise ObservationError("query_row_forbidden_fields:" + ",".join(unknown))
            handle = self.registry.register(
                object_type=object_type, canonical_identity=identity,
                attributes=row, source_tool=source_tool,
            )
            public = {"handle": handle, "handle_type": object_type} | row
            _validate_json(public, f"query_row_{index}")
            _assert_public_safe(public, f"query_row_{index}")
            normalized.append((_sort_metric(query_name, public) + (identity,), public))
        normalized.sort(key=lambda item: item[0])

        requested_count = top_k
        available_count = len(normalized)
        row_allowance = max(0, self.session_result_max_rows - self._session_result_rows)
        selected = [row for _, row in normalized[: min(top_k, row_allowance)]]
        reasons: list[str] = []
        if available_count > top_k:
            reasons.append("top_k")
        if min(top_k, available_count) > row_allowance:
            reasons.append("session_row_limit")

        def payload(rows: list[dict[str, Any]]) -> dict[str, Any]:
            selected_hash = hashlib.sha256(canonical_json_bytes(rows)).hexdigest()
            return {
                "schema_version": QUERY_Q_V3,
                "query_name": query_name,
                "requested_count": requested_count,
                "available_count": available_count,
                "returned_count": len(rows),
                "rows": rows,
                "truncated": bool(reasons or len(rows) < min(top_k, available_count)),
                "reason": sorted(set(reasons)) or ([missing_reason] if missing_reason else []),
                "private_source_sha256": raw_hash,
                "selected_rows_sha256": selected_hash,
                "missing_reason": missing_reason,
            }

        while selected:
            raw = canonical_json_bytes(payload(selected))
            if len(raw) <= self.individual_result_max_bytes and self._session_result_bytes + len(raw) <= self.session_result_max_bytes:
                break
            selected.pop()
            if len(raw) > self.individual_result_max_bytes:
                reasons.append("individual_byte_limit")
            else:
                reasons.append("session_byte_limit")
        value = payload(selected)
        raw = canonical_json_bytes(value)
        if len(raw) > self.individual_result_max_bytes or self._session_result_bytes + len(raw) > self.session_result_max_bytes:
            raise ObservationError("query_metadata_exceeds_byte_budget")
        measured = max(0.0, time.monotonic() - started) if latency_seconds is None else float(latency_seconds)
        if not math.isfinite(measured) or measured < 0:
            raise ObservationError("query_latency_invalid")
        frozen = tuple(MappingProxyType(row) for row in selected)
        result = QueryResult(
            query_name, frozen, raw, hashlib.sha256(raw).hexdigest(), raw_hash,
            measured, bool(value["truncated"]), missing_reason, tuple(tool_call_ids),
        )
        self._results[key] = result
        self._query_count += 1
        self._session_result_bytes += len(raw)
        self._session_result_rows += len(selected)
        return result

    @property
    def materialization_usage(self) -> dict[str, int]:
        return {
            "query_results": self._query_count,
            "returned_rows": self._session_result_rows,
            "canonical_result_bytes": self._session_result_bytes,
        }


__all__ = ["CommonQuerySurfaceV3", "QUERY_Q_V3"]
