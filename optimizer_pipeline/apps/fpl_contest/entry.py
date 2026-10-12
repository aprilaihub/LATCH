"""Contest-compatible entry adapter for expansion-based optimization profiles."""

from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

from optimizer_pipeline import DeterministicAnalyzer, DeterministicFlowExecutor, OutputValidator, OptimizationConfig, PipelineStatus
from optimizer_pipeline.apps.fpl_contest.analysis_profiles import AnalysisProfileError, AnalysisProfileSpec, resolve_analysis_profile
from optimizer_pipeline.apps.fpl_contest.network_policy import NetworkPolicyError, RunModeDecision, enforce_network_policy
from optimizer_pipeline.apps.fpl_contest.official_score import OfficialContestTracer
from optimizer_pipeline.apps.fpl_contest.output_publisher import BestDcpPublisher
from optimizer_pipeline.apps.fpl_contest.profiles import run_profile
from optimizer_pipeline.real_analysis_provider import build_real_analysis_provider
from optimizer_pipeline.capabilities.intervention import NoImprovementInterventionStrategy
from optimizer_pipeline.run import bootstrap_run
from optimizer_pipeline.run.paths import validate_new_run_paths


RunTestMode = Callable[..., Awaitable[Any]]
DEFAULT_CONTEST_OPENROUTER_BUDGET_USD = 0.80


