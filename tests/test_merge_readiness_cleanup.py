import importlib
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from optimizer_pipeline import (
    DeterministicAnalyzer,
    OptimizationConfig,
)
from optimizer_pipeline.capabilities.search.policies.registry import PolicyResources, build_policy_registry


def test_legacy_llm_pipeline_entrypoints_are_removed():
    repo_root = Path(__file__).resolve().parents[1]

    help_text = subprocess.check_output([sys.executable, "dcp_optimizer.py", "--help"], cwd=repo_root, text=True)
    make_help = subprocess.check_output(["make", "-n", "help"], cwd=repo_root, text=True)

    assert "--llm-pipeline" not in help_text
    assert "--llm-provider" in help_text
    assert "--run-mode" in help_text
    assert "--pipeline" in help_text
    assert "--max-iterations" not in help_text
    assert "run_llm_pipeline" not in make_help

    for module_name in [
        "optimizer_pipeline.runner",
        "optimizer_pipeline.policy",
        "optimizer_pipeline.search",
        "optimizer_pipeline.selector",
        "optimizer_pipeline.strategies",
    ]:
        with pytest.raises(ModuleNotFoundError):
            importlib.import_module(module_name)


def test_frontier_cli_analysis_overrides(monkeypatch):
    from scripts import run_frontier_expansion

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "run_frontier_expansion.py",
            "input.dcp",
            "--mock",
            "--analysis-state-budget-sec",
            "42",
            "--analysis-max-states-per-expansion",
            "2",
            "--analysis-fresh-probe-contract-mode",
            "required",
            "--no-dotenv",
        ],
    )
    args = run_frontier_expansion.parse_args()
    config = run_frontier_expansion.apply_cli_overrides(OptimizationConfig(), args)

    assert config.analysis_state_budget_sec == 42.0
    assert config.analysis_max_states_per_expansion == 2
    assert config.analysis_fresh_probe_contract_mode == "required"


def test_contest_clean_rejects_external_history_config():
    for kwargs in (
        {"retrieval_index_path": "experiments/old/retrieval_index.pkl"},
        {"reliability_stats_path": "experiments/old/reliability_summary.json"},
        {"policy_type": "retrieval"},
        {"llm_context_mode": "retrieval"},
        {"knowledge_mode": "benchmark_history"},
        {"knowledge_mode": "both"},
    ):
        config = OptimizationConfig(contest_clean=True, **kwargs)
        with pytest.raises(ValueError, match="contest_clean forbids external history"):
            config.validate_contest_clean()

    OptimizationConfig(contest_clean=True, knowledge_mode="reference_manual").validate_contest_clean()


def test_frontier_cli_contest_clean_rejects_retrieval_index(monkeypatch):
    from scripts import run_frontier_expansion

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "run_frontier_expansion.py",
            "input.dcp",
            "--mock",
            "--contest-clean",
            "--retrieval-index",
            "experiments/old/retrieval_index.pkl",
            "--no-dotenv",
        ],
    )
    args = run_frontier_expansion.parse_args()
    with pytest.raises(ValueError, match="contest_clean forbids external history"):
        run_frontier_expansion.apply_cli_overrides(OptimizationConfig(), args)


def test_contest_clean_policy_registry_rejects_external_resources():
    registry = build_policy_registry()
    config = OptimizationConfig(contest_clean=True)

    with pytest.raises(ValueError, match="externally supplied policy resources"):
        registry.create(
            "analysis_priority",
            resources=PolicyResources(config=config, retrieval_index=object()),
        )

    with pytest.raises(ValueError, match="externally supplied policy resources"):
        registry.create(
            "deterministic",
            resources=PolicyResources(config=config, reliability_table=object()),
        )


def test_optimization_config_strict_unknown_fields_and_analysis_model():
    with pytest.raises(ValueError, match="Unknown OptimizationConfig field"):
        OptimizationConfig.from_dict({"not_a_real_field": 1})

    config = OptimizationConfig.from_dict(
        {
            "note": "human-readable context only",
            "description": "also metadata only",
            "$schema": "local-schema-placeholder",
            "analysis_loop_model": "gpt-analysis-test",
        }
    )

    assert config.analysis_loop_model == "gpt-analysis-test"
    assert config.analysis_decision_model() == "gpt-analysis-test"


