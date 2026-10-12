# Ported from a722b1e; import paths only adapted.
import asyncio
import json
import tempfile
from pathlib import Path

from optimizer_pipeline import DeterministicAnalyzer, OutputValidator
from experiments.ablation.config import OptimizationConfig
from experiments.ablation.runtime.artifacts import ArtifactStore
from experiments.ablation.runtime.expansion.frontier_runner import FrontierExpansionRunner
from experiments.ablation.runtime.expansion.policies.core import DeterministicExpansionPolicy

from experiments.ablation.runtime.analysis import StaticAnalysisBudgetPolicy, TimePressureStopPolicy
from experiments.ablation.runtime.analysis.schemas import AnalysisBudget, AnalysisToolCall
from optimizer_pipeline.models import ExecutionResult


class FakeExecutor:
    async def execute(self, input_dcp, output_dcp, run_dir, flow):
        run_dir.mkdir(parents=True, exist_ok=True)
        output_dcp.parent.mkdir(parents=True, exist_ok=True)
        output_dcp.write_text("fake dcp\n")
        gain = {"auto_pblock": 12.0, "phys_opt": 8.0, "retime": 5.0}.get(flow, 1.0)
        metrics = {
            "flow": flow,
            "clock_period_ns": 2.5,
            "initial_fmax_mhz": 400.0,
            "final_fmax_mhz": 400.0 + gain,
            "initial_wns_ns": -0.5,
            "final_wns_ns": -0.5 + gain / 100.0,
            "initial_tns_ns": -20.0,
            "final_tns_ns": -20.0 + gain / 2.0,
            "total_runtime_seconds": 0.01,
        }
        summary = run_dir / "run_summary.json"
        summary.write_text(json.dumps(metrics))
        return ExecutionResult(True, flow, str(input_dcp), str(output_dcp), str(run_dir), str(summary), metrics, 0)


async def fake_analysis_provider(input_dcp, config):
    return {
        "benchmark_name": Path(input_dcp).stem,
        "clock_period_ns": 2.5,
        "initial_fmax_mhz": 400.0,
        "initial_wns_ns": -0.5,
        "initial_tns_ns": -20.0,
        "failing_endpoints": 10,
        "utilization_summary": {"luts": 10, "ffs": 20},
        "critical_path_features": {"route_delay_ratio": 0.72, "dominant_path_type": "routing_dominated"},
        "flow_scores": [
            {"flow": "auto_pblock", "flow_heuristic_score": 10.0},
            {"flow": "phys_opt", "flow_heuristic_score": 8.0},
            {"flow": "retime", "flow_heuristic_score": 6.0},
        ],
        "selected_top3": ["auto_pblock", "phys_opt", "retime"],
    }


def test_analyzer_schema_populates_provider_flow_scores_and_top3(tmp_path: Path):
    input_dcp = tmp_path / "input.dcp"
    input_dcp.write_text("fake\n")
    config = OptimizationConfig(policy_type="deterministic")
    ctx = asyncio.run(DeterministicAnalyzer(analysis_provider=fake_analysis_provider).analyze(input_dcp, [], config))
    assert ctx["candidate_flows"]["deterministic_scores"]
    assert ctx["candidate_flows"]["selected_top3"] == ["auto_pblock", "phys_opt", "retime"]
    assert ctx["flow_scores"] == ctx["candidate_flows"]["deterministic_scores"]
    assert ctx["raw_provider_context"]["selected_top3"] == ["auto_pblock", "phys_opt", "retime"]


def test_time_pressure_stop_policy_hard_stops_near_budget_when_stable():
    policy = TimePressureStopPolicy()
    budget = AnalysisBudget(max_wall_time_sec=600, max_tool_calls=10)
    calls = [AnalysisToolCall("load_node_summary", {}, "now", 0.1, True, output={})]
    decision = policy.should_continue(elapsed_sec=590, budget=budget, tool_calls=calls, next_tool="vivado_report_timing_summary")
    assert not decision.continue_analysis
    assert "strong" in decision.stop_reason or "budget" in decision.stop_reason


