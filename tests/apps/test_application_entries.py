from __future__ import annotations

import argparse
import asyncio

import pytest

from optimizer_pipeline.apps.direct_action.entry import (
    build_direct_action_parser,
    run_direct_action_entry,
)
from optimizer_pipeline.apps.experiment.entry import (
    build_experiment_parser,
    run_experiment_entry,
)
from optimizer_pipeline.apps.fpl_contest.args import build_contest_parser


def test_contest_parser_no_longer_exposes_direct_test_or_flow_options():
    options = {
        option
        for action in build_contest_parser()._actions
        for option in action.option_strings
    }
    assert "--test" not in options
    assert "--flow" not in options


def test_direct_action_parser_requires_explicit_action():
    parser = build_direct_action_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["input.dcp"])
    args = parser.parse_args(["input.dcp", "--action", "phys_opt"])
    assert args.action == "phys_opt"


def test_direct_action_passes_explicit_action_for_renamed_input(tmp_path):
    input_dcp = tmp_path / "opaque-copy.dcp"
    input_dcp.write_text("mock")
    calls = []

    async def fake_run(input_path, output_path, **kwargs):
        calls.append((input_path, output_path, kwargs))
        output_path.write_text("output")
        return 0

    args = build_direct_action_parser().parse_args(
        [
            str(input_dcp),
            "--action",
            "phys_opt",
            "--parameters-json",
            '{"directive":"Explore"}',
            "--run-dir",
            str(tmp_path / "run"),
            "--output",
            str(tmp_path / "output.dcp"),
        ]
    )
    assert asyncio.run(
        run_direct_action_entry(args, run_flow=fake_run, terminal_log=False)
    ) == 0
    assert calls[0][2]["flow"] == "phys_opt"
    assert calls[0][2]["optimizer_params"] == {"directive": "Explore"}
    assert "opaque-copy" not in calls[0][2]["flow"]


def test_generic_experiment_entry_has_no_contest_policy_options():
    parser = build_experiment_parser()
    args = parser.parse_args(["input.dcp", "--custom-pipeline", "pkg.module:run", "--mock"])
    assert args.custom_pipeline == "pkg.module:run"
    assert not hasattr(args, "run_mode")
    assert not hasattr(args, "pipeline")
    assert args.budget_runs is None
    assert args.parallelism is None

    missing = argparse.Namespace(custom_pipeline=None)
    with pytest.raises(ValueError, match="requires --custom-pipeline"):
        asyncio.run(run_experiment_entry(missing))
