"""Offline fixtures for input protection, continuation and required examples."""

import asyncio
import json
import sqlite3
from pathlib import Path

import pytest

from optimizer_pipeline import DeterministicAnalyzer, OutputValidator
from optimizer_pipeline.apps.experiment import entry
from optimizer_pipeline.apps.experiment.mock import MockExecutor, mock_analysis_provider
from optimizer_pipeline.apps.fpl_contest import entry as contest_entry
from optimizer_pipeline.apps.fpl_contest.args import build_contest_parser
from optimizer_pipeline.core.store import SQLiteRunStore
from optimizer_pipeline.models import ExecutionResult, PipelineStatus
from optimizer_pipeline.run import OptimizationConfig, bootstrap_run, resume_run


def _kwargs(tmp_path, *, executor=None, runs=2):
    root = tmp_path / "input.dcp"
    root.write_text("protected input fixture\n")
    return {
        "input_dcp": root,
        "run_dir": tmp_path / "run",
        "config": OptimizationConfig(max_total_runs=runs, max_parallel_jobs=1),
        "analyzer": DeterministicAnalyzer(analysis_provider=mock_analysis_provider),
        "executor": executor or MockExecutor(),
        "output_validator": OutputValidator(),
    }


@pytest.mark.parametrize("existing", ["symlink", "hardlink", "ordinary", "manifest"])
def test_api_rejects_dirty_workspace_before_analysis_or_writes(tmp_path, existing):
    kwargs = _kwargs(tmp_path)
    workspace = kwargs["run_dir"]
    output = workspace / "transition_000_phys_opt" / "output.dcp"
    output.parent.mkdir(parents=True)
    if existing == "symlink":
        output.symlink_to(kwargs["input_dcp"])
    elif existing == "hardlink":
        output.hardlink_to(kwargs["input_dcp"])
    elif existing == "ordinary":
        output.write_text("keep existing output\n")
    else:
        (workspace / "result_manifest.json").write_text('{"keep": true}\n')
    before = {path.relative_to(workspace): path.read_bytes()
              for path in workspace.rglob("*") if path.is_file()}

    class UnexpectedAnalyzer:
        async def analyze(self, *args):
            pytest.fail("dirty workspace reached analysis")

    kwargs["analyzer"] = UnexpectedAnalyzer()
    with pytest.raises(FileExistsError, match="absent or empty"):
        asyncio.run(bootstrap_run(**kwargs))
    assert kwargs["input_dcp"].read_text() == "protected input fixture\n"
    assert before == {path.relative_to(workspace): path.read_bytes()
                      for path in workspace.rglob("*") if path.is_file()}
    assert not (workspace / "run.db").exists()
    assert not (workspace / "workspaces").exists()


def test_api_accepts_existing_empty_workspace(tmp_path):
    kwargs = _kwargs(tmp_path)
    kwargs["run_dir"].mkdir()
    ctx = asyncio.run(bootstrap_run(**kwargs))
    try:
        assert ctx.current_dcp == kwargs["input_dcp"]
    finally:
        ctx.close()


async def _selected_child(kwargs):
    ctx = await bootstrap_run(**kwargs)
    records = await ctx.expand.fixed(["phys_opt"])
    assert records[0].outcome.accepted_by_transition_executor
    selected = ctx.run_store.selections()[-1]
    assert selected.state_id != "node_000_root"
    artifact = next(item for item in ctx.run_store.artifacts()
                    if item.artifact_id == selected.artifact_id)
    assert artifact.sha256 is not None
    return ctx, artifact


def _damage(path, change):
    if change == "modified":
        path.write_text("changed after validation\n")
    else:
        path.unlink()


@pytest.mark.parametrize("change", ["modified", "missing"])
def test_resume_rejects_changed_selection_and_closes_without_new_facts(tmp_path, monkeypatch, change):
    kwargs = _kwargs(tmp_path)
    ctx, artifact = asyncio.run(_selected_child(kwargs))
    facts = (ctx.run_store.run_events(), ctx.run_store.attempts(),
             ctx.run_store.outcomes(), ctx.run_store.artifacts())
    ctx.close()
    _damage(Path(artifact.uri), change)
    closed = []
    original_close = SQLiteRunStore.close

    def close(store):
        closed.append(store)
        original_close(store)

    monkeypatch.setattr(SQLiteRunStore, "close", close)
    expected = ValueError if change == "modified" else FileNotFoundError
    with pytest.raises(expected, match="recorded selected DCP"):
        asyncio.run(resume_run(**kwargs))
    assert len(closed) == 1
    with pytest.raises(sqlite3.ProgrammingError):
        closed[0].get_run()
    store = SQLiteRunStore(kwargs["run_dir"] / "run.db", migrate=False)
    try:
        assert facts == (store.run_events(), store.attempts(), store.outcomes(), store.artifacts())
    finally:
        store.close()
    assert not (kwargs["run_dir"] / "result_manifest.json").exists()
    assert kwargs["input_dcp"].read_text() == "protected input fixture\n"