def test_shell_script_python_references_exist():
    repo_root = Path(__file__).resolve().parents[1]
    scripts_dir = repo_root / "scripts"
    missing = []
    for shell_script in sorted(scripts_dir.glob("*.sh")):
        text = shell_script.read_text()
        for match in re.finditer(r"scripts/[A-Za-z0-9_./-]+\.py", text):
            candidate = repo_root / match.group(0)
            if not candidate.exists():
                missing.append(f"{shell_script.name}: {match.group(0)}")
    assert missing == []


def test_make_run_optimizer_mock_smoke_uses_contest_shell(tmp_path):
    repo_root = Path(__file__).resolve().parents[1]
    input_dcp = tmp_path / "input.dcp"
    input_dcp.write_text("mock input\n")

    proc = subprocess.run(
        [
            "make",
            "run_optimizer",
            f"PYTHON={sys.executable}",
            f"DCP={input_dcp}",
            "PIPELINE=linear",
            "BUDGET_RUNS=2",
            "SINGLE_PATH_DEPTH=2",
            "LLM_PROVIDER=none",
            "ANALYSIS=none",
            "MOCK=1",
        ],
        cwd=repo_root,
        text=True,
        capture_output=True,
    )

    try:
        assert proc.returncode == 0, proc.stderr + proc.stdout
        assert "dcp_optimizer.py" not in proc.stderr
        optimized = sorted(tmp_path.glob("input_optimized-*.dcp"))
        assert optimized and optimized[-1].is_file()
        assert "Run dir:" in proc.stdout
        run_dir_text = proc.stdout.split("Run dir:", 1)[1].strip().splitlines()[0]
        run_dir = Path(run_dir_text)
        assert (run_dir / "result_manifest.json").is_file()
        assert (run_dir / "run.db").is_file()
    finally:
        if "Run dir:" in proc.stdout:
            run_dir_text = proc.stdout.split("Run dir:", 1)[1].strip().splitlines()[0]
            shutil.rmtree(Path(run_dir_text), ignore_errors=True)


def test_make_run_optimizer_delegates_defaults_to_contest_parser(tmp_path):
    repo_root = Path(__file__).resolve().parents[1]
    input_dcp = tmp_path / "input.dcp"
    input_dcp.write_text("mock input\n")

    proc = subprocess.run(
        [
            "make",
            "-n",
            "run_optimizer",
            f"PYTHON={sys.executable}",
            f"DCP={input_dcp}",
        ],
        cwd=repo_root,
        text=True,
        capture_output=True,
    )

    assert proc.returncode == 0, proc.stderr + proc.stdout
    rendered = proc.stdout
    for option in (
        "--run-mode",
        "--pipeline",
        "--policy",
        "--analysis",
        "--llm-provider",
        "--budget-runs",
        "--single-path-depth",
        "--parallelism",
        "--rescue-mode",
        "--knowledge-mode",
    ):
        assert option not in rendered

    makefile = (repo_root / "Makefile").read_text()
    assert "TEMPORARY: defaults for AWS" not in makefile


def test_analyzer_uses_expansion_config(tmp_path):
    async def provider(_input_dcp, config):
        assert isinstance(config, OptimizationConfig)
        return {
            "benchmark_name": "input",
            "initial_fmax_mhz": 400.0,
            "flow_scores": [{"flow": "phys_opt", "flow_heuristic_score": 1.0}],
        }

    input_dcp = tmp_path / "input.dcp"
    input_dcp.write_text("fake\n")
    analyzer = DeterministicAnalyzer(analysis_provider=provider)

    import asyncio

    context = asyncio.run(analyzer.analyze(input_dcp, [], OptimizationConfig()))

    assert context["schema_version"] == 2
    assert context["benchmark_name"] == "input"
