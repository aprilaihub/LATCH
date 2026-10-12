"""Subprocess runner for analysis-priority frontier experiments."""

from __future__ import annotations

import json
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

from experiments.llm.runtime.decision_events import read_llm_decision_events

from .matrix import ExperimentRunSpec, expand_experiment_matrix
from .preflight import ExperimentPreflightError, preflight_experiment
from .schema import ExperimentSpec


@dataclass(frozen=True)
class ExperimentRunResult:
    run_spec: ExperimentRunSpec
    status: str
    exit_code: Optional[int]
    duration_seconds: Optional[float]
    command: List[str]
    error: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            **self.run_spec.to_dict(),
            "status": self.status,
            "exit_code": self.exit_code,
            "duration_seconds": self.duration_seconds,
            "command": list(self.command),
            "error": self.error,
        }


def run_experiment(
    spec: ExperimentSpec,
    *,
    repo_root: Path,
    output_root: Optional[Path] = None,
    python_exe: Optional[str] = None,
    dry_run: bool = False,
    run_preflight: bool = True,
    strict_experiment: bool = False,
) -> Dict[str, Any]:
    _validate_base(spec.base)
    runs = expand_experiment_matrix(spec, repo_root=repo_root, output_root=output_root)
    experiment_root = _experiment_root(runs, repo_root=repo_root, spec=spec, output_root=output_root)
    experiment_root.mkdir(parents=True, exist_ok=True)
    effective_strict = strict_experiment or spec.strict_experiment
    preflight_report = None
    if run_preflight:
        preflight_report = preflight_experiment(spec, repo_root=repo_root, output_root=output_root)
        _write_json(experiment_root / "preflight_report.json", preflight_report.to_dict())
        if not preflight_report.ok:
            raise ExperimentPreflightError(preflight_report)
    _write_json(experiment_root / "experiment_matrix.json", [run.to_dict() for run in runs])
    results: List[ExperimentRunResult] = []
    for run in runs:
        command = build_frontier_command(run, repo_root=repo_root, python_exe=python_exe or sys.executable)
        if dry_run:
            run.run_dir.mkdir(parents=True, exist_ok=True)
            result = ExperimentRunResult(run, "dry_run", None, None, command)
        else:
            result = _run_logged(run, command, repo_root)
            if effective_strict and result.status == "passed":
                strict_failure = _strict_run_failure_reason(run)
                if strict_failure:
                    result = ExperimentRunResult(
                        run,
                        "failed",
                        result.exit_code,
                        result.duration_seconds,
                        command,
                        error=strict_failure,
                    )
        results.append(result)
        _write_json(experiment_root / "run_status.json", [item.to_dict() for item in results])
    return {
        "experiment_root": experiment_root,
        "runs": runs,
        "results": results,
        "preflight": preflight_report,
    }


def build_frontier_command(run: ExperimentRunSpec, *, repo_root: Path, python_exe: str) -> List[str]:
    base = run.base
    factors = run.factors
    budget_runs = base.get("budget_runs", base.get("max_runs", 1))
    parallelism = base.get("parallelism", base.get("jobs", 1))
    command = [
        python_exe,
        str(repo_root / "scripts" / "run_frontier_expansion.py"),
        str(_resolve_path(repo_root, run.input_dcp)),
        "--out",
        str(run.run_dir),
        "--policy",
        str(base.get("policy") or "analysis_priority"),
        "--max-runs",
        str(budget_runs),
        "--jobs",
        str(parallelism),
        "--no-dotenv",
    ]
    if bool(base.get("mock", False)):
        command.append("--mock")
    _append_optional(command, "--provider", factors.get("provider", base.get("provider")))
    _append_optional(command, "--model", factors.get("model", base.get("model")))
    _append_optional(command, "--prompt-id", factors.get("prompt_id", base.get("prompt_id")))
    _append_optional(command, "--prompt-root", factors.get("prompt_root", base.get("prompt_root")))
    _append_optional(command, "--temperature", factors.get("temperature", base.get("temperature")))
    _append_optional(command, "--max-tokens", factors.get("max_tokens", base.get("max_tokens")))
    _append_optional(command, "--llm-timeout-sec", factors.get("llm_timeout_sec", base.get("llm_timeout_sec")))
    _append_optional(command, "--llm-response-mode", factors.get("llm_response_mode", base.get("llm_response_mode")))
    _append_optional(command, "--provider-base-url", factors.get("provider_base_url", base.get("provider_base_url")))
    _append_optional(command, "--provider-api-key-env", factors.get("provider_api_key_env", base.get("provider_api_key_env")))
    _append_optional(command, "--pricing-profile", factors.get("pricing_profile", base.get("pricing_profile")))
    _append_optional(command, "--reliability-stats", factors.get("reliability_stats", base.get("reliability_stats")))
    _append_optional(command, "--retrieval-index", factors.get("retrieval_index", base.get("retrieval_index")))
    _append_optional(command, "--retrieval-k", factors.get("retrieval_k", base.get("retrieval_k")))
    _append_optional(command, "--config", base.get("config"))
    _append_optional(command, "--max-depth", base.get("max_depth"))
    _append_optional(command, "--branching-factor", base.get("branching_factor"))
    _append_optional(command, "--beam-width", base.get("beam_width"))
    _append_optional(command, "--batch-size", base.get("batch_size"))
    _append_optional(command, "--scheduler-mode", base.get("scheduler_mode"))
    _append_optional(command, "--llm-context", factors.get("llm_context", base.get("llm_context")))
    _append_optional(command, "--llm-prune-mode", base.get("llm_prune_mode"))
    _append_optional(command, "--max-nets", base.get("max_nets"))
    return command


