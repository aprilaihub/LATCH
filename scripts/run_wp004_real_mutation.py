#!/usr/bin/env python3
"""Freeze or execute the exactly-once WP-004A real mutation smoke."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path[:] = [
    entry
    for entry in sys.path
    if Path(entry or os.curdir).resolve() != REPO_ROOT
]
sys.path.insert(0, str(REPO_ROOT))

from optimizer_pipeline.dof import mutation_worker_binding as _binding_module
from optimizer_pipeline.dof import real_mutation as _real_mutation_module
from optimizer_pipeline.dof.real_mutation import (
    execute_real_mutation,
    freeze_real_mutation_plan,
    load_real_mutation_plan,
    write_frozen_plan,
)

if Path(_real_mutation_module.__file__).resolve() != (
    REPO_ROOT / "optimizer_pipeline/dof/real_mutation.py"
).resolve() or Path(_binding_module.__file__).resolve() != (
    REPO_ROOT / "optimizer_pipeline/dof/mutation_worker_binding.py"
).resolve():
    raise RuntimeError("wp004a_real_runner_import_origin_mismatch")
from optimizer_pipeline.dof.mutation_worker_binding import (
    verified_fake_attestation_evidence_sha256,
)


def main() -> int:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    freeze = subparsers.add_parser("freeze")
    freeze.add_argument("--root-dcp", type=Path, required=True)
    freeze.add_argument("--repository", type=Path, default=REPO_ROOT)
    freeze.add_argument("--output", type=Path, required=True)
    freeze.add_argument("--run-dir", type=Path, required=True)
    freeze.add_argument("--vivado-exec", type=Path, required=True)
    freeze.add_argument("--python-exec", type=Path, required=True)
    freeze.add_argument("--fake-startup-attestation", type=Path, required=True)
    execute = subparsers.add_parser("execute")
    execute.add_argument("--plan", type=Path, required=True)
    execute.add_argument("--root-dcp", type=Path, required=True)
    execute.add_argument("--repository", type=Path, default=REPO_ROOT)
    execute.add_argument("--run-dir", type=Path, required=True)
    execute.add_argument("--vivado-exec", type=Path, default=None)
    execute.add_argument("--python-exec", type=Path, default=None)
    args = parser.parse_args()

    if args.command == "freeze":
        plan = freeze_real_mutation_plan(
            args.root_dcp,
            repository_anchor=args.repository,
            python_exec=args.python_exec,
            vivado_exec=args.vivado_exec,
            run_dir=args.run_dir,
            fake_startup_attestation_sha256=(
                verified_fake_attestation_evidence_sha256(
                    args.fake_startup_attestation
                )
            ),
        )
        write_frozen_plan(plan, args.output)
        print(json.dumps({"status": "FROZEN", "plan_sha256": plan["plan_sha256"]}))
        return 0

    vivado = args.vivado_exec or os.environ.get("VIVADO_EXEC")
    python = args.python_exec or os.environ.get("PYTHON")
    if not vivado or not python:
        parser.error("execute requires --vivado-exec/--python-exec or VIVADO_EXEC/PYTHON")
    result = execute_real_mutation(
        load_real_mutation_plan(args.plan),
        args.root_dcp,
        args.run_dir,
        vivado_exec=vivado,
        python_exec=python,
        repository_anchor=args.repository,
    )
    print(
        json.dumps(
            {
                "endpoint": result["endpoint"],
                "real_mutation_invocations": result["real_mutation_invocations"],
                "root_unchanged": result["root_unchanged"],
                "plan_sha256": result["plan_sha256"],
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
