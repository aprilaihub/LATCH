from __future__ import annotations

import inspect
from pathlib import Path
import os
import random

import pytest

from optimizer_pipeline.dof import wp016b
from optimizer_pipeline.dof.wp016_canary import CanaryController
from scripts.run_wp016b_core import exact_static_invariants, whole_core_preflight_cost


def test_exact_core_arithmetic_and_authority() -> None:
    authority = wp016b.exact_authority()
    manifest, schedule, rows = exact_static_invariants()
    assert (len(rows), len(manifest["cells"]), len(schedule["blocks"])) == (24, 48, 16)
    assert authority["maximum_provider_requests"] == 48 * 4 == 192


def test_each_block_has_three_arms_once() -> None:
    _manifest, schedule, _rows = exact_static_invariants()
    expected = set(wp016b.ARMS)
    assert all({cell["arm"] for cell in block["cells"]} == expected for block in schedule["blocks"])


def test_schedule_is_reproducible_from_frozen_seed() -> None:
    _manifest, observed, rows = exact_static_invariants()
    _rebuilt_manifest, rebuilt = wp016b.core_manifest_and_schedule(rows)
    assert rebuilt == observed
    rng = random.Random(271550774)
    assert rng.random() == random.Random(wp016b.SEED).random()


def test_all_runtime_identities_are_unique_and_not_canary() -> None:
    manifest, _schedule, _rows = exact_static_invariants()
    for key in ("configuration_id", "cell_id", "session_id", "run_id", "log_id", "output_id"):
        assert len({cell[key] for cell in manifest["cells"]}) == 48
    assert "canary" not in str(manifest["cells"]).lower()


def test_only_authorised_core_roots_and_models_exist() -> None:
    manifest, _schedule, rows = exact_static_invariants()
    assert {cell["root_id"] for cell in manifest["cells"]} == set(wp016b.ROOTS)
    assert {cell["model_system"] for cell in manifest["cells"]} == set(wp016b.MODELS)
    assert not {"titan23_orig_gsm_x6", "mlcad_d181"} & {row["root_id"] for row in rows}


def test_luna_and_sol_use_exact_direct_profiles() -> None:
    from optimizer_pipeline.dof.multi_provider import PROFILES

    for model in wp016b.MODELS:
        profile = PROFILES[model]
        assert profile.provider == "openai"
        assert profile.route == "direct"
        assert profile.credential_env == "OPENAI_API_KEY"


def test_public_final_text_and_endpoint_are_in_durable_receipt_path() -> None:
    source = inspect.getsource(CanaryController.run_cell)
    assert "public_final_text_utf8" in source
    assert "public_final_text_sha256" in source
    assert "parsed_public_endpoint" in source
    assert "raw_private_reasoning_persisted" in source


def test_budget_preflight_fits_literal_authority() -> None:
    _manifest, schedule, _rows = exact_static_invariants()
    ceiling = whole_core_preflight_cost(schedule)
    assert ceiling <= wp016b.USD_CAP
    assert ceiling > 0


def test_no_backend_or_extension_wiring() -> None:
    core = Path(wp016b.__file__).read_text(encoding="utf-8").lower()
    runner = (Path(__file__).parents[1] / "scripts/run_wp016b_core.py").read_text(encoding="utf-8").lower()
    for forbidden in ("place_design", "route_design", "rapidwright", "vivado_exec"):
        assert forbidden not in core
        assert forbidden not in runner
    assert "extension_authorised\": false" in wp016b.AUTHORITY.read_text(encoding="utf-8").lower()


def test_source_index_defensive_invariant_remains_present() -> None:
    from optimizer_pipeline.dof import named_family

    source = inspect.getsource(named_family)
    assert "result_source_indices" in source
    assert "selected_source_indices" in source
    assert "result_indices != selected_indices" in source


def test_scientific_controller_has_four_turn_and_zero_retry_contract() -> None:
    from optimizer_pipeline.dof.wp016_canary import TURN_CAP

    assert TURN_CAP == 4
    authority = wp016b.exact_authority()
    assert authority["retry_count"] == authority["fallback_count"] == authority["response_repair_count"] == 0


def _historical_path(relative: str) -> Path | None:
    workspace = os.environ.get("LATCH_HISTORICAL_WORKSPACE")
    return Path(workspace) / relative if workspace else None


CAPTURE = _historical_path("wp015-runs/capture/final-v2")


@pytest.mark.skipif(
    CAPTURE is None or not CAPTURE.is_dir(),
    reason="local frozen capture unavailable",
)
def test_offline_replay_covers_24_configurations_without_calls() -> None:
    import asyncio
    from scripts.run_wp016b_core import offline_replay

    hardblock = Path("/tmp/wp016a-r1-rebound-capsule/hardblock-scan")
    if not hardblock.is_dir():
        pytest.skip("accepted local hardblock capsule unavailable")
    result = asyncio.run(offline_replay(
        CAPTURE, hardblock,
    ))
    assert result["status"] == "PASS_OFFLINE_REPLAY"
    assert result["unique_configurations_replayed"] == 24
    assert result["provider_calls"] == result["backend_calls"] == 0
