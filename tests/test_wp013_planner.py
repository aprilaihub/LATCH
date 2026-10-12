from __future__ import annotations

import hashlib

import pytest

from optimizer_pipeline.dof.single_submission.handles import TrialHandleRegistry
from optimizer_pipeline.dof.single_submission.language import CompileContext
from optimizer_pipeline.dof.single_submission.observation import CommonQuerySurface
from optimizer_pipeline.dof.single_submission.planner import PlannerError, SingleSubmissionSession, SystemBinding
from tests.test_wp013_observation_handles import observation


def session(level: str = "L4") -> SingleSubmissionSession:
    obs = observation()
    root = hashlib.sha256(b"planner-root").hexdigest()
    registry = TrialHandleRegistry(trial_id="planner-trial", root_sha256=root, secret=hashlib.sha256(b"planner-secret").digest())
    queries = CommonQuerySurface(registry)
    context = CompileContext(registry, "ROUTED", (), (0, 10, 0, 10))
    binding = SystemBinding.create(observation_hash=obs.sha256, root_binding=root, catalogue_version="catalogue.v1", catalogue_hash=hashlib.sha256(b"catalogue").hexdigest(), provider="openrouter", model="pinned-model", route="pinned-route", session_nonce="one")

    def provider(name, top_k):
        return ([{"canonical_identity": "private/net/N", "handle_type": "net", "object_type": "net", "fanout": 20}], ["call-readonly-1"], 0.01, None)

    return SingleSubmissionSession(level, obs, queries, context, binding, provider)


def test_planner_binds_envelope_and_accepts_exactly_one_commit() -> None:
    item = session("L2")
    result = item.accept({"kind": "COMMIT", "candidate": {"language": "M", "macro": "macro.place_route"}})
    assert result.status == "COMMITTED"
    assert item.evidence()["valid_commits"] == 1
    with pytest.raises(PlannerError, match="already_terminal"):
        item.accept({"kind": "COMMIT", "candidate": {"language": "M", "macro": "macro.phys_opt"}})


def test_model_cannot_supply_system_fields_and_feedback_contains_no_qor() -> None:
    item = session("L3")
    result = item.accept({"kind": "COMMIT", "decision_id": "invented", "candidate": {"language": "M", "macro": "macro.phys_opt"}})
    assert result.status == "CONSTRUCTION_INVALID"
    assert result.feedback == ("model_supplied_system_owned_field",)
    evidence = item.evidence()
    assert evidence["precommit_qor_feedback_count"] == 0
    assert evidence["precommit_mutation_count"] == 0
    assert all(row["qor_feedback"] is False and row["mutation"] is False for row in evidence["transcript"])


def test_common_query_then_valid_commit_uses_two_turns() -> None:
    item = session("L4")
    query = item.accept({"kind": "QUERY", "query": {"name": "high_fanout_net_details", "top_k": 4}})
    assert query.status == "QUERY_RESULT"
    committed = item.accept({"kind": "COMMIT", "candidate": {"language": "M", "macro": "macro.congestion_spread"}})
    assert committed.status == "COMMITTED"
    assert item.evidence()["turns_used"] == 2


def test_semantic_level_nesting_is_enforced() -> None:
    p = {"language": "P", "steps": [{"step_id": "s1", "op": "CALL_MACRO", "args": {"macro": "macro.place_route"}}]}
    l2 = session("L2").accept({"kind": "COMMIT", "candidate": p})
    assert l2.feedback == ("language_not_allowed_at_level",)
    assert session("L3").accept({"kind": "COMMIT", "candidate": p}).status == "COMMITTED"
    assert session("L4").accept({"kind": "COMMIT", "candidate": p}).status == "COMMITTED"


def test_feedback_and_turn_limits_end_no_valid_submission() -> None:
    item = session("L4")
    for _ in range(3):
        assert item.accept({"kind": "COMMIT", "candidate": {"language": "G", "statements": []}}).status == "CONSTRUCTION_INVALID"
    fourth = item.accept({"kind": "COMMIT", "candidate": {"language": "G", "statements": []}})
    assert fourth.status == "NO_VALID_SUBMISSION"
    assert item.terminal == "NO_VALID_SUBMISSION"
