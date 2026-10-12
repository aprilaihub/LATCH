#!/usr/bin/env python3
"""Generate the zero-call WP-016A R1 metered-frontier audit receipt."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any

from jsonschema import Draft202012Validator


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from optimizer_pipeline.dof.wp016 import (  # noqa: E402
    RETRIEVAL_FRONTIER_SEMANTICS, RetrievalBudgetState, canonical_bytes,
    compact_protocol_definition, fragment_retrieval_registry,
)


REGISTRY = ROOT / "configs/wp016a/closed-retrieval-registry.json"


def _state(registry: dict[str, Any], capability: str, reserve: int) -> RetrievalBudgetState:
    mask = ("M", "P", "G") if capability == "G" else ("M", "P")
    return RetrievalBudgetState(registry, mask, {}, reserve, 8_192, 4_096)


def _turn(state: RetrievalBudgetState, request: dict[str, Any]) -> dict[str, Any]:
    assert state.begin_turn()["status"] == "TURN_STARTED"
    assert state.record_output_tokens(8)["status"] == "OUTPUT_ACCOUNTED"
    assert state.add_history(request)["status"] == "OK"
    return state.dispatch(request)


def _g_path(registry: dict[str, Any], *, child_then_nvs: bool) -> dict[str, Any]:
    state = _state(registry, "G", 6_214)
    example = _turn(state, {
        "endpoint": "GET_EXAMPLE",
        "content_id": "EXAMPLE/G/G_BOUNDARY_NONPREFIX_MULTI_NET",
    })
    after_example = state.remaining_reserve_bytes
    request = {
        "endpoint": "GET_SCHEMA_FRAGMENT",
        "content_id": "SCHEMA/G/program_schema",
    }
    assert state.begin_turn()["status"] == "TURN_STARTED"
    assert state.record_output_tokens(8)["status"] == "OUTPUT_ACCOUNTED"
    assert state.add_history(request)["status"] == "OK"
    before_frontier = state.remaining_reserve_bytes
    retrieval_before = state.retrieval_bytes
    frontier = state.dispatch(request)
    schema = compact_protocol_definition()["response_schemas_by_status"][frontier["status"]]
    schema_errors = sorted(error.message for error in Draft202012Validator(schema).iter_errors(frontier))
    rows = frontier["available_child_fragments"]
    frontier_content = {key: frontier[key] for key in (
        "frontier_semantics", "requested_content_id", "requested_size_bytes",
        "requested_content_sha256", "maximum_retrievable_bytes",
        "available_child_fragments",
    )}
    selected_child = next(row["content_id"] for row in rows if row["retrievable_now"])
    child = None
    if child_then_nvs:
        child = _turn(state, {"endpoint": "GET_SCHEMA_FRAGMENT", "content_id": selected_child})
    terminal = _turn(state, {
        "endpoint": "NO_VALID_SUBMISSION", "reason_code": "DYNAMIC_BUDGET_EXHAUSTED",
    })
    return {
        "path": "CHILD_THEN_NVS" if child_then_nvs else "DIRECT_NVS",
        "initial_reserve_bytes": 6_214,
        "after_example_remaining_bytes": after_example,
        "before_frontier_remaining_bytes": before_frontier,
        "example_status": example["status"],
        "frontier_status": frontier["status"],
        "frontier_semantics": frontier["frontier_semantics"],
        "requested_content_absent": "content" not in frontier,
        "requested_content_bytes_charged": frontier["requested_content_bytes_charged"],
        "frontier_metadata_bytes": frontier["frontier_metadata_bytes"],
        "frontier_canonical_bytes_recomputed": len(canonical_bytes(frontier_content)),
        "consumed_bytes": frontier["consumed_bytes"],
        "retrieval_bytes_increase": state.retrieval_bytes - retrieval_before - (
            int(child["consumed_bytes"]) if child is not None else 0
        ),
        "post_frontier_remaining_bytes": frontier["remaining_bytes"],
        "frontier_rows": len(rows),
        "retrievable_rows": sum(row["retrievable_now"] for row in rows),
        "frontier_order_lexicographic": [row["content_id"] for row in rows] == sorted(
            row["content_id"] for row in rows
        ),
        "post_charge_retrievable_now_exact": all(
            row["retrievable_now"] == (row["size_bytes"] <= frontier["remaining_bytes"])
            for row in rows
        ),
        "response_schema_errors": schema_errors,
        "child_status": child["status"] if child is not None else None,
        "terminal_status": terminal["status"],
        "turns_used": state.turn_count,
    }


def _whole_schema_control(
    registry: dict[str, Any], *, capability: str, reserve: int, content_id: str,
) -> dict[str, Any]:
    state = _state(registry, capability, reserve)
    before = state.retrieval_bytes
    response = _turn(state, {"endpoint": "GET_SCHEMA_FRAGMENT", "content_id": content_id})
    return {
        "capability": capability, "reserve_bytes": reserve,
        "content_id": content_id, "status": response["status"],
        "consumed_bytes": response["consumed_bytes"],
        "retrieval_bytes_increase": state.retrieval_bytes - before,
        "frontier_used": "frontier_metadata_bytes" in response,
    }


def audit() -> dict[str, Any]:
    protocol = compact_protocol_definition()
    registry = fragment_retrieval_registry(
        json.loads(REGISTRY.read_text(encoding="utf-8")), protocol=protocol,
    )
    cannot_fit = _state(registry, "G", 220)
    fail = _turn(cannot_fit, {
        "endpoint": "GET_SCHEMA_FRAGMENT", "content_id": "SCHEMA/G/program_schema",
    })
    result = {
        "schema_version": "wp016a.metered-frontier-audit.v1",
        "status": "PASS_ZERO_CALL_METERED_FRONTIER",
        "frontier_semantics": RETRIEVAL_FRONTIER_SEMANTICS,
        "protocol_schema_version": protocol["schema_version"],
        "g_low_paths": [
            _g_path(registry, child_then_nvs=True),
            _g_path(registry, child_then_nvs=False),
        ],
        "controls": [
            _whole_schema_control(
                registry, capability="G", reserve=58_278,
                content_id="SCHEMA/G/program_schema",
            ),
            _whole_schema_control(
                registry, capability="P", reserve=25_351,
                content_id="SCHEMA/P/program_schema",
            ),
        ],
        "frontier_cannot_fit": {
            "status": fail["status"], "reason": fail.get("reason"),
            "frontier_evidence_absent": not any(key in fail for key in (
                "available_child_fragments", "requested_content_sha256",
                "frontier_semantics",
            )),
            "retrieval_bytes": cannot_fit.retrieval_bytes,
        },
        "provider_calls": 0, "backend_calls": 0,
        "attempt2_token_issued": False, "wp016b_authorised": False,
    }
    for path in result["g_low_paths"]:
        assert path["frontier_semantics"] == RETRIEVAL_FRONTIER_SEMANTICS
        assert path["requested_content_bytes_charged"] == 0
        assert path["frontier_metadata_bytes"] == path["frontier_canonical_bytes_recomputed"]
        assert path["consumed_bytes"] == path["frontier_metadata_bytes"]
        assert path["retrieval_bytes_increase"] == path["frontier_metadata_bytes"]
        assert path["terminal_status"] == "NO_VALID_SUBMISSION_RECEIVED"
        assert not path["response_schema_errors"]
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = audit()
    text = json.dumps(result, indent=2, sort_keys=True) + "\n"
    if args.output is not None:
        if args.output.exists():
            raise FileExistsError("audit_output_already_exists")
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text, encoding="utf-8")
    else:
        print(text, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