def test_static_budget_keeps_root_budget_at_ten_minutes_and_report_probes_enabled():
    policy = StaticAnalysisBudgetPolicy(root_budget_sec=600, node_budget_sec=120, allow_report_probes=True, allow_act_probes=False)
    trigger = type("T", (), {"budget_profile": "root"})()
    budget = policy.budget_for(trigger)
    assert budget.max_wall_time_sec == 600
    assert budget.allow_report_probes is True
    assert budget.allow_act_probes is False


def test_online_analysis_mock_run_writes_analysis_artifacts_and_does_not_change_qor_semantics():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        input_dcp = root / "input.dcp"
        input_dcp.write_text("fake\n")
        config = OptimizationConfig(
            policy_type="deterministic",
            max_depth=1,
            max_total_runs=2,
            branching_factor=2,
            beam_width=1,
            allowed_flows=["auto_pblock", "phys_opt", "retime"],
            fallback_flow="phys_opt",
            online_deep_analysis=True,
            analysis_level="report_probe",
            analysis_root_budget_sec=600,
        )
        runner = FrontierExpansionRunner(
            config=config,
            artifact_store=ArtifactStore(root / "run"),
            analyzer=DeterministicAnalyzer(analysis_provider=fake_analysis_provider),
            executor=FakeExecutor(),
            output_validator=OutputValidator(),
            policy=DeterministicExpansionPolicy(policy_name="deterministic"),
        )
        result = asyncio.run(runner.run(input_dcp))
        assert result.success
        run_dir = Path(result.run_dir)
        assert (run_dir / "analysis" / "analysis_events.jsonl").is_file()
        assert (run_dir / "analysis" / "candidate_priors.jsonl").is_file()
        summary = json.loads((run_dir / "pipeline_summary.json").read_text())
        assert summary["analysis"]["enabled"] is True
        best_qor = summary["best_pure_qor"]
        assert best_qor["weights"]["runtime_minutes"] == 0.0


def test_model_token_budgets_does_not_match_mini_inside_gemini():
    """"mini" as a bare substring matches inside "gemini".

    That silently gave every Gemini model the mini-class analysis budget
    (500k hard vs 250k), so Gemini never starved while its comparators did,
    and cross-model cost comparison was invalid. Match a delimited token.
    """
    from experiments.ablation.runtime.analysis.dynamic_loop import _model_token_budgets

    # Standard and mini tiers are numerically identical since the ceiling
    # was raised to 500k, so budget values can no longer distinguish them.
    # Assert the predicate directly -- misclassification was the actual bug.
    standard = (420_000, 700_000)
    mini = (420_000, 700_000)

    # the regression itself
    assert _model_token_budgets("gemini-3.1-pro-preview") == standard
    assert _model_token_budgets("gemini-3.7-flash") == standard

    # other non-mini models stay standard
    for model in ("claude-opus-5", "gpt-5.6-luna", "o3"):
        assert _model_token_budgets(model) == standard, model

    # genuine mini models still get the larger budget
    for model in ("gpt-5.4-mini", "gpt-4o-mini", "openai/o4-mini"):
        assert _model_token_budgets(model) == mini, model

    # a Gemini model that really is mini-class is still matched
    assert _model_token_budgets("gemini-2.5-flash-mini") == mini

    # the 5.5 branch is untouched
    assert _model_token_budgets("gpt-5.5") == (75_000, 100_000)

    # the predicate itself -- independent of whatever the tiers are worth
    from experiments.ablation.runtime.analysis.dynamic_loop import _is_mini_class

    for model in ("gemini-3.1-pro-preview", "gemini-3.6-flash", "claude-sonnet-5",
                  "claude-opus-5", "gpt-5.6-luna", "gpt-5.6-sol", "o3"):
        assert not _is_mini_class(model), model
    for model in ("gpt-5.4-mini", "gpt-4o-mini", "openai/o4-mini",
                  "gemini-2.5-flash-mini"):
        assert _is_mini_class(model), model