async def run_contest_entry(args: Any, *, run_test_mode: Optional[RunTestMode] = None) -> int:
    """Run the selected contest-compatible profile and return a process exit code."""

    if not getattr(args, "no_dotenv", False):
        _load_dotenv(Path(__file__).resolve().parents[3] / ".env")
        _load_dotenv(Path.cwd() / ".env")

    try:
        policy = enforce_network_policy(args)
    except NetworkPolicyError as exc:
        print(f"Network policy error: {exc}", file=sys.stderr)
        return 2
    _apply_cli_api_key(args, policy)
    try:
        analysis_profile = resolve_analysis_profile(args, policy)
    except AnalysisProfileError as exc:
        print(f"Analysis profile error: {exc}", file=sys.stderr)
        return 2
    setattr(args, "_run_mode_policy", policy)
    setattr(args, "_analysis_profile", analysis_profile)

    input_dcp = Path(args.input_dcp)
    if not input_dcp.exists() and not getattr(args, "mock", False):
        print(f"Error: input DCP not found: {input_dcp}", file=sys.stderr)
        return 1

    timestamp = time.strftime("%Y%m%d_%H%M%S")
    output_dcp = Path(args.output_dcp) if getattr(args, "output_dcp", None) else input_dcp.parent / f"{input_dcp.stem}_optimized-{timestamp}.dcp"
    run_dir = Path(args.run_dir) if getattr(args, "run_dir", None) else Path.cwd() / f"dcp_optimizer_run-{timestamp}"
    try:
        validate_new_run_paths(input_dcp=input_dcp, run_dir=run_dir, output_dcp=output_dcp)
        if output_dcp.resolve() in {run_dir.resolve() / "run.db", run_dir.resolve() / "result_manifest.json"}:
            raise ValueError("contest output cannot replace canonical run records")
    except (OSError, ValueError) as exc:
        print(f"Run path error: {exc}", file=sys.stderr)
        return 2

    try:
        config = _build_config(args, policy, analysis_profile)
    except (NetworkPolicyError, ValueError) as exc:
        print(f"Contest config error: {exc}", file=sys.stderr)
        return 2
    if getattr(args, "mock", False) and not input_dcp.exists():
        input_dcp.parent.mkdir(parents=True, exist_ok=True)
        input_dcp.write_text("mock input\n")
    run_dir.mkdir(parents=True, exist_ok=True)
    if not bool(getattr(args, "mock", False)) and config.rescue_mode in {"synthesis", "synthesis_loop", "manual_loop"}:
        from integrations.llm.intervention_generator import ProviderInterventionGenerator

        config.intervention_generator = ProviderInterventionGenerator(config)
    analyzer = _build_analyzer(args, config=config, run_dir=run_dir)
    executor = _build_executor(args, config, run_test_mode)
    validator = OutputValidator(config.validation_mode)

    llm_model_label = _resolved_llm_model_label(args, config, policy)
    tracer = OfficialContestTracer(
        run_dir=run_dir,
        run_mode=policy.run_mode,
        contest_eligible=policy.contest_eligible,
        llm_provider=policy.llm_provider,
        llm_model=llm_model_label,
        noncontest_reason=policy.reason,
    )

    try:
        ctx = await bootstrap_run(
            input_dcp=input_dcp,
            run_dir=run_dir,
            config=config,
            analyzer=analyzer,
            executor=executor,
            output_validator=validator,
            run_id=run_dir.name,
            mode=f"contest_entry:{args.pipeline}",
            max_eda_runs=config.max_total_runs,
            max_parallel_jobs=config.max_parallel_jobs,
            stop_on_reject=False,
        )
    except Exception as exc:
        print(f"Contest bootstrap failed ({type(exc).__name__}); retain the run for diagnosis.", file=sys.stderr)
        return 1
    try:
        # Bootstrap must see a new workspace. It can itself create a requested
        # output alias (such as SQLite WAL/SHM), which must never be published over.
        if output_dcp.exists() or output_dcp.is_symlink():
            raise FileExistsError("contest output was created during bootstrap")
        publisher = BestDcpPublisher(
            output_path=output_dcp,
            run_dir=run_dir,
            run_mode=policy.run_mode,
            contest_eligible=policy.contest_eligible,
        )
        publisher.publish_baseline(input_dcp)
    except Exception as exc:
        print(f"Contest baseline publication failed ({type(exc).__name__}); retain the run for diagnosis.", file=sys.stderr)
        try:
            ctx.finalize(success=False, status=PipelineStatus.FAILED, extra={"error": type(exc).__name__})
        except Exception:
            print("Failure finalization was incomplete; retain the run for diagnosis.", file=sys.stderr)
        finally:
            ctx.close()
        return 1
    if config.online_deep_analysis:
        from integrations.llm.analysis_provider import build_external_analysis_policy

        ctx.analysis.decision_policy_factory = build_external_analysis_policy
    baseline_fmax = _first_float(ctx.root_metrics, "initial_fmax_mhz", "final_fmax_mhz", "fmax_mhz")
    if baseline_fmax is not None:
        publisher.best_fmax_mhz = baseline_fmax
    root_row = tracer.append_root(input_dcp=input_dcp, root_metrics=ctx.root_metrics, published_output=output_dcp)

    observer = _ContestTransitionObserver(publisher=publisher, tracer=tracer, baseline_fmax_mhz=baseline_fmax)
    ctx.transition_observers.append(observer)

    exit_code = 0
    error: Optional[str] = None
    try:
        await run_profile(ctx, args)
        if str(getattr(args, "rescue_mode", "off") or "off").strip().lower() != "off":
            await ctx.intervention.run(
                NoImprovementInterventionStrategy(
                    trigger=getattr(args, "rescue_trigger", "no_improvement"),
                    min_main_delta_mhz=getattr(args, "rescue_min_main_delta_mhz", 0.1),
                ),
                mode=getattr(args, "rescue_mode", "off"),
                trigger=getattr(args, "rescue_trigger", "no_improvement"),
                max_runs=getattr(args, "rescue_max_runs", 1),
                min_main_delta_mhz=getattr(args, "rescue_min_main_delta_mhz", 0.1),
                mock_llm=bool(getattr(args, "mock", False)),
            )
    except Exception as exc:
        exit_code = 1
        error = f"{type(exc).__name__}: {exc}"
        print(f"Contest entry failed: {error}", file=sys.stderr)

    analysis_usage = _analysis_usage_summary(ctx)
    application_detail = {
        "run_mode": policy.run_mode,
        "contest_eligible": policy.contest_eligible,
        "contest_eligibility_reason": policy.reason,
        "pipeline": args.pipeline,
        "policy": args.policy,
        "analysis": args.analysis,
        "llm_provider": policy.llm_provider,
        "llm_model": llm_model_label,
        "analysis_llm_model": _resolved_analysis_model_label(args, config, policy),
        "policy_llm_model": None,
        "llm_api_key_source": policy.api_key_source,
        "llm_cost_source": policy.cost_source,
        "run_mode_policy": policy.to_dict(),
        "contest_output_dcp": str(output_dcp),
        "published_best": publisher.snapshot(),
        "analysis_cost_summary": analysis_usage,
        "analysis_total_cost_usd": analysis_usage.get("total_cost_usd"),
        "llm_total_cost_usd": analysis_usage.get("total_cost_usd") if (analysis_profile.uses_llm or config.rescue_mode in {"llm", "synthesis", "synthesis_loop", "manual_loop"}) else None,
        "llm_cost_budget_usd": config.llm_cost_budget_usd,
        "knowledge_mode": config.knowledge_mode,
        "knowledge_base_path": config.knowledge_base_path,
        "rescue_mode": config.rescue_mode,
        "rescue_trigger": config.rescue_trigger,
        "rescue_max_runs": config.rescue_max_runs,
        "rescue_min_main_delta_mhz": config.rescue_min_main_delta_mhz,
        "rescue_synthesis_max_steps": config.rescue_synthesis_max_steps,
        "rescue_synthesis_fallback": config.rescue_synthesis_fallback,
        "rescue_loop_policy": config.rescue_loop_policy,
        "rescue_parent_strategy": config.rescue_parent_strategy,
        "rescue_manual_parent_selector": config.rescue_manual_parent_selector,
        "rescue_manual_min_publish_delta_mhz": config.rescue_manual_min_publish_delta_mhz,
        "rescue_manual_allow_nonclean_publish": config.rescue_manual_allow_nonclean_publish,
        "rescue_evidence_mode": config.rescue_evidence_mode,
        "rescue_history_scope": config.rescue_history_scope,
        "rescue_enable_act_probes": config.rescue_enable_act_probes,
        "rescue_max_act_probes": config.rescue_max_act_probes,
        "rescue_act_probe_wall_sec": config.rescue_act_probe_wall_sec,
        "error": error,
    }
    if not ctx.finalized:
        ctx.record_run_detail("application_summary", application_detail)
    summary_extra = {
        "run_mode": policy.run_mode,
        "contest_eligible": policy.contest_eligible,
        "pipeline": args.pipeline,
        "contest_output_dcp": str(output_dcp),
        "error": error,
    }
    if not ctx.finalized:
        ctx.finalize(
            extra=summary_extra,
            success=exit_code == 0,
            status=PipelineStatus.COMPLETED if exit_code == 0 else PipelineStatus.FAILED,
        )
    else:
        ctx.write_result_manifest({
            "schema_version": "managed_result.v1",
            "success": exit_code == 0,
            **summary_extra,
        })
        ctx.ensure_terminal_event(success=exit_code == 0)
    print(f"Optimized DCP: {output_dcp}")
    print(f"Run dir: {run_dir}")
    ctx.run_store.close()
    return exit_code


