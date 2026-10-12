"""Application-neutral custom pipeline CLI over the shared managed run API.

This entry does not load .env, select contest profiles, or certify outputs.
Study protocols belong to experiments/, not to this application.
"""
from __future__ import annotations

import argparse
import asyncio
import importlib
import inspect
import json
import shutil
import sys
from collections.abc import Sequence
from pathlib import Path

from optimizer_pipeline import DeterministicAnalyzer, DeterministicFlowExecutor, OutputValidator, PipelineStatus
from optimizer_pipeline.run import OptimizationConfig, bootstrap_run
from optimizer_pipeline.run.paths import validate_new_run_paths


def build_experiment_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run a custom pipeline using the shared LATCH harness.")
    parser.add_argument("input_dcp", type=Path)
    parser.add_argument("--custom-pipeline", help="Installed module:function accepting ctx")
    parser.add_argument("--run-dir", type=Path, help="New, independent run directory (required to run)")
    parser.add_argument("--output", dest="output_dcp", type=Path, help="New selected-DCP export path")
    parser.add_argument("--config", type=Path, help="OptimizationConfig JSON; omitted CLI fields preserve it")
    parser.add_argument("--budget-runs", type=int, default=None)
    parser.add_argument("--parallelism", type=int, default=None)
    parser.add_argument("--tool-config", type=Path, help="Explicit python/vivado/rapidwright/java_home JSON for real mode")
    parser.add_argument("--mock", action="store_true", help="No tools or model calls; requires an existing fixture input")
    parser.add_argument("--no-dotenv", action="store_true", help="Compatibility option; this entry never reads .env")
    return parser


def load_custom_pipeline(target: str):
    module_name, separator, function_name = target.partition(":")
    if not separator or not module_name or not function_name:
        raise ValueError("custom pipeline must be module:function")
    function = getattr(importlib.import_module(module_name), function_name)
    if not callable(function):
        raise ValueError("custom pipeline target is not callable")
    return function


def _build_services(args, config, run_dir):
    if args.mock:
        from .mock import MockExecutor, mock_analysis_provider
        return DeterministicAnalyzer(analysis_provider=mock_analysis_provider), MockExecutor()
    if args.tool_config is None:
        raise ValueError("real mode requires --tool-config; no hidden tool installation is selected")
    from optimizer_pipeline.backends.xilinx.configured_environment import configured_environment, load_tool_config
    from optimizer_pipeline.backends.xilinx import run_test_mode
    from optimizer_pipeline.real_analysis_provider import build_real_analysis_provider
    import os

    tool_config = load_tool_config(args.tool_config)
    os.environ.update(configured_environment(**tool_config))
    analyzer = DeterministicAnalyzer(analysis_provider=build_real_analysis_provider(run_dir=run_dir, config=config))
    executor = DeterministicFlowExecutor(run_test_mode=run_test_mode, debug=config.debug, max_nets=config.max_nets, preserve_summary=False)
    return analyzer, executor


async def run_experiment_entry(args) -> int:
    if not getattr(args, "custom_pipeline", None):
        raise ValueError("generic experiment entry requires --custom-pipeline module:function")
    ctx = None
    try:
        if args.run_dir is None:
            raise ValueError("--run-dir is required; outputs never default next to the input")
        input_dcp, run_dir = args.input_dcp.resolve(strict=True), args.run_dir.resolve()
        # Keep the last path component intact so dangling symlinks are rejected,
        # rather than resolving them into a new destination outside this run.
        output = args.output_dcp.absolute() if args.output_dcp else run_dir / "selected.dcp"
        validate_new_run_paths(input_dcp=input_dcp, run_dir=run_dir, output_dcp=output)
        config = OptimizationConfig.from_json_file(args.config)
        if args.budget_runs is not None:
            if args.budget_runs < 0:
                raise ValueError("--budget-runs must be non-negative")
            config.max_total_runs = args.budget_runs
        if args.parallelism is not None:
            if args.parallelism < 1:
                raise ValueError("--parallelism must be positive")
            config.max_parallel_jobs = args.parallelism
        config.__post_init__()
        pipeline = load_custom_pipeline(args.custom_pipeline)
        analyzer, executor = _build_services(args, config, run_dir)
        ctx = await bootstrap_run(input_dcp=input_dcp, run_dir=run_dir, config=config,
            analyzer=analyzer, executor=executor, output_validator=OutputValidator(config.validation_mode),
            mode="custom_pipeline", stop_on_reject=True)
        if config.online_deep_analysis and not args.mock:
            from integrations.llm.analysis_provider import build_external_analysis_policy
            ctx.analysis.decision_policy_factory = build_external_analysis_policy
        result = pipeline(ctx)
        if inspect.isawaitable(result):
            await result
        manifest = ctx.finalize() if not ctx.finalized else run_dir / "result_manifest.json"
        result_manifest = json.loads(manifest.read_text())
        if not result_manifest.get("success", False):
            return 1
        selected = Path(result_manifest["final_dcp"])
        if not selected.is_file():
            raise FileNotFoundError("selected artifact is missing")
        output.parent.mkdir(parents=True, exist_ok=True)
        with output.open("xb") as handle, selected.open("rb") as source:
            shutil.copyfileobj(source, handle)
        print(f"Selected DCP: {output}")
        print(f"Run dir: {run_dir}")
        print("Execution completion is not physical or functional certification; inspect run.db validations.")
        return 0
    except Exception as exc:
        if ctx is not None and not ctx.finalized:
            try:
                ctx.finalize(success=False, status=PipelineStatus.FAILED, extra={"error": type(exc).__name__})
            except Exception:
                print("Failure finalization was incomplete; retain the run for diagnosis.", file=sys.stderr)
        print(f"Experiment failed ({type(exc).__name__}); check paths, explicit dependencies and run records.", file=sys.stderr)
        return 1
    finally:
        if ctx is not None:
            ctx.close()


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_experiment_parser()
    args = parser.parse_args(argv)
    if not args.custom_pipeline:
        parser.error("--custom-pipeline is required")
    if args.run_dir is None:
        parser.error("--run-dir is required")
    return asyncio.run(run_experiment_entry(args))
