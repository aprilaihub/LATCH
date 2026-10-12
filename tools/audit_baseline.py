#!/usr/bin/env python3
"""Verify the immutable refactoring baseline without exposing local paths."""

from __future__ import annotations

import argparse
import importlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from typing import Sequence


BASELINE_COMMIT = "1320c6612ea90db58325ea7048dba0ad6f41e41f"
EXPECTED_SUBMODULES = {
    "RapidWright": "2fc8b6aec859c20c04218573f9dda09db0a72e3f",
    "RapidWright/interchange/fpga-interchange-schema": "c985b4648e66414b250261c1ba4cbe45a2971b1c",
    "RapidWright/test/RapidWrightDCP": "f9625fc62d290926668c4955c3a76e9d2044e916",
}
EXPECTED_VIVADO_RELEASE = "2025.1"


def run(command: Sequence[str], *, cwd: Path) -> str:
    result = subprocess.run(
        command,
        cwd=cwd,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    if result.returncode:
        rendered = " ".join(command)
        raise RuntimeError(f"command failed ({result.returncode}): {rendered}\n{result.stdout.strip()}")
    return result.stdout.rstrip()


def parse_submodule_status(output: str) -> dict[str, str]:
    statuses: dict[str, str] = {}
    for line in output.splitlines():
        if not line:
            continue
        prefix = line[0]
        fields = line[1:].strip().split()
        if len(fields) < 2:
            raise RuntimeError(f"unrecognized submodule status: {line!r}")
        if prefix != " ":
            reason = "not initialized" if prefix == "-" else "not at locked commit"
            raise RuntimeError(f"submodule {fields[1]} is {reason}")
        statuses[fields[1]] = fields[0]
    return statuses


def git_evidence(repo: Path, *, require_clean: bool) -> dict[str, object]:
    head = run(["git", "rev-parse", "HEAD"], cwd=repo)
    run(["git", "merge-base", "--is-ancestor", BASELINE_COMMIT, head], cwd=repo)

    statuses = parse_submodule_status(
        run(["git", "submodule", "status", "--recursive"], cwd=repo)
    )
    if statuses != EXPECTED_SUBMODULES:
        raise RuntimeError(
            "submodule lock mismatch: "
            + json.dumps({"expected": EXPECTED_SUBMODULES, "actual": statuses}, sort_keys=True)
        )

    if require_clean:
        dirty = run(["git", "status", "--porcelain", "--untracked-files=no"], cwd=repo)
        if dirty:
            raise RuntimeError("tracked worktree changes are present")

    return {
        "baseline_commit": BASELINE_COMMIT,
        "head_commit": head,
        "baseline_is_ancestor": True,
        "submodules": statuses,
        "tracked_worktree_clean": True if require_clean else None,
    }


def toolchain_evidence(repo: Path) -> dict[str, object]:
    configured_python = os.environ.get("PYTHON")
    if not configured_python:
        raise RuntimeError("PYTHON is unset; configure an explicit isolated interpreter")
    if Path(configured_python).resolve() != Path(sys.executable).resolve():
        raise RuntimeError("audit is not running with the Python selected by .env")

    importlib.import_module("mcp")

    vivado_exec = os.environ.get("VIVADO_EXEC")
    if not vivado_exec or not os.access(vivado_exec, os.X_OK):
        raise RuntimeError("VIVADO_EXEC is unset or not executable")
    vivado_output = run([vivado_exec, "-version"], cwd=repo)
    match = re.search(r"vivado v([0-9.]+)", vivado_output, flags=re.IGNORECASE)
    if not match:
        raise RuntimeError("could not parse Vivado version")
    vivado_release = match.group(1)
    if vivado_release != EXPECTED_VIVADO_RELEASE:
        raise RuntimeError(
            f"Vivado release mismatch: expected {EXPECTED_VIVADO_RELEASE}, got {vivado_release}"
        )

    rapidwright_path = os.environ.get("RAPIDWRIGHT_PATH")
    if not rapidwright_path or not Path(rapidwright_path).is_dir():
        raise RuntimeError("RAPIDWRIGHT_PATH is unset or not a directory")
    rapidwright_commit = run(["git", "rev-parse", "HEAD"], cwd=Path(rapidwright_path))
    if rapidwright_commit != EXPECTED_SUBMODULES["RapidWright"]:
        raise RuntimeError("RAPIDWRIGHT_PATH does not point at the locked RapidWright commit")
    configured_jars = Path(rapidwright_path) / "jars"
    configured_bin = Path(rapidwright_path) / "bin"
    if not configured_jars.is_dir() or not any(configured_jars.glob("*.jar")) or not configured_bin.is_dir():
        raise RuntimeError("RAPIDWRIGHT_PATH is locked but its jars/bin runtime has not been built")

    repository_rapidwright = repo / "RapidWright"
    repository_commit = run(["git", "rev-parse", "HEAD"], cwd=repository_rapidwright)
    repository_jars = repository_rapidwright / "jars"
    repository_bin = repository_rapidwright / "bin"
    if repository_commit != EXPECTED_SUBMODULES["RapidWright"]:
        raise RuntimeError("repository RapidWright submodule is not at the locked commit")
    if not repository_jars.is_dir() or not any(repository_jars.glob("*.jar")) or not repository_bin.is_dir():
        raise RuntimeError("repository RapidWright submodule is locked but its jars/bin runtime has not been built")

    return {
        "python_version": sys.version.split()[0],
        "mcp_import": "ok",
        "vivado_release": vivado_release,
        "rapidwright_commit": rapidwright_commit,
        "configured_rapidwright_runtime": "ready",
        "repository_rapidwright_runtime": "ready",
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--repo",
        type=Path,
        default=Path(__file__).resolve().parents[1],
        help="Repository root (defaults to the parent of tools/).",
    )
    parser.add_argument(
        "--require-clean",
        action="store_true",
        help="Fail when tracked worktree changes are present.",
    )
    parser.add_argument(
        "--toolchain",
        action="store_true",
        help="Also verify the sourced Python, MCP, Vivado, and RapidWright environment.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        evidence = {"git": git_evidence(args.repo.resolve(), require_clean=args.require_clean)}
        if args.toolchain:
            evidence["toolchain"] = toolchain_evidence(args.repo.resolve())
    except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
        print(f"baseline audit failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(evidence, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
