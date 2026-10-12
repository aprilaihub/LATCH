from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from scripts.run_wp013a_e0 import (
    MACRO_OBSERVED_HEAVY_CALLS,
    MODELS,
    SCHEMA,
    SharedCost,
    _binding,
    _language_contract,
    _macro_from_candidate,
    digest,
    strict_json_object,
)
from optimizer_pipeline.dof.openrouter import (
    OPENROUTER_MAX_BILLABLE_INPUT_BYTES,
    OPENROUTER_WP013A_MAX_BILLABLE_INPUT_BYTES,
    build_openrouter_outbound,
)


REPO = Path(__file__).resolve().parents[1]


def load(name: str) -> dict:
    return json.loads((REPO / "configs" / "wp013a" / name).read_text())


def test_frozen_plan_is_exactly_bounded_development_e0() -> None:
    plan = load("e0-v4-plan.json")
    assert plan["schema_version"] == "dof.wp013a-e0-v4-plan.v1"
    assert plan["plan_sha256"] == digest(
        {key: value for key, value in plan.items() if key != "plan_sha256"}
    )
    assert plan["matrix"] == {
        "roots": 2,
        "jobs": 16,
        "levels": ["L0", "L1", "L2", "L3", "L4"],
        "fresh_protocol_epoch": True,
    }
    assert tuple(row["model"] for row in plan["models"]) == MODELS
    assert plan["guards"]["implementation_heavy_call_cap"] == 64
    assert plan["guards"]["openrouter_cost_cap_usd"] == 1.0
    assert plan["guards"]["max_parallel_weekday"] == 8
    assert plan["guards"]["planning_input_byte_ceiling"] == 32768
    assert OPENROUTER_MAX_BILLABLE_INPUT_BYTES == 16384
    assert OPENROUTER_WP013A_MAX_BILLABLE_INPUT_BYTES == plan["guards"]["planning_input_byte_ceiling"]
    assert plan["non_scope"]["primary_matrix"] is False
    assert plan["completion"]["primary_matrix_auto_start"] is False


def test_frozen_protocol_and_macro_accounting_are_self_consistent() -> None:
    protocol = load("protocol-freeze-v2.json")
    assert protocol["freeze_sha256"] == digest(
        {key: value for key, value in protocol.items() if key != "freeze_sha256"}
    )
    assert protocol["primary_aggregate_eligible"] is False
    assert protocol["resource_envelope"]["implementation_heavy_calls"] == 64
    overlay = load("final-m-v2-overlay.json")
    observed = {
        row["action_key"].removeprefix("macro."): row["heavy_call_range"][0]
        for row in overlay["actions"]
    }
    assert observed == MACRO_OBSERVED_HEAVY_CALLS
    assert all(row["heavy_call_range"][0] == row["heavy_call_range"][1] for row in overlay["actions"])
    assert all("action_key" in row and "macro" not in row for row in overlay["actions"])


def test_language_contracts_are_nested_and_public_schema_is_v2() -> None:
    l2, l3, l4 = (_language_contract(level) for level in ("L2", "L3", "L4"))
    assert SCHEMA.endswith("v1")
    assert l2["M"] == l3["M"] == l4["M"]
    assert "P" not in l2 and l3["P"] == l4["P"]
    assert "G" not in l3 and "G" in l4


@pytest.mark.parametrize(
    ("candidate", "expected"),
    [
        ({"language": "M", "action_key": "macro.phys_opt"}, "phys_opt"),
        ({"language": "P", "steps": [{"step_id": "s", "op": "CALL_MACRO", "args": {"action_key": "macro.place_route"}}]}, "place_route"),
        ({"language": "G", "statements": [{"statement_id": "s", "node": "CALL_MACRO", "args": {"action_key": "macro.memory_path_opt"}}]}, "memory_path_opt"),
        (None, None),
    ],
)
def test_controller_extracts_only_canonical_action_key(candidate: dict | None, expected: str | None) -> None:
    assert _macro_from_candidate(candidate) == expected


def test_strict_json_rejects_duplicate_keys_and_non_objects() -> None:
    with pytest.raises(ValueError, match="duplicate_json_key"):
        strict_json_object(b'{"kind":"STOP","kind":"COMMIT"}')
    with pytest.raises(ValueError, match="planner_message_not_object"):
        strict_json_object(b"[]")


def test_exact_wp013a_v2_binding_uses_32768_byte_guard() -> None:
    context = b"x" * 18_000
    prompt = b"bounded-v2-prompt"
    binding = _binding(
        job={"job_id": "byte-bound", "level": "L4"},
        model=MODELS[0], turn=0, context=context, prompt=prompt,
        catalogue_hash=digest("catalogue"), reserve=0.05,
    )
    outbound = build_openrouter_outbound(binding)
    assert outbound.requested_model == MODELS[0]


def test_shared_cost_reservation_is_fail_closed() -> None:
    async def exercise() -> None:
        cost = SharedCost(1.0, spent=0.6)
        assert await cost.acquire(0.15)
        assert await cost.acquire(0.15)
        assert not await cost.acquire(0.15)
        await cost.settle(0.15, 0.02)
        assert await cost.acquire(0.15)

    asyncio.run(exercise())
