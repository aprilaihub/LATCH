#!/usr/bin/env python3
"""Freeze and audit Protocol E1-v1 without hidden defaults."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
import sys

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from optimizer_pipeline.dof.e1 import E1_BENCHMARKS, e1_protocol_manifest
from optimizer_pipeline.dof.e1_audit import assemble_e1_root_audit, audit_e1_roots
from optimizer_pipeline.dof.e1_job import (
    execute_real_e1_job,
    freeze_e1_job_bundle,
    validate_e1_job_bundle,
)
from optimizer_pipeline.dof.openrouter import (
    discover_openrouter_endpoints,
    discover_openrouter_models,
)


def _write_new(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as stream:
        stream.write(json.dumps(value, sort_keys=True, indent=2).encode() + b"\n")


def _roots(directory: Path) -> dict[str, Path]:
    filenames = {
        "amd_mini_isp": "amd_mini-isp_2025.1.dcp",
        "boom_soc": "boom_soc_2025.1.dcp",
        "corescore_500_mod": "corescore_500_mod_2025.1.dcp",
        "finn_radioml": "finn_radioml_2025.1.dcp",
        "logicnets_jscl": "logicnets_jscl_2025.1.dcp",
        "rosetta_digit-recognition": "rosetta_digit-recognition_2025.1.dcp",
        "rosetta_optical-flow": "rosetta_optical-flow_2025.1.dcp",
        "vexriscv_re-place": "vexriscv_re-place_2025.1.dcp",
    }
    roots = {name: (directory / filenames[name]).resolve() for name in E1_BENCHMARKS}
    missing = [name for name, path in roots.items() if not path.is_file()]
    if missing:
        raise SystemExit("missing E1 roots: " + ",".join(missing))
    return roots


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    freeze = sub.add_parser("freeze")
    freeze.add_argument("--output", type=Path, required=True)
    discover = sub.add_parser("discover-openrouter")
    discover.add_argument("--output", type=Path, required=True)
    audit = sub.add_parser("audit-roots")
    audit.add_argument("--roots-dir", type=Path, required=True)
    audit.add_argument("--vivado-exec", type=Path, required=True)
    audit.add_argument("--output", type=Path, required=True)
    assemble = sub.add_parser("assemble-root-audit")
    assemble.add_argument("--scores-dir", type=Path, required=True)
    assemble.add_argument("--output", type=Path, required=True)
    freeze_bundle = sub.add_parser("freeze-bundle")
    freeze_bundle.add_argument("--roots-dir", type=Path, required=True)
    freeze_bundle.add_argument("--root-audit", type=Path, required=True)
    freeze_bundle.add_argument("--provider-discovery", type=Path, required=True)
    freeze_bundle.add_argument("--fake-attestation", type=Path, required=True)
    freeze_bundle.add_argument("--output", type=Path, required=True)
    run_job = sub.add_parser("run-job")
    run_job.add_argument("--bundle", type=Path, required=True)
    run_job.add_argument("--job-id", required=True)
    run_job.add_argument("--roots-dir", type=Path, required=True)
    run_job.add_argument("--vivado-exec", type=Path, required=True)
    run_job.add_argument("--python-exec", type=Path, required=True)
    run_job.add_argument("--output", type=Path, required=True)
    run_job.add_argument("--remaining-program-cost-usd", type=float, required=True)
    args = parser.parse_args()
    repository = REPOSITORY_ROOT
    if args.command == "freeze":
        _write_new(args.output, e1_protocol_manifest())
        return 0
    if args.command == "discover-openrouter":
        key = os.environ.get("OPENROUTER_API_KEY")
        if not key:
            raise SystemExit("OPENROUTER_API_KEY unavailable")
        models = asyncio.run(discover_openrouter_models(key))
        endpoints = asyncio.run(discover_openrouter_endpoints(key))
        evidence = {
            "schema_version": "dof.e1-openrouter-discovery-bundle.v1",
            "models": models,
            "endpoints": endpoints,
            "all_profiles_available": (
                models["all_required_available"]
                and endpoints["all_profiles_available"]
            ),
        }
        _write_new(args.output, evidence)
        return 0 if evidence["all_profiles_available"] else 2
    if args.command == "freeze-bundle":
        root_audit = json.loads(args.root_audit.read_text())
        provider_discovery = json.loads(args.provider_discovery.read_text())
        bundle = freeze_e1_job_bundle(
            _roots(args.roots_dir),
            root_audit,
            provider_discovery,
            repository_anchor=repository,
            fake_startup_attestation=args.fake_attestation,
        )
        _write_new(args.output, bundle)
        return 0
    if args.command == "assemble-root-audit":
        observations = {
            benchmark: json.loads(
                (args.scores_dir / benchmark / "score.json").read_text()
            )
            for benchmark in E1_BENCHMARKS
        }
        audit_result = assemble_e1_root_audit(observations)
        _write_new(args.output, audit_result)
        return 0 if audit_result["all_accepted"] else 3
    if args.command == "run-job":
        bundle = validate_e1_job_bundle(json.loads(args.bundle.read_text()))
        matches = [
            spec for spec in bundle["jobs"] if spec["job"]["job_id"] == args.job_id
        ]
        if len(matches) != 1:
            raise SystemExit("job-id not unique in bundle")
        spec = matches[0]
        execute_real_e1_job(
            spec,
            _roots(args.roots_dir)[spec["job"]["benchmark"]],
            args.output,
            vivado_exec=args.vivado_exec,
            python_exec=args.python_exec,
            repository_anchor=repository,
            scorer_script=repository / "scripts" / "e1_score_dcp.tcl",
            fake_startup_attestation_sha256=bundle["freeze"][
                "fake_startup_attestation_evidence_sha256"
            ],
            remaining_program_cost_usd=args.remaining_program_cost_usd,
        )
        return 0
    result = audit_e1_roots(
        _roots(args.roots_dir),
        vivado_exec=args.vivado_exec,
        scorer_script=repository / "scripts" / "e1_score_dcp.tcl",
        output_dir=args.output,
    )
    return 0 if result["all_accepted"] else 3


if __name__ == "__main__":
    raise SystemExit(main())
