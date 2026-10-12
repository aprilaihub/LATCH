import json
from pathlib import Path

from experiments.tuning.collector import aggregate_rows, collect_run_rows
from experiments.tuning.matrix import expand_experiment_matrix
from experiments.tuning.preflight import preflight_experiment
from experiments.tuning.report import render_comparison_report
from experiments.tuning.runner import _strict_run_failure_reason, build_frontier_command
from experiments.tuning.schema import ExperimentSpec, load_experiment_spec
from experiments.llm.runtime.decision_events import write_llm_decision_events


def test_experiment_matrix_expands_semantic_variant_and_trial_dirs(tmp_path: Path):
    spec = ExperimentSpec.from_dict({
        "experiment_id": "frontier_llm_tuning_smoke",
        "input_dcps": ["examples/mock_input.dcp"],
        "base": {"pipeline": "frontier", "policy": "analysis_priority", "mock": True, "parallelism": 1},
        "matrix": {
            "provider": ["mock"],
            "model": ["mock_good", "mock_noisy"],
            "prompt_id": ["frontier_expansion.baseline"],
            "temperature": [0.0, 0.3],
        },
        "repeats": 2,
    })

    runs = expand_experiment_matrix(spec, repo_root=tmp_path)

    assert len(runs) == 8
    assert {run.trial_slug for run in runs} == {"primary", "secondary"}
    assert any("temperature-cold" in run.variant_slug for run in runs)
    assert any("temperature-mild" in run.variant_slug for run in runs)
    assert all("run_0001" not in str(run.run_dir) for run in runs)
    assert all("patch" not in run.variant_slug for run in runs)


def test_experiment_config_yaml_subset_loads_default_smoke_config():
    config_path = Path(__file__).resolve().parents[1] / "configs" / "experiments" / "frontier_llm_tuning_smoke.yaml"
    spec = load_experiment_spec(config_path)

    assert spec.experiment_id == "frontier_llm_tuning_smoke"
    assert spec.matrix["prompt_id"] == ["frontier_expansion.baseline"]
    assert "mock_malformed_json" in spec.matrix["model"]
    assert spec.repeats == 2


def test_experiment_config_schema_accepts_strict_experiment_flag():
    spec = ExperimentSpec.from_dict({
        "experiment_id": "strict_smoke",
        "input_dcps": ["examples/mock_input.dcp"],
        "strict_experiment": True,
    })

    assert spec.strict_experiment is True


def test_experiment_runner_command_carries_llm_tuning_flags(tmp_path: Path):
    spec = ExperimentSpec.from_dict({
        "experiment_id": "frontier_llm_tuning_smoke",
        "input_dcps": ["examples/mock_input.dcp"],
        "base": {
            "pipeline": "frontier",
            "policy": "analysis_priority",
            "mock": True,
            "parallelism": 1,
            "budget_runs": 3,
            "batch_size": 1,
            "retrieval_index": "configs/retrieval_frontier_default.json",
            "reliability_stats": "configs/reliability_frontier_default.json",
        },
        "matrix": {
            "provider": ["mock"],
            "model": ["mock_good"],
            "prompt_id": ["frontier_expansion.baseline"],
            "temperature": [0.0],
            "llm_context": ["retrieval"],
            "retrieval_k": [3],
        },
        "repeats": 1,
    })
    run = expand_experiment_matrix(spec, repo_root=tmp_path)[0]

    command = build_frontier_command(run, repo_root=tmp_path, python_exe="python")

    assert "--mock" in command
    assert command[command.index("--prompt-id") + 1] == "frontier_expansion.baseline"
    assert command[command.index("--temperature") + 1] == "0.0"
    assert command[command.index("--provider") + 1] == "mock"
    assert command[command.index("--model") + 1] == "mock_good"
    assert command[command.index("--retrieval-index") + 1] == "configs/retrieval_frontier_default.json"
    assert command[command.index("--reliability-stats") + 1] == "configs/reliability_frontier_default.json"
    assert command[command.index("--llm-context") + 1] == "retrieval"
    assert command[command.index("--retrieval-k") + 1] == "3"


def test_experiment_preflight_accepts_default_mock_smoke_config(tmp_path: Path):
    repo_root = Path(__file__).resolve().parents[1]
    spec = load_experiment_spec(repo_root / "configs" / "experiments" / "frontier_llm_tuning_smoke.yaml")

    report = preflight_experiment(spec, repo_root=repo_root, output_root=tmp_path, environ={})

    assert report.ok
    assert report.run_count > 0


