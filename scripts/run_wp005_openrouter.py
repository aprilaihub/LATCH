#!/usr/bin/env python3
"""Isolated OpenRouter control entry for WP-005 discovery and preflight."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
import sys


if not sys.flags.isolated or not sys.flags.no_site:
    raise RuntimeError("wp005_openrouter_runner_requires_python_isolated_no_site_mode")

REPO_ROOT = Path(__file__).resolve().parents[1]
_VENV_ROOT = Path(sys.executable).absolute().parent.parent
_VENV_SITE_PACKAGES = (
    _VENV_ROOT
    / "lib"
    / f"python{sys.version_info.major}.{sys.version_info.minor}"
    / "site-packages"
)
if not _VENV_SITE_PACKAGES.is_dir():
    raise RuntimeError("wp005_openrouter_runner_site_packages_unavailable")
sys.path.insert(0, str(REPO_ROOT))
sys.path.append(str(_VENV_SITE_PACKAGES))

from optimizer_pipeline.dof import openrouter as _openrouter_module
from optimizer_pipeline.dof.openrouter import (
    OPENROUTER_CREDENTIAL_ENV,
    discover_openrouter_models,
    write_discovery_evidence,
)
from optimizer_pipeline.dof.e0 import run_synthetic_openrouter_preflight

if Path(_openrouter_module.__file__).resolve() != (
    REPO_ROOT / "optimizer_pipeline/dof/openrouter.py"
).resolve():
    raise RuntimeError("wp005_openrouter_runner_import_origin_mismatch")

# The provider dependency is resolved only after local import roots are gone.
_LOCAL_ROOTS = {REPO_ROOT, Path(__file__).resolve().parent}
sys.path[:] = [
    entry
    for entry in sys.path
    if Path(entry or os.curdir).resolve() not in _LOCAL_ROOTS
]


def main() -> int:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    discover = subparsers.add_parser("discover-models")
    discover.add_argument("--output", type=Path, required=True)
    preflight = subparsers.add_parser("synthetic-preflight")
    preflight.add_argument("--model", required=True)
    preflight.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()

    if args.command == "discover-models":
        credential = os.environ.get(OPENROUTER_CREDENTIAL_ENV)
        if not credential:
            raise RuntimeError("openrouter_credential_unavailable")
        evidence = asyncio.run(discover_openrouter_models(credential))
        write_discovery_evidence(evidence, args.output)
        print(
            json.dumps(
                {
                    "all_required_available": evidence["all_required_available"],
                    "required_models": evidence["required_models"],
                    "physical_requests": evidence["physical_requests"],
                },
                sort_keys=True,
            )
        )
        return 0 if evidence["all_required_available"] else 3
    if args.command == "synthetic-preflight":
        credential = os.environ.get(OPENROUTER_CREDENTIAL_ENV)
        if not credential:
            raise RuntimeError("openrouter_credential_unavailable")
        result = run_synthetic_openrouter_preflight(
            args.model,
            args.output_dir,
            credential=credential,
        )
        print(
            json.dumps(
                {
                    "model": result["model"],
                    "endpoint": result["endpoint"],
                    "transport_evidence_valid": result[
                        "transport_evidence_valid"
                    ],
                    "provider_decision_accepted": result[
                        "provider_decision_accepted"
                    ],
                },
                sort_keys=True,
            )
        )
        return 0 if result["transport_evidence_valid"] else 4
    raise AssertionError(args.command)


if __name__ == "__main__":
    raise SystemExit(main())
