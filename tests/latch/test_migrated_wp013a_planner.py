from __future__ import annotations

import hashlib

from experiments.dof.protocol.handles import TrialHandleRegistry
from experiments.dof.protocol.language import CompileContext
from experiments.dof.protocol.observation import build_common_observation
from experiments.dof.protocol.observation_v2 import CommonQuerySurfaceV2
from experiments.dof.protocol.planner import SystemBinding
from experiments.dof.protocol.planner_v2 import SingleSubmissionSessionV2


def session(level: str = "L4") -> SingleSubmissionSessionV2:
    root = hashlib.sha256(b"wp013a-planner-root").hexdigest()
    observation = build_common_observation(
        root_pseudonym="root-" + root[:16],
        target_clock={}, timing={}, utilization={}, implementation={}, constraints={},
        geometry={}, congestion={}, critical_paths=[],
        budget={"planning_turns": 4, "queries": 8, "feedback_rounds": 3},
    )
    registry = TrialHandleRegistry(
        trial_id="wp013a-planner", root_sha256=root,
        secret=hashlib.sha256(b"wp013a-planner-secret").digest(),
    )
    binding = SystemBinding.create(
        observation_hash=observation.sha256, root_binding=root,
        catalogue_version="wp013a.v2", catalogue_hash=hashlib.sha256(b"cat").hexdigest(),
        provider="openrouter", model="pinned", route="pinned", session_nonce="one",
    )

    def provider(name: str, top_k: int):
        row_type = {
            "critical_path_details": "path",
            "critical_object_locations": "cell",
            "high_fanout_net_details": "net",
            "critical_hardblock_candidates": "hardblock",
            "placement_summary": "region",
            "route_summary": "net",
        }[name]
        return (
            [{"canonical_identity": f"private/{name}/{top_k}", "handle_type": row_type, "status": "captured"}],
            ["frozen-read-only"], 0.0, None,
        )

    return SingleSubmissionSessionV2(
        level, observation, CommonQuerySurfaceV2(registry), CompileContext(registry, "ROUTED"),
        binding, provider,
    )


def test_eight_queries_are_reachable_in_one_batched_turn() -> None:
    item = session()
    names = [
        "critical_path_details", "critical_object_locations", "high_fanout_net_details",
        "critical_hardblock_candidates", "placement_summary", "route_summary",
        "critical_path_details", "route_summary",
    ]
    result = item.accept(
        {"kind": "QUERY", "queries": [{"name": name, "top_k": index + 1} for index, name in enumerate(names)]}
    )
    assert result.status == "QUERY_RESULT"
    assert item.query_requests_used == 8 and item.turns_used == 1
    assert len(result.query_batch["individual_hashes"]) == 8
    assert result.query_batch["aggregate_sha256"]


def test_final_turn_accepts_commit_but_mechanically_rejects_query() -> None:
    accepted = session("L2")
    for _ in range(3):
        assert accepted.accept(
            {"kind": "DRAFT", "candidate": {"language": "M", "action_key": "macro.place_route"}}
        ).status == "CONSTRUCTION_VALID"
    assert accepted.allowed_kinds_this_turn() == ("COMMIT", "NO_VALID_SUBMISSION", "STOP")
    assert accepted.accept(
        {"kind": "COMMIT", "candidate": {"language": "M", "action_key": "macro.place_route"}}
    ).status == "COMMITTED"

    rejected = session("L2")
    for _ in range(3):
        rejected.accept(
            {"kind": "DRAFT", "candidate": {"language": "M", "action_key": "macro.place_route"}}
        )
    result = rejected.accept(
        {"kind": "QUERY", "queries": [{"name": "route_summary", "top_k": 1}]}
    )
    assert result.status == "NO_VALID_SUBMISSION"
    assert result.feedback[0]["error_code"] == "kind_not_allowed_on_final_turn"
    assert rejected.query_requests_used == 0


def test_feedback_is_actionable_bounded_and_contains_no_qor() -> None:
    item = session("L2")
    result = item.accept(
        {"kind": "COMMIT", "candidate": {"language": "M", "macro": "macro.phys_opt"}}
    )
    feedback = dict(result.feedback[0])
    assert set(feedback) == {
        "error_code", "location", "expected_shape_or_schema_ref", "remaining_turns",
        "remaining_feedback_rounds", "remaining_query_budget",
    }
    assert feedback["expected_shape_or_schema_ref"] == "#/M/candidate_schema"
    assert not {"fmax", "wns", "tns", "ranking"} & set(feedback)


def test_batch_and_individual_materialization_hashes_are_equal() -> None:
    batched = session()
    batch = batched.accept(
        {"kind": "QUERY", "queries": [{"name": "route_summary", "top_k": 1}, {"name": "critical_path_details", "top_k": 2}]}
    )
    individual = session()
    first = individual.accept(
        {"kind": "QUERY", "queries": [{"name": "route_summary", "top_k": 1}]}
    )
    second = individual.accept(
        {"kind": "QUERY", "queries": [{"name": "critical_path_details", "top_k": 2}]}
    )
    assert batch.query_batch["individual_hashes"] == [
        first.query_batch["individual_hashes"][0],
        second.query_batch["individual_hashes"][0],
    ]


def test_system_owned_fields_and_level_nesting_fail_closed() -> None:
    system = session("L2").accept(
        {"kind": "COMMIT", "decision_id": "invented", "candidate": {"language": "M", "action_key": "macro.phys_opt"}}
    )
    assert system.feedback[0]["error_code"] == "model_supplied_system_owned_field"
    p = {"language": "P", "steps": [{"step_id": "s1", "op": "CALL_MACRO", "args": {"action_key": "macro.place_route"}}]}
    assert session("L2").accept({"kind": "COMMIT", "candidate": p}).feedback[0]["error_code"] == "language_not_allowed_at_level"
    assert session("L3").accept({"kind": "COMMIT", "candidate": p}).status == "COMMITTED"
    assert session("L4").accept({"kind": "COMMIT", "candidate": p}).status == "COMMITTED"
