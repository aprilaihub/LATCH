from __future__ import annotations

import json
import importlib.util
from pathlib import Path
import subprocess
import sys


REPO = Path(__file__).resolve().parents[2]
MANIFEST = REPO / "tests" / "real" / "golden_matrix.json"


def load_runner():
    spec = importlib.util.spec_from_file_location("real_golden_runner", REPO / "tools/run_real_golden_matrix.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_real_matrix_has_unique_cases_and_required_categories():
    data = json.loads(MANIFEST.read_text())
    cases = data["cases"]
    ids = [case["case_id"] for case in cases]
    assert len(ids) == len(set(ids))
    categories = {case["category"] for case in cases}
    assert {
        "qor_repeat",
        "fixed_parent_chain",
        "frontier_budget_branching",
        "rapidwright_backend",
        "dsp_heavy",
        "large_dsp_bram",
        "explicit_action_no_filename_detection",
        "typed_intervention_recipe",
        "manual_invalid_scratch_continuation",
        "contest_timeout_baseline_safety",
        "full_certification",
    } <= categories
    assert sum(case["category"] == "qor_repeat" for case in cases) >= 3
    assert all("--test" not in case["args"] for case in cases)
    renamed = next(case for case in cases if case["category"] == "explicit_action_no_filename_detection")
    assert renamed["renamed_input"] == "opaque_design_2025.1.dcp"
    assert renamed["args"][renamed["args"].index("--plan") + 1] == "phys_opt"
    manual = next(case for case in cases if case["category"] == "manual_invalid_scratch_continuation")
    assert manual["require_invalid_parent_continuation"] is True


def test_external_llm_credentials_require_per_case_opt_in():
    runner = load_runner()
    base = {"OPENAI_API_KEY": "secret-a", "OPENROUTER_API_KEY": "secret-b", "SAFE": "yes"}
    isolated = runner.environment_for_case(base, {})
    assert isolated == {"SAFE": "yes"}
    opted_in = runner.environment_for_case(base, {"allow_external_llm": True})
    assert opted_in == base


def test_verify_expected_locks_lineage_parallelism_and_budget(tmp_path):
    runner = load_runner()
    input_dcp = tmp_path / "input.dcp"
    output = tmp_path / "output.dcp"
    input_dcp.write_bytes(b"input")
    output.write_bytes(b"output")
    case = {
        "expected_parent_lineage": [
            {"attempt_index": 0, "output_state": "state_001", "parent_state": "state_000_root"}
        ],
        "expected_max_parallel_jobs": 2,
        "expected_budget_summary": {"scheduled_eda_runs": 2, "failed_eda_runs": 1},
    }
    trace = {
        "parent_lineage": case["expected_parent_lineage"],
        "resolved_config": {"max_parallel_jobs": 2},
        "budget_summary": {"scheduled_eda_runs": 2, "failed_eda_runs": 1},
        "output_exists": True,
        "selection": {"valid_for_publication": True},
    }
    assert runner.verify_expected(case, trace, timed_out=False, output=output, input_dcp=input_dcp) == []
    trace["resolved_config"]["max_parallel_jobs"] = 1
    trace["budget_summary"]["failed_eda_runs"] = 0
    errors = runner.verify_expected(case, trace, timed_out=False, output=output, input_dcp=input_dcp)
    assert "max parallel jobs mismatch: 1" in errors
    assert "budget summary mismatch: {'failed_eda_runs': 0}" in errors


def test_real_matrix_list_mode_needs_no_toolchain():
    result = subprocess.run(
        [sys.executable, "tools/run_real_golden_matrix.py", "--list"],
        cwd=REPO,
        check=True,
        text=True,
        stdout=subprocess.PIPE,
    )
    assert "vexriscv_rapidwright\trapidwright_backend\tvexriscv_re-place_2025.1.dcp" in result.stdout
    assert "renamed_explicit_phys_opt\texplicit_action_no_filename_detection" in result.stdout
