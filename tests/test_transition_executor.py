import asyncio
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from optimizer_pipeline import DeterministicAnalyzer, DeterministicFlowExecutor, OutputValidator, OptimizationConfig
from optimizer_pipeline.capabilities.analysis.report_probe_backends import CachedAnalysisContextReportBackend
from optimizer_pipeline.execution.diagnostics import parse_executor_diagnostics
from optimizer_pipeline.execution import DcpTransitionExecutor, DcpTransitionRequest
from optimizer_pipeline.models import ExecutionResult
from tests.test_analysis import FakeExecutor, fake_analysis_provider


class MetricsBackfillExecutor:
    async def execute(self, input_dcp, output_dcp, run_dir, flow):
        run_dir.mkdir(parents=True, exist_ok=True)
        output_dcp.parent.mkdir(parents=True, exist_ok=True)
        output_dcp.write_text("fake dcp\n")
        metrics = {
            "flow": flow,
            "clock_period_ns": 2.5,
            "target_clock": "clk",
            "initial_fmax_mhz": 400.0,
            "final_fmax_mhz": 411.0,
            "initial_wns_ns": -0.5,
            "final_wns_ns": -0.12,
            "initial_tns_ns": -20.0,
            "final_tns_ns": -4.0,
            "total_runtime_seconds": 12.0,
            "final_utilization": {"luts": 100, "ffs": 200, "dsps": 3, "brams": 4},
            "utilization_delta": {"luts": 5},
        }
        summary = run_dir / "run_summary.json"
        summary.write_text(json.dumps(metrics))
        return ExecutionResult(True, flow, str(input_dcp), str(output_dcp), str(run_dir), str(summary), metrics, 0)


class DiagnosticArtifactExecutor(MetricsBackfillExecutor):
    async def execute(self, input_dcp, output_dcp, run_dir, flow):
        result = await super().execute(input_dcp, output_dcp, run_dir, flow)
        (run_dir / "critical_paths.json").write_text(json.dumps([
            [
                "top/u_pipe/data_reg",
                "top/u_pipe/stage_i_1",
                "top/u_mem/linebuffer_sdpram/ram_reg_bram_0_i_1",
                "top/u_mem/linebuffer_sdpram/ram_reg_bram_1",
            ],
            [
                "top/u_pipe/data_reg",
                "top/u_pipe/stage_i_2",
                "top/u_dsp/filter_dsp_inst",
                "top/u_mem/linebuffer_sdpram/ram_reg_bram_1",
            ],
        ]))
        return result


def test_managed_executor_keeps_summary_until_transition_validation(tmp_path: Path):
    async def run_test_mode(input_dcp, output_dcp, *, run_dir, flow, **kwargs):
        output_dcp.write_text("fake dcp\n")
        (run_dir / "run_summary.json").write_text(json.dumps({"final_fmax_mhz": 411.0}))
        (run_dir / "run_info_input_20260101.json").write_text(json.dumps({"wns": -0.12}))
        return 0

    input_dcp = tmp_path / "input.dcp"
    input_dcp.write_text("fake\n")
    config = OptimizationConfig(policy_type="deterministic")
    inner = DcpTransitionExecutor(
        executor=DeterministicFlowExecutor(run_test_mode, preserve_summary=False),
        output_validator=OutputValidator(),
        analyzer=DeterministicAnalyzer(),
        pipeline_config=config,
        persist_diagnostics=False,
    )

    result = asyncio.run(inner.run(DcpTransitionRequest(
        input_dcp=input_dcp,
        optimizer_name="phys_opt",
        output_dir=tmp_path / "run" / "step_000_phys_opt",
        root_metrics={"initial_fmax_mhz": 400.0},
        parent_node_id="root",
        child_node_id="managed_000_phys_opt",
    )))

    assert result.accepted is True
    assert result.output_validation.evidence["artifact_checks"]["summary_exists"] is True
    assert not list((tmp_path / "run" / "step_000_phys_opt" / "executor_run").glob("*.json"))


def test_executor_diagnostics_parse_critical_paths_and_congestion_log(tmp_path: Path):
    run_dir = tmp_path / "executor_run"
    run_dir.mkdir()
    (run_dir / "critical_paths_retime.json").write_text(json.dumps([
        ["top/a_reg", "top/b_i_1", "top/mem/ramb18_inst"],
        ["top/a_reg", "top/dsp/dsp48_inst"],
    ]))
    (run_dir / "route_status.rpt").write_text("Route Design completed successfully. No congestion observed. Fully routed.")

    parsed = parse_executor_diagnostics(run_dir)

    critical = parsed["critical_path_features"]
    assert critical["num_paths_analyzed"] == 2
    assert critical["avg_path_cell_count"] == pytest.approx(2.5)
    assert critical["max_path_cell_count"] == 3
    assert critical["primitive_mix"]["bram"] == 1
    assert critical["primitive_mix"]["dsp"] == 1
    assert critical["hardblock_on_path"] is True
    assert critical["top_repeated_cells"][0] == {"name": "top/a_reg", "count": 2}
    assert critical["source_artifacts"] == ["executor_run/critical_paths_retime.json"]
    assert parsed["fanout"]["proxy_available"] is True
    assert parsed["fanout"]["max_path_recurrence_count"] == 2
    assert parsed["congestion"]["evidence_checked"] is True
    assert parsed["congestion"]["hint"] == "not_observed"