def run_contest_entry_sync(args: Any, *, run_test_mode: Optional[RunTestMode] = None) -> int:
    return asyncio.run(run_contest_entry(args, run_test_mode=run_test_mode))


class _ContestTransitionObserver:
    def __init__(self, *, publisher: BestDcpPublisher, tracer: OfficialContestTracer, baseline_fmax_mhz: Optional[float]) -> None:
        self.publisher = publisher
        self.tracer = tracer
        self.baseline_fmax_mhz = baseline_fmax_mhz
        self.seen_transition_ids: set[str] = set()

    def __call__(self, ctx: Any, record: Any) -> None:
        intent = getattr(record, "intent", None)
        transition_id = str(getattr(intent, "transition_id", None))
        if transition_id in self.seen_transition_ids:
            return
        outcome = getattr(record, "outcome", None)
        if outcome is None:
            return
        self.seen_transition_ids.add(transition_id)
        row = self.tracer.row_from_transition(
            record,
            baseline_fmax_mhz=self.baseline_fmax_mhz,
            openrouter_cost_usd=_current_openrouter_cost_usd(ctx, self.tracer.llm_provider),
        )
        self._apply_manual_publication_guard(ctx, row)
        if self.baseline_fmax_mhz is None and row.get("baseline_fmax_mhz") is not None:
            self.baseline_fmax_mhz = float(row["baseline_fmax_mhz"])
            self.publisher.best_fmax_mhz = self.baseline_fmax_mhz
            self.publisher.best_delta_fmax_mhz = 0.0
            self.tracer.update_baseline_row(
                fmax_mhz=self.baseline_fmax_mhz,
                wns_ns=row.get("baseline_wns_ns"),
                tns_ns=row.get("baseline_tns_ns"),
            )
        event = self.publisher.consider_transition(row)
        if event is not None:
            if isinstance(row.get("publication_selection"), dict):
                row["publication_selection"]["selected_as_published_best"] = True
            row["contest_output_dcp"] = event["output_path"]
        self.tracer.append(row)

    def _apply_manual_publication_guard(self, ctx: Any, row: dict) -> None:
        if str(row.get("optimizer_name") or "") != "manual_action":
            return
        publication = row.get("publication_selection") if isinstance(row.get("publication_selection"), dict) else {}
        row["publication_selection"] = publication
        manual_state = row.get("manual_checkpoint_state") if isinstance(row.get("manual_checkpoint_state"), dict) else {}
        if manual_state.get("placement") != "placed" or manual_state.get("routing") != "routed":
            row["valid_for_scoring"] = False
            row["fully_routed"] = False
            publication["eligible"] = False
            publication["manual_publish_block_reason"] = "manual_checkpoint_not_fully_routed"
            return
        status = str(row.get("validation_status") or "").upper()
        if status != "CLEAN" and not bool(getattr(ctx.config, "rescue_manual_allow_nonclean_publish", False)):
            row["valid_for_scoring"] = False
            publication["manual_publish_block_reason"] = "manual_nonclean_output"
            return
        fmax = _first_float(row, "fmax_output_mhz")
        threshold = max(0.0, float(getattr(ctx.config, "rescue_manual_min_publish_delta_mhz", 0.01) or 0.0))
        if self.publisher.best_fmax_mhz is not None and (fmax is None or fmax < self.publisher.best_fmax_mhz + threshold):
            row["valid_for_scoring"] = False
            publication["manual_publish_block_reason"] = "manual_delta_below_threshold"


