from __future__ import annotations

import asyncio
from decimal import Decimal
import inspect
from pathlib import Path
import os
import random

import pytest

from optimizer_pipeline.dof import wp016b_expansion as expansion
from optimizer_pipeline.dof.multi_provider import PROFILES
from optimizer_pipeline.dof.wp016_canary import ConcurrentRequestTracker, LiteralBudget
from scripts.run_wp016b_expansion import (
    CORE_DERIVED_SHA256,
    CORE_RAW_SHA256,
    exact_static_invariants,
    offline_replay,
    whole_expansion_preflight_cost,
)


def _historical_path(relative: str) -> Path | None:
    workspace = os.environ.get("LATCH_HISTORICAL_WORKSPACE")
    return Path(workspace) / relative if workspace else None


CORE_RAW = _historical_path('wp016b-core-runtime/wp016b-core__a037ad943f3c1103798638fa/final/core-result.json')
CORE_DERIVED = _historical_path('wp016b-core-derived/core-result.per-turn-cost-attribution-v1.json')
CAPTURE = _historical_path('wp015-runs/capture/final-v5')
HARDBLOCK = _historical_path('wp016b-expansion-inputs/hardblock-scan')


def _static():
    if CORE_RAW is None or CORE_DERIVED is None or not CORE_RAW.is_file() or not CORE_DERIVED.is_file():
        pytest.skip("accepted local WP-016B Core evidence unavailable")
    return exact_static_invariants(CORE_RAW, CORE_DERIVED)


def test_authority_and_exact_inventory_arithmetic() -> None:
    authority = expansion.exact_authority()
    manifest, schedule, rows = _static()
    assert authority["new_live_cells"] == 96
    assert authority["combined_cells"] == 144
    assert (len(rows), len(manifest["cells"]), len(schedule["blocks"])) == (48, 96, 32)
    assert sum(c["model_system"] == "openai/gpt-5.6-terra" and c["root_id"] in expansion.CORE_ROOTS for c in manifest["cells"]) == 24
    assert sum(c["root_id"] in expansion.NEW_ROOTS for c in manifest["cells"]) == 72


def test_schedule_seed_and_block_randomisation_are_reproducible() -> None:
    _manifest, observed, rows = _static()
    _rebuilt_manifest, rebuilt = expansion.expansion_manifest_and_schedule(rows)
    assert rebuilt == observed
    expected_hex = __import__("hashlib").sha256(b"271550774WP016B_EXPANSION_96").hexdigest()
    assert expansion.DERIVED_SEED_HEX == expected_hex
    assert expansion.DERIVED_SEED == int(expected_hex, 16) % (2**63)
    assert random.Random(expansion.DERIVED_SEED).random() == random.Random(expansion.DERIVED_SEED).random()
    assert all(len(block["cells"]) == 3 and {c["arm"] for c in block["cells"]} == set(expansion.ARMS) for block in observed["blocks"])


def test_expansion_identities_are_fresh_and_not_core_or_canary() -> None:
    manifest, _schedule, _rows = _static()
    core = __import__("json").loads(CORE_RAW.read_text())
    for key in ("configuration_id", "cell_id", "session_id", "run_id", "log_id", "output_id"):
        values = {str(c[key]) for c in manifest["cells"]}
        assert len(values) == 96
        assert not values & {str(c[key]) for c in core["cells"]}
    assert "canary" not in str(manifest["cells"]).lower()


def test_direct_openai_profiles_and_literal_budget_preflight() -> None:
    _manifest, schedule, _rows = _static()
    for model in expansion.MODELS:
        profile = PROFILES[model]
        assert (profile.provider, profile.route, profile.credential_env) == (
            "openai", "direct", "OPENAI_API_KEY",
        )
    ceiling = whole_expansion_preflight_cost(schedule)
    assert Decimal("0") < ceiling <= expansion.USD_CAP


