"""CLI for one predeclared, read-only L0 real-DCP preflight.

The plan is public experiment evidence.  Local DCP, output, interpreter, and
Vivado paths are private launch bindings: they are passed to the preflight
adapter but are never added to the JSON emitted by this command.
"""

from __future__ import annotations

import argparse
from collections.abc import Callable, Mapping, Sequence
import json
from pathlib import Path
import sys
from typing import Any


PreflightRunner = Callable[..., dict[str, Any]]


def build_dof_preflight_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="fpga-dof-preflight",
        description=(
            "Run exactly one predeclared, read-only L0 real-DCP preflight. "
            "All local paths are private launch bindings."
        ),
        allow_abbrev=False,
    )
    parser.add_argument(
        "plan",
        type=Path,
        help="Strict JSON preflight plan with frozen DCP and repository fingerprints.",
    )
    parser.add_argument(
        "--dcp",
        type=Path,
        required=True,
        help="Private local path to the single predeclared input DCP.",
    )
    parser.add_argument(
        "--run-dir",
        type=Path,
        required=True,
        help="Private local path to a new or empty evidence directory.",
    )
    parser.add_argument(
        "--vivado-exec",
        type=Path,
        required=True,
        help="Private local path to the Vivado executable selected for this DCP.",
    )
    parser.add_argument(
        "--python-exec",
        type=Path,
        required=True,
        help="Private local path to the project Python from the sourced environment.",
    )
    return parser


def run_dof_preflight_entry(
    args: argparse.Namespace,
    *,
    runner: PreflightRunner | None = None,
) -> int:
    """Validate the CLI boundary, dispatch once, and emit only public JSON."""

    bindings = {
        "plan": _required_path(args, "plan"),
        "dcp_path": _required_path(args, "dcp"),
        "run_dir": _required_path(args, "run_dir"),
        "vivado_exec": _required_path(args, "vivado_exec"),
        "python_exec": _required_path(args, "python_exec"),
    }
    if runner is None:
        from optimizer_pipeline.dof.preflight import run_l0_real_preflight

        runner = run_l0_real_preflight
    summary = runner(
        bindings["plan"],
        dcp_path=bindings["dcp_path"],
        run_dir=bindings["run_dir"],
        vivado_exec=bindings["vivado_exec"],
        python_exec=bindings["python_exec"],
    )
    if not isinstance(summary, Mapping):
        raise TypeError("preflight runner must return a public JSON object")
    endpoint_success = summary.get("endpoint_success")
    if not isinstance(endpoint_success, bool):
        raise TypeError("preflight runner result requires boolean endpoint_success")
    print(
        json.dumps(
            dict(summary),
            indent=2,
            sort_keys=True,
            ensure_ascii=False,
            allow_nan=False,
        )
    )
    return 0 if endpoint_success else 1


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_dof_preflight_parser()
    args = parser.parse_args(argv)
    try:
        return run_dof_preflight_entry(args)
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        endpoint = getattr(exc, "endpoint", None)
        label = (
            str(endpoint)
            if isinstance(endpoint, str) and endpoint
            else type(exc).__name__
        )
        print(f"fpga-dof-preflight: {label}", file=sys.stderr)
        return 2


def _required_path(args: argparse.Namespace, name: str) -> Path:
    value = getattr(args, name, None)
    if value is None:
        raise ValueError(f"missing required private launch binding: {name}")
    return Path(value)


__all__ = [
    "build_dof_preflight_parser",
    "main",
    "run_dof_preflight_entry",
]
