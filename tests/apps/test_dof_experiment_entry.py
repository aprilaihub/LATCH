from __future__ import annotations

import argparse
import ast
from pathlib import Path

import pytest

from optimizer_pipeline.apps.dof_experiment.entry import (
    build_dof_experiment_parser,
    run_dof_experiment_entry,
)


def test_dof_entry_requires_a_spec_and_run_directory() -> None:
    parser = build_dof_experiment_parser()
    with pytest.raises(SystemExit):
        parser.parse_args([])
    args = parser.parse_args(["condition.json", "--run-dir", "run"])
    assert args.spec == Path("condition.json")
    assert args.execution_mode == "mock"


def test_dof_entry_invokes_only_the_injected_mock_runner(capsys: pytest.CaptureFixture[str]) -> None:
    calls: list[tuple[Path, Path]] = []

    def fake_runner(spec: Path, run_dir: Path) -> dict[str, object]:
        calls.append((spec, run_dir))
        return {"endpoint": "VALID_COMPLETION", "run_id": "run-test"}

    args = build_dof_experiment_parser().parse_args(
        ["condition.json", "--run-dir", "run"]
    )
    assert run_dof_experiment_entry(args, runner=fake_runner) == 0
    assert calls == [(Path("condition.json"), Path("run"))]
    assert "VALID_COMPLETION" in capsys.readouterr().out


def test_dof_entry_rejects_non_mock_execution_before_runner_dispatch() -> None:
    args = argparse.Namespace(
        spec=Path("condition.json"),
        run_dir=Path("run"),
        execution_mode="real",
    )
    called = False

    def forbidden_runner(_spec: Path, _run_dir: Path) -> dict[str, object]:
        nonlocal called
        called = True
        return {}

    with pytest.raises(ValueError, match="mock execution only"):
        run_dof_experiment_entry(args, runner=forbidden_runner)
    assert called is False


def test_dof_entry_has_no_contest_or_real_tool_dependency() -> None:
    source = (Path(__file__).resolve().parents[2] / "optimizer_pipeline/apps/dof_experiment/entry.py").read_text()
    imports: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            imports.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.append(node.module)
    assert not any(
        name.startswith(("optimizer_pipeline.apps.fpl_contest", "optimizer_pipeline.backends.xilinx"))
        or name.startswith(("VivadoMCP", "RapidWrightMCP", "dotenv"))
        for name in imports
    )