@pytest.mark.parametrize("change", ["modified", "missing"])
def test_finalize_does_not_promote_changed_selection_but_can_record_failure(tmp_path, change):
    kwargs = _kwargs(tmp_path)
    ctx, artifact = asyncio.run(_selected_child(kwargs))
    try:
        _damage(Path(artifact.uri), change)
        events = ctx.run_store.run_events()
        expected = ValueError if change == "modified" else FileNotFoundError
        with pytest.raises(expected, match="recorded selected DCP"):
            ctx.finalize()
        assert not ctx.finalized
        assert ctx.run_store.run_events() == events
        assert not (ctx.run_dir / "result_manifest.json").exists()
        manifest = ctx.finalize(success=False, status=PipelineStatus.FAILED)
        assert json.loads(manifest.read_text())["success"] is False
        assert ctx.run_store.run_events()[-1].status.value == "FAILED"
    finally:
        ctx.close()


@pytest.mark.parametrize("payload", [[], None, "invalid", True, 7])
def test_config_rejects_explicit_non_object_payload(tmp_path, payload):
    with pytest.raises(ValueError, match="JSON object"):
        OptimizationConfig.from_dict(payload)
    path = tmp_path / "config.json"
    path.write_text(json.dumps(payload))
    with pytest.raises(ValueError, match="JSON object"):
        OptimizationConfig.from_json_file(path)


def test_omitted_and_empty_object_config_keep_defaults():
    assert OptimizationConfig.from_json_file(None) == OptimizationConfig()
    assert OptimizationConfig.from_dict({}) == OptimizationConfig()


def _args(tmp_path, pipeline="one_physical_transition"):
    root = tmp_path / "input.dcp"
    root.write_text("protected input fixture\n")
    return entry.build_experiment_parser().parse_args([
        str(root), "--mock", "--run-dir", str(tmp_path / "run"),
        "--custom-pipeline", f"optimizer_pipeline.apps.experiment.pipelines:{pipeline}",
    ])


def test_cli_rejects_non_object_config_before_creating_run(tmp_path):
    args = _args(tmp_path)
    args.config = tmp_path / "config.json"
    args.config.write_text("[]\n")
    assert asyncio.run(entry.run_experiment_entry(args)) == 1
    assert not args.run_dir.exists()
    assert args.input_dcp.read_text() == "protected input fixture\n"


class _FailedExecutor:
    def __init__(self, kind):
        self.kind = kind

    async def execute(self, input_dcp, output_dcp, run_dir, flow, optimizer_params=None):
        if self.kind == "exception":
            raise RuntimeError("fixture executor unavailable")
        if self.kind == "invalid_output":
            result = await MockExecutor().execute(input_dcp, output_dcp, run_dir, flow)
            (run_dir / "terminal.log").write_text("route_design failed\n")
            return result
        return ExecutionResult(
            success=False, flow=flow, input_dcp=str(input_dcp), output_dcp=str(output_dcp),
            run_dir=str(run_dir), metrics={"total_runtime_seconds": 0.01}, exit_code=17,
            error="fixture executor failed",
        )


def _inject_executor(monkeypatch, executor):
    monkeypatch.setattr(entry, "_build_services", lambda *args: (
        DeterministicAnalyzer(analysis_provider=mock_analysis_provider), executor,
    ))


@pytest.mark.parametrize("pipeline", ["one_physical_transition", "analysis_then_fixed"])
@pytest.mark.parametrize("kind", ["exception", "failed_result", "invalid_output"])
def test_required_examples_report_failed_transition(tmp_path, monkeypatch, pipeline, kind):
    args = _args(tmp_path, pipeline)
    _inject_executor(monkeypatch, _FailedExecutor(kind))
    assert asyncio.run(entry.run_experiment_entry(args)) == 1
    manifest = json.loads((args.run_dir / "result_manifest.json").read_text())
    assert manifest["success"] is False
    assert manifest["selected_state_id"] == "node_000_root"
    assert manifest["budget"]["failed_eda_runs"] == 1
    assert not (args.run_dir / "selected.dcp").exists()
    store = SQLiteRunStore(args.run_dir / "run.db", migrate=False)
    try:
        expected_outcome = "SUCCEEDED" if kind == "invalid_output" else "FAILED"
        assert store.outcomes()[0].status.value == expected_outcome
        assert any(item.status.value == "INVALID" for item in store.validations()
                   if item.level.value == "ATTEMPT")
        assert store.run_events()[-1].status.value == "FAILED"
    finally:
        store.close()


def test_required_example_refuses_unexecuted_transition(tmp_path):
    args = _args(tmp_path)
    args.budget_runs = 0
    assert asyncio.run(entry.run_experiment_entry(args)) == 1
    manifest = json.loads((args.run_dir / "result_manifest.json").read_text())
    assert manifest["success"] is False
    assert manifest["attempt_count"] == 0


