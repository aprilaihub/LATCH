from __future__ import annotations

import json
from pathlib import Path

from optimizer_pipeline.dof.single_submission.contract_v6 import validate_protocol_cell
from scripts.run_wp014c_scientific import capture, jobs_v3


def test_final_dual_track_matrix_has_exact_tracks_and_calibration() -> None:
    stress = json.loads(Path("configs/wp014/stress-masks-v1.json").read_text())
    jobs = jobs_v3(stress)
    assert len(jobs) == 162
    assert sum(row["phase"] == "N_K" for row in jobs) == 90
    assert sum(row["phase"] == "S_N" for row in jobs) == 36
    assert sum(row["phase"] == "C_S" for row in jobs) == 36
    assert sum(row["phase"] == "C_S" and row["calibration_cell"] for row in jobs) == 18
    assert {row["study_track"] for row in jobs if row["phase"] == "S_N"} == {"S_NEUTRAL_REDUCED"}
    for row in jobs:
        validate_protocol_cell(
            study_track=row["study_track"], coverage_regime=row["coverage_regime"],
            language_condition=row["language_condition"], level=row["agent_level"],
        )


def test_capture_uses_rapidwright_as_the_only_wp014c_fanout_authority(monkeypatch, tmp_path) -> None:
    import argparse
    import asyncio
    from scripts import run_wp014c_scientific as scientific

    observed = {}

    async def fake_legacy_capture(args):
        observed["omit_vivado_fanout"] = args.omit_vivado_fanout
        return 2

    monkeypatch.setattr(scientific.legacy, "capture", fake_legacy_capture)
    args = argparse.Namespace(out=tmp_path)
    assert asyncio.run(capture(args)) == 2
    assert observed == {"omit_vivado_fanout": True}