def test_transition_executor_runs_one_mock_step(tmp_path: Path):
    input_dcp = tmp_path / "input.dcp"
    input_dcp.write_text("fake\n")
    config = OptimizationConfig(policy_type="deterministic")
    inner = DcpTransitionExecutor(
        executor=FakeExecutor(),
        output_validator=OutputValidator(),
        analyzer=DeterministicAnalyzer(analysis_provider=fake_analysis_provider),
        pipeline_config=config,
        qos_weights=config.qor_weights(),
    )
    result = asyncio.run(inner.run(DcpTransitionRequest(
        input_dcp=input_dcp,
        optimizer_name="phys_opt",
        output_dir=tmp_path / "run" / "step_000_phys_opt",
        root_metrics={"initial_fmax_mhz": 400.0, "initial_wns_ns": -0.5, "initial_tns_ns": -20.0},
        parent_node_id="root",
        child_node_id="manual_000_phys_opt",
    )))
    assert result.accepted is True
    assert result.output_dcp and Path(result.output_dcp).exists()
    assert result.transition_record["optimizer_name"] == "phys_opt"
    assert (tmp_path / "run" / "step_000_phys_opt" / "dcp_transition_result.json").is_file()


def test_transition_executor_infers_qor_baseline_when_root_metrics_are_empty(tmp_path: Path):
    input_dcp = tmp_path / "input.dcp"
    input_dcp.write_text("fake\n")
    config = OptimizationConfig(policy_type="deterministic")
    inner = DcpTransitionExecutor(
        executor=FakeExecutor(),
        output_validator=OutputValidator(),
        analyzer=DeterministicAnalyzer(analysis_provider=fake_analysis_provider),
        pipeline_config=config,
        qos_weights=config.qor_weights(),
    )
    result = asyncio.run(inner.run(DcpTransitionRequest(
        input_dcp=input_dcp,
        optimizer_name="phys_opt",
        output_dir=tmp_path / "run_empty_root" / "step_000_phys_opt",
        root_metrics={},
        parent_node_id="root",
        child_node_id="manual_000_phys_opt",
    )))
    assert result.accepted is True
    assert result.metrics["qor_root_metrics_inferred"] is True
    assert result.metrics["qor_root_metrics"]["initial_fmax_mhz"] == 400.0
    assert result.qor_score.components["root_fmax_mhz"] == 400.0
    assert result.qor_score.status == "improved"
    assert result.pure_qor_score and result.pure_qor_score > 0.0


def test_transition_executor_backfills_execution_metrics_into_analysis_context(tmp_path: Path):
    input_dcp = tmp_path / "input.dcp"
    input_dcp.write_text("fake\n")
    config = OptimizationConfig(policy_type="deterministic")
    inner = DcpTransitionExecutor(
        executor=MetricsBackfillExecutor(),
        output_validator=OutputValidator(),
        analyzer=DeterministicAnalyzer(),
        pipeline_config=config,
        qos_weights=config.qor_weights(),
    )
    result = asyncio.run(inner.run(DcpTransitionRequest(
        input_dcp=input_dcp,
        optimizer_name="phys_opt",
        output_dir=tmp_path / "run_metrics" / "step_000_phys_opt",
        root_metrics={"initial_fmax_mhz": 400.0, "initial_wns_ns": -0.5, "initial_tns_ns": -20.0},
        parent_node_id="root",
        child_node_id="manual_000_phys_opt",
    )))
    ctx = result.analysis_context
    assert ctx["timing"]["target_clock"] == "clk"
    assert ctx["timing"]["final_fmax_mhz"] == 411.0
    assert ctx["final_fmax_mhz"] == 411.0
    assert ctx["timing"]["final_wns_ns"] == -0.12
    assert ctx["final_wns_ns"] == -0.12
    assert ctx["runtime"]["duration_seconds"] == 12.0
    assert ctx["runtime"]["duration_minutes"] == pytest.approx(0.2)
    assert ctx["utilization_summary"] == {"luts": 100, "ffs": 200, "dsps": 3, "brams": 4}
    assert ctx["execution_utilization_delta"] == {"luts": 5}

    backend = CachedAnalysisContextReportBackend()
    probe_ctx = SimpleNamespace(
        node=SimpleNamespace(node_id="manual_000_phys_opt", metrics={}),
        analysis_contexts={"manual_000_phys_opt": ctx},
    )
    timing_report = backend.report_timing_summary(args={}, context=probe_ctx)
    assert timing_report["timing"]["fmax_mhz"] == 411.0
    assert timing_report["timing"]["wns_ns"] == -0.12


