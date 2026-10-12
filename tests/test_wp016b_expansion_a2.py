from __future__ import annotations

import asyncio
from decimal import Decimal
import json
from pathlib import Path
import os

import pytest

from optimizer_pipeline.dof import wp016b_expansion_a2 as a2
from optimizer_pipeline.dof.wp016_canary import file_sha256, read_json
from scripts.replay_wp016b_expansion_a2_orchestrator import replay
from scripts.run_wp016b_expansion_a2 import (
    ATTEMPT2_USD_CAP, CUMULATIVE_USD_CAP, exact_static_invariants,
    whole_attempt2_preflight_cost,
)


def _historical_path(relative: str) -> Path | None:
    workspace = os.environ.get("LATCH_HISTORICAL_WORKSPACE")
    return Path(workspace) / relative if workspace else None


CORE_RAW = _historical_path('wp016b-core-runtime/wp016b-core__a037ad943f3c1103798638fa/final/core-result.json')
CORE_DERIVED = _historical_path('wp016b-core-derived/core-result.per-turn-cost-attribution-v1.json')
ATTEMPT1_PACKAGE = Path(__file__).parents[1] / ".research-flow/outbox/codex_to_pro_C001_WP016B-EXPANSION.zip"


def test_direct_authority_and_attempt1_acceptance_are_exact() -> None:
    authority = a2.exact_authority()
    assert authority["attempt"] == 2
    assert authority["new_scientific_cells"] == 96
    assert authority["maximum_provider_concurrency"] == 20
    assert authority["attempt3_engineering_only_fallback_authorised"] is True
    assert authority["scientific_definition_change_authorised"] is False
    assert authority["attempt1_acceptance"]["scientific_analysis_eligible"] is False


def test_attempt2_manifest_is_deterministic_and_all_runtime_ids_are_fresh() -> None:
    manifest, schedule = a2.build_attempt2_manifest_and_schedule()
    assert manifest == read_json(a2.MANIFEST)
    assert schedule == read_json(a2.SCHEDULE)
    joined = a2.joined_schedule_cells(manifest, schedule)
    assert len(joined) == 96 and len(schedule["blocks"]) == 32
    attempt1 = read_json(Path(a2.ATTEMPT1_MANIFEST))
    for key in a2.IDENTITY_KEYS:
        fresh = {row[2][key] for row in joined}
        old = {row[key] for row in attempt1["cells"]}
        assert len(fresh) == 96
        assert not fresh & old
    assert [row[2]["launch_rank"] for row in joined] == list(range(1, 97))


def test_join_fails_closed_on_missing_duplicate_and_mismatched_metadata() -> None:
    manifest, schedule = a2.build_attempt2_manifest_and_schedule()
    missing = json.loads(json.dumps(schedule))
    missing["blocks"][0]["cells"][0].pop("launch_rank")
    with pytest.raises(RuntimeError, match="metadata_mismatch:launch_rank"):
        a2.joined_schedule_cells(manifest, missing)

    duplicate = json.loads(json.dumps(schedule))
    duplicate["blocks"][0]["cells"][1] = dict(duplicate["blocks"][0]["cells"][0])
    with pytest.raises(RuntimeError, match="duplicate_schedule_cell_id"):
        a2.joined_schedule_cells(manifest, duplicate)

    mismatch = json.loads(json.dumps(schedule))
    mismatch["blocks"][0]["cells"][0]["root_id"] = "wrong"
    with pytest.raises(RuntimeError, match="metadata_mismatch:root_id"):
        a2.joined_schedule_cells(manifest, mismatch)


@pytest.mark.skipif(
    not all(path is not None and path.is_file() for path in (CORE_RAW, CORE_DERIVED, ATTEMPT1_PACKAGE)),
    reason="accepted local Core/Attempt-1 evidence unavailable",
)
def test_exact_static_gate_binds_core_attempt1_and_a2() -> None:
    manifest, schedule, rows, joined = exact_static_invariants(
        CORE_RAW, CORE_DERIVED, ATTEMPT1_PACKAGE,
    )
    assert (len(manifest["cells"]), len(schedule["blocks"]), len(rows), len(joined)) == (96, 32, 48, 96)
    assert file_sha256(ATTEMPT1_PACKAGE) == a2.ATTEMPT1_PACKAGE_SHA256
    ceiling = whole_attempt2_preflight_cost(schedule)
    assert Decimal("0") < ceiling <= ATTEMPT2_USD_CAP < CUMULATIVE_USD_CAP


def test_full_orchestrator_zero_call_gate_covers_success_and_all_failures(tmp_path: Path) -> None:
    result = asyncio.run(replay(tmp_path / "runtime"))
    success = result["success_fixture"]
    assert result["status"] == "PASS_FULL_ORCHESTRATOR_ZERO_CALL_GATE"
    assert (success["completed_cells"], success["completed_blocks"]) == (96, 32)
    assert success["measured_peak_inflight_requests"] == 20
    assert success["cost_reconciliation_pass"] is True
    assert success["active_request_count_after"] == 0
    assert success["unique_mock_request_ids"] == 96
    assert result["provider_calls"] == result["backend_calls"] == 0
    assert set(result["failure_fixtures"]) == {
        "controller", "scientific_decoration", "durable_cell_publication",
        "block_aggregation", "final_aggregation",
    }
    for value in result["failure_fixtures"].values():
        assert value["status"] == "STOP_SHARED_INTEGRATION_DEFECT"
        assert value["shared_stop_records"]
        assert value["active_request_count_after"] == 0


def test_runner_contains_no_eda_or_selective_resume_surface() -> None:
    runner = (Path(__file__).parents[1] / "scripts/run_wp016b_expansion_a2.py").read_text().lower()
    for forbidden in ("place_design", "route_design", "vivado_exec", "dcp_optimizer", "selective_resume"):
        assert forbidden not in runner
    assert "wp016c_executed" in runner
    assert "attempt1_partial_traffic_reused" in runner
