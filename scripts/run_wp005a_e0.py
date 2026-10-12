#!/usr/bin/env python3
"""Frozen serial entry for WP-005A bound E0 jobs."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from optimizer_pipeline.dof.e0_job import (
    execute_real_e0_job,
    execute_real_e0_matrix,
    freeze_e0_job_bundle,
    validate_e0_job_bundle,
)


def _roots(args: argparse.Namespace) -> dict[str, Path]:
    return {
        "vexriscv_re-place": args.vexriscv_root.resolve(),
        "logicnets_jscl": args.logicnets_root.resolve(),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    freeze = commands.add_parser("freeze")
    execute = commands.add_parser("execute-matrix")
    one = commands.add_parser("execute-job")
    for item in (freeze, execute):
        item.add_argument("--vexriscv-root", type=Path, required=True)
        item.add_argument("--logicnets-root", type=Path, required=True)
    freeze.add_argument("--repository", type=Path, default=REPO_ROOT)
    freeze.add_argument("--fake-startup-attestation", type=Path, required=True)
    freeze.add_argument("--output", type=Path, required=True)
    execute.add_argument("--repository", type=Path, default=REPO_ROOT)
    execute.add_argument("--python-exec", type=Path, required=True)
    execute.add_argument("--vivado-exec", type=Path, required=True)
    execute.add_argument("--bundle", type=Path, required=True)
    execute.add_argument("--output-dir", type=Path, required=True)
    execute.add_argument("--initial-program-cost-usd", type=float, default=0.0057207)
    one.add_argument("--repository", type=Path, default=REPO_ROOT)
    one.add_argument("--python-exec", type=Path, required=True)
    one.add_argument("--vivado-exec", type=Path, required=True)
    one.add_argument("--bundle", type=Path, required=True)
    one.add_argument("--job-id", required=True)
    one.add_argument("--root-dcp", type=Path, required=True)
    one.add_argument("--output-dir", type=Path, required=True)
    one.add_argument("--remaining-program-cost-usd", type=float, required=True)
    args = parser.parse_args()
    if args.command == "freeze":
        payload = freeze_e0_job_bundle(
            _roots(args),
            repository_anchor=args.repository,
            fake_startup_attestation=args.fake_startup_attestation,
        )
        if args.output.exists():
            raise FileExistsError(args.output)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_bytes(json.dumps(payload, sort_keys=True, indent=2).encode() + b"\n")
        print(json.dumps({"jobs": len(payload["jobs"]), "bundle_sha256": payload["bundle_sha256"]}))
        return 0
    bundle = validate_e0_job_bundle(json.loads(args.bundle.read_text()))
    if args.command == "execute-matrix":
        result = execute_real_e0_matrix(
            bundle,
            _roots(args),
            args.output_dir,
            vivado_exec=args.vivado_exec,
            python_exec=args.python_exec,
            repository_anchor=args.repository,
            initial_program_cost_usd=args.initial_program_cost_usd,
            credential=os.environ.get("OPENROUTER_API_KEY"),
        )
        print(json.dumps({"jobs_completed": result["jobs_completed"], "fresh_provider_requests": result["fresh_provider_requests"], "combined_cost_usd": result["combined_wp005_wp005a_cost_usd"]}))
        return 0
    if args.command == "execute-job":
        matches = [item for item in bundle["jobs"] if item["job"]["job_id"] == args.job_id]
        if len(matches) != 1:
            raise ValueError("exact_job_id_required")
        result = execute_real_e0_job(
            matches[0],
            args.root_dcp,
            args.output_dir,
            vivado_exec=args.vivado_exec,
            python_exec=args.python_exec,
            repository_anchor=args.repository,
            fake_startup_attestation_sha256=bundle["protocol"]["fake_startup_attestation_evidence_sha256"],
            remaining_program_cost_usd=args.remaining_program_cost_usd,
            credential=os.environ.get("OPENROUTER_API_KEY"),
        )
        print(json.dumps({"job_id": result["job"]["job_id"], "endpoint": result["endpoint"], "heavy_calls": result["heavy_calls"], "model_requests": result["model_requests"]}))
        return 0
    raise AssertionError(args.command)


if __name__ == "__main__":
    raise SystemExit(main())
