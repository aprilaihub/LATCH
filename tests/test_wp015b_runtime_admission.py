from scripts.run_wp015b_runtime_admission import FAMILIES, candidate_rows, replay_saved_queries, select
from optimizer_pipeline.dof.single_submission.handles import TrialHandleRegistry


def _row(family: str, model: str, valid: bool = True) -> dict:
    return {
        "strict_mechanism": family,
        "model_key": model,
        "strict_candidate_valid": valid,
    }


def test_candidate_sources_are_separated_and_ordered() -> None:
    legacy = {"rows": [
        _row(FAMILIES[0], "openai/a"),
        _row(FAMILIES[1], "anthropic/b"),
        _row(FAMILIES[2], "must/not-use"),
    ]}
    hardblock = {"status": "PASS", "rows": [
        _row(FAMILIES[2], "openai/c"),
        _row(FAMILIES[0], "must/not-use"),
    ]}
    rows = candidate_rows(legacy, hardblock)
    assert [row["candidate_admission"] for row in rows[FAMILIES[0]]] == ["wp015a"]
    assert [row["candidate_admission"] for row in rows[FAMILIES[2]]] == ["wp015b"]
    order = {"orders": {
        FAMILIES[0]: ["openai/a"], FAMILIES[1]: ["anthropic/b"],
        FAMILIES[2]: ["openai/c"],
    }}
    selected = select(legacy, hardblock, order, candidate_index=0, families=FAMILIES)
    assert [row["model_key"] for row in selected] == ["openai/a", "anthropic/b", "openai/c"]


def test_saved_query_replay_restores_planning_registry() -> None:
    registry = TrialHandleRegistry(trial_id="trial", root_sha256="a" * 64, secret=b"x" * 32)
    rows = {"critical_object_locations": [{
        "canonical_identity": "cell/private", "handle_type": "cell",
        "path_member": True, "path_rank": 1, "resource_type": "LUT",
    }]}
    surface = __import__(
        "optimizer_pipeline.dof.single_submission.observation_v3", fromlist=["CommonQuerySurfaceV3"]
    ).CommonQuerySurfaceV3(registry)
    expected = surface.materialize(
        query_name="critical_object_locations", top_k=1,
        private_rows=rows["critical_object_locations"], tool_call_ids=("evidence",), latency_seconds=0.0,
    )
    planning = {
        "turns": [{"planner_result": {"query_batch": {"items": [{
            "request": {"name": "critical_object_locations", "top_k": 1},
            "result_sha256": expected.sha256,
        }]}}}],
        "query_handle_manifest": registry.public_manifest(),
    }
    replay_registry = TrialHandleRegistry(trial_id="trial", root_sha256="a" * 64, secret=b"x" * 32)
    proof = replay_saved_queries(
        registry=replay_registry,
        private={"rows": rows, "missing_reasons": {}}, planning_result=planning,
    )
    assert proof["query_count"] == 1
    assert replay_registry.public_manifest() == planning["query_handle_manifest"]
