"""Fail-fast validation for frontier experiment specs."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional

from experiments.llm.runtime.pricing import DEFAULT_PRICING_TABLE
from experiments.llm.runtime.provider_profiles import LLMProviderProfile, load_provider_profile
from experiments.llm.prompts.loader import PromptLoadError, PromptLoader

from .matrix import ExperimentRunSpec, expand_experiment_matrix
from .schema import ExperimentSpec


FRONTIER_TASK = "frontier_expansion"
REAL_PROVIDER_KEY_REQUIRED_EXCEPTIONS = {"mock", "local"}


@dataclass(frozen=True)
class PreflightIssue:
    severity: str
    code: str
    message: str
    run_slug: Optional[str] = None
    field: Optional[str] = None
    value: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        payload = {
            "severity": self.severity,
            "code": self.code,
            "message": self.message,
        }
        if self.run_slug is not None:
            payload["run_slug"] = self.run_slug
        if self.field is not None:
            payload["field"] = self.field
        if self.value is not None:
            payload["value"] = self.value
        return payload


@dataclass(frozen=True)
class PreflightReport:
    issues: List[PreflightIssue]
    run_count: int = 0

    @property
    def errors(self) -> List[PreflightIssue]:
        return [issue for issue in self.issues if issue.severity == "error"]

    @property
    def warnings(self) -> List[PreflightIssue]:
        return [issue for issue in self.issues if issue.severity == "warning"]

    @property
    def ok(self) -> bool:
        return not self.errors

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ok": self.ok,
            "run_count": self.run_count,
            "error_count": len(self.errors),
            "warning_count": len(self.warnings),
            "issues": [issue.to_dict() for issue in self.issues],
        }

    def format_text(self) -> str:
        status = "OK" if self.ok else "FAILED"
        lines = [f"Experiment preflight: {status}", f"Expanded runs: {self.run_count}"]
        for issue in self.issues:
            location = f" [{issue.run_slug}]" if issue.run_slug else ""
            field = f" {issue.field}={issue.value}" if issue.field and issue.value is not None else ""
            lines.append(f"- {issue.severity.upper()} {issue.code}{location}{field}: {issue.message}")
        return "\n".join(lines)


class ExperimentPreflightError(RuntimeError):
    def __init__(self, report: PreflightReport) -> None:
        super().__init__(report.format_text())
        self.report = report


def preflight_experiment(
    spec: ExperimentSpec,
    *,
    repo_root: Path,
    output_root: Optional[Path] = None,
    environ: Optional[Mapping[str, str]] = None,
) -> PreflightReport:
    env = environ if environ is not None else os.environ
    issues: List[PreflightIssue] = []
    try:
        runs = expand_experiment_matrix(spec, repo_root=repo_root, output_root=output_root)
    except Exception as exc:
        return PreflightReport([
            PreflightIssue("error", "matrix_expansion_failed", f"Experiment matrix did not expand: {exc}")
        ])

    issues.extend(_base_issues(spec.base))
    issues.extend(_variant_matrix_issues(spec))
    issues.extend(_duplicate_run_dir_issues(runs))

    pricing_table = _load_pricing_table()
    provider_cache: Dict[str, Optional[LLMProviderProfile]] = {}

    for run in runs:
        issues.extend(_provider_issues(run, env=env, cache=provider_cache))
        issues.extend(_pricing_issues(run, pricing_table=pricing_table))
        issues.extend(_path_issues(run, repo_root=repo_root))

    return PreflightReport(issues=issues, run_count=len(runs))


def _base_issues(base: Dict[str, Any]) -> List[PreflightIssue]:
    issues: List[PreflightIssue] = []
    if str(base.get("pipeline") or "frontier") != "frontier":
        issues.append(PreflightIssue("error", "unsupported_pipeline", "Only pipeline: frontier is supported", field="pipeline", value=str(base.get("pipeline"))))
    if str(base.get("policy") or "analysis_priority") != "analysis_priority":
        issues.append(PreflightIssue("error", "unsupported_policy", "Only policy: analysis_priority is supported", field="policy", value=str(base.get("policy"))))
    parallelism = int(base.get("parallelism", base.get("jobs", 1)) or 1)
    if parallelism != 1:
        issues.append(PreflightIssue("error", "unsupported_parallelism", "Experiment entries must run sequentially for reproducible harness accounting", field="parallelism", value=str(parallelism)))
    return issues


def _variant_matrix_issues(spec: ExperimentSpec) -> List[PreflightIssue]:
    issues: List[PreflightIssue] = []
    if not spec.variants:
        return issues
    for index, variant in enumerate(spec.variants):
        name = str(variant.get("name") or f"variant_{index}")
        for key, variant_value in variant.items():
            if key == "name" or key not in spec.matrix:
                continue
            matrix_values = [str(value) for value in spec.matrix.get(key, [])]
            if str(variant_value) not in matrix_values:
                issues.append(PreflightIssue(
                    "error",
                    "variant_matrix_mismatch",
                    f"Variant {name!r} fixes {key}={variant_value!r}, but matrix.{key} does not include that value",
                    run_slug=name,
                    field=key,
                    value=str(variant_value),
                ))
    return issues


def _duplicate_run_dir_issues(runs: Iterable[ExperimentRunSpec]) -> List[PreflightIssue]:
    issues: List[PreflightIssue] = []
    seen: Dict[Path, ExperimentRunSpec] = {}
    for run in runs:
        previous = seen.get(run.run_dir)
        if previous:
            issues.append(PreflightIssue(
                "error",
                "duplicate_run_dir",
                f"Expanded run directory collides with {previous.variant_slug}/{previous.trial_slug}",
                run_slug=f"{run.variant_slug}/{run.trial_slug}",
                field="run_dir",
                value=str(run.run_dir),
            ))
        else:
            seen[run.run_dir] = run
    return issues


def _prompt_issues(run: ExperimentRunSpec, *, repo_root: Path, cache: Dict[tuple[str, str], Optional[str]]) -> List[PreflightIssue]:
    prompt_id = _factor(run, "prompt_id") or FRONTIER_TASK + ".baseline"
    prompt_root = _resolve_optional_path(repo_root, _factor(run, "prompt_root")) or (repo_root / "prompts")
    cache_key = (str(prompt_root), str(prompt_id))
    if cache_key not in cache:
        try:
            template = PromptLoader(prompt_root).load(str(prompt_id))
            cache[cache_key] = template.manifest.task
        except PromptLoadError:
            cache[cache_key] = None
    task = cache[cache_key]
    run_slug = f"{run.variant_slug}/{run.trial_slug}"
    if task is None:
        return [PreflightIssue("error", "missing_prompt", f"Prompt manifest could not be loaded from {prompt_root}", run_slug=run_slug, field="prompt_id", value=str(prompt_id))]
    if task != FRONTIER_TASK:
        return [PreflightIssue("error", "prompt_task_mismatch", f"Prompt task must be {FRONTIER_TASK!r}, got {task!r}", run_slug=run_slug, field="prompt_id", value=str(prompt_id))]
    return []


def _provider_issues(
    run: ExperimentRunSpec,
    *,
    env: Mapping[str, str],
    cache: Dict[str, Optional[LLMProviderProfile]],
) -> List[PreflightIssue]:
    provider = str(_factor(run, "provider") or "mock").lower()
    run_slug = f"{run.variant_slug}/{run.trial_slug}"
    if provider == "mock":
        return []
    profile: Optional[LLMProviderProfile] = None
    if provider == "openai_compatible":
        if not _factor(run, "provider_base_url"):
            return [PreflightIssue("error", "missing_provider_base_url", "provider=openai_compatible requires provider_base_url", run_slug=run_slug, field="provider", value=provider)]
    else:
        if provider not in cache:
            cache[provider] = load_provider_profile(provider)
        profile = cache[provider]
        if profile is None:
            return [PreflightIssue("error", "missing_provider_profile", f"No provider profile found for {provider!r}", run_slug=run_slug, field="provider", value=provider)]

    if provider in REAL_PROVIDER_KEY_REQUIRED_EXCEPTIONS:
        return []
    api_key_env = _factor(run, "provider_api_key_env") or (profile.api_key_env if profile else None) or "OPENAI_API_KEY"
    if not env.get(str(api_key_env)):
        return [PreflightIssue("error", "missing_provider_key", f"Provider {provider!r} requires API key env {api_key_env!r}", run_slug=run_slug, field="provider_api_key_env", value=str(api_key_env))]
    return []


def _pricing_issues(run: ExperimentRunSpec, *, pricing_table: Dict[str, Any]) -> List[PreflightIssue]:
    pricing_profile = _factor(run, "pricing_profile")
    if pricing_profile is None:
        return []
    if str(pricing_profile) not in pricing_table:
        return [PreflightIssue("error", "unknown_pricing_profile", "pricing_profile is not present in configs/pricing/llm_pricing.json", run_slug=f"{run.variant_slug}/{run.trial_slug}", field="pricing_profile", value=str(pricing_profile))]
    return []


def _path_issues(run: ExperimentRunSpec, *, repo_root: Path) -> List[PreflightIssue]:
    issues: List[PreflightIssue] = []
    for field, code in (("retrieval_index", "missing_retrieval_index"), ("reliability_stats", "missing_reliability_stats")):
        value = _factor(run, field) or _factor(run, f"{field}_path")
        if value is None:
            continue
        path = _resolve_optional_path(repo_root, value)
        if path is not None and not path.exists():
            issues.append(PreflightIssue("error", code, f"{field} path does not exist", run_slug=f"{run.variant_slug}/{run.trial_slug}", field=field, value=str(path)))
    return issues


def _load_pricing_table() -> Dict[str, Any]:
    try:
        data = json.loads(DEFAULT_PRICING_TABLE.read_text())
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _factor(run: ExperimentRunSpec, key: str) -> Any:
    return run.factors.get(key, run.base.get(key))


def _resolve_optional_path(repo_root: Path, value: Any) -> Optional[Path]:
    if value is None:
        return None
    path = Path(str(value))
    return path if path.is_absolute() else repo_root / path
