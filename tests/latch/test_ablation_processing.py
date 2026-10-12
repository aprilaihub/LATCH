import csv
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from experiments.ablation.processing.run import (
    ProcessingError,
    SCRIPT_ROOT,
    STAGES,
    STAGE_OUTPUTS,
    load_config,
    run_processing,
    validate_inputs,
)

try:
    import scipy.stats  # noqa: F401
except ImportError:
    _HAS_SCIPY = False
else:
    _HAS_SCIPY = True


def _write_jsonl(path: Path, row: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(row) + "\n", encoding="utf-8")


def _input_config(tmp_path: Path) -> tuple[Path, Path, Path]:
    raw_root = tmp_path / "synthetic-raw" / "ablation_study"
    for level in ("S1", "S2", "S3"):
        run = raw_root / "bench-a" / level / "luna" / "repeat_1"
        run.mkdir(parents=True)
        (run / "pipeline_summary.json").write_text("{}\n", encoding="utf-8")
        _write_jsonl(run / "analysis" / "analysis_events.jsonl", {"tokens": 10, "cost_usd": 0.001})
        _write_jsonl(
            run / "analysis" / "node_diagnoses.jsonl",
            {"candidate_priorities": [{"flow": "place_design", "priority_score": 1.0}]},
        )
        _write_jsonl(run / "analysis" / "loop_decisions.jsonl", {"decision_index": 0})
        _write_jsonl(run / "policy_candidates.jsonl", {"candidate_id": "c1"})

    s0_root = tmp_path / "synthetic-raw" / "s0-sweep"
    s0_run = s0_root / "bench-a"
    s0_run.mkdir(parents=True)
    (s0_run / "pipeline_summary.json").write_text("{}\n", encoding="utf-8")

    config_path = tmp_path / "processing.json"
    config_path.write_text(
        json.dumps(
            {
                "participant": "matt",
                "raw_run_root": str(raw_root),
                "s0_sweep_root": str(s0_root),
                "s0_extra_roots": {},
            }
        ),
        encoding="utf-8",
    )
    return config_path, raw_root, s0_root


def _fake_executor(calls: list[str]):
    def execute(script: Path, env: dict[str, str]) -> None:
        calls.append(script.name)
        data_dir = Path(env["PAPER_ANALYSIS_ROOT"]) / "data"
        data_dir.mkdir(parents=True, exist_ok=True)
        stage = script.stem
        for name in STAGE_OUTPUTS[stage]:
            path = data_dir / name
            if name in {"runs.csv", "runs_enriched.csv"}:
                rows = [
                    {
                        "source": "matt" if level != "S0" else "S0sweep",
                        "participant": "matt" if level != "S0" else "sweep",
                        "benchmark": "bench-a",
                        "level": level,
                        "model": "luna" if level != "S0" else "none",
                        "repeat": "1",
                        "cost_usd": "0.01" if level != "S0" else "0",
                        "best_fmax_mhz": "110" if level != "S0" else "100",
                    }
                    for level in ("S0", "S1", "S2", "S3")
                ]
            else:
                rows = [{"fixture": "synthetic"}]
            with path.open("w", newline="", encoding="utf-8") as stream:
                writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
                writer.writeheader()
                writer.writerows(rows)

    return execute


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value) + "\n", encoding="utf-8")


