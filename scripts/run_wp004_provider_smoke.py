#!/usr/bin/env python3
"""Freeze or execute the WP-004A synthetic provider smoke."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys

if not sys.flags.isolated or not sys.flags.no_site:
    raise RuntimeError(
        "wp004a_provider_runner_requires_python_isolated_no_site_mode"
    )

REPO_ROOT = Path(__file__).resolve().parents[1]
_VENV_ROOT = Path(sys.executable).absolute().parent.parent
_VENV_SITE_PACKAGES = (
    _VENV_ROOT
    / "lib"
    / f"python{sys.version_info.major}.{sys.version_info.minor}"
    / "site-packages"
)
if not _VENV_SITE_PACKAGES.is_dir():
    raise RuntimeError("wp004a_provider_runner_site_packages_unavailable")
sys.path[:] = [
    entry
    for entry in sys.path
    if Path(entry or os.curdir).resolve() != REPO_ROOT
]
sys.path.insert(0, str(REPO_ROOT))
# Add exactly the interpreter-local package directory as a plain import root.
# Unlike ``site`` initialization, this does not execute sitecustomize or .pth
# hooks before the runtime distribution manifest and origin checks run.
sys.path.append(str(_VENV_SITE_PACKAGES))

from optimizer_pipeline.dof import provider_smoke as _provider_smoke_module
from optimizer_pipeline.dof.provider_smoke import (
    execute_provider_smoke,
    freeze_provider_smoke_plan,
    load_provider_smoke_plan,
    write_provider_smoke_plan,
)

if Path(_provider_smoke_module.__file__).resolve() != (
    REPO_ROOT / "optimizer_pipeline/dof/provider_smoke.py"
).resolve():
    raise RuntimeError("wp004a_provider_runner_import_origin_mismatch")

# All project modules needed by the runner are now imported.  Remove the
# repository and script directories before any third-party provider runtime
# module is resolved, so a local httpx/openai shadow cannot receive the key.
_LOCAL_IMPORT_ROOTS = {REPO_ROOT, Path(__file__).resolve().parent}
sys.path[:] = [
    entry
    for entry in sys.path
    if Path(entry or os.curdir).resolve() not in _LOCAL_IMPORT_ROOTS
]


def main() -> int:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    freeze = subparsers.add_parser("freeze")
    freeze.add_argument("--repository", type=Path, default=REPO_ROOT)
    freeze.add_argument("--output", type=Path, required=True)
    execute = subparsers.add_parser("execute")
    execute.add_argument("--plan", type=Path, required=True)
    execute.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()

    if args.command == "freeze":
        plan = freeze_provider_smoke_plan(args.repository)
        write_provider_smoke_plan(plan, args.output)
        print(json.dumps({"status": "FROZEN", "plan_sha256": plan["plan_sha256"]}))
        return 0
    result = execute_provider_smoke(
        load_provider_smoke_plan(args.plan),
        args.output_dir,
    )
    print(
        json.dumps(
            {
                key: result[key]
                for key in (
                    "endpoint",
                    "transport_requests",
                    "physical_transport_requests",
                    "backend_dispatches",
                )
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
