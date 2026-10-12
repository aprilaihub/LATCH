"""Safe offline regression for the handoff's concrete path/lifecycle failures."""
import asyncio
import json
import sqlite3
from pathlib import Path

import pytest
from optimizer_pipeline.apps.experiment import entry
from optimizer_pipeline.apps.fpl_contest.args import build_contest_parser
from optimizer_pipeline.apps.fpl_contest.entry import run_contest_entry
from optimizer_pipeline.run import bootstrap_run, resume_run
from optimizer_pipeline.run.paths import validate_new_run_paths
from tests.test_expansion_lifecycle import _kwargs


def test_contest_refuses_reused_workspace_before_output_publish(tmp_path):
    root = tmp_path / "input.dcp"
    root.write_text("original input")
    output = tmp_path / "selected.dcp"
    output.write_text("keep old output")
    workspace = tmp_path / "run"
    workspace.mkdir()
    (workspace / "run.db").write_text("old run")
    args = build_contest_parser().parse_args([str(root), "--mock", "--no-dotenv",
        "--run-dir", str(workspace), "--output", str(output)])
    assert asyncio.run(run_contest_entry(args)) != 0
    assert output.read_text() == "keep old output"
    assert root.read_text() == "original input"
    assert (workspace / "run.db").read_text() == "old run"


@pytest.mark.parametrize("case", ["same", "hardlink", "contained", "existing_output", "nonempty_run"])
def test_new_paths_do_not_write_or_overwrite_input(tmp_path, case):
    root = tmp_path / "input.dcp"
    root.write_text("original")
    workspace, output = tmp_path / "run", tmp_path / "output.dcp"
    if case == "same": output = root
    elif case == "hardlink": output.hardlink_to(root)
    elif case == "contained": workspace = tmp_path
    elif case == "existing_output": output.write_text("old")
    elif case == "nonempty_run":
        workspace.mkdir()
        (workspace / "keep.txt").write_text("keep")
    with pytest.raises((ValueError, FileExistsError)):
        validate_new_run_paths(input_dcp=root, run_dir=workspace, output_dcp=output)
    assert root.read_text() == "original"


@pytest.mark.parametrize("change", ["different_input", "changed_input", "config", "budget", "run_id"])
def test_resume_refuses_root_or_configuration_drift(tmp_path, change):
    async def run():
        kwargs = _kwargs(tmp_path)
        ctx = await bootstrap_run(**kwargs)
        ctx.close()
        if change == "different_input":
            other = tmp_path / "other.dcp"
            other.write_text("fake\n")
            kwargs["input_dcp"] = other
        elif change == "changed_input": kwargs["input_dcp"].write_text("changed")
        elif change == "config": kwargs["config"].beam_width += 1
        elif change == "budget": kwargs["max_eda_runs"] += 1
        elif change == "run_id": kwargs["run_id"] = "other-run"
        with pytest.raises(ValueError): await resume_run(**kwargs)
    asyncio.run(run())


def test_resume_missing_run_is_not_created(tmp_path):
    async def run():
        kwargs = _kwargs(tmp_path)
        with pytest.raises(FileNotFoundError): await resume_run(**kwargs)
        assert not kwargs["run_dir"].exists()
    asyncio.run(run())


def test_resuming_same_input_alias_is_safe(tmp_path):
    async def run():
        kwargs = _kwargs(tmp_path)
        ctx = await bootstrap_run(**kwargs)
        ctx.close()
        alias = tmp_path / "alias.dcp"
        alias.symlink_to(kwargs["input_dcp"])
        kwargs["input_dcp"] = alias
        ctx = await resume_run(**kwargs)
        await ctx.expand.fixed(["phys_opt"])
        ctx.finalize()
        ctx.close()
    asyncio.run(run())