def _write_synthetic_run(run: Path, participant: str, level: str, model: str = "luna") -> None:
    run.mkdir(parents=True)
    is_baseline = level == "S0"
    cost = {"S1": 0.01, "S2": 0.02, "S3": 0.03}.get(level, 0.0)
    _write_json(
        run / "pipeline_summary.json",
        {
            "config": {"model": model, "knowledge_mode": "off", "allowed_flows": ["place_design"]},
            "root_metrics": {
                "initial_fmax_mhz": 100.0,
                "initial_wns_ns": -1.0,
                "initial_tns_ns": -5.0,
                "failing_endpoints": 2,
                "clock_period_ns": 10.0,
                "target_clock": "clk",
            },
            "best_pure_qor": {
                "status": "valid",
                "components": {
                    "node_fmax_mhz": 100.0 if is_baseline else 110.0,
                    "fmax_delta_mhz": 0.0 if is_baseline else 10.0,
                    "fmax_improvement_ratio": 0.0 if is_baseline else 0.1,
                    "node_wns_ns": -1.0 if is_baseline else 0.0,
                    "wns_improvement_ns": 0.0 if is_baseline else 1.0,
                },
            },
            "phase_runtime_breakdown": {
                "analysis_seconds": 1.0,
                "diagnostic_seconds": 1.0,
                "diagnostic_calls": 1,
                "execution_productive_seconds": 2.0,
                "execution_failed_seconds": 0.0,
                "verification_seconds": 1.0,
                "partition_valid": True,
                "phases_run_concurrently": False,
            },
            "state": {
                "status": "complete",
                "stop_reason": "COMPLETE",
                "equivalence_gate": {"status": "passed", "passed": True, "phase1_passed": True},
            },
            "analysis": {"sessions": 1, "tool_calls": 1, "analysis_plan_count": 1},
            "system_conditions": {"samples": [], "end": {}},
            "validation_counts": {"CLEAN": 2, "WARNING": 0, "INVALID": 0},
            "risk_budget": {"invalid_rate_so_far": 0.0, "warning_rate_so_far": 0.0},
            "repo_revision": {"branch": "synthetic", "commit": "fixture"},
            "best_flow_sequence": [] if is_baseline else ["place_design"],
            "best_node_id": "root" if is_baseline else "node_1",
            "best_pure_qor_score": 1.0,
            "best_validation_status": "CLEAN",
            "nodes_total": 2,
            "eda_runs": 1,
            "scheduled_runs": 1,
            "nodes_completed": 1,
            "nodes_failed": 0,
            "nodes_pruned": 0,
            "total_wall_runtime_seconds": 60.0,
            "analysis_tokens": 10,
            "analysis_llm_calls": 1,
            "analysis_total_cost_usd": cost,
            "cost_source": "synthetic",
        },
    )
    _write_json(
        run / "graph_stats.json",
        {"best_depth": 1, "max_depth_reached": 1, "max_children_per_node": 1},
    )
    _write_json(
        run / "results_summary.json",
        [
            {
                "node_id": "root",
                "depth": 0,
                "flow": "root",
                "final_fmax_mhz": 100.0,
                "final_wns_ns": -1.0,
                "final_tns_ns": -5.0,
                "validation_status": "CLEAN",
                "validation_valid": True,
                "status": "complete",
                "qor_status": "valid",
                "pure_qor_score": 1.0,
            },
            {
                "node_id": "node_1",
                "depth": 1,
                "flow": "place_design",
                "flow_sequence": ["place_design"],
                "parent_id": "root",
                "final_fmax_mhz": 100.0 if is_baseline else 110.0,
                "final_wns_ns": -1.0 if is_baseline else 0.0,
                "final_tns_ns": -5.0 if is_baseline else 0.0,
                "node_runtime_seconds": 2.0,
                "path_runtime_seconds": 2.0,
                "validation_status": "CLEAN",
                "validation_valid": True,
                "validation_error_count": 0,
                "validation_warning_count": 0,
                "status": "complete",
                "qor_status": "valid",
                "pure_qor_score": 1.0,
            },
        ],
    )
    _write_jsonl(
        run / "analysis" / "loop_decisions.jsonl",
        {
            "decision_index": 0,
            "node_id": "node_1",
            "estimated_prompt_tokens": 2,
            "estimated_total_tokens": 10,
        },
    )
    _write_jsonl(
        run / "analysis" / "node_diagnoses.jsonl",
        {
            "node_id": "root",
            "bottleneck_class": "timing",
            "bottleneck_confidence": 0.9,
            "analysis_type": "synthetic",
            "candidate_priorities": [
                {
                    "flow": "place_design",
                    "priority_score": 1.0,
                    "analysis_session_id": f"{participant}-{level}",
                    "evidence_basis": "synthetic",
                }
            ],
        },
    )
    _write_jsonl(
        run / "policy_candidates.jsonl",
        {
            "candidate_id": "c1",
            "candidate_flow": "place_design",
            "candidate_rank": 1,
            "clipped_priority": 1.0,
            "approximate_base_priority_without_analysis": 0.5,
            "analysis_prior_applied": True,
            "analysis_priority_delta": 0.5,
            "analysis_risk_delta": 0.0,
            "analysis_confidence": 0.9,
            "confidence": 0.9,
            "executed": True,
            "accepted_by_transition_executor": True,
            "final_status": "accepted",
            "outcome_node_id": "node_1",
            "outcome_status": "complete",
            "outcome_qor_status": "valid",
            "outcome_validation_status": "CLEAN",
            "outcome_pure_qor_score": 1.0,
            "decision_valid": True,
        },
    )
    if participant == "matt" and not is_baseline:
        _write_jsonl(
            run / "analysis" / "analysis_events.jsonl",
            {"tokens": 10, "cost_usd": cost, "llm_calls": 1, "session_id": f"matt-{level}"},
        )


