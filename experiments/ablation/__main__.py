"""Portable, explicit entrypoint for S0-S3. No implicit dotenv or model fallback."""
from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import os
import sys
from pathlib import Path

from optimizer_pipeline import DeterministicAnalyzer, DeterministicFlowExecutor, OutputValidator
from optimizer_pipeline.models import PipelineStatus
from experiments.ablation.config import OptimizationConfig
from experiments.ablation.runtime.artifacts import ArtifactStore
from experiments.ablation.runtime.expansion.frontier_runner import FrontierExpansionRunner
from experiments.ablation.runtime.expansion.policies.registry import PolicyResources, build_policy_registry
from experiments.ablation.runner_cli import MockExecutor, mock_analysis_provider, run_equivalence_gate
from integrations.llm.runtime.provider_profiles import load_provider_profile


def study_config(condition: str, *, mode: str, provider: str | None = None,
                 model: str | None = None, cost_budget: float | None = None,
                 s0_phase: str = "baseline") -> OptimizationConfig:
    config = OptimizationConfig.from_json_file(Path(__file__).parent / "configs/frontier_expansion_default.json")
    config.scheduler_mode = "batch"
    config.max_total_runs = 11
    config.max_depth = 11 if condition == "S1" else 6
    narrow = condition in {"S0", "S1"}
    config.branching_factor = config.beam_width = config.max_parallel_jobs = config.batch_size = 1 if narrow else 3
    if not narrow:
        config.beam_width = 2  # Recorded, but analysis_priority does not filter by it.
    config.online_deep_analysis = condition != "S0"
    config.analysis_tool_loop_mode = "mock_llm_closed_loop" if mode == "mock" else "openai_llm_closed_loop"
    config.knowledge_mode = "reference_manual" if condition == "S3" else "off"
    config.policy_type = "auto_top8" if condition == "S0" else "analysis_priority"
    if condition == "S0":
        # The published S0 used 3 baseline attempts and a separate +8 extension.
        config.max_depth = config.max_total_runs = 3 if s0_phase == "baseline" else 8
    elif mode == "real":
        if not provider or not model:
            raise ValueError("Real S1-S3 require an explicit provider and model; no replacement is selected.")
        if cost_budget is None or cost_budget <= 0:
            raise ValueError("Supply a positive --cost-budget-usd approved for this run.")
        profile = load_provider_profile(provider)
        if profile is None or not profile.api_key_env or not os.environ.get(profile.api_key_env):
            raise ValueError(f"Provider {provider!r} is not configured; no condition will be dropped.")
        sdk = {"anthropic_native": "anthropic", "google_native": "google.genai", "openai": "openai", "openai_compatible": "openai"}.get(profile.adapter_type)
        try:
            available = sdk and importlib.util.find_spec(sdk)
        except ModuleNotFoundError:
            available = None
        if not available:
            raise ValueError(f"Missing optional provider SDK: {sdk}")
        config.provider_api_key_env = profile.api_key_env
    config.provider = config.analysis_loop_provider = provider or "mock"
    config.model = config.analysis_loop_model = model or ("fixed_mock" if mode == "mock" else None)
    config.analysis_llm_cost_budget_usd = cost_budget
    config.prune_dcps_keep_winning_path = False
    config.__post_init__()
    return config


