import subprocess
import sys
from pathlib import Path

from optimizer_pipeline.core.store import load_run_view


def test_analysis_only_mock_llm_closed_loop(tmp_path):
    repo = Path(__file__).resolve().parents[1]
    run_dir = tmp_path / "analysis_only"
    cmd = [
        sys.executable,
        "scripts/run_frontier_expansion.py",
        str(tmp_path / "input.dcp"),
        "--mock",
        "--config",
        "configs/online_deep_analysis_mock.json",
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
    view = load_run_view(run_dir)
    assert view["attempt_count"] == 0
    assert view["budget"]["reserved"] == 0
    assert len(view["analysis_sessions"]) == 1
    node = view["analysis_sessions"][0]
    assert len(node["tool_calls"]) >= 3
    assert len(node["loop_decisions"]) >= 2
    assert len(node["candidate_priors"]) > 0
    assert node["tool_loop_mode"] == "mock_llm_closed_loop"
    assert not list((run_dir / "analysis").glob("*.json*"))
