from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys

import pytest


REPO = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "run_wp013_e0", REPO / "scripts" / "run_wp013_e0.py"
)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


def test_frozen_plan_is_exact_two_root_sixteen_job_contract() -> None:
    plan = json.loads((REPO / "configs/wp013/e0-v3-plan.json").read_text())
    assert [row["root_id"] for row in plan["roots"]] == [
        "vexriscv_re-place",
        "external_compatibility_a",
    ]
    assert [row["model"] for row in plan["models"]] == list(MODULE.MODELS)
    assert plan["guards"]["max_parallel_weekday"] == 8
    assert plan["guards"]["pre_e0_observed_heavy_calls"] == 242
    assert plan["guards"]["e0_heavy_budget_remaining"] == 38
    assert plan["guards"]["pre_e0_observed_heavy_calls"] + plan["guards"]["e0_heavy_budget_remaining"] == 280
    assert plan["completion"] == {
        "status": "NEEDS_PRO_EVIDENCE_REVIEW",
        "next_actor": "GPT_PRO",
        "primary_matrix_auto_start": False,
    }


@pytest.mark.parametrize(
    ("candidate", "expected"),
    [
        ({"language": "M", "macro": "macro.phys_opt"}, "phys_opt"),
        (
            {
                "language": "P",
                "steps": [
                    {
                        "step_id": "s1",
                        "op": "CALL_MACRO",
                        "args": {"macro": "memory_path_opt"},
                    }
                ],
            },
            "memory_path_opt",
        ),
        (
            {
                "language": "G",
                "statements": [
                    {
                        "statement_id": "g1",
                        "node": "CALL_MACRO",
                        "args": {"macro": "macro.retime"},
                    }
                ],
            },
            "retime",
        ),
        ({"language": "P", "steps": []}, None),
    ],
)
def test_macro_extraction_is_closed_and_exact(candidate, expected) -> None:
    assert MODULE._macro_from_candidate(candidate) == expected


def test_strict_json_rejects_duplicate_keys_and_non_object() -> None:
    with pytest.raises(ValueError, match="duplicate_json_key"):
        MODULE.strict_json_object(b'{"kind":"STOP","kind":"COMMIT"}')
    with pytest.raises(ValueError, match="planner_message_not_object"):
        MODULE.strict_json_object(b"[]")


def test_level_contracts_are_nested_and_do_not_contain_asset_identity() -> None:
    l2 = MODULE._language_contract("L2")
    l3 = MODULE._language_contract("L3")
    l4 = MODULE._language_contract("L4")
    assert l2["allowed_languages"] == ["M"]
    assert l3["allowed_languages"] == ["M", "P"]
    assert l4["allowed_languages"] == ["M", "P", "G"]
    assert l2["M"] == l3["M"] == l4["M"]
    assert l3["P"] == l4["P"]
    encoded = MODULE.canonical_json_bytes(l4)
    assert b"vexriscv" not in encoded
    assert b"external_compatibility" not in encoded
    assert b"historical_winner" not in encoded


def test_canonical_terminal_taxonomy_includes_scientific_negative_paths() -> None:
    assert {
        "NO_VALID_SUBMISSION",
        "PROVIDER_FAILURE",
        "TOOL_FAILURE",
        "CAPABILITY_ACTION_REJECTION",
        "BUDGET_BLOCKED",
    } <= MODULE.TERMINALS


def test_macro_heavy_accounting_uses_observed_wrapper_calls() -> None:
    assert MODULE.MACRO_OBSERVED_HEAVY_CALLS["memory_path_opt"] == 4
    assert MODULE.MACRO_OBSERVED_HEAVY_CALLS["hardblock_relocate"] == 4
    assert sum(MODULE.MACRO_OBSERVED_HEAVY_CALLS.values()) == 23
    erratum = json.loads(
        (REPO / "configs/wp013/macro-heavy-accounting-erratum-v1.json").read_text()
    )
    assert {
        key: value["observed_calls_per_job"]
        for key, value in erratum["actions"].items()
    } == MODULE.MACRO_OBSERVED_HEAVY_CALLS
    assert erratum["qor_used_for_correction"] is False
