#!/usr/bin/env python3
"""Small serial control entry for the WP-005 E0 matrix."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import sys


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from optimizer_pipeline.dof.contracts import DoFLevel
from optimizer_pipeline.dof.e0 import (
    e0_action_variants,
    e0_matrix_manifest,
    run_fake_e0_matrix,
)
from optimizer_pipeline.dof.mutation_worker_binding import (
    verified_fake_attestation_evidence_sha256,
)
from optimizer_pipeline.dof.preflight import (
    freeze_e0_preflight_plan,
    run_l0_real_preflight,
    write_e0_preflight_plan,
)
from optimizer_pipeline.dof.real_mutation import (
    execute_e0_real_action,
    freeze_e0_real_action_plan,
    load_e0_real_action_plan,
    write_e0_frozen_plan,
)


def main() -> int:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    matrix = subparsers.add_parser("matrix")
    matrix.add_argument("--output", type=Path, required=True)
    fake = subparsers.add_parser("fake-run")
    fake.add_argument("--output-dir", type=Path, required=True)
    freeze = subparsers.add_parser("freeze-plans")
    freeze.add_argument("--vexriscv-root", type=Path, required=True)
    freeze.add_argument("--logicnets-root", type=Path, required=True)
    freeze.add_argument("--repository", type=Path, default=REPO_ROOT)
    freeze.add_argument("--python-exec", type=Path, required=True)
    freeze.add_argument("--vivado-exec", type=Path, required=True)
    freeze.add_argument("--fake-startup-attestation", type=Path, required=True)
    freeze.add_argument("--model-discovery", type=Path, required=True)
    freeze.add_argument("--planned-run-root", type=Path, required=True)
    freeze.add_argument("--output-dir", type=Path, required=True)
    l0 = subparsers.add_parser("execute-l0")
    l0.add_argument("--plan", type=Path, required=True)
    l0.add_argument("--root-dcp", type=Path, required=True)
    l0.add_argument("--run-dir", type=Path, required=True)
    l0.add_argument("--repository", type=Path, default=REPO_ROOT)
    l0.add_argument("--python-exec", type=Path, required=True)
    l0.add_argument("--vivado-exec", type=Path, required=True)
    action = subparsers.add_parser("execute-action")
    action.add_argument("--plan", type=Path, required=True)
    action.add_argument("--dispatch-authorization", type=Path, required=True)
    action.add_argument("--causal-evidence", type=Path, required=True)
    action.add_argument("--root-dcp", type=Path, required=True)
    action.add_argument("--run-dir", type=Path, required=True)
    action.add_argument("--repository", type=Path, default=REPO_ROOT)
    action.add_argument("--python-exec", type=Path, required=True)
    action.add_argument("--vivado-exec", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "matrix":
        payload = e0_matrix_manifest()
        args.output.parent.mkdir(parents=True, exist_ok=True)
        if args.output.exists():
            raise FileExistsError(args.output)
        args.output.write_bytes(json.dumps(payload, sort_keys=True, indent=2).encode() + b"\n")
        print(json.dumps({"jobs": len(payload["jobs"]), "matrix_sha256": payload["matrix_sha256"]}))
        return 0
    if args.command == "fake-run":
        result = run_fake_e0_matrix(args.output_dir)
        print(json.dumps({"jobs_run": result["jobs_run"], "jobs_passed": result["jobs_passed"]}))
        return 0
    if args.command == "freeze-plans":
        discovery = json.loads(args.model_discovery.read_text())
        if discovery.get("all_required_available") is not True:
            raise RuntimeError("MODEL_AVAILABILITY_BLOCKER")
        output = args.output_dir.resolve()
        output.mkdir(parents=True, exist_ok=False)
        attestation_hash = verified_fake_attestation_evidence_sha256(
            args.fake_startup_attestation
        )
        roots = {
            "vexriscv_re-place": args.vexriscv_root.resolve(),
            "logicnets_jscl": args.logicnets_root.resolve(),
        }
        manifest = e0_matrix_manifest()
        plan_rows = []
        for benchmark, root in roots.items():
            l0_plan = freeze_e0_preflight_plan(
                root,
                dcp_pseudonym=benchmark,
                run_id=f"e0-{benchmark}-l0",
                repository_anchor=args.repository,
            )
            l0_uri = Path("plans") / benchmark / "l0.json"
            write_e0_preflight_plan(l0_plan, output / l0_uri)
            plan_rows.append(
                {
                    "job_id": f"{benchmark}__l0",
                    "kind": "l0",
                    "uri": l0_uri.as_posix(),
                    "plan_sha256": l0_plan.spec_hash,
                }
            )
            jobs = [job for job in manifest["jobs"] if job["benchmark"] == benchmark and job["level"] != "L0"]
            for job in jobs:
                level = DoFLevel(job["level"])
                variants = (
                    [row for row in e0_action_variants(level) if row["action_key"] == "macro.phys_opt"]
                    if level is DoFLevel.L1
                    else e0_action_variants(level)
                )
                for variant in variants:
                    token = variant["variant_sha256"][:16]
                    relative = Path("plans") / benchmark / job["job_id"] / f"{token}.json"
                    planned_run = args.planned_run_root.resolve() / job["job_id"] / token
                    plan = freeze_e0_real_action_plan(
                        root,
                        root_pseudonym=benchmark,
                        level=level,
                        action_key=variant["action_key"],
                        parameters=variant["parameters"],
                        run_id=("e0-" + hashlib.sha256((job["job_id"] + token).encode()).hexdigest()[:24]),
                        repository_anchor=args.repository,
                        python_exec=args.python_exec,
                        vivado_exec=args.vivado_exec,
                        run_dir=planned_run,
                        fake_startup_attestation_sha256=attestation_hash,
                    )
                    write_e0_frozen_plan(plan, output / relative)
                    plan_rows.append(
                        {
                            "job_id": job["job_id"],
                            "kind": "action",
                            "level": level.value,
                            "model": job["model"],
                            "action_key": variant["action_key"],
                            "parameters": variant["parameters"],
                            "variant_sha256": variant["variant_sha256"],
                            "uri": relative.as_posix(),
                            "plan_sha256": plan["plan_sha256"],
                            "planned_run_id": plan["run_id"],
                        }
                    )
        payload = {
            "schema_version": "dof.e0-frozen-execution-bundle.v1",
            "matrix": manifest,
            "model_discovery_sha256": hashlib.sha256(args.model_discovery.read_bytes()).hexdigest(),
            "fake_startup_attestation_evidence_sha256": attestation_hash,
            "plans": plan_rows,
        }
        payload["bundle_sha256"] = hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        (output / "e0-frozen-execution-bundle.json").write_bytes(
            json.dumps(payload, sort_keys=True, indent=2).encode() + b"\n"
        )
        print(json.dumps({"plans": len(plan_rows), "bundle_sha256": payload["bundle_sha256"]}))
        return 0
    if args.command == "execute-l0":
        result = run_l0_real_preflight(
            args.plan,
            dcp_path=args.root_dcp,
            run_dir=args.run_dir,
            vivado_exec=args.vivado_exec,
            python_exec=args.python_exec,
            repository_anchor=args.repository,
        )
        print(json.dumps({"endpoint": result["endpoint"], "root_unchanged": result["root_unchanged"]}))
        return 0
    if args.command == "execute-action":
        result = execute_e0_real_action(
            load_e0_real_action_plan(args.plan),
            args.root_dcp,
            args.run_dir,
            dispatch_authorization=json.loads(args.dispatch_authorization.read_text()),
            causal_evidence=json.loads(args.causal_evidence.read_text()),
            vivado_exec=args.vivado_exec,
            python_exec=args.python_exec,
            repository_anchor=args.repository,
        )
        print(json.dumps({"endpoint": result["endpoint"], "root_unchanged": result["root_unchanged"]}))
        return 0
    raise AssertionError(args.command)


if __name__ == "__main__":
    raise SystemExit(main())