def _multi_participant_config(tmp_path: Path) -> tuple[Path, dict[str, Path]]:
    participant_roots: dict[str, Path] = {}
    benchmarks = {"cristian": "vexriscv_re-place", "matt": "boom_soc", "shu": "logicnets_jscl"}
    for participant, benchmark in benchmarks.items():
        root = tmp_path / "raw" / participant / "ablation_study"
        participant_roots[participant] = root
        for level in ("S1", "S2", "S3"):
            for model in ("luna", "sonnet5", "gemini36flash"):
                _write_synthetic_run(
                    root / benchmark / level / model / "repeat_1", participant, level, model
                )
    s0_root = tmp_path / "raw" / "s0-sweep"
    for participant, benchmark in benchmarks.items():
        _write_synthetic_run(s0_root / benchmark, "sweep", "S0")
    config_path = tmp_path / "multi-participant.json"
    config_path.write_text(
        json.dumps(
            {
                "participant_roots": {name: str(root) for name, root in participant_roots.items()},
                "s0_sweep_root": str(s0_root),
                "s0_extra_roots": {},
            }
        ),
        encoding="utf-8",
    )
    return config_path, participant_roots


class AblationProcessingTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._temporary.name)

    def tearDown(self) -> None:
        self._temporary.cleanup()

    def test_runs_only_the_ordered_matt_prefix_and_reports_bounded_scope(self) -> None:
        config_path, _, _ = _input_config(self.tmp_path)
        output_parent = self.tmp_path / "results"
        output_parent.mkdir()
        output_dir = output_parent / "fresh-run"
        reference_dir = self.tmp_path / "reference"
        reference_dir.mkdir()
        reference_path = reference_dir / "runs_enriched.csv"
        reference_rows = [
            {
                "source": "matt" if level != "S0" else "S0sweep",
                "participant": "matt" if level != "S0" else "sweep",
                "benchmark": "bench-a",
                "level": level,
                "model": "luna" if level != "S0" else "none",
                "repeat": "1",
                "cost_usd": "0.005" if level == "S1" else "0.01" if level != "S0" else "0",
                "best_fmax_mhz": "110" if level != "S0" else "100",
            }
            for level in ("S0", "S1", "S2", "S3")
        ]
        with reference_path.open("w", newline="", encoding="utf-8") as stream:
            writer = csv.DictWriter(stream, fieldnames=list(reference_rows[0]))
            writer.writeheader()
            writer.writerows(reference_rows)
        calls: list[str] = []

        report = run_processing(
            config_path,
            output_dir,
            executor=_fake_executor(calls),
            reference_processed=reference_dir,
        )

        expected = [script for _, script in STAGES]
        self.assertEqual(calls, expected)
        self.assertEqual(report["completed_stages"], [stage for stage, _ in STAGES])
        self.assertEqual(report["participants_processed"], ["matt"])
        self.assertEqual(report["mechanism_scan_participant"], "matt")
        self.assertEqual(report["v2_followthrough_participants"], ["matt"])
        self.assertEqual(report["v3_cost_scan_participant"], "matt")
        self.assertFalse(report["full_historical_reproduction"])
        self.assertEqual(
            report["optional_outputs_absent"],
            ["data/setaside.csv (extract.py reported no set-aside rows)"],
        )
        self.assertEqual(report["stage_summaries"]["extract"]["row_counts"]["runs.csv"], 4)
        self.assertEqual(report["key_totals"]["num_runs"], 4)
        comparison = report["reference_comparison"]["tables"]["runs_enriched.csv"]
        self.assertEqual(comparison["row_count_delta"], 0)
        self.assertEqual(comparison["numeric_columns"]["cost_usd"]["different_value_count"], 1)
        self.assertEqual(report["reference_comparison"]["mode"], "descriptive_comparison_only_no_equality_gate")
        saved = json.loads((output_dir / "processing_report.json").read_text(encoding="utf-8"))
        self.assertEqual(saved["status"], "completed_bounded_processing")

    def test_missing_required_raw_level_stops_before_creating_output(self) -> None:
        config_path, raw_root, _ = _input_config(self.tmp_path)
        (raw_root / "bench-a" / "S3" / "luna" / "repeat_1" / "pipeline_summary.json").unlink()
        output_parent = self.tmp_path / "results"
        output_parent.mkdir()
        output_dir = output_parent / "must-not-be-created"
        calls: list[str] = []

        with self.assertRaisesRegex(ProcessingError, "missing: S3"):
            run_processing(config_path, output_dir, executor=_fake_executor(calls))

        self.assertEqual(calls, [])
        self.assertFalse(output_dir.exists())

    def test_analysis_artifacts_are_reported_not_used_as_preflight_gates(self) -> None:
        config_path, raw_root, _ = _input_config(self.tmp_path)
        for run in raw_root.glob("*/*/*/repeat_*"):
            for name in (
                "analysis_events.jsonl",
                "node_diagnoses.jsonl",
                "loop_decisions.jsonl",
                "policy_candidates.jsonl",
            ):
                path = run / "analysis" / name if name != "policy_candidates.jsonl" else run / name
                if path.exists():
                    path.unlink()

        input_report = validate_inputs(load_config(config_path))
        missing = input_report["optional_raw_artifact_counts_by_participant"]["matt"]
        self.assertEqual(missing["runs_without_analysis_events"], 3)
        self.assertEqual(missing["runs_without_node_diagnoses"], 3)
        self.assertEqual(missing["runs_without_loop_decisions"], 3)
        self.assertEqual(missing["runs_without_policy_candidates"], 3)

    def test_output_must_be_new_and_outside_raw_roots(self) -> None:
        config_path, raw_root, _ = _input_config(self.tmp_path)
        existing = self.tmp_path / "already-there"
        existing.mkdir()
        with self.assertRaisesRegex(ProcessingError, "new directory"):
            run_processing(config_path, existing)

        with self.assertRaisesRegex(ProcessingError, "inside a raw input tree"):
            run_processing(config_path, raw_root / "processing-output")

    def test_copied_scripts_have_no_personal_path_defaults(self) -> None:
        for script in SCRIPT_ROOT.glob("*.py"):
            source = script.read_text(encoding="utf-8")
            self.assertNotIn("/home/", source, script.name)
            self.assertNotIn("~/", source, script.name)

    def test_actual_ordered_scripts_process_a_tiny_three_participant_fixture(self) -> None:
        config_path, _ = _multi_participant_config(self.tmp_path)
        output_parent = self.tmp_path / "actual-results"
        output_parent.mkdir()
        output_dir = output_parent / "full-mini-prefix"
        test_support = self.tmp_path / "test-support"
        if not _HAS_SCIPY:
            scipy_package = test_support / "scipy"
            scipy_package.mkdir(parents=True)
            (scipy_package / "__init__.py").write_text("from . import stats\n", encoding="utf-8")
            (scipy_package / "stats.py").write_text(
                "class _Result:\n    pvalue = 1.0\n\n"
                "def wilcoxon(values):\n    return _Result()\n",
                encoding="utf-8",
            )

        def quiet_executor(script: Path, env: dict[str, str]) -> None:
            script_env = env.copy()
            if not _HAS_SCIPY:
                old_pythonpath = script_env.get("PYTHONPATH", "")
                script_env["PYTHONPATH"] = str(test_support) + (os.pathsep + old_pythonpath if old_pythonpath else "")
            completed = subprocess.run(
                [sys.executable, str(script)],
                env=script_env,
                capture_output=True,
                text=True,
                check=False,
            )
            if completed.returncode:
                raise RuntimeError(
                    f"{script.name} exited {completed.returncode}\n"
                    f"stdout:\n{completed.stdout[-4000:]}\n"
                    f"stderr:\n{completed.stderr[-4000:]}"
                )

        report = run_processing(config_path, output_dir, executor=quiet_executor)
        with (output_dir / "data" / "runs.csv").open(newline="", encoding="utf-8") as stream:
            extracted = list(csv.DictReader(stream))
        self.assertEqual({row["participant"] for row in extracted if row["level"] != "S0"},
                         {"cristian", "matt", "shu"})
        self.assertEqual(len(extracted), 30)
        self.assertEqual(report["participants_not_configured"], [])
        self.assertEqual(report["mechanism_scan_participant"], "matt")
        self.assertEqual(report["v2_followthrough_participants"], ["cristian", "matt", "shu"])
        self.assertEqual(report["v3_cost_scan_participant"], "matt")
        self.assertEqual(report["completed_stages"], [stage for stage, _ in STAGES])
        self.assertEqual(report["stage_summaries"]["prep"]["key_totals"]["num_runs"], 30)
        self.assertEqual(report["key_totals"]["final_fmax_mhz"]["source_column"], "best_fmax_mhz")
        with (output_dir / "data" / "analysis_sessions.csv").open(newline="", encoding="utf-8") as stream:
            analysis_sessions = list(csv.DictReader(stream))
        benchmark_by_participant = {
            row["participant"]: row["benchmark"]
            for row in extracted
            if row["level"] != "S0"
        }
        self.assertEqual(len(analysis_sessions), 27)
        self.assertEqual(
            {row["benchmark"] for row in analysis_sessions},
            set(benchmark_by_participant.values()),
        )
        self.assertEqual(
            {benchmark: sum(row["benchmark"] == benchmark for row in analysis_sessions)
             for benchmark in benchmark_by_participant.values()},
            {benchmark: 9 for benchmark in benchmark_by_participant.values()},
        )
        self.assertNotIn("participant", analysis_sessions[0])


if __name__ == "__main__":
    unittest.main()