def _build_config(args: Any, policy: RunModeDecision, analysis_profile: AnalysisProfileSpec) -> OptimizationConfig:
    config = OptimizationConfig.from_json_file(getattr(args, "config", None))
    _validate_contest_provider_config(config, args, policy)
    config.policy_type = str(getattr(args, "policy", "deterministic") or "deterministic")
    config.contest_clean = bool(policy.contest_eligible)
    config.max_total_runs = max(0, int(getattr(args, "budget_runs", 6) or 0))
    config.max_parallel_jobs = max(1, int(getattr(args, "parallelism", 1) or 1))
    config.batch_size = max(config.branching_factor, config.max_parallel_jobs)
    config.provider = policy.llm_provider
    requested_model = _optional_str(getattr(args, "model", None))
    if policy.llm_provider == "openrouter" and requested_model is None:
        requested_model = os.environ.get("OPENROUTER_MODEL") or os.environ.get("FPL_AGENT_MODEL") or "openai/gpt-5.4-mini"
    config.model = requested_model
    requested_budget = getattr(args, "llm_cost_budget_usd", None)
    if requested_budget is not None:
        config.llm_cost_budget_usd = max(0.0, float(requested_budget))
    elif policy.run_mode == "contest-strict" and policy.llm_provider == "openrouter":
        config.llm_cost_budget_usd = DEFAULT_CONTEST_OPENROUTER_BUDGET_USD
    config.max_nets = int(getattr(args, "max_nets", 5) or 5)
    config.debug = bool(getattr(args, "debug", False))
    config.rescue_mode = str(getattr(args, "rescue_mode", "off") or "off")
    config.rescue_trigger = str(getattr(args, "rescue_trigger", "no_improvement") or "no_improvement")
    config.rescue_max_runs = max(0, int(getattr(args, "rescue_max_runs", 1) or 0))
    config.rescue_max_episodes = max(1, int(getattr(args, "rescue_max_episodes", 1) or 1))
    config.rescue_min_main_delta_mhz = max(0.0, float(getattr(args, "rescue_min_main_delta_mhz", 0.1) or 0.0))
    config.rescue_synthesis_max_steps = max(1, int(getattr(args, "rescue_synthesis_max_steps", 8) or 8))
    config.rescue_synthesis_fallback = str(getattr(args, "rescue_synthesis_fallback", "deterministic") or "deterministic")
    config.rescue_loop_policy = str(getattr(args, "rescue_loop_policy", "fixed_attempts") or "fixed_attempts")
    config.rescue_parent_strategy = str(getattr(args, "rescue_parent_strategy", "current_best") or "current_best")
    config.rescue_manual_parent_selector = str(getattr(args, "rescue_manual_parent_selector", "last_scratch") or "last_scratch")
    config.rescue_manual_min_publish_delta_mhz = max(0.0, float(getattr(args, "rescue_manual_min_publish_delta_mhz", 0.01) or 0.0))
    config.rescue_manual_allow_nonclean_publish = bool(getattr(args, "rescue_manual_allow_nonclean_publish", False))
    evidence_mode = str(getattr(args, "rescue_evidence_mode", "compact") or "compact")
    if config.rescue_mode == "synthesis_loop" and evidence_mode == "compact":
        evidence_mode = "planner"
    config.rescue_evidence_mode = evidence_mode
    config.rescue_history_scope = str(getattr(args, "rescue_history_scope", "in_run") or "in_run")
    config.rescue_enable_act_probes = bool(getattr(args, "rescue_enable_act_probes", False))
    config.rescue_max_act_probes = max(0, int(getattr(args, "rescue_max_act_probes", 0) or 0))
    config.rescue_act_probe_wall_sec = max(1.0, float(getattr(args, "rescue_act_probe_wall_sec", 300.0) or 300.0))
    config.rescue_allow_invalid_scratch = bool(getattr(args, "rescue_allow_invalid_scratch", True))
    cli_knowledge_mode = _optional_str(getattr(args, "knowledge_mode", None))
    if cli_knowledge_mode:
        config.knowledge_mode = cli_knowledge_mode
    if getattr(args, "knowledge_base_path", None) is not None:
        config.knowledge_base_path = str(getattr(args, "knowledge_base_path"))
    if analysis_profile.enabled:
        config.online_deep_analysis = True
        config.analysis_level = analysis_profile.analysis_level
        config.analysis_tool_loop_mode = analysis_profile.loop_mode
        if analysis_profile.tool_plan_policy:
            config.analysis_tool_plan_policy = analysis_profile.tool_plan_policy
        config.analysis_loop_provider = policy.llm_provider if policy.llm_provider != "none" else config.analysis_loop_provider
        config.analysis_loop_model = requested_model
        config.analysis_llm_cost_budget_usd = config.llm_cost_budget_usd
    if config.rescue_mode in {"llm", "synthesis", "synthesis_loop", "manual_loop"}:
        config.online_deep_analysis = True
        config.analysis_loop_provider = policy.llm_provider if policy.llm_provider != "none" else config.analysis_loop_provider
        config.analysis_loop_model = requested_model
        config.analysis_llm_cost_budget_usd = config.llm_cost_budget_usd
        if config.rescue_enable_act_probes:
            config.analysis_level = "act_probe"
            config.analysis_allow_report_probes = True
            config.analysis_allow_act_probes = True
            config.analysis_tool_timeout_sec = min(float(config.analysis_tool_timeout_sec), config.rescue_act_probe_wall_sec)
    config.__post_init__()
    config.validate_contest_clean()
    return config


