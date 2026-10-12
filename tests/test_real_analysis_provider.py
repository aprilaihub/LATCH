import asyncio
from argparse import Namespace
from pathlib import Path

from optimizer_pipeline import DeterministicAnalyzer, OptimizationConfig
from optimizer_pipeline.apps.fpl_contest.entry import _build_analyzer as build_contest_analyzer
from optimizer_pipeline.real_analysis_provider import build_real_analysis_provider
from scripts.run_fixed_sequence_expansion import build_analyzer_for_args as build_fixed_sequence_analyzer


class FakeOptimizerTest:
    instances = []

    def __init__(self, *, debug, run_dir):
        self.debug = debug
        self.run_dir = Path(run_dir)
        self.cleaned = False
        FakeOptimizerTest.instances.append(self)

    async def start_servers(self):
        self.run_dir.mkdir(parents=True, exist_ok=True)

    async def analyze_design_for_flow_selection(self, input_dcp):
        return {
            "benchmark_name": Path(input_dcp).stem,
            "flow_scores": [{"flow": "phys_opt", "flow_heuristic_score": 9.0}],
            "selected_top3": ["phys_opt"],
        }

    async def cleanup(self):
        self.cleaned = True


class FailingOptimizerTest(FakeOptimizerTest):
    async def analyze_design_for_flow_selection(self, input_dcp):
        raise RuntimeError("provider boom")


def test_real_analysis_provider_uses_isolated_analysis_run_and_cleans_up(tmp_path: Path):
    FakeOptimizerTest.instances = []
    input_dcp = tmp_path / "foo bar.dcp"
    input_dcp.write_text("fake\n")
    config = OptimizationConfig(policy_type="deterministic", debug=True)
    provider = build_real_analysis_provider(
        run_dir=tmp_path / "run",
        config=config,
        optimizer_test_cls=FakeOptimizerTest,
    )

    result = asyncio.run(provider(input_dcp, config))

    assert result["selected_top3"] == ["phys_opt"]
    assert len(FakeOptimizerTest.instances) == 1
    instance = FakeOptimizerTest.instances[0]
    assert instance.debug is True
    assert instance.run_dir == tmp_path / "run" / "analysis_runs" / "analysis_001_foo_bar"
    assert instance.run_dir.is_dir()
    assert instance.cleaned is True


def test_provider_exception_surfaces_as_analysis_provider_error(tmp_path: Path):
    input_dcp = tmp_path / "input.dcp"
    input_dcp.write_text("fake\n")
    config = OptimizationConfig(policy_type="deterministic")
    provider = build_real_analysis_provider(
        run_dir=tmp_path / "run",
        config=config,
        optimizer_test_cls=FailingOptimizerTest,
    )

    ctx = asyncio.run(DeterministicAnalyzer(analysis_provider=provider).analyze(input_dcp, [], config))

    assert "RuntimeError: provider boom" in ctx["analysis_provider_error"]


def test_fixed_sequence_analyzer_wiring_keeps_mock_provider_and_adds_real_provider(tmp_path: Path):
    config = OptimizationConfig(policy_type="deterministic")

    mock_analyzer = build_fixed_sequence_analyzer(Namespace(mock=True, out=tmp_path / "mock"), config)
    real_analyzer = build_fixed_sequence_analyzer(Namespace(mock=False, out=tmp_path / "real"), config)

    assert mock_analyzer.analysis_provider is not None
    assert real_analyzer.analysis_provider is not None


def test_contest_entry_real_analyzer_has_default_provider(tmp_path: Path):
    config = OptimizationConfig(policy_type="deterministic")

    mock_analyzer = build_contest_analyzer(Namespace(mock=True), config=config, run_dir=tmp_path / "mock")
    real_analyzer = build_contest_analyzer(Namespace(mock=False), config=config, run_dir=tmp_path / "real")

    assert mock_analyzer.analysis_provider is not None
    assert real_analyzer.analysis_provider is not None
