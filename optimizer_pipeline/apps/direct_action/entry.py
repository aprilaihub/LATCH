"""CLI for exactly one explicitly named Xilinx backend action."""

from __future__ import annotations

import argparse
import asyncio
import json
import time
from collections.abc import Awaitable, Callable, Sequence
from pathlib import Path
from typing import Any

from optimizer_pipeline.backends.xilinx import (
    XILINX_FLOW_NAMES,
    restore_terminal_log,
    run_test_mode,
    setup_terminal_log,
    xilinx_action_catalog,
)

from .contracts import DirectActionRequest


RunFlow = Callable[..., Awaitable[Any]]


def _json_object(value: str) -> dict[str, object]:
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise argparse.ArgumentTypeError(f"invalid JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise argparse.ArgumentTypeError("action parameters must be a JSON object")
    return parsed


def build_direct_action_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="fpga-direct-action",
        description="Run one explicitly named backend action; filename inference is unsupported.",
    )
    parser.add_argument("input_dcp", type=Path)
    parser.add_argument("--action", required=True, choices=XILINX_FLOW_NAMES)
    parser.add_argument("--parameters-json", type=_json_object, default={})
    parser.add_argument("--output", "-o", dest="output_dcp", type=Path)
    parser.add_argument("--run-dir", type=Path)
    parser.add_argument("--max-nets", type=int, default=5)
    parser.add_argument("--debug", action="store_true")
    return parser


async def run_direct_action_entry(
    args: argparse.Namespace,
    *,
    run_flow: RunFlow = run_test_mode,
    terminal_log: bool = True,
) -> int:
    input_dcp = Path(args.input_dcp)
    if not input_dcp.is_file():
        raise FileNotFoundError(input_dcp)
    timestamp = time.strftime("%Y%m%d_%H%M%S")
    output_dcp = Path(args.output_dcp) if args.output_dcp else (
        input_dcp.parent / f"{input_dcp.stem}_{args.action}-{timestamp}.dcp"
    )
    run_dir = Path(args.run_dir) if args.run_dir else (
        Path.cwd() / f"direct_action_run-{timestamp}"
    )
    request = DirectActionRequest(
        action_id="direct-action-1",
        run_id=run_dir.name,
        scope_id="direct-action",
        parent_state_id="input-state",
        input_artifact_id=str(input_dcp),
        output_uri=str(output_dcp),
        action_name=str(args.action),
        parameters=dict(args.parameters_json or {}),
    )
    action = request.resolve(xilinx_action_catalog())
    run_dir.mkdir(parents=True, exist_ok=True)
    output_dcp.parent.mkdir(parents=True, exist_ok=True)
    log_state = setup_terminal_log(run_dir) if terminal_log else None
    try:
        return int(
            await run_flow(
                input_dcp,
                output_dcp,
                debug=bool(args.debug),
                max_nets=max(1, int(args.max_nets)),
                run_dir=run_dir,
                flow=action.action_type,
                optimizer_params=action.parameters.to_dict(),
            )
        )
    finally:
        if log_state is not None:
            restore_terminal_log(*log_state)


def main(argv: Sequence[str] | None = None) -> int:
    args = build_direct_action_parser().parse_args(argv)
    try:
        return asyncio.run(run_direct_action_entry(args))
    except (FileNotFoundError, ValueError) as exc:
        raise SystemExit(str(exc)) from exc
