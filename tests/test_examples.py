import os
import subprocess
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]

EXAMPLE_SCRIPTS = [
    "examples/run_frontier_deterministic_no_llm.sh",
    "examples/run_single_path_one_openai_analysis.sh",
    "examples/run_fixed_pblock_retime_no_llm.sh",
    "examples/run_custom_pblock_retime_frontier.sh",
    "examples/run_frontier_single_path3_analysis_frontier.sh",
    "examples/run_flow_matrix_compare.sh",
]


def run_example(script: str, *, env: dict[str, str], cwd: Path = REPO_ROOT) -> subprocess.CompletedProcess[str]:
    merged = os.environ.copy()
    merged.update(env)
    return subprocess.run([script], cwd=cwd, env=merged, text=True, capture_output=True)


def test_example_shell_syntax():
    for script in ["examples/_example_common.sh", *EXAMPLE_SCRIPTS]:
        proc = subprocess.run(["bash", "-n", script], cwd=REPO_ROOT, text=True, capture_output=True)
        assert proc.returncode == 0, proc.stderr + proc.stdout


def test_example_dry_runs_print_expected_commands():
    env = {"DRY_RUN": "1", "NO_DOTENV": "1"}
    expected = {
        "examples/run_frontier_deterministic_no_llm.sh": ["--pipeline frontier", "--policy deterministic", "--llm-provider none"],
        "examples/run_single_path_one_openai_analysis.sh": [
            "--run-mode research-dev",
            "--pipeline analysis_single_path",
            "--analysis deep_llm",
            "--llm-provider openai",
            "--single-path-depth 1",
            "--budget-runs 1",
        ],
        "examples/run_fixed_pblock_retime_no_llm.sh": ["--pipeline fixed", "--plan pblock,retime", "--llm-provider none"],
        "examples/run_custom_pblock_retime_frontier.sh": [
            "--pipeline custom",
            "--custom-pipeline examples.custom_pipelines:pblock_retime_then_frontier_demo",
            "--llm-provider none",
        ],
        "examples/run_frontier_single_path3_analysis_frontier.sh": [
            "--run-mode research-dev",
            "--pipeline custom",
            "--custom-pipeline examples.custom_pipelines:frontier_single_path3_analysis_frontier_demo",
            "--analysis deep_llm",
            "--llm-provider openai",
            "--budget-runs 10",
            "--parallelism 2",
        ],
        "examples/run_flow_matrix_compare.sh": ["scripts/run_flow_matrix.py", "--flows pblock,retime,auto_pblock,phys_opt"],
    }

    for script, fragments in expected.items():
        proc = run_example(script, env=env)
        output = proc.stdout + proc.stderr
        normalized_output = output.replace("\\,", ",")
        assert proc.returncode == 0, output
        assert "dry_run:      command not executed" in output
        for fragment in fragments:
            assert fragment in normalized_output


def test_example_mock_smokes_write_expected_artifacts(tmp_path: Path):
    for script in EXAMPLE_SCRIPTS:
        name = Path(script).stem
        input_dcp = tmp_path / name / "input.dcp"
        out_dir = tmp_path / name / "run"
        input_dcp.parent.mkdir(parents=True, exist_ok=True)
        input_dcp.write_text("mock input\n")
        proc = run_example(
            script,
            env={
                "MOCK": "1",
                "NO_DOTENV": "1",
                "DCP": str(input_dcp),
                "OUT": str(out_dir),
            },
        )
        output = proc.stdout + proc.stderr
        assert proc.returncode == 0, output
        if script.endswith("run_flow_matrix_compare.sh"):
            assert (out_dir / "flow_matrix_manifest.json").is_file()
            assert (out_dir / "flow_matrix_summary.json").is_file()
            assert (out_dir / "flow_matrix_summary.csv").is_file()
        else:
            assert (out_dir / "result_manifest.json").is_file()
            assert (out_dir / "run.db").is_file()