async def run(args) -> int:
    if args.mode == "real":
        if not args.tool_config:
            raise ValueError("Real mode requires an explicit --tool-config; no hidden tool installation is selected")
        from optimizer_pipeline.backends.xilinx.configured_environment import configured_environment, load_tool_config
        tools = load_tool_config(args.tool_config)
        chosen_python = Path(tools["python"]).absolute()
        current_python = Path(sys.executable).absolute()
        if chosen_python.parent != current_python.parent or chosen_python.resolve() != current_python.resolve():
            raise ValueError("Invoke this entry with the configured Python environment; MCP tools share its interpreter")
        os.environ.update(configured_environment(**{key: tools[key] for key in ("python", "vivado", "rapidwright", "java_home")}))
    config = study_config(args.condition, mode=args.mode, provider=args.provider,
                          model=args.model, cost_budget=args.cost_budget_usd, s0_phase=args.s0_phase)
    analysis_client = None
    config.acceptance_owned_process = bool(getattr(args, "acceptance_config", None))
    if getattr(args, "acceptance_config", None) and args.condition != "S0":
        if args.mode != "real":
            raise ValueError("Acceptance client requires real mode")
        from integrations.llm.runtime.batch_budget import client_from_config
        analysis_client = client_from_config(args.acceptance_config, args.condition)
        config.analysis_reasoning_effort = "medium"
        config.llm_timeout_sec = 300
    output = args.output.resolve()
    input_dcp = args.input.resolve() if args.input else None
    if args.mode == "real" and (input_dcp is None or not input_dcp.is_file()):
        raise ValueError("Real mode requires an existing --input DCP.")
    if output.exists():
        raise ValueError("--output must be a new directory; existing data are never overwritten.")
    if args.condition == "S0" and args.s0_phase == "extension":
        if not args.prior_run:
            raise ValueError("S0 extension requires --prior-run from the 3-attempt baseline.")
        summary = json.loads((args.prior_run / "pipeline_summary.json").read_text())
        expected = summary.get("final_output_dcp") or summary.get("input_dcp") or summary.get("root_dcp")
        if not expected or input_dcp != Path(expected).resolve() or summary.get("max_total_runs", summary.get("max_runs")) != 3:
            raise ValueError("S0 extension input must be the recorded baseline output with a 3-attempt budget.")
    if args.mode == "real":
        # The user must separately approve tool use and API ceiling before invoking.
        from optimizer_pipeline.backends.xilinx.toolchain import check_environment
        if config.acceptance_owned_process:
            await asyncio.to_thread(check_environment, strict=True, owned=True)
        else:
            check_environment(strict=True)
        from optimizer_pipeline.backends.xilinx import run_test_mode
        from experiments.ablation.runtime.real_analysis_provider import build_real_analysis_provider
        analyzer = DeterministicAnalyzer(analysis_provider=build_real_analysis_provider(run_dir=output, config=config))
        executor = DeterministicFlowExecutor(run_test_mode=run_test_mode, debug=False, max_nets=config.max_nets)
    else:
        analyzer = DeterministicAnalyzer(analysis_provider=mock_analysis_provider)
        executor = MockExecutor()
    store = ArtifactStore(output)
    if input_dcp is None:
        input_dcp = output / "mock_input.dcp"
        input_dcp.write_text("mock input; not a physical checkpoint\n")
    if not input_dcp.is_file():
        raise ValueError("Input not found; no caller-owned input is created.")
    policy = build_policy_registry().create(config.policy_type, resources=PolicyResources(config=config))
    async def final_check(golden, revised):
        if getattr(args, "acceptance_config", None):
            golden = Path(json.loads(args.acceptance_config.read_text())["golden"]).resolve(strict=True)
        return await run_equivalence_gate(golden, revised, workspace_dir=output / "final-validation")
    runner = FrontierExpansionRunner(config=config, artifact_store=store, analyzer=analyzer,
        executor=executor, output_validator=OutputValidator(config.validation_mode), policy=policy,
        equivalence_checker=final_check if args.mode == "real" else None,
        analysis_llm_client=analysis_client)
    prior_threads = os.environ.get("LATCH_VIVADO_MAX_THREADS")
    if args.mode == "real":
        os.environ["LATCH_VIVADO_MAX_THREADS"] = "8"
    try:
        result = await runner.run(input_dcp)
    finally:
        if prior_threads is None:
            os.environ.pop("LATCH_VIVADO_MAX_THREADS", None)
        else:
            os.environ["LATCH_VIVADO_MAX_THREADS"] = prior_threads
    print(json.dumps({"condition": args.condition, "mode": args.mode,
        "execution_completed": result.state.status == PipelineStatus.COMPLETED,
        "pipeline_ok": result.success,
        "functional_check": getattr(result.state, "equivalence_gate", None),
        "run_db": str(output / "run.db"), "summary": result.summary_path,
        "historical_replay": False, "migration_source": "a722b1e"}, indent=2))
    return 0 if result.success else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--condition", choices=["S0", "S1", "S2", "S3"], required=True)
    parser.add_argument("--mode", choices=["mock", "real"], required=True)
    parser.add_argument("--input", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--provider")
    parser.add_argument("--model")
    parser.add_argument("--cost-budget-usd", type=float)
    parser.add_argument("--tool-config", type=Path, help="Explicit python/vivado/rapidwright/java_home JSON; use that Python to invoke")
    parser.add_argument("--s0-phase", choices=["baseline", "extension"], default="baseline")
    parser.add_argument("--prior-run", type=Path)
    parser.add_argument("--acceptance-config", type=Path, help="Opt-in shared batch ledger; does not change historical defaults")
    args = parser.parse_args()
    try:
        code = asyncio.run(run(args))
    except (ValueError, RuntimeError, OSError, KeyError, ImportError) as exc:
        parser.exit(2, f"Cannot run requested condition: {exc}\n")
    raise SystemExit(code)


if __name__ == "__main__":
    main()
