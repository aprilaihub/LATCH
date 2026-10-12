import csv
import json
import subprocess
import sys
from pathlib import Path

from optimizer_pipeline.backends.xilinx.legacy import DCPOptimizerBase
from optimizer_pipeline.execution.vivado_utilization import format_pblock_utilization, parse_pblock_utilization_text


REPO_ROOT = Path(__file__).resolve().parents[1]


def read_json(path: Path) -> dict:
    return json.loads(path.read_text())


def test_vivado_pblock_utilization_parser_handles_commas_floats_and_clb_registers():
    report = """
| Slice LUTs       | 12,345 |
| CLB Registers    | 67,890 |
| DSPs             | 12.2   |
| Block RAM Tile   | 45.1   |
| URAM             | 3      |
"""

    parsed = format_pblock_utilization(parse_pblock_utilization_text(report))

    assert "LUTs:    12,345" in parsed
    assert "FFs:     67,890" in parsed
    assert "DSPs:        13" in parsed
    assert "BRAMs:       46" in parsed
    assert "URAMs:        3" in parsed


def test_run_summary_records_utilization_snapshots_and_delta(tmp_path: Path):
    optimizer = DCPOptimizerBase(run_dir=tmp_path)
    optimizer.initial_wns = -1.0
    optimizer.final_wns = -0.5
    optimizer.clock_period = 2.5
    optimizer.initial_utilization = {"luts": 100, "ffs": 200, "dsps": 1, "brams": 2, "urams": 0}
    optimizer.final_utilization = {"luts": 125, "ffs": 190, "dsps": 2, "brams": 2, "urams": 0}

    optimizer.save_run_summary(
        input_dcp=tmp_path / "input.dcp",
        output_dcp=tmp_path / "output.dcp",
        mode="test",
        flow="phys_opt",
    )

    summary = read_json(tmp_path / "run_summary.json")
    assert summary["initial_utilization"]["luts"] == 100
    assert summary["final_utilization"]["luts"] == 125
    assert summary["utilization_delta"]["luts"] == 25
    assert summary["utilization_delta"]["ffs"] == -10


def test_flow_matrix_dry_run_writes_manifest_and_per_flow_status(tmp_path: Path):
    input_dcp = tmp_path / "input.dcp"
    out = tmp_path / "matrix"
    cmd = [
        sys.executable,
        "scripts/run_flow_matrix.py",
        str(input_dcp),
        "--flows",
        "pblock,retime",
        "--out",
        str(out),
        "--dry-run",
    ]

    proc = subprocess.run(cmd, cwd=REPO_ROOT, text=True, capture_output=True)

    assert proc.returncode == 0, proc.stderr + proc.stdout
    manifest = read_json(out / "flow_matrix_manifest.json")
    summary = read_json(out / "flow_matrix_summary.json")
    assert manifest["summary"]["dry_run"] == 2
    assert [row["flow"] for row in summary["flows"]] == ["pblock", "retime"]
    assert (out / "flow_matrix_summary.csv").is_file()
    assert (out / "status" / "01_pblock.status.json").is_file()
    assert (out / "logs" / "02_retime.log").is_file()


def test_flow_matrix_mock_run_skips_functional_validation(tmp_path: Path):
    input_dcp = tmp_path / "input.dcp"
    input_dcp.write_text("mock input\n")
    out = tmp_path / "matrix"
    cmd = [
        sys.executable,
        "scripts/run_flow_matrix.py",
        str(input_dcp),
        "--flows",
        "pblock,retime",
        "--out",
        str(out),
        "--mock",
    ]

    proc = subprocess.run(cmd, cwd=REPO_ROOT, text=True, capture_output=True)

    assert proc.returncode == 0, proc.stderr + proc.stdout
    summary = read_json(out / "flow_matrix_summary.json")
    rows = summary["flows"]
    assert [row["status"] for row in rows] == ["passed", "passed"]
    assert [row["validation_status"] for row in rows] == ["skipped_mock", "skipped_mock"]
    assert (out / "runs" / "01_pblock" / "run.db").is_file()
    assert (out / "runs" / "02_retime" / "result_manifest.json").is_file()


def test_iterative_optimizer_dry_run_writes_three_benchmark_commands(tmp_path: Path):
    out = tmp_path / "iterative"
    inputs = [
        tmp_path / "amd_mini-isp_2025.1.dcp",
        tmp_path / "boom_soc_2025.1.dcp",
        tmp_path / "corescore_500_mod_2025.1.dcp",
    ]
    cmd = [
        sys.executable,
        "scripts/run_iterative_optimizer_workflow.py",
        *(str(path) for path in inputs),
        "--flows",
        "pblock_retime,phys_opt",
        "--out",
        str(out),
        "--dry-run",
    ]

    proc = subprocess.run(cmd, cwd=REPO_ROOT, text=True, capture_output=True)

    assert proc.returncode == 0, proc.stderr + proc.stdout
    suite = read_json(out / "suite_summary.json")
    assert suite["summary"]["benchmarks"] == 3
    assert suite["summary"]["dry_run_rows"] == 6
    for name in ["amd_mini-isp", "boom_soc", "corescore_500_mod"]:
        bench_dir = out / name
        assert (bench_dir / "round_01" / "flow_matrix" / "logs" / "01_pblock_retime.log").is_file()
        log = (bench_dir / "round_01" / "flow_matrix" / "logs" / "01_pblock_retime.log").read_text()
        assert "--plan pblock,retime" in log
        assert "--budget-runs 2" in log
        summary = read_json(bench_dir / "benchmark_summary.json")
        assert [row["optimiser"] for row in summary["rows"]] == ["pblock_retime", "phys_opt"]


def test_iterative_optimizer_mock_run_writes_final_best_and_suite_csv(tmp_path: Path):
    out = tmp_path / "iterative"
    inputs = [
        tmp_path / "amd_mini-isp_2025.1.dcp",
        tmp_path / "boom_soc_2025.1.dcp",
        tmp_path / "corescore_500_mod_2025.1.dcp",
    ]
    for path in inputs:
        path.write_text("mock input\n")
    cmd = [
        sys.executable,
        "scripts/run_iterative_optimizer_workflow.py",
        *(str(path) for path in inputs),
        "--flows",
        "pblock_retime,phys_opt",
        "--out",
        str(out),
        "--mock",
        "--skip-functional-validation",
        "--max-rounds",
        "1",
    ]

    proc = subprocess.run(cmd, cwd=REPO_ROOT, text=True, capture_output=True)

    assert proc.returncode == 0, proc.stderr + proc.stdout
    suite = read_json(out / "suite_summary.json")
    assert suite["summary"]["benchmarks"] == 3
    assert suite["summary"]["candidate_rows"] == 6
    assert suite["summary"]["selected_rows"] == 3
    with (out / "suite_summary.csv").open(newline="") as handle:
        csv_rows = list(csv.DictReader(handle))
    assert len(csv_rows) == 6
    assert any(row["optimiser"] == "pblock_retime" and row["executed_plan"] == "pblock,retime" for row in csv_rows)
    for name in ["amd_mini-isp", "boom_soc", "corescore_500_mod"]:
        bench_dir = out / name
        assert (bench_dir / "final_best.dcp").is_file()
        summary = read_json(bench_dir / "benchmark_summary.json")
        selected = [row for row in summary["rows"] if row["selected"]]
        assert len(selected) == 1
        assert selected[0]["validation_status"] == "skipped_mock"
        assert selected[0]["overall_pure_qor_score"] is not None
