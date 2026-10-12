#!/usr/bin/env python3
"""Run research frontier expansion for FPGA implementation optimisation.

Server usage:
  cd "$(git rev-parse --show-toplevel)"
  export PYTHON=/absolute/path/to/isolated/python
  $PYTHON scripts/run_frontier_expansion.py \
    fpl26_contest_benchmarks/logicnets_jscl_2025.1.dcp \
    --out experiments/frontier_expansion/logicnets_analysis_priority \
    --policy analysis_priority --max-runs 15

Dry/mock usage without Vivado:
  python scripts/run_frontier_expansion.py mock_input.dcp --mock --policy deterministic --out /tmp/mock_frontier
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, Optional

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from optimizer_pipeline import (  # noqa: E402
    DeterministicAnalyzer,
    DeterministicFlowExecutor,
    OutputValidator,
    OptimizationConfig,
)
from optimizer_pipeline.models import ExecutionResult  # noqa: E402
from optimizer_pipeline.capabilities.search.policies.registry import PolicyResources, build_policy_registry  # noqa: E402
from optimizer_pipeline.real_analysis_provider import build_real_analysis_provider  # noqa: E402


class MockExecutor:
    """Small deterministic executor for local smoke tests."""

    async def execute(self, input_dcp: Path, output_dcp: Path, run_dir: Path, flow: str, optimizer_params: Optional[Dict[str, Any]] = None) -> ExecutionResult:
        run_dir.mkdir(parents=True, exist_ok=True)
        output_dcp.parent.mkdir(parents=True, exist_ok=True)
        output_dcp.write_text(f"mock output after {flow}\n")
        base = 400.0
        flow_gain = {
            "auto_pblock": 12.0,
            "phys_opt": 9.0,
            "lut_cone": 5.0,
            "retime": 6.0,
            "congestion_spread": 4.0,
            "memory_path_opt": 7.0,
            "hardblock_relocate": 3.0,
            "net_swap": 2.0,
        }.get(flow, 1.0)
        # Make repeated depth observable via filename length, deterministic but simple.
        depth_bonus = min(4.0, str(input_dcp).count("node_"))
        final = base + flow_gain + depth_bonus
        metrics = {
            "mode": "mock",
            "flow": flow,
            "optimizer_params": dict(optimizer_params or {}),
            "clock_period_ns": 2.5,
            "initial_fmax_mhz": base,
            "final_fmax_mhz": final,
            "initial_wns_ns": -0.5,
            "final_wns_ns": -0.5 + (final - base) / 100.0,
            "initial_tns_ns": -20.0,
            "final_tns_ns": -20.0 + (final - base) / 2.0,
            "total_runtime_seconds": 0.01,
        }
        summary = run_dir / "run_summary.json"
        summary.write_text(json.dumps(metrics, indent=2))
        return ExecutionResult(
            success=True,
            flow=flow,
            input_dcp=str(input_dcp),
            output_dcp=str(output_dcp),
            run_dir=str(run_dir),
            summary_path=str(summary),
            metrics=metrics,
            exit_code=0,
        )


async def mock_analysis_provider(input_dcp: Path, _config: Any) -> Dict[str, Any]:
    name = input_dcp.stem
    base = 400.0 + min(10.0, str(input_dcp).count("node_"))
    return {
        "benchmark_name": name,
        "target_clock": "clk_mock",
        "clock_period_ns": 2.5,
        "initial_wns_ns": -0.5 + min(0.1, str(input_dcp).count("node_") * 0.02),
        "initial_tns_ns": -20.0,
        "failing_endpoints": 42,
        "initial_fmax_mhz": base,
        "utilization_summary": {"luts": 1000, "ffs": 2000, "dsps": 3, "brams": 4},
        "critical_path_features": {
            "route_delay_ratio": 0.72,
            "dominant_path_type": "routing_dominated",
            "avg_logic_levels": 4,
        },
        "flow_scores": [
            {"flow": "auto_pblock", "flow_heuristic_score": 10.0},
            {"flow": "phys_opt", "flow_heuristic_score": 8.0},
            {"flow": "retime", "flow_heuristic_score": 6.0},
            {"flow": "congestion_spread", "flow_heuristic_score": 5.0},
            {"flow": "lut_cone", "flow_heuristic_score": 4.0},
        ],
        "selected_top3": ["auto_pblock", "phys_opt", "retime"],
    }


def load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export "):].strip()
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run validation-gated frontier expansion")
    parser.add_argument("input_dcp", type=Path, help="Input design checkpoint (.dcp)")
    parser.add_argument("--out", type=Path, default=None, help="Experiment output directory")
    parser.add_argument("--config", type=Path, default=REPO_ROOT / "configs" / "frontier_expansion_default.json")
    parser.add_argument("--policy", default=None, help="Policy registered in optimizer_pipeline.capabilities.search.policies.registry")
    parser.add_argument("--provider", default="openai", help="Analysis LLM provider label when analysis tool-loop mode uses an LLM")
    parser.add_argument("--model", default=None, help="Analysis LLM model string, e.g. gpt-5.4")
    parser.add_argument("--api-key", default=None, help="Explicit analysis LLM API key override; defaults are resolved by provider profiles")
    parser.add_argument("--prompt-id", default=None, help="Deprecated; frontier policy prompts are not used by analysis_priority")
    parser.add_argument("--prompt-root", type=Path, default=None, help="Optional prompt root directory")
    parser.add_argument("--temperature", type=float, default=None, help="LLM sampling temperature")
    parser.add_argument("--max-tokens", type=int, default=None, help="LLM max_tokens request limit")
    parser.add_argument("--llm-timeout-sec", type=float, default=None, help="LLM provider request timeout")
    parser.add_argument("--llm-response-mode", choices=["json_object", "text"], default=None, help="LLM response mode")
    parser.add_argument("--provider-base-url", default=None, help="OpenAI-compatible provider base URL")
    parser.add_argument("--provider-api-key-env", default=None, help="Environment variable name containing the provider API key")
    parser.add_argument("--pricing-profile", default=None, help="Optional local pricing profile for cost estimation")
    parser.add_argument("--llm-cost-budget-usd", type=float, default=None, help="Optional analysis LLM cost budget for this run")
    parser.add_argument("--knowledge-mode", choices=["off", "reference_manual"], default=None, help="Inject static optimizer reference manual into analysis LLM prompts; default off")
    parser.add_argument("--knowledge-base-path", type=Path, default=None, help="Knowledgebase JSON path; defaults to the bundled reference manual when --knowledge-mode=reference_manual")
    parser.add_argument("--max-depth", type=int, default=None)
    parser.add_argument("--branching-factor", type=int, default=None)
    parser.add_argument("--beam-width", type=int, default=None)
    parser.add_argument("--max-runs", type=int, default=None)
    parser.add_argument("--jobs", type=int, default=None, help="Parallel candidate jobs within a frontier-expansion batch")
    parser.add_argument("--batch-size", type=int, default=None, help="Maximum candidates proposed/executed per policy event")
    parser.add_argument("--scheduler-mode", choices=["async", "batch"], default=None, help="Frontier-expansion scheduler mode; async refills freed slots immediately")
    parser.add_argument("--llm-context", choices=["compact", "balanced", "full", "reliability", "retrieval"], default=None, help="Deprecated frontier context packing mode retained for config compatibility")
    parser.add_argument("--reliability-stats", type=Path, default=None, help="Optional reliability_summary.json or transitions.jsonl prior")
    parser.add_argument("--retrieval-index", type=Path, default=None, help="Optional retrieval_index.pkl")
    parser.add_argument("--retrieval-k", type=int, default=None, help="Nearest neighbours per candidate for retrieval")
    parser.add_argument("--llm-prune-mode", choices=["advisory", "hard"], default=None, help="Deprecated frontier prune mode retained for config compatibility")
    parser.add_argument("--contest-clean", action="store_true", help="Forbid external retrieval/reliability/seed history; only current-run evidence may guide analysis.")
    parser.add_argument("--max-nets", type=int, default=None)
    parser.add_argument("--debug", action="store_true")
    parser.add_argument("--mock", action="store_true", help="Use fake analysis/execution without Vivado")
    parser.add_argument("--label", default=None, help="Optional method label stored in canonical run metadata")
    parser.add_argument("--online-deep-analysis", action="store_true", help="Enable  online root/key-node deep analysis")
    parser.add_argument("--analysis-level", choices=["none", "read_only", "report_probe", "act_probe"], default=None)
    parser.add_argument("--analysis-root-budget-sec", type=float, default=None, help="Root deep-analysis hard budget; default 600 seconds")
    parser.add_argument("--analysis-state-budget-sec", dest="analysis_state_budget_sec", type=float, default=None, help="State-level deep-analysis budget; default 120 seconds")
    parser.add_argument("--analysis-max-states-per-expansion", dest="analysis_max_states_per_expansion", type=int, default=None)
    parser.add_argument("--analysis-allow-report-probes", action="store_true", help="Allow non-mutating report-probe tools during online analysis")
    parser.add_argument("--analysis-disable-report-probes", action="store_true", help="Disable report-probe tools even when analysis_level=report_probe")
    parser.add_argument("--analysis-allow-act-probes", action="store_true", help="Allow bounded optimisation probes; default disabled and not used in main experiments")
    parser.add_argument("--analysis-report-probe-backend", choices=["cached", "local_vivado", "vivado_mcp"], default=None, help="Read-only report-probe backend; default cached")
    parser.add_argument("--analysis-fresh-probe-contract-mode", choices=["auto", "off", "required"], default=None, help="Require minimum fresh Vivado report-probe evidence before closed-loop analysis stops")
    parser.add_argument("--analysis-tool-timeout-sec", type=float, default=None, help="Per-tool timeout for online analysis tools")
    parser.add_argument("--analysis-tool-plan-policy", default=None, help="Analysis tool-plan policy label")
    parser.add_argument("--analysis-tool-loop-mode", choices=["static_plan", "mock_llm_closed_loop", "scripted_llm_closed_loop", "openai_llm_closed_loop", "dynamic_llm_reserved"], default=None, help=" analysis run tool-loop mode")
    parser.add_argument("--analysis-only", action="store_true", help="Run root/deep analysis run only and do not schedule implementation candidates")
    parser.add_argument("--no-dotenv", action="store_true", help="Do not read .env automatically")
    return parser.parse_args()


def apply_cli_overrides(config: OptimizationConfig, args: argparse.Namespace) -> OptimizationConfig:
    if args.policy:
        config.policy_type = args.policy
    if args.provider:
        config.provider = args.provider
        config.analysis_loop_provider = args.provider
    if args.model:
        config.model = args.model
        config.analysis_loop_model = args.model
    if args.prompt_id is not None:
        config.prompt_id = args.prompt_id
    if args.prompt_root is not None:
        config.prompt_root = str(args.prompt_root)
    if args.temperature is not None:
        config.temperature = args.temperature
    if args.max_tokens is not None:
        config.max_tokens = args.max_tokens
    if args.llm_timeout_sec is not None:
        config.llm_timeout_sec = args.llm_timeout_sec
    if args.llm_response_mode is not None:
        config.llm_response_mode = args.llm_response_mode
    if args.provider_base_url is not None:
        config.provider_base_url = args.provider_base_url
    if args.provider_api_key_env is not None:
        config.provider_api_key_env = args.provider_api_key_env
    if args.pricing_profile is not None:
        config.pricing_profile = args.pricing_profile
    if args.llm_cost_budget_usd is not None:
        config.llm_cost_budget_usd = max(0.0, float(args.llm_cost_budget_usd))
        config.analysis_llm_cost_budget_usd = config.llm_cost_budget_usd
    if getattr(args, "knowledge_mode", None) is not None:
        config.knowledge_mode = args.knowledge_mode
    if getattr(args, "knowledge_base_path", None) is not None:
        config.knowledge_base_path = str(args.knowledge_base_path)
    if args.max_depth is not None:
        config.max_depth = args.max_depth
    if args.branching_factor is not None:
        config.branching_factor = args.branching_factor
    if args.beam_width is not None:
        config.beam_width = args.beam_width
    if args.max_runs is not None:
        config.max_total_runs = args.max_runs
    if args.jobs is not None:
        config.max_parallel_jobs = args.jobs
    if args.batch_size is not None:
        config.batch_size = args.batch_size
    if args.scheduler_mode is not None:
        config.scheduler_mode = args.scheduler_mode
    if args.llm_context is not None:
        config.llm_context_mode = args.llm_context
    if args.max_nets is not None:
        config.max_nets = args.max_nets
    if args.reliability_stats is not None:
        config.reliability_stats_path = str(args.reliability_stats)
    if args.retrieval_index is not None:
        config.retrieval_index_path = str(args.retrieval_index)
    if args.retrieval_k is not None:
        config.retrieval_k = args.retrieval_k
    if args.llm_prune_mode is not None:
        config.llm_prune_mode = args.llm_prune_mode
    if getattr(args, "contest_clean", False):
        config.contest_clean = True
    if getattr(args, "online_deep_analysis", False):
        config.online_deep_analysis = True
    if getattr(args, "analysis_level", None) is not None:
        config.analysis_level = args.analysis_level
    if getattr(args, "analysis_root_budget_sec", None) is not None:
        config.analysis_root_budget_sec = args.analysis_root_budget_sec
    if getattr(args, "analysis_state_budget_sec", None) is not None:
        config.analysis_state_budget_sec = args.analysis_state_budget_sec
    if getattr(args, "analysis_max_states_per_expansion", None) is not None:
        config.analysis_max_states_per_expansion = args.analysis_max_states_per_expansion
    if getattr(args, "analysis_allow_report_probes", False):
        config.analysis_allow_report_probes = True
    if getattr(args, "analysis_disable_report_probes", False):
        config.analysis_allow_report_probes = False
    if getattr(args, "analysis_allow_act_probes", False):
        config.analysis_allow_act_probes = True
        config.analysis_level = "act_probe"
    if getattr(args, "analysis_report_probe_backend", None) is not None:
        config.analysis_report_probe_backend = args.analysis_report_probe_backend
    if getattr(args, "analysis_fresh_probe_contract_mode", None) is not None:
        config.analysis_fresh_probe_contract_mode = args.analysis_fresh_probe_contract_mode
    if getattr(args, "analysis_tool_timeout_sec", None) is not None:
        config.analysis_tool_timeout_sec = args.analysis_tool_timeout_sec
    if getattr(args, "analysis_tool_plan_policy", None) is not None:
        config.analysis_tool_plan_policy = args.analysis_tool_plan_policy
    if getattr(args, "analysis_tool_loop_mode", None) is not None:
        config.analysis_tool_loop_mode = args.analysis_tool_loop_mode
    if getattr(args, "analysis_only", False):
        config.analysis_only = True
        config.online_deep_analysis = True
    if args.debug:
        config.debug = True
    config.__post_init__()
    config.validate_contest_clean()
    return config


async def main_async() -> int:
    args = parse_args()
    if not args.no_dotenv:
        load_dotenv(REPO_ROOT / ".env")
        load_dotenv(Path.cwd() / ".env")

    if not args.mock and not args.input_dcp.exists():
        print(f"Error: input DCP not found: {args.input_dcp}", file=sys.stderr)
        return 2
    if args.mock and not args.input_dcp.exists():
        args.input_dcp.parent.mkdir(parents=True, exist_ok=True)
        args.input_dcp.write_text("mock input\n")

    config = apply_cli_overrides(OptimizationConfig.from_json_file(args.config), args)
    if not args.model:
        config.model = None
    config.validate_contest_clean()

    model_tag = config.model or "none"
    run_dir = args.out or (Path.cwd() / "experiments" / "frontier_expansion" / f"{args.input_dcp.stem}_{config.policy_type}_{model_tag}_{time.strftime('%Y%m%d_%H%M%S')}")

    if args.mock:
        analyzer = DeterministicAnalyzer(analysis_provider=mock_analysis_provider)
        executor = MockExecutor()
    else:
        try:
            from optimizer_pipeline.backends.xilinx import run_test_mode
        except Exception as exc:
            print(f"Error importing dcp_optimizer dependencies: {type(exc).__name__}: {exc}", file=sys.stderr)
            print("Configure an explicit isolated interpreter and tool environment, or rerun with --mock for local smoke tests.", file=sys.stderr)
            return 3

        analyzer = DeterministicAnalyzer(analysis_provider=build_real_analysis_provider(run_dir=run_dir, config=config))
        executor = DeterministicFlowExecutor(run_test_mode=run_test_mode, debug=config.debug, max_nets=config.max_nets)

    api_key = args.api_key
    policy_registry = build_policy_registry()
    policy = policy_registry.create(
        config.policy_type,
        resources=PolicyResources(config=config, api_key=api_key),
    )

    if args.label:
        config.policy_type = args.label

    from optimizer_pipeline.run import bootstrap_run

    ctx = await bootstrap_run(
        input_dcp=args.input_dcp,
        run_dir=run_dir,
        config=config,
        analyzer=analyzer,
        executor=executor,
        output_validator=OutputValidator(config.validation_mode),
        run_id=run_dir.name,
        mode="frontier_expansion",
        max_eda_runs=config.max_total_runs,
        max_parallel_jobs=config.max_parallel_jobs,
        stop_on_reject=False,
    )
    if config.online_deep_analysis:
        from experiments.llm.analysis_provider import build_external_analysis_policy

        ctx.analysis.decision_policy_factory = build_external_analysis_policy

    print("FPGA Frontier Expansion")
    print("=========================")
    print(f"Input:        {args.input_dcp}")
    print(f"Run dir:      {run_dir}")
    print(f"Policy:       {config.policy_type}")
    print(f"Provider:     {config.provider if config.online_deep_analysis else 'n/a'}")
    print(f"Model:        {config.model if config.online_deep_analysis else 'n/a'}")
    print(f"Max runs:     {config.max_total_runs}")
    print(f"Max depth:    {config.max_depth}")
    print(f"Branching:    {config.branching_factor}")
    print(f"Beam width:   {config.beam_width}")
    print(f"Jobs:         {config.max_parallel_jobs}")
    print(f"Batch size:   {config.batch_size}")
    print(f"Scheduler:    {config.scheduler_mode}")
    print(f"Rel. stats:   {config.reliability_stats_path}")
    print(f"Retr. index:  {config.retrieval_index_path}")
    print(f"Retr. k:      {config.retrieval_k}")
    print(f"Validation:   {config.validation_mode}")
    print(f"Contest clean: {config.contest_clean}")
    print(f"Knowledge:    mode={config.knowledge_mode}, path={config.knowledge_base_path or 'bundled' if config.knowledge_mode == 'reference_manual' else 'n/a'}")
    if config.online_deep_analysis:
        print(f"Analysis:     level={config.analysis_level}, backend={config.analysis_report_probe_backend}, timeout={config.analysis_tool_timeout_sec}s, loop={config.analysis_tool_loop_mode}, analysis_only={config.analysis_only}")
    print(f"Allowed:      {', '.join(config.allowed_flows)}")
    if config.online_deep_analysis and config.analysis_tool_loop_mode.endswith("llm_closed_loop") and config.provider != "mock":
        print(f"API key:      {'explicit CLI override' if api_key else 'provider profile/env'}")
    print()

    result = await ctx.expand.frontier(
        policy=policy,
        max_transitions=config.max_total_runs,
        parallelism=config.max_parallel_jobs,
        branching_factor=config.branching_factor,
        beam_width=config.beam_width,
        batch_size=config.batch_size,
        max_depth=config.max_depth,
    )
    ctx.finalize(success=bool(result and result.success))
    if result and result.success:
        print("\n[FRONTIER] Completed")
        print(f"[FRONTIER] Summary: {result.summary_path}")
        print(f"[FRONTIER] Final output: {result.final_output}")
        print(f"[FRONTIER] Run dir: {result.run_dir}")
        return 0
    print("\n[FRONTIER] Failed")
    print(f"[FRONTIER] Summary/Error: {result.summary_path if result else run_dir / 'result_manifest.json'}")
    print(f"[FRONTIER] Run dir: {result.run_dir if result else run_dir}")
    return 1


def main() -> None:
    raise SystemExit(asyncio.run(main_async()))


if __name__ == "__main__":
    main()