def test_experiment_preflight_ignores_removed_policy_prompt_and_checks_provider_pricing(tmp_path: Path):
    repo_root = Path(__file__).resolve().parents[1]
    spec = ExperimentSpec.from_dict({
        "experiment_id": "bad_real_provider",
        "input_dcps": ["examples/mock_input.dcp"],
        "base": {"pipeline": "frontier", "policy": "analysis_priority", "mock": True, "parallelism": 1},
        "matrix": {
            "provider": ["openai"],
            "model": ["gpt-test"],
            "prompt_id": ["frontier_expansion.missing"],
            "pricing_profile": ["missing-price"],
        },
    })

    report = preflight_experiment(spec, repo_root=repo_root, output_root=tmp_path, environ={})
    codes = {issue.code for issue in report.errors}

    assert not report.ok
    assert {"missing_provider_key", "unknown_pricing_profile"} <= codes
    assert "missing_prompt" not in codes


def test_experiment_preflight_requires_openai_compatible_base_url(tmp_path: Path):
    repo_root = Path(__file__).resolve().parents[1]
    spec = ExperimentSpec.from_dict({
        "experiment_id": "bad_openai_compatible",
        "input_dcps": ["examples/mock_input.dcp"],
        "base": {"pipeline": "frontier", "policy": "analysis_priority", "mock": True, "parallelism": 1},
        "matrix": {
            "provider": ["openai_compatible"],
            "model": ["custom-model"],
            "prompt_id": ["frontier_expansion.baseline"],
        },
    })

    report = preflight_experiment(spec, repo_root=repo_root, output_root=tmp_path, environ={"OPENAI_API_KEY": "test"})

    assert "missing_provider_base_url" in {issue.code for issue in report.errors}


def test_experiment_preflight_checks_knowledge_base_paths(tmp_path: Path):
    repo_root = Path(__file__).resolve().parents[1]
    spec = ExperimentSpec.from_dict({
        "experiment_id": "bad_kb_paths",
        "input_dcps": ["examples/mock_input.dcp"],
        "base": {
            "pipeline": "frontier",
            "policy": "analysis_priority",
            "mock": True,
            "parallelism": 1,
            "retrieval_index": "configs/does_not_exist_retrieval.json",
            "reliability_stats": "configs/does_not_exist_reliability.json",
        },
        "matrix": {
            "provider": ["mock"],
            "model": ["mock_good"],
            "prompt_id": ["frontier_expansion.baseline"],
        },
    })

    report = preflight_experiment(spec, repo_root=repo_root, output_root=tmp_path, environ={})

    assert {"missing_retrieval_index", "missing_reliability_stats"} <= {issue.code for issue in report.errors}


def test_experiment_preflight_rejects_variant_matrix_mismatch(tmp_path: Path):
    repo_root = Path(__file__).resolve().parents[1]
    spec = ExperimentSpec.from_dict({
        "experiment_id": "bad_variant_matrix",
        "input_dcps": ["examples/mock_input.dcp"],
        "base": {"pipeline": "frontier", "policy": "analysis_priority", "mock": True, "parallelism": 1},
        "variants": [{"name": "openai_variant", "provider": "openai", "model": "gpt-test"}],
        "matrix": {
            "provider": ["deepseek"],
            "model": ["gpt-test"],
            "prompt_id": ["frontier_expansion.baseline"],
        },
    })

    report = preflight_experiment(spec, repo_root=repo_root, output_root=tmp_path, environ={"OPENAI_API_KEY": "test"})

    assert "variant_matrix_mismatch" in {issue.code for issue in report.errors}


def test_experiment_variants_expand_legal_provider_model_pairs(tmp_path: Path):
    spec = ExperimentSpec.from_dict({
        "experiment_id": "frontier_provider_variant_smoke",
        "input_dcps": ["examples/mock_input.dcp"],
        "base": {
            "pipeline": "frontier",
            "policy": "analysis_priority",
            "mock": True,
            "parallelism": 1,
            "budget_runs": 1,
        },
        "variants": [
            {"name": "openai_gpt_baseline", "provider": "openai", "model": "gpt-test"},
            {"name": "deepseek_chat", "provider": "deepseek", "model": "deepseek-chat", "pricing_profile": "deepseek:deepseek-chat"},
        ],
        "matrix": {
            "provider": ["openai", "deepseek"],
            "model": ["gpt-test", "deepseek-chat"],
            "prompt_id": ["frontier_expansion.baseline"],
            "temperature": [0.0, 0.3],
        },
        "repeats": 1,
    })

    runs = expand_experiment_matrix(spec, repo_root=tmp_path)
    pairs = {(run.factors["provider"], run.factors["model"]) for run in runs}

    assert len(runs) == 4
    assert pairs == {("openai", "gpt-test"), ("deepseek", "deepseek-chat")}
    assert all("openai-gpt-baseline" in run.variant_slug or "deepseek-chat" in run.variant_slug for run in runs)
    assert all("__prompt-baseline__" in run.variant_slug for run in runs)