def test_concurrent_budget_reservations_are_globally_safe_and_cell_isolated() -> None:
    budget = LiteralBudget(
        request_cap=40,
        literal_usd_cap=Decimal("2.00"),
        request_bounder=lambda _body, _model: Decimal("0.10"),
    )
    for index in range(20):
        _size, bound = budget.guard(b"p", b"c", {"messages": []})
        budget.reserve(f"r{index}", bound)
    assert len(budget.pending_reservations) == 20
    with pytest.raises(RuntimeError, match="literal_usd_cap_pre_request_stop"):
        budget.guard(b"p", b"c", {"messages": []})
    costs = [Decimal("0.001") if index % 2 else Decimal("0.009") for index in range(20)]
    for index in reversed(range(20)):
        budget.record(costs[index], reservation_id=f"r{index}")
    assert budget.requests == 20
    assert budget.charged == sum(costs)
    assert not budget.pending_reservations

    fail_closed = LiteralBudget(
        request_cap=1, literal_usd_cap=Decimal("1"),
        request_bounder=lambda _body, _model: Decimal("0.10"),
    )
    fail_closed.reserve("bounded", Decimal("0.10"))
    with pytest.raises(RuntimeError, match="exceeds_reserved_liability"):
        fail_closed.record(Decimal("0.11"), reservation_id="bounded")


def test_measured_concurrency_is_not_configured_concurrency() -> None:
    tracker = ConcurrentRequestTracker(cap=20)
    starts = [tracker.start(request_id=f"r{i}", cell_id=f"c{i}", turn=1) for i in range(20)]
    assert tracker.measured_peak == 20
    with pytest.raises(RuntimeError, match="cap_exceeded"):
        tracker.start(request_id="overflow", cell_id="overflow", turn=1)
    for i in reversed(range(20)):
        tracker.finish(request_id=f"r{i}", cell_id=f"c{i}", turn=1, started=starts[i])
    assert tracker.inflight == 0 and not tracker.active
    assert len(tracker.events) == 40


def test_no_backend_core_rerun_or_further_expansion_wiring() -> None:
    runner = (Path(__file__).parents[1] / "scripts/run_wp016b_expansion.py").read_text().lower()
    support = Path(expansion.__file__).read_text().lower()
    for forbidden in ("place_design", "route_design", "vivado_exec", "dcp_optimizer"):
        assert forbidden not in runner
        assert forbidden not in support
    assert '"core_cells_rerun": false' in support
    assert '"further_expansion_authorised": false' in expansion.AUTHORITY.read_text().lower()
    assert '"wp016c_authorised": false' in expansion.AUTHORITY.read_text().lower()


def test_source_index_defensive_invariant_remains_present() -> None:
    from optimizer_pipeline.dof import named_family

    source = inspect.getsource(named_family)
    assert "result_source_indices" in source
    assert "selected_source_indices" in source
    assert "result_indices != selected_indices" in source


@pytest.mark.skipif(
    not all(path is not None and path.exists() for path in (CORE_RAW, CORE_DERIVED, CAPTURE, HARDBLOCK)),
    reason="local frozen Expansion replay inputs unavailable",
)
def test_offline_replay_covers_48_sources_without_provider_calls() -> None:
    result = asyncio.run(offline_replay(CORE_RAW, CORE_DERIVED, CAPTURE, HARDBLOCK))
    assert result["status"] == "PASS_OFFLINE_REPLAY"
    assert result["unique_configurations_replayed"] == 48
    assert result["scientific_identities_instantiated"] == 96
    assert result["provider_calls"] == result["backend_calls"] == 0


def test_accepted_core_hash_constants_are_explicit() -> None:
    assert CORE_RAW_SHA256 == "0453c94feba05fca47d1b67827e4832b1bf7096e6a39be321988a3b7a8b1509a"
    assert CORE_DERIVED_SHA256 == "2521616250e3aaecd199255cdfffb8c4ae1777753b940088a89995ece1eebde1"


def test_combined_analysis_contract_has_exact_24_pairs_per_contrast() -> None:
    from scripts.analyze_wp016b_expansion import contrast_summary, paired

    rows = []
    for root in (*expansion.CORE_ROOTS, *expansion.NEW_ROOTS):
        for model in expansion.MODELS:
            for capability in expansion.CAPABILITIES:
                for repeat in expansion.REPEATS:
                    for arm in expansion.ARMS:
                        rows.append({
                            "root_id": root, "root_stratum": (
                                "SOURCE_TIMED_TRANSFER" if root in expansion.CORE_ROOTS
                                else "HELDOUT_PLUS_SYNTHETIC_TIMING"
                            ),
                            "model_system": model, "capability": capability,
                            "repeat": repeat, "arm": arm,
                            "primary_success": int(arm == "LEGACY_STATIC"),
                        })
    summaries = contrast_summary(paired(rows))
    assert len(summaries) == 5
    assert {row["pairs"] for row in summaries} == {24}