def test_custom_pipeline_can_deliberately_keep_safe_baseline(tmp_path, monkeypatch):
    args = _args(tmp_path)
    _inject_executor(monkeypatch, _FailedExecutor("failed_result"))

    async def fallback(ctx):
        await ctx.expand.fixed(["phys_opt"])

    monkeypatch.setattr(entry, "load_custom_pipeline", lambda target: fallback)
    assert asyncio.run(entry.run_experiment_entry(args)) == 0
    manifest = json.loads((args.run_dir / "result_manifest.json").read_text())
    assert manifest["success"] is True
    assert manifest["selected_state_id"] == "node_000_root"
    assert manifest["budget"]["failed_eda_runs"] == 1
    assert (args.run_dir / "selected.dcp").read_text() == "protected input fixture\n"


@pytest.mark.parametrize("final_fmax", [400.0, 390.0])
def test_required_execution_can_succeed_without_performance_gain(tmp_path, monkeypatch, final_fmax):
    class NonImprovingExecutor(MockExecutor):
        async def execute(self, *args, **kwargs):
            result = await super().execute(*args, **kwargs)
            result.metrics.update(final_fmax_mhz=final_fmax, final_wns_ns=-0.5,
                                  final_tns_ns=-20.0)
            return result

    args = _args(tmp_path)
    _inject_executor(monkeypatch, NonImprovingExecutor())
    assert asyncio.run(entry.run_experiment_entry(args)) == 0
    manifest = json.loads((args.run_dir / "result_manifest.json").read_text())
    assert manifest["success"] is True
    assert manifest["selected_state_id"] == "node_000_root"
    assert manifest["budget"]["completed_eda_runs"] == 1


def _contest_args(tmp_path, output, *, runs=1):
    root = tmp_path / "input.dcp"
    root.write_text("protected input fixture\n")
    return build_contest_parser().parse_args([
        str(root), "--mock", "--no-dotenv", "--pipeline", "fixed", "--plan", "phys_opt",
        "--run-dir", str(tmp_path / "run"), "--output", str(output), "--budget-runs", str(runs),
    ])


@pytest.mark.parametrize("relative", ["selected.dcp", "nested/selected.dcp"])
@pytest.mark.parametrize("runs", [0, 1])
def test_contest_can_publish_new_output_inside_run_after_bootstrap(tmp_path, relative, runs):
    output = tmp_path / "run" / relative
    args = _contest_args(tmp_path, output, runs=runs)
    assert asyncio.run(contest_entry.run_contest_entry(args)) == 0
    manifest = json.loads((args.run_dir / "result_manifest.json").read_text())
    assert manifest["success"] is True
    assert manifest["attempt_count"] == runs
    assert output.is_file()
    assert args.input_dcp.read_text() == "protected input fixture\n"
    if runs == 0:
        assert output.read_bytes() == args.input_dcp.read_bytes()


@pytest.mark.parametrize("name", ["run.db", "result_manifest.json"])
def test_contest_rejects_canonical_output_target_before_run(tmp_path, name):
    args = _contest_args(tmp_path, tmp_path / "run" / name)
    assert asyncio.run(contest_entry.run_contest_entry(args)) != 0
    assert not args.run_dir.exists()
    assert args.input_dcp.read_text() == "protected input fixture\n"


@pytest.mark.parametrize("name", ["run.db-wal", "run.db-shm", "created.dcp"])
def test_contest_does_not_overwrite_bootstrap_created_output_and_closes(tmp_path, monkeypatch, name):
    args = _contest_args(tmp_path, tmp_path / "run" / name)
    contexts = []
    original = contest_entry.bootstrap_run

    async def bootstrap(**kwargs):
        ctx = await original(**kwargs)
        contexts.append(ctx)
        if name == "created.dcp":
            args.output_dcp.write_text("created during bootstrap\n")
        else:
            assert args.output_dcp.exists()
        return ctx

    monkeypatch.setattr(contest_entry, "bootstrap_run", bootstrap)
    assert asyncio.run(contest_entry.run_contest_entry(args)) == 1
    with pytest.raises(sqlite3.ProgrammingError):
        contexts[0].run_store.get_run()
    store = SQLiteRunStore(args.run_dir / "run.db", migrate=False)
    try:
        assert store.get_run() is not None
        assert store.run_events()[-1].status.value == "FAILED"
        assert not store.attempts()
    finally:
        store.close()
    if name == "created.dcp":
        assert args.output_dcp.read_text() == "created during bootstrap\n"
    assert args.input_dcp.read_text() == "protected input fixture\n"


def test_contest_bootstrap_failure_returns_nonzero_without_publishing(tmp_path, monkeypatch):
    args = _contest_args(tmp_path, tmp_path / "run" / "selected.dcp")

    async def fail(**kwargs):
        raise RuntimeError("injected bootstrap failure")

    monkeypatch.setattr(contest_entry, "bootstrap_run", fail)
    assert asyncio.run(contest_entry.run_contest_entry(args)) == 1
    assert not args.output_dcp.exists()