def _mock_args(tmp_path):
    root = tmp_path / "input.dcp"
    root.write_text("existing mock fixture")
    return entry.build_experiment_parser().parse_args([str(root), "--mock",
        "--run-dir", str(tmp_path / "run"), "--custom-pipeline",
        "optimizer_pipeline.apps.experiment.pipelines:one_physical_transition"])


def test_generic_cli_preserves_omitted_config_fields_and_closes(tmp_path, monkeypatch):
    args = _mock_args(tmp_path)
    args.config = tmp_path / "config.json"
    args.config.write_text(json.dumps({"max_total_runs": 2, "max_parallel_jobs": 2, "beam_width": 3}))
    captured = []
    original = entry.bootstrap_run
    async def bootstrap(**kwargs):
        ctx = await original(**kwargs)
        captured.append(ctx)
        return ctx
    monkeypatch.setattr(entry, "bootstrap_run", bootstrap)
    assert asyncio.run(entry.run_experiment_entry(args)) == 0
    assert captured[0].config.max_total_runs == 2
    assert captured[0].config.max_parallel_jobs == 2
    assert captured[0].config.beam_width == 3
    with pytest.raises(sqlite3.ProgrammingError): captured[0].run_store.get_run()
    manifest = json.loads((args.run_dir / "result_manifest.json").read_text())
    assert manifest["mode"] == "custom_pipeline"
    assert manifest["attempt_count"] == 1
    assert (args.run_dir / "selected.dcp").exists()
    assert asyncio.run(entry.run_experiment_entry(args)) != 0
    assert manifest == json.loads((args.run_dir / "result_manifest.json").read_text())


def test_generic_failure_is_recorded_and_store_closed(tmp_path, monkeypatch):
    args = _mock_args(tmp_path)
    async def fail(ctx): raise RuntimeError("injected failure")
    monkeypatch.setattr(entry, "load_custom_pipeline", lambda target: fail)
    assert asyncio.run(entry.run_experiment_entry(args)) == 1
    result = json.loads((args.run_dir / "result_manifest.json").read_text())
    assert result["success"] is False
    assert not (args.run_dir / "selected.dcp").exists()


def test_generic_never_loads_dotenv(tmp_path, monkeypatch):
    args = _mock_args(tmp_path)
    (tmp_path / ".env").write_text("LATCH_UNREQUESTED_DOTENV=must_not_load\n")
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("LATCH_UNREQUESTED_DOTENV", raising=False)
    assert asyncio.run(entry.run_experiment_entry(args)) == 0
    import os
    assert "LATCH_UNREQUESTED_DOTENV" not in os.environ


def test_generic_real_dependencies_missing_stop_before_run(tmp_path):
    args = _mock_args(tmp_path)
    args.mock = False
    assert asyncio.run(entry.run_experiment_entry(args)) == 1
    assert not args.run_dir.exists()


def test_generic_rejects_dangling_output_symlink_before_execution(tmp_path):
    args = _mock_args(tmp_path)
    target = tmp_path / "elsewhere.dcp"
    args.output_dcp = tmp_path / "symlink.dcp"
    args.output_dcp.symlink_to(target)
    assert asyncio.run(entry.run_experiment_entry(args)) == 1
    assert args.output_dcp.is_symlink()
    assert not target.exists()
    assert not args.run_dir.exists()


def test_handoff_inventory_includes_shared_scoring_resource_and_changed_compat():
    from tests.latch.test_historical_semantics import historical
    historical("scripts/e1_score_dcp.tcl", "652aea7")  # Git-only source evidence, not runtime requirement.
    from tools.source_inventory import inventory
    paths = {r["path"] for r in inventory()}
    assert "scripts/e1_score_dcp.tcl" in paths
    assert "experiments/llm/runtime/client.py" in paths
    assert "configs/providers/anthropic.json" in paths
    assert "configs/wp014e/provider-profiles-v1.json" in paths
    assert "tests/test_wp015_cells.py" in paths
    assert not any("private" in Path(p).parts for p in paths)
