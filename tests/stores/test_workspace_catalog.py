import asyncio
from pathlib import Path
import sqlite3

from optimizer_pipeline import DeterministicAnalyzer, OptimizationConfig, OutputValidator
from optimizer_pipeline.core.store import rebuild_workspace_catalog
from optimizer_pipeline.run import bootstrap_run
from tests.test_analysis import FakeExecutor, fake_analysis_provider


def test_workspace_catalog_is_fully_rebuildable_and_not_run_truth(tmp_path: Path):
    async def make_run(name: str):
        input_dcp = tmp_path / f"{name}.dcp"
        input_dcp.write_text("fake\n")
        ctx = await bootstrap_run(
            input_dcp=input_dcp,
            run_dir=tmp_path / name,
            config=OptimizationConfig(max_total_runs=1, allowed_flows=["phys_opt"]),
            analyzer=DeterministicAnalyzer(analysis_provider=fake_analysis_provider),
            executor=FakeExecutor(),
            output_validator=OutputValidator(),
            max_eda_runs=1,
        )
        await ctx.expand.fixed(["phys_opt"])
        ctx.finalize()

    asyncio.run(make_run("run-a"))
    asyncio.run(make_run("run-b"))
    catalog = tmp_path / "workspace.db"

    assert rebuild_workspace_catalog(catalog, [tmp_path]) == 2
    with sqlite3.connect(catalog) as connection:
        first = connection.execute(
            "SELECT run_id, status, attempt_count FROM runs ORDER BY run_id"
        ).fetchall()
    catalog.unlink()
    assert (tmp_path / "run-a" / "run.db").is_file()
    assert rebuild_workspace_catalog(catalog, [tmp_path]) == 2
    with sqlite3.connect(catalog) as connection:
        second = connection.execute(
            "SELECT run_id, status, attempt_count FROM runs ORDER BY run_id"
        ).fetchall()
    assert second == first