def _run_logged(run: ExperimentRunSpec, command: List[str], repo_root: Path) -> ExperimentRunResult:
    run.run_dir.mkdir(parents=True, exist_ok=True)
    run.log_path.parent.mkdir(parents=True, exist_ok=True)
    started = time.time()
    with run.log_path.open("w") as handle:
        handle.write("$ " + " ".join(command) + "\n\n")
        proc = subprocess.run(command, cwd=repo_root, text=True, stdout=handle, stderr=subprocess.STDOUT)
    duration = time.time() - started
    status = "passed" if proc.returncode == 0 else "failed"
    error = None if proc.returncode == 0 else f"frontier command exited with {proc.returncode}"
    return ExperimentRunResult(run, status, proc.returncode, duration, command, error=error)


def _validate_base(base: Dict[str, Any]) -> None:
    if str(base.get("pipeline") or "frontier") != "frontier":
        raise ValueError("The tuning harness MVP only supports pipeline: frontier")
    if str(base.get("policy") or "analysis_priority") != "analysis_priority":
        raise ValueError("The tuning harness only supports policy: analysis_priority")
    if not bool(base.get("mock", False)):
        raise ValueError("The tuning harness MVP requires base.mock: true to avoid real Vivado/RapidWright runs")
    parallelism = int(base.get("parallelism", base.get("jobs", 1)) or 1)
    if parallelism != 1:
        raise ValueError("The tuning harness MVP currently runs experiment entries sequentially; use parallelism: 1")


def _experiment_root(
    runs: List[ExperimentRunSpec],
    *,
    repo_root: Path,
    spec: ExperimentSpec,
    output_root: Optional[Path],
) -> Path:
    if runs:
        return runs[0].run_dir.parents[2]
    root = output_root or (Path(spec.output_root) if spec.output_root else repo_root / "experiments" / "analysis_priority_tuning")
    return root / spec.experiment_id


def _resolve_path(repo_root: Path, value: Any) -> Path:
    path = Path(str(value))
    return path if path.is_absolute() else repo_root / path


def _append_optional(command: List[str], flag: str, value: Any) -> None:
    if value is None:
        return
    command.extend([flag, str(value)])


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")


def _strict_run_failure_reason(run: ExperimentRunSpec) -> Optional[str]:
    summary = _read_json(run.run_dir / "pipeline_summary.json")
    if not isinstance(summary, dict):
        return "strict experiment: missing or malformed pipeline_summary.json"
    policy_events = _read_json(run.run_dir / "policy_events.json")
    if not isinstance(policy_events, list):
        policy_events = []
    if summary.get("success") is False:
        return "strict experiment: pipeline summary reported success=false"
    if _config_mismatch(run, summary):
        return "strict experiment: actual provider/model/prompt did not match requested config"
    fallback_count = _fallback_count(policy_events, summary)
    if fallback_count:
        return f"strict experiment: {fallback_count} policy fallback event(s) occurred"
    provider_failure = _provider_failure_text(read_llm_decision_events(run.run_dir))
    if provider_failure:
        return f"strict experiment: provider call unavailable ({provider_failure})"
    return None


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text())
    except Exception:
        return {}


def _config_mismatch(run: ExperimentRunSpec, summary: Dict[str, Any]) -> bool:
    pairs = [
        (run.factors.get("provider", run.base.get("provider")), summary.get("llm_provider")),
        (run.factors.get("model", run.base.get("model")), summary.get("llm_model")),
        (run.factors.get("prompt_id", run.base.get("prompt_id")), summary.get("llm_prompt_id")),
    ]
    return any(requested is not None and actual is not None and str(requested) != str(actual) for requested, actual in pairs)


def _fallback_count(policy_events: List[Any], summary: Dict[str, Any]) -> int:
    count = 0
    for event in policy_events:
        if not isinstance(event, dict):
            continue
        decision = event.get("decision") if isinstance(event.get("decision"), dict) else {}
        if decision.get("fallback_used"):
            count += 1
    if count:
        return count
    fallback_events = summary.get("fallback_events") if isinstance(summary.get("fallback_events"), list) else []
    return len(fallback_events)


def _provider_failure_text(llm_decision_events: List[Any]) -> Optional[str]:
    fragments = ("provider call failed", "no api key", "llm frontier policy call failed", "llmclienterror")
    for event in llm_decision_events:
        if not isinstance(event, dict):
            continue
        text = _attempt_error_text(event)
        if any(fragment in text for fragment in fragments):
            return text
    return None


def _attempt_error_text(attempt: Dict[str, Any]) -> str:
    parts: List[str] = []
    errors = attempt.get("validation_errors") if isinstance(attempt.get("validation_errors"), list) else []
    parts.extend(str(error) for error in errors)
    return " | ".join(parts).lower()
