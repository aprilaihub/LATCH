"""Canonical-owner, SQLite outcome and portable entrypoint checks."""
import asyncio
import json
from pathlib import Path
import subprocess
import sys

import pytest

from experiments.ablation.__main__ import study_config
from experiments.ablation.config import OptimizationConfig
from experiments.ablation.runtime.artifacts import ArtifactStore
from experiments.ablation.runtime.expansion.frontier_runner import FrontierExpansionRunner
from experiments.ablation.runtime.expansion.policies.core import DeterministicExpansionPolicy
from experiments.ablation.runner_cli import MockExecutor, mock_analysis_provider
from optimizer_pipeline import DeterministicAnalyzer, OutputValidator
from optimizer_pipeline.core.store import SQLiteRunStore
from optimizer_pipeline.core.domain import ValidationLevel, ValidationStatus


def test_common_provider_identity():
    from experiments.llm.runtime.types import LLMRequest as compatibility
    from integrations.llm.runtime.types import LLMRequest as canonical
    assert compatibility is canonical


def test_shared_runtime_has_no_experiment_import():
    root = Path(__file__).resolve().parents[2]
    owners = [root / "integrations", root / "optimizer_pipeline/core", root / "optimizer_pipeline/backends",
              root / "optimizer_pipeline/execution", root / "optimizer_pipeline/run", root / "optimizer_pipeline/capabilities"]
    for owner in owners:
        for path in owner.rglob("*.py"):
            text = path.read_text()
            assert "from experiments" not in text and "import experiments" not in text, path
    assert "optimizer_pipeline.dof" not in (root / "optimizer_pipeline/apps/fpl_contest/official_score.py").read_text()


@pytest.mark.parametrize("condition,depth,branch,beam,runs", [("S0",3,1,1,3),("S1",11,1,1,11),("S2",6,3,2,11),("S3",6,3,2,11)])
def test_frozen_condition_settings(condition, depth, branch, beam, runs):
    config = study_config(condition, mode="mock")
    assert (config.max_depth, config.branching_factor, config.beam_width, config.max_total_runs) == (depth, branch, beam, runs)
    assert config.knowledge_mode == ("reference_manual" if condition == "S3" else "off")
    assert not config.prune_dcps_keep_winning_path
    assert not any(flow.endswith("_rw") for flow in config.allowed_flows)


def test_s0_extension_keeps_eight_attempt_budget():
    config = study_config("S0", mode="mock", s0_phase="extension")
    assert config.max_total_runs == config.max_depth == 8


def test_missing_model_stops_before_any_tool():
    with pytest.raises(ValueError, match="explicit provider and model"):
        study_config("S1", mode="real", cost_budget=1)


@pytest.mark.parametrize("passed", [True, False])
def test_candidate_execution_and_final_validation_are_distinct(tmp_path, passed):
    input_dcp = tmp_path / "input.dcp"
    input_dcp.write_text("mock root")
    calls = []
    async def checker(root, child):
        calls.append((root, child))
        return {"passed": passed}
    runner = FrontierExpansionRunner(config=OptimizationConfig(max_total_runs=1, allowed_flows=["phys_opt"], policy_type="deterministic"),
        artifact_store=ArtifactStore(tmp_path / "run"), analyzer=DeterministicAnalyzer(analysis_provider=mock_analysis_provider),
        executor=MockExecutor(), output_validator=OutputValidator(), policy=DeterministicExpansionPolicy(), equivalence_checker=checker)
    result = asyncio.run(runner.run(input_dcp))
    assert result.success is passed
    assert len(calls) == 1
    store = SQLiteRunStore(tmp_path / "run/run.db")
    try:
        assert len(store.outcomes()) == 1
        certification = [row for row in store.validations() if row.validation_id == "validation-final"]
        assert len(certification) == 1
        assert certification[0].level == ValidationLevel.CERTIFICATION
        assert certification[0].status == (ValidationStatus.PASSED if passed else ValidationStatus.FAILED)
    finally:
        store.close()


def test_final_checker_exception_is_not_a_functional_mismatch(tmp_path):
    from types import SimpleNamespace
    calls = []
    async def checker(root, child):
        calls.append((root, child))
        raise FileNotFoundError("synthetic missing netlist")
    owner = SimpleNamespace(equivalence_checker=checker, verification_seconds=0, artifacts=None)
    state = SimpleNamespace()
    result = asyncio.run(FrontierExpansionRunner._run_equivalence_gate(
        owner, tmp_path / "root", tmp_path / "child", state))
    assert len(calls) == 1
    assert result["passed"] is False and result["status"] == "failed"
    assert result["infrastructure_failure"] is True
    assert result["error_type"] == "FileNotFoundError"
    assert state.equivalence_gate == result


def test_module_entry_from_unrelated_directory(tmp_path):
    import os
    root = Path(__file__).resolve().parents[2]
    output = tmp_path / "new-run"
    env = dict(os.environ, PYTHONPATH=str(root))
    command = [sys.executable, "-m", "experiments.ablation", "--condition", "S0", "--mode", "mock", "--output", str(output)]
    first = subprocess.run(command, cwd=tmp_path, env=env, capture_output=True, text=True)
    assert first.returncode == 0, first.stderr + first.stdout
    facts = json.loads(first.stdout)
    assert facts["functional_check"]["passed"] is None
    before = (output / "run.db").read_bytes()
    second = subprocess.run(command, cwd=tmp_path, env=env, capture_output=True, text=True)
    assert second.returncode == 2
    assert (output / "run.db").read_bytes() == before