def _validate_contest_provider_config(config: OptimizationConfig, args: Any, policy: RunModeDecision) -> None:
    if policy.run_mode != "contest-strict":
        return
    if getattr(args, "api_key", None):
        raise NetworkPolicyError("run_mode=contest-strict forbids --api-key; use organizer-provided OPENROUTER_API_KEY.")
    violations = []
    if config.provider_base_url:
        violations.append("provider_base_url")
    if config.provider_api_key_env:
        violations.append("provider_api_key_env")
    if violations:
        raise NetworkPolicyError(
            "run_mode=contest-strict forbids provider endpoint/key-env overrides: "
            + ", ".join(violations)
        )


def _build_analyzer(args: Any, *, config: OptimizationConfig, run_dir: Path) -> DeterministicAnalyzer:
    if getattr(args, "mock", False):
        from scripts.run_frontier_expansion import mock_analysis_provider

        return DeterministicAnalyzer(analysis_provider=mock_analysis_provider)
    return DeterministicAnalyzer(analysis_provider=build_real_analysis_provider(run_dir=run_dir, config=config))


def _build_executor(args: Any, config: OptimizationConfig, run_test_mode: Optional[RunTestMode]) -> Any:
    if getattr(args, "mock", False):
        from scripts.run_frontier_expansion import MockExecutor

        return MockExecutor()
    if run_test_mode is None:
        raise RuntimeError("Real DCP execution requires a run_test_mode callable")
    return DeterministicFlowExecutor(
        run_test_mode=run_test_mode,
        debug=config.debug,
        max_nets=config.max_nets,
        preserve_summary=False,
    )


