# Ported from a722b1e; import paths only adapted.
import json
import subprocess
import sys
from pathlib import Path


def test_analysis_only_mock_llm_closed_loop(tmp_path):
    repo = Path(__file__).resolve().parents[2]
    run_dir = tmp_path / "analysis_only"
    cmd = [
        sys.executable,
        "-m", "experiments.ablation.runner_cli",
        str(tmp_path / "input.dcp"),
        "--mock",
        "--config",
        "experiments/ablation/configs/frontier_expansion_default.json",
        "--policy",
        "deterministic",
        "--online-deep-analysis",
        "--analysis-level",
        "report_probe",
        "--analysis-tool-loop-mode",
        "mock_llm_closed_loop",
        "--analysis-report-probe-backend",
        "cached",
        "--analysis-tool-timeout-sec",
        "5",
        "--analysis-only",
        "--out",
        str(run_dir),
        "--max-runs",
        "0",
        "--max-depth",
        "1",
        "--jobs",
        "1",
        "--batch-size",
        "1",
        "--no-dotenv",
    ]
    proc = subprocess.run(cmd, cwd=repo, text=True, capture_output=True)
    assert proc.returncode == 0, proc.stderr + proc.stdout
    summary = json.loads((run_dir / "pipeline_summary.json").read_text())
    agent = summary["analysis"]
    assert agent["enabled"] is True
    assert agent["sessions"] == 1
    assert agent["tool_calls"] >= 3
    assert agent["loop_decisions"] >= 2
    assert agent["candidate_prior_count"] > 0
    assert summary["eda_runs"] == 0
    assert (run_dir / "analysis" / "analysis_runs.jsonl").is_file()
    node = json.loads((run_dir / "analysis" / "analysis_runs.jsonl").read_text().splitlines()[0])
    assert node["tool_loop_mode"] == "mock_llm_closed_loop"
    assert node["status"] in {"COMPLETED", "STOPPED", "TIMEOUT"}
