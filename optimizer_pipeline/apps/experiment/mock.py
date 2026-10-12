"""Packaged deterministic fixtures for harness examples, never physical DCPs."""
from pathlib import Path
from optimizer_pipeline.models import ExecutionResult


class MockExecutor:
    async def execute(self, input_dcp: Path, output_dcp: Path, run_dir: Path, flow: str, optimizer_params=None):
        run_dir.mkdir(parents=True, exist_ok=True)
        output_dcp.parent.mkdir(parents=True, exist_ok=True)
        output_dcp.write_text("mock output; not a physical checkpoint\n")
        return ExecutionResult(success=True, flow=flow, input_dcp=str(input_dcp),
            output_dcp=str(output_dcp), run_dir=str(run_dir), summary_path=None,
            metrics={"mode": "mock", "initial_fmax_mhz": 400.0, "final_fmax_mhz": 409.0,
                "clock_period_ns": 2.5, "initial_wns_ns": -0.5, "final_wns_ns": -0.4,
                "initial_tns_ns": -20.0, "final_tns_ns": -10.0, "total_runtime_seconds": 0.01},
            exit_code=0)


async def mock_analysis_provider(input_dcp: Path, config):
    return {"benchmark_name": input_dcp.stem, "mode": "mock", "target_clock": "clk_mock",
        "clock_period_ns": 2.5, "initial_fmax_mhz": 400.0, "initial_wns_ns": -0.5,
        "initial_tns_ns": -20.0, "flow_scores": [{"flow": "phys_opt", "flow_heuristic_score": 1.0}],
        "selected_top3": ["phys_opt"]}
