import asyncio
from pathlib import Path

import pytest

from optimizer_pipeline import DeterministicAnalyzer, OutputValidator, OptimizationConfig
from optimizer_pipeline.core.store import load_run_view
from optimizer_pipeline.core.domain import ActionSpecification, Attempt
from optimizer_pipeline.run import bootstrap_run, resume_run
from tests.test_analysis import FakeExecutor, fake_analysis_provider


def _kwargs(tmp_path: Path, *, runs: int = 2):
    input_dcp = tmp_path / "input.dcp"
    input_dcp.write_text("fake\n")
    return {
        "input_dcp": input_dcp,
        "run_dir": tmp_path / "run",
        "config": OptimizationConfig(
            max_total_runs=runs,
            allowed_flows=["auto_pblock", "phys_opt"],
            fallback_flow="phys_opt",
        ),
        "analyzer": DeterministicAnalyzer(analysis_provider=fake_analysis_provider),
        "executor": FakeExecutor(),
        "output_validator": OutputValidator(),
        "max_eda_runs": runs,
    }


def test_context_records_transitions_and_balanced_budget(tmp_path: Path):
    async def run():
        ctx = await bootstrap_run(**_kwargs(tmp_path))
        await ctx.expand.fixed(["auto_pblock", "phys_opt"])
        ctx.finalize()
        return ctx

    ctx = asyncio.run(run())
    view = load_run_view(ctx.run_dir)
    assert [row["action_type"] for row in view["attempts"]] == ["auto_pblock", "phys_opt"]
    assert view["budget"] == {"reserved": 2, "completed": 2, "failed": 0}
    assert view["orphan_attempt_ids"] == []


def test_bootstrap_refuses_existing_canonical_run(tmp_path: Path):
    async def run():
        kwargs = _kwargs(tmp_path, runs=1)
        await bootstrap_run(**kwargs)
        await bootstrap_run(**kwargs)

    with pytest.raises(FileExistsError, match="use resume_run"):
        asyncio.run(run())


def test_frontier_uses_policy_ranking_and_keeps_sibling_lineage(tmp_path: Path):
    async def run():
        kwargs = _kwargs(tmp_path)
        kwargs["config"].allowed_flows = ["phys_opt", "auto_pblock"]
        ctx = await bootstrap_run(**kwargs)
        await ctx.expand.frontier(policy="deterministic", max_transitions=2, parallelism=2)
        ctx.finalize()
        return ctx

    ctx = asyncio.run(run())
    view = load_run_view(ctx.run_dir)
    assert [row["action_type"] for row in view["attempts"]] == ["auto_pblock", "phys_opt"]
    assert {row["parent_state_id"] for row in view["attempts"]} == {"node_000_root"}
    assert {row["scope_id"] for row in view["attempts"]} == {"scope-frontier-000"}


def test_resume_continues_without_duplicate_attempts(tmp_path: Path):
    async def run():
        kwargs = _kwargs(tmp_path)
        ctx = await bootstrap_run(**kwargs)
        await ctx.expand.fixed(["auto_pblock"])
        resumed = await resume_run(**kwargs)
        await resumed.expand.fixed(["phys_opt"])
        resumed.finalize()
        return resumed

    ctx = asyncio.run(run())
    view = load_run_view(ctx.run_dir)
    assert view["attempt_count"] == 2
    assert [row["output_state_id"] for row in view["attempts"]] == [
        "node_001_auto_pblock", "node_002_phys_opt",
    ]


def test_resume_blocks_orphaned_canonical_attempt(tmp_path: Path):
    async def run():
        kwargs = _kwargs(tmp_path, runs=1)
        ctx = await bootstrap_run(**kwargs)
        ctx.run_store.add_action(ActionSpecification(
            "action-orphan", ctx.run_id, "scope-root", "phys_opt",
        ))
        ctx.run_store.add_attempt(Attempt(
            "attempt-orphan", ctx.run_id, "scope-root", "node_000_root", "action-orphan",
        ))
        await resume_run(**kwargs)

    with pytest.raises(RuntimeError, match="orphaned attempts: attempt-orphan"):
        asyncio.run(run())


def test_frontier_uses_policy_ranking_and_keeps_sibling_parents(tmp_path: Path):
    async def run():
        ctx = await bootstrap_run(**_kwargs(tmp_path))
        await ctx.expand.frontier(
            policy="deterministic",
            max_transitions=2,
            parallelism=2,
            branching_factor=2,
            beam_width=2,
            batch_size=2,
        )
        ctx.finalize()
        return ctx

    ctx = asyncio.run(run())
    view = load_run_view(ctx.run_dir)
    assert [row["action_type"] for row in view["attempts"]] == ["auto_pblock", "phys_opt"]
    assert [row["parent_state_id"] for row in view["attempts"]] == [
        "node_000_root", "node_000_root",
    ]
