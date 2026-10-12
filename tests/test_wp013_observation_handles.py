from __future__ import annotations

import hashlib

import pytest

from optimizer_pipeline.dof.single_submission.handles import HandleError, TrialHandleRegistry
from optimizer_pipeline.dof.single_submission.observation import (
    CommonQuerySurface,
    ObservationError,
    build_common_observation,
)


ROOT = hashlib.sha256(b"immutable-root").hexdigest()
SECRET = hashlib.sha256(b"test-only-trial-secret").digest()


def registry(trial: str = "trial-1", root: str = ROOT) -> TrialHandleRegistry:
    return TrialHandleRegistry(trial_id=trial, root_sha256=root, secret=SECRET)


def observation():
    return build_common_observation(
        root_pseudonym="root-" + ROOT[:16],
        target_clock={"handle": "clock_h0123456789abcdef01234567", "period_ns": 3.0},
        timing={"fmax_mhz": 300.0, "wns_ns": -0.1, "tns_ns": -2.0, "failing_endpoint_count": 12},
        utilization={"lut": 100, "ff": 200, "dsp": 4, "bram": 2, "uram": 0},
        implementation={"placement_state": "PLACED", "route_state": "ROUTED", "route_validity": "VALID"},
        constraints={"pblock_count": 0, "constrained_cell_count": 0, "summary": "none"},
        geometry={"device_class": "xcvu", "slr_bounds": [0, 2], "clock_region_bounds": [0, 11, 0, 7]},
        congestion={"coarse_class": "medium", "hotspot_count": 2},
        critical_paths=[{"rank": 1, "slack_ns": -0.1, "delay_ns": 3.1, "resources": {"dsp": 1}}],
        budget={"planning_turns": 4, "queries": 8, "feedback_rounds": 3, "heavy_calls": 4, "light_mutations": 8},
    )


def test_common_observation_bytes_are_identical_for_l2_l3_l4() -> None:
    item = observation()
    assert item.for_level("L2") == item.for_level("L3") == item.for_level("L4")
    assert item.sha256 == hashlib.sha256(item.canonical_bytes).hexdigest()


def test_observation_rejects_path_leakage() -> None:
    with pytest.raises(ObservationError, match="path_leakage"):
        build_common_observation(
            root_pseudonym="root-" + ROOT[:16],
            target_clock={}, timing={}, utilization={}, implementation={},
            constraints={"summary": "/secret/design.dcp"}, geometry={}, congestion={},
            critical_paths=[], budget={},
        )


def test_handles_stable_within_trial_and_fail_closed_across_trials_and_types() -> None:
    first = registry()
    handle = first.register(object_type="cell", canonical_identity="private/hier/U0", attributes={"fanout": 4})
    assert first.register(object_type="cell", canonical_identity="private/hier/U0", attributes={"fanout": 4}) == handle
    assert first.resolve(handle, expected_type="cell").canonical_identity == "private/hier/U0"
    with pytest.raises(HandleError, match="type_mismatch"):
        first.resolve(handle, expected_type="net")
    with pytest.raises(HandleError, match="unknown_or_stale"):
        registry("trial-2").resolve(handle, expected_type="cell")


def test_query_executes_deterministic_order_truncation_and_never_exposes_identity() -> None:
    surface = CommonQuerySurface(registry())
    rows = [
        {"canonical_identity": "net/private_b", "handle_type": "net", "fanout": 20, "object_type": "net"},
        {"canonical_identity": "net/private_a", "handle_type": "net", "fanout": 20, "object_type": "net"},
        {"canonical_identity": "net/private_c", "handle_type": "net", "fanout": 8, "object_type": "net"},
    ]
    result = surface.materialize(query_name="high_fanout_net_details", top_k=2, private_rows=rows, latency_seconds=0.1)
    payload = result.public_payload()
    assert payload["returned"] == 2 and payload["truncated"] is True
    assert "private_a" not in result.canonical_bytes.decode()
    assert [row["fanout"] for row in payload["rows"]] == [20, 20]
    replay = surface.materialize(query_name="high_fanout_net_details", top_k=2, private_rows=rows, latency_seconds=99.0)
    assert replay is result


def test_query_result_change_within_trial_fails_closed() -> None:
    surface = CommonQuerySurface(registry())
    base = [{"canonical_identity": "cell/A", "handle_type": "cell", "path_rank": 1}]
    surface.materialize(query_name="critical_object_locations", top_k=1, private_rows=base)
    changed = [{"canonical_identity": "cell/B", "handle_type": "cell", "path_rank": 1}]
    with pytest.raises(ObservationError, match="changed_within_trial"):
        surface.materialize(query_name="critical_object_locations", top_k=1, private_rows=changed)


def test_cross_tool_identity_map_must_be_one_to_one() -> None:
    audit = registry().validate_cross_tool_identity_map(
        vivado_identities=["a", "b"], rapidwright_to_vivado={"rw1": "a", "rw2": "a"}
    )
    assert audit["accepted"] is False
    assert "rapidwright_many_to_one_mapping" in audit["errors"]