def test_transition_executor_backfills_executor_diagnostics_into_analysis_context(tmp_path: Path):
    input_dcp = tmp_path / "input.dcp"
    input_dcp.write_text("fake\n")
    config = OptimizationConfig(policy_type="deterministic")
    inner = DcpTransitionExecutor(
        executor=DiagnosticArtifactExecutor(),
        output_validator=OutputValidator(),
        analyzer=DeterministicAnalyzer(),
        pipeline_config=config,
        qos_weights=config.qor_weights(),
    )
    result = asyncio.run(inner.run(DcpTransitionRequest(
        input_dcp=input_dcp,
        optimizer_name="phys_opt",
        output_dir=tmp_path / "run_diagnostics" / "step_000_phys_opt",
        root_metrics={"initial_fmax_mhz": 400.0, "initial_wns_ns": -0.5, "initial_tns_ns": -20.0},
        parent_node_id="root",
        child_node_id="manual_000_phys_opt",
    )))

    ctx = result.analysis_context
    critical = ctx["critical_path_features"]
    assert critical["num_paths_analyzed"] == 2
    assert critical["primitive_mix"]["bram"] == 3
    assert critical["primitive_mix"]["dsp"] == 1
    assert critical["hardblock_on_path"] is True
    assert critical["source_artifacts"] == ["executor_run/critical_paths.json"]
    assert ctx["fanout"]["proxy_available"] is True
    assert ctx["fanout"]["proxy_source"] == "critical_path_cell_recurrence"
    assert ctx["fanout"].get("max_fanout") is None
    assert ctx["fanout"].get("num_high_fanout_nets") is None
    assert ctx["congestion"].get("hint", "unknown") == "unknown"
    assert ctx["diagnostic_provenance"]["critical_path_features"]["artifacts"] == ["executor_run/critical_paths.json"]
    assert ctx["diagnostic_provenance"]["fanout"]["artifacts"] == ["executor_run/critical_paths.json"]


def test_transition_executor_diagnostic_backfill_preserves_provider_values(tmp_path: Path):
    class StaticContextAnalyzer:
        async def analyze(self, input_dcp, history, config):
            return {
                "schema_version": 2,
                "benchmark_name": Path(input_dcp).stem,
                "timing": {"initial_fmax_mhz": 400.0, "initial_wns_ns": -0.5, "initial_tns_ns": -20.0},
                "critical_path_features": {
                    "route_delay_ratio": 0.72,
                    "dominant_path_type": "routing_dominated",
                    "num_paths_analyzed": 99,
                },
                "congestion": {"report_available": False},
                "fanout": {"max_fanout": None, "num_high_fanout_nets": None, "top_nets": []},
                "utilization_summary": {},
                "candidate_flows": {"deterministic_scores": [], "selected_top3": []},
            }

    input_dcp = tmp_path / "input.dcp"
    input_dcp.write_text("fake\n")
    config = OptimizationConfig(policy_type="deterministic")
    inner = DcpTransitionExecutor(
        executor=DiagnosticArtifactExecutor(),
        output_validator=OutputValidator(),
        analyzer=StaticContextAnalyzer(),
        pipeline_config=config,
        qos_weights=config.qor_weights(),
    )
    result = asyncio.run(inner.run(DcpTransitionRequest(
        input_dcp=input_dcp,
        optimizer_name="phys_opt",
        output_dir=tmp_path / "run_preserve" / "step_000_phys_opt",
        root_metrics={"initial_fmax_mhz": 400.0, "initial_wns_ns": -0.5, "initial_tns_ns": -20.0},
        parent_node_id="root",
        child_node_id="manual_000_phys_opt",
    )))

    critical = result.analysis_context["critical_path_features"]
    assert critical["route_delay_ratio"] == 0.72
    assert critical["dominant_path_type"] == "routing_dominated"
    assert critical["num_paths_analyzed"] == 99
    assert critical["primitive_mix"]["bram"] == 3


def test_manual_transition_executor_plan_script_uses_same_transition_executor(tmp_path: Path):
    input_dcp = tmp_path / "input.dcp"
    out = tmp_path / "manual"
    cmd = [
        sys.executable,
        "scripts/run_fixed_sequence_expansion.py",
        str(input_dcp),
        "--mock",
        "--plan",
        "memory_path_opt",
        "phys_opt",
        "--out",
        str(out),
        "--no-dotenv",
    ]
    subprocess.run(cmd, cwd=Path(__file__).resolve().parents[1], check=True, text=True, capture_output=True)
    from optimizer_pipeline.core.store import load_run_view

    view = load_run_view(out)
    summary = json.loads((out / "result_manifest.json").read_text())
    assert view["attempt_count"] == 2
    assert view["attempts"][-1]["validation_status"] == "CLEAN"
    assert Path(summary["final_dcp"]).exists()