def _analysis_usage_summary(ctx: Any) -> dict:
    controller = getattr(ctx, "analysis_controller", None)
    if controller is None and getattr(ctx, "analysis", None) is not None:
        controller = getattr(ctx.analysis, "controller", None)
    summary: dict = {}
    if controller is not None and hasattr(controller, "summary"):
        try:
            value = controller.summary()
            if isinstance(value, dict):
                summary = dict(value)
        except Exception:
            summary = {}
    manual = getattr(ctx, "manual_rescue_usage", None)
    if isinstance(manual, dict):
        summary = _merge_usage_summaries(summary, manual)
    return summary


def _merge_usage_summaries(left: dict, right: dict) -> dict:
    result = dict(left)
    for key in (
        "total_llm_calls",
        "total_prompt_tokens",
        "total_completion_tokens",
        "total_tokens",
        "total_cost_usd",
        "total_provider_reported_cost_usd",
        "total_estimated_cost_usd",
        "total_latency_sec",
    ):
        left_value = left.get(key)
        right_value = right.get(key)
        if right_value is None:
            continue
        result[key] = right_value if left_value is None else left_value + right_value
    if right.get("cost_source"):
        result["cost_source"] = right["cost_source"] if not left.get("cost_source") or left.get("cost_source") == right["cost_source"] else "mixed"
    if right.get("pricing_profile_id"):
        result["pricing_profile_id"] = right["pricing_profile_id"] if not left.get("pricing_profile_id") or left.get("pricing_profile_id") == right["pricing_profile_id"] else "mixed"
    return result


def _current_openrouter_cost_usd(ctx: Any, provider: str) -> Optional[float]:
    if provider != "openrouter":
        return 0.0
    summary = _analysis_usage_summary(ctx)
    value = summary.get("total_cost_usd")
    if value is None:
        return None
    try:
        return float(value)
    except Exception:
        return None


def _apply_cli_api_key(args: Any, policy: RunModeDecision) -> None:
    api_key = _optional_str(getattr(args, "api_key", None))
    if not api_key:
        return
    if policy.llm_provider == "openai":
        os.environ["OPENAI_API_KEY"] = api_key
    elif policy.llm_provider == "openrouter":
        os.environ["OPENROUTER_API_KEY"] = api_key


def _optional_str(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _resolved_llm_model_label(args: Any, config: OptimizationConfig, policy: RunModeDecision) -> Optional[str]:
    return _resolved_analysis_model_label(args, config, policy)


def _resolved_policy_model_label(config: OptimizationConfig, policy: RunModeDecision) -> Optional[str]:
    return None


def _resolved_analysis_model_label(args: Any, config: OptimizationConfig, policy: RunModeDecision) -> Optional[str]:
    analysis_profile = getattr(args, "_analysis_profile", None)
    rescue_uses_llm = str(getattr(args, "rescue_mode", "off") or "off").strip().lower() in {"llm", "synthesis", "synthesis_loop", "manual_loop"}
    if (
        (
            not isinstance(analysis_profile, AnalysisProfileSpec)
            or not analysis_profile.enabled
            or not analysis_profile.uses_llm
        )
        and not rescue_uses_llm
        or policy.llm_provider == "none"
    ):
        return None
    return (
        _optional_str(config.analysis_loop_model)
        or _optional_str(config.model)
        or (os.environ.get("OPENROUTER_MODEL") if policy.llm_provider == "openrouter" else None)
        or os.environ.get("FPL_AGENT_MODEL")
        or os.environ.get("OPENAI_MODEL")
        or ("openai/gpt-5.4-mini" if policy.llm_provider == "openrouter" else None)
        or ("gpt-5.4-mini" if policy.llm_provider == "openai" else None)
    )


def _load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def _first_float(data: dict, *keys: str) -> Optional[float]:
    for key in keys:
        value = data.get(key)
        if value is None or value == "":
            continue
        try:
            return float(value)
        except Exception:
            continue
    return None