def test_variant_command_uses_variant_provider_model_and_sweep_fields(tmp_path: Path):
    spec = ExperimentSpec.from_dict({
        "experiment_id": "frontier_provider_variant_smoke",
        "input_dcps": ["examples/mock_input.dcp"],
        "base": {"pipeline": "frontier", "policy": "analysis_priority", "mock": True, "parallelism": 1, "budget_runs": 1},
        "variants": [
            {"name": "deepseek_chat", "provider": "deepseek", "model": "deepseek-chat", "pricing_profile": "deepseek:deepseek-chat"},
        ],
        "matrix": {"prompt_id": ["frontier_expansion.baseline"], "temperature": [0.0]},
        "repeats": 1,
    })
    run = expand_experiment_matrix(spec, repo_root=tmp_path)[0]

    command = build_frontier_command(run, repo_root=tmp_path, python_exe="python")

    assert command[command.index("--provider") + 1] == "deepseek"
    assert command[command.index("--model") + 1] == "deepseek-chat"
    assert command[command.index("--pricing-profile") + 1] == "deepseek:deepseek-chat"
    assert command[command.index("--prompt-id") + 1] == "frontier_expansion.baseline"


def test_strict_experiment_marks_fallback_artifact_as_failure(tmp_path: Path):
    spec = ExperimentSpec.from_dict({
        "experiment_id": "strict_artifact",
        "input_dcps": ["examples/mock_input.dcp"],
        "base": {"pipeline": "frontier", "policy": "analysis_priority", "mock": True, "parallelism": 1},
        "matrix": {
            "provider": ["mock"],
            "model": ["mock_good"],
            "prompt_id": ["frontier_expansion.baseline"],
        },
    })
    run = expand_experiment_matrix(spec, repo_root=tmp_path)[0]
    run.run_dir.mkdir(parents=True)
    (run.run_dir / "pipeline_summary.json").write_text(json.dumps({
        "success": True,
        "llm_provider": "mock",
        "llm_model": "mock_good",
        "llm_prompt_id": "frontier_expansion.baseline",
    }))
    (run.run_dir / "policy_events.json").write_text(json.dumps([
        {"decision": {"fallback_used": True}, "validation": {"valid": True}}
    ]))

    assert _strict_run_failure_reason(run) == "strict experiment: 1 policy fallback event(s) occurred"


def test_strict_experiment_marks_provider_error_artifact_as_failure(tmp_path: Path):
    spec = ExperimentSpec.from_dict({
        "experiment_id": "strict_provider_error",
        "input_dcps": ["examples/mock_input.dcp"],
        "base": {"pipeline": "frontier", "policy": "analysis_priority", "mock": True, "parallelism": 1},
        "matrix": {
            "provider": ["openai"],
            "model": ["gpt-test"],
            "prompt_id": ["frontier_expansion.baseline"],
        },
    })
    run = expand_experiment_matrix(spec, repo_root=tmp_path)[0]
    run.run_dir.mkdir(parents=True)
    (run.run_dir / "pipeline_summary.json").write_text(json.dumps({
        "success": True,
        "llm_provider": "openai",
        "llm_model": "gpt-test",
        "llm_prompt_id": "frontier_expansion.baseline",
    }))
    (run.run_dir / "policy_events.json").write_text(json.dumps([
        {
            "decision": {"fallback_used": False},
            "validation": {"valid": False},
        }
    ]))
    write_llm_decision_events(run.run_dir, [
        {
            "schema_version": "llm_decision_attempt.v1",
            "task": "frontier_expansion",
            "run_id": run.run_dir.name,
            "event_index": 0,
            "attempt_index": 1,
            "provider": "openai",
            "model": "gpt-test",
            "prompt_id": "frontier_expansion.baseline",
            "json_parse_success": False,
            "domain_validation_success": False,
            "validation_errors": ["Provider call failed: no api key"],
            "fallback_triggered_after_attempt": False,
        }
    ])

    reason = _strict_run_failure_reason(run)

    assert reason is not None
    assert reason.startswith("strict experiment: provider call unavailable")


