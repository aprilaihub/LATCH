from __future__ import annotations

import json
from pathlib import Path

from optimizer_pipeline.dof.single_submission.specs import (
    MAX_HEAVY_CALLS,
    MAX_LIGHT_MUTATIONS,
    build_protocol_freeze,
    protocol_freeze_hash,
    protocol_freeze_payload,
)


REPO = Path(__file__).resolve().parents[1]


def test_gate_a_has_exactly_eight_unique_hash_bound_specs() -> None:
    specs = build_protocol_freeze()
    assert len(specs) == 8
    assert len({item.name for item in specs}) == 8
    assert len({item.version for item in specs}) == 8
    assert all(len(item.sha256) == 64 for item in specs)
    assert len(protocol_freeze_hash()) == 64


def test_m_freeze_guarantees_exact_m8_and_forbids_alias_surfaces() -> None:
    spec = next(item for item in build_protocol_freeze() if item.name == "M_LANGUAGE_V1")
    body = spec.payload()["body"]
    assert [row["action_key"] for row in body["guaranteed_actions"]] == [
        "macro.phys_opt",
        "macro.place_route",
        "macro.auto_pblock",
        "macro.retime",
        "macro.net_swap",
        "macro.memory_path_opt",
        "macro.hardblock_relocate",
        "macro.congestion_spread",
    ]
    assert [row["action_key"] for row in body["conditional_actions"]] == ["macro.fanout", "macro.lut_cone"]
    assert "*_rw" in body["mandatory_exclusions"]
    assert "manual_action" in body["mandatory_exclusions"]


def test_p_and_g_share_bounded_cost_envelope_and_required_mechanisms() -> None:
    by_name = {item.name: item.payload()["body"] for item in build_protocol_freeze()}
    p = by_name["P_LANGUAGE_V1"]
    g = by_name["G_AST_LANGUAGE_V1"]
    assert p["limits"]["max_actual_heavy_calls"] == g["limits"]["max_actual_heavy_calls"] == MAX_HEAVY_CALLS
    assert p["limits"]["max_light_mutations"] == g["limits"]["max_light_mutations"] == MAX_LIGHT_MUTATIONS
    p_ops = {row["op"] for row in p["operations"]}
    assert {"CALL_MACRO", "ASSIGN", "RELOCATE_HARDBLOCK"} <= p_ops
    g_nodes = {row["node"] for row in g["generated_nodes"]}
    assert {"SELECT_WHERE", "CREATE_REGION", "ASSIGN_REGION", "RELOCATE_HARDBLOCK"} <= g_nodes
    assert "full tree" in g["predicate_ast"]["evaluation"]


def test_planner_is_single_submission_and_has_no_precommit_qor() -> None:
    planner = next(item for item in build_protocol_freeze() if item.name == "SINGLE_SUBMISSION_PLANNER_V1").payload()["body"]
    assert planner["limits"] == {
        "model_turns": 4,
        "read_only_queries": 8,
        "construction_feedback_rounds": 3,
        "valid_commits": 1,
        "post_commit_qor_resubmissions": 0,
        "provider_retry": 0,
        "provider_fallback": 0,
    }
    assert "real design mutation" in planner["pre_commit_forbidden"]


def test_checked_in_freeze_is_byte_exact() -> None:
    path = REPO / "configs" / "wp013" / "protocol-freeze-v1.json"
    checked = json.loads(path.read_text())
    assert checked["bundle_sha256"] == protocol_freeze_hash()
    expected = protocol_freeze_payload() | {"bundle_sha256": protocol_freeze_hash()}
    assert checked == expected
