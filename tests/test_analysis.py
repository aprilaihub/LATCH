import asyncio
import json
from pathlib import Path

from optimizer_pipeline import DeterministicAnalyzer, OptimizationConfig
from optimizer_pipeline.capabilities.analysis import StaticAnalysisBudgetPolicy, TimePressureStopPolicy
from optimizer_pipeline.capabilities.analysis.schemas import AnalysisBudget, AnalysisToolCall
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