def test_real_provider_variant_example_loads_without_cartesian_pairs():
    config_path = Path(__file__).resolve().parents[1] / "configs" / "experiments" / "frontier_provider_variant_sweep.yaml"
    spec = load_experiment_spec(config_path)
    runs = expand_experiment_matrix(spec, repo_root=Path("/tmp/repo"))
    pairs = {(run.factors["provider"], run.factors["model"]) for run in runs}

    assert ("openai", "deepseek-chat") not in pairs
    assert ("deepseek", "gpt-test") not in pairs
    assert {run.factors["prompt_id"] for run in runs} == {"frontier_expansion.baseline", "frontier_expansion.strict_json", "frontier_expansion.risk_aware"}


def test_collector_and_report_contain_stability_quality_cost_columns(tmp_path: Path):
    spec = ExperimentSpec.from_dict({
        "experiment_id": "frontier_llm_tuning_smoke",
        "input_dcps": ["examples/mock_input.dcp"],
        "base": {"pipeline": "frontier", "policy": "analysis_priority", "mock": True, "parallelism": 1},
        "matrix": {
            "provider": ["mock"],
            "model": ["mock_good"],
            "prompt_id": ["frontier_expansion.baseline"],
            "temperature": [0.0],
        },
        "repeats": 2,
    })
    runs = expand_experiment_matrix(spec, repo_root=tmp_path)
    for run in runs:
        run.run_dir.mkdir(parents=True)
        (run.run_dir / "pipeline_summary.json").write_text(json.dumps({
            "success": True,
            "llm_provider": "mock",
            "llm_model": "mock_good",
            "llm_prompt_id": "frontier_expansion.baseline",
            "llm_temperature": 0.0,
            "llm_response_mode": "json_object",
            "best_pure_qor_score": 2.0,
            "best_validation_status": "CLEAN",
            "total_llm_calls": 1,
            "total_prompt_tokens": 3,
            "total_completion_tokens": 4,
            "total_tokens": 7,
            "total_cost_usd": 0.02,
            "cost_source": "estimated_pricing_profile",
            "pricing_profile_id": "unit-test",
            "total_latency_sec": 0.0,
        }))
        (run.run_dir / "policy_events.json").write_text(json.dumps([
            {
                "validation": {"valid": True},
                "decision": {"fallback_used": False},
            }
        ]))
        write_llm_decision_events(run.run_dir, [
            {
                "schema_version": "llm_decision_attempt.v1",
                "task": "frontier_expansion",
                "run_id": run.run_dir.name,
                "event_index": 0,
                "attempt_index": 1,
                "provider": "mock",
                "model": "mock_good",
                "prompt_id": "frontier_expansion.baseline",
                "json_parse_success": True,
                "domain_validation_success": False,
                "validation_errors": ["unknown flow: mock_unknown_flow"],
                "fallback_triggered_after_attempt": False,
            },
            {
                "schema_version": "llm_decision_attempt.v1",
                "task": "frontier_expansion",
                "run_id": run.run_dir.name,
                "event_index": 0,
                "attempt_index": 2,
                "provider": "mock",
                "model": "mock_good",
                "prompt_id": "frontier_expansion.baseline",
                "json_parse_success": True,
                "domain_validation_success": True,
                "validation_errors": [],
                "fallback_triggered_after_attempt": False,
            },
        ])

    rows = collect_run_rows(runs)
    aggregate = aggregate_rows(rows)
    report = render_comparison_report(spec.experiment_id, aggregate)

    assert aggregate[0]["success_rate"] == 1.0
    assert aggregate[0]["pipeline_valid_decision_rate"] == 1.0
    assert aggregate[0]["llm_attempt_valid_rate"] == 0.5
    assert aggregate[0]["llm_domain_validation_failure_count"] == 2.0
    assert aggregate[0]["fallback_event_rate"] == 0.0
    assert aggregate[0]["best_pure_qor_score_mean"] == 2.0
    assert aggregate[0]["estimated_or_reported_cost_usd"] == 0.04
    assert aggregate[0]["cost_source"] == "estimated_pricing_profile"
    assert "cost_per_valid_llm_attempt" in aggregate[0]
    assert "llm_attempt_valid_rate" in report
