#!/usr/bin/env python3
"""Freeze, run, stop, and aggregate the bounded WP-007 E1-v2 protocol."""

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

from optimizer_pipeline.dof.artifacts import fingerprint_file
from optimizer_pipeline.dof.catalogues import nested_capability_audit
from optimizer_pipeline.dof.common_observation import common_observation_equality_audit
from optimizer_pipeline.dof.e1_audit import run_readonly_score
from optimizer_pipeline.dof.e1v2 import (
    E0V2_MATRIX,
    E1V2_REPEAT1_MATRIX,
    WP012_H_MATRIX,
    WP012_K_MATRIX,
    WP012_U_MATRIX,
    content_hash,
    expansion_inheritance_audit,
    protocol_manifest,
)

MATRICES = (
    E0V2_MATRIX,
    E1V2_REPEAT1_MATRIX,
    WP012_K_MATRIX,
    WP012_U_MATRIX,
    WP012_H_MATRIX,
)
from optimizer_pipeline.dof.e1v2_job import (
    execute_real_e1v2_job,
    freeze_e1v2_job_bundle,
    validate_e1v2_job_bundle,
)
from optimizer_pipeline.dof.job_queue import CompleteJobQueue, QueueJob, aggregate_trajectory_results
from optimizer_pipeline.dof.openrouter import (
    discover_openrouter_endpoints,
    discover_openrouter_models,
    verify_openrouter_unknown_cost_reserve,
)


def _read(path: Path) -> dict:
    value = json.loads(path.read_text())
    if not isinstance(value, dict):
        raise SystemExit(f"JSON object required: {path}")
    return value


def _write_new(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as stream:
        stream.write(json.dumps(value, sort_keys=True, indent=2).encode() + b"\n")


def _roots(path: Path) -> dict[str, Path]:
    value = _read(path)
    raw = value.get("roots", value)
    if not isinstance(raw, dict) or not raw:
        raise SystemExit("roots manifest must contain a non-empty roots object")
    roots = {str(name): Path(location).resolve() for name, location in raw.items()}
    missing = [name for name, location in roots.items() if not location.is_file()]
    if missing:
        raise SystemExit("missing roots: " + ",".join(missing))
    return roots


def _absolute_preserving_symlink(path: Path) -> str:
    """Normalize a CLI path without replacing a virtualenv launcher symlink."""

    return os.path.abspath(os.fspath(path))


def _assemble_root_audit(roots: dict[str, Path], scores_dir: Path) -> dict:
    rows = []
    for name, root in roots.items():
        score = _read(scores_dir / name / "score.json")
        observed = fingerprint_file(root)
        identity = score.get("artifact") == {
            "sha256": observed.sha256,
            "size_bytes": observed.size_bytes,
        }
        accepted = bool(identity and score.get("route", {}).get("is_fully_routed") is True)
        rows.append(
            {
                "benchmark": name,
                "artifact": score["artifact"],
                "score": score["score"],
                "route": score["route"],
                "validation_sha256": score["result_sha256"],
                "wall_seconds": score["wall_seconds"],
                "identity_accepted": identity,
                "accepted": accepted,
            }
        )
    core = {
        "schema_version": "dof.e1v2-root-scorer-audit.v1",
        "root_count": len(rows),
        "all_accepted": all(row["accepted"] for row in rows),
        "rows": rows,
    }
    return core | {"audit_sha256": content_hash(core)}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    manifest = sub.add_parser("manifest")
    manifest.add_argument(
        "--matrix", choices=MATRICES, required=True
    )
    manifest.add_argument("--roots-manifest", type=Path, required=True)
    manifest.add_argument("--output", type=Path, required=True)
    nested = sub.add_parser("nested-audit")
    nested.add_argument("--output", type=Path, required=True)
    observation = sub.add_parser("observation-audit")
    observation.add_argument("--source", type=Path, required=True)
    observation.add_argument("--artifact-sha256", required=True)
    observation.add_argument("--decision-index", type=int, default=0)
    observation.add_argument("--output", type=Path, required=True)
    discover = sub.add_parser("discover-openrouter")
    discover.add_argument("--output", type=Path, required=True)
    score = sub.add_parser("score-root")
    score.add_argument("--root", type=Path, required=True)
    score.add_argument("--vivado-exec", type=Path, required=True)
    score.add_argument("--output", type=Path, required=True)
    assemble = sub.add_parser("assemble-root-audit")
    assemble.add_argument("--roots-manifest", type=Path, required=True)
    assemble.add_argument("--scores-dir", type=Path, required=True)
    assemble.add_argument("--output", type=Path, required=True)
    freeze = sub.add_parser("freeze-bundle")
    freeze.add_argument(
        "--matrix", choices=MATRICES, required=True
    )
    freeze.add_argument("--roots-manifest", type=Path, required=True)
    freeze.add_argument("--root-audit", type=Path, required=True)
    freeze.add_argument("--provider-discovery", type=Path, required=True)
    freeze.add_argument("--fake-attestation", type=Path, required=True)
    freeze.add_argument("--output", type=Path, required=True)
    validate = sub.add_parser("validate-bundle")
    validate.add_argument("--bundle", type=Path, required=True)
    run_job = sub.add_parser("run-job")
    run_job.add_argument("--bundle", type=Path, required=True)
    run_job.add_argument("--job-id", required=True)
    run_job.add_argument("--roots-manifest", type=Path, required=True)
    run_job.add_argument("--vivado-exec", type=Path, required=True)
    run_job.add_argument("--python-exec", type=Path, required=True)
    run_job.add_argument("--output", type=Path)
    queue = sub.add_parser("run-queue")
    queue.add_argument("--bundle", type=Path, required=True)
    queue.add_argument("--roots-manifest", type=Path, required=True)
    queue.add_argument("--vivado-exec", type=Path, required=True)
    queue.add_argument("--python-exec", type=Path, required=True)
    queue.add_argument("--output-root", type=Path, required=True)
    queue.add_argument("--max-concurrency", type=int, default=16)
    queue.add_argument(
        "--repeat",
        type=int,
        choices=(2, 3),
        help="Run one frozen WP-012 K repeat as a distinct queue identity.",
    )
    queue.add_argument(
        "--cost-cap-usd",
        type=float,
        help="Queue guard; must not exceed the frozen phase aggregate cap.",
    )
    stop = sub.add_parser("stop")
    stop.add_argument("--output-root", type=Path, required=True)
    stop.add_argument("--reason", default="operator_stop")
    aggregate = sub.add_parser("aggregate")
    aggregate.add_argument("--output-root", type=Path, required=True)
    return parser


def main() -> int:
    args = _parser().parse_args()
    if args.command == "manifest":
        _write_new(args.output, protocol_manifest(matrix=args.matrix, roots=tuple(_roots(args.roots_manifest))))
        return 0
    if args.command == "nested-audit":
        payload = {
            "schema_version": "dof.e1v2-static-audit-bundle.v1",
            "capability": nested_capability_audit(),
            "expansion": expansion_inheritance_audit(),
        }
        _write_new(args.output, payload | {"audit_sha256": content_hash(payload)})
        return 0
    if args.command == "observation-audit":
        result = common_observation_equality_audit(
            source=_read(args.source),
            current_artifact_sha256=args.artifact_sha256,
            decision_index=args.decision_index,
        )
        _write_new(args.output, result)
        return 0 if result["accepted"] else 2
    if args.command == "discover-openrouter":
        key = os.environ.get("OPENROUTER_API_KEY")
        if not key:
            raise SystemExit("OPENROUTER_API_KEY unavailable")
        models = asyncio.run(discover_openrouter_models(key))
        endpoints = asyncio.run(discover_openrouter_endpoints(key))
        reserve = verify_openrouter_unknown_cost_reserve(endpoints)
        result = {
            "schema_version": "dof.e1v2-openrouter-discovery-bundle.v1",
            "models": models,
            "endpoints": endpoints,
            "unknown_cost_reserve_verification": reserve,
            "all_profiles_available": (
                models["all_required_available"]
                and endpoints["all_profiles_available"]
                and reserve["all_models_accepted"]
            ),
        }
        _write_new(args.output, result)
        return 0 if result["all_profiles_available"] else 2
    if args.command == "score-root":
        run_readonly_score(
            args.root,
            vivado_exec=args.vivado_exec,
            scorer_script=REPOSITORY_ROOT / "scripts" / "e1_score_dcp.tcl",
            run_dir=args.output,
        )
        return 0
    if args.command == "assemble-root-audit":
        result = _assemble_root_audit(_roots(args.roots_manifest), args.scores_dir)
        _write_new(args.output, result)
        return 0 if result["all_accepted"] else 3
    if args.command == "freeze-bundle":
        result = freeze_e1v2_job_bundle(
            _roots(args.roots_manifest),
            _read(args.root_audit),
            _read(args.provider_discovery),
            matrix=args.matrix,
            repository_anchor=REPOSITORY_ROOT,
            fake_startup_attestation=args.fake_attestation,
        )
        _write_new(args.output, result)
        return 0
    if args.command == "validate-bundle":
        validate_e1v2_job_bundle(_read(args.bundle))
        return 0
    if args.command == "run-job":
        bundle = validate_e1v2_job_bundle(_read(args.bundle))
        matches = [row for row in bundle["jobs"] if row["job"]["job_id"] == args.job_id]
        if len(matches) != 1:
            raise SystemExit("job-id not unique in bundle")
        spec = matches[0]
        roots = _roots(args.roots_manifest)
        output = args.output
        if output is None:
            attempt = os.environ.get("DOF_JOB_OUTPUT")
            if not attempt:
                raise SystemExit("--output or DOF_JOB_OUTPUT required")
            output = Path(attempt) / "trajectory"
        execute_real_e1v2_job(
            spec,
            roots[spec["job"]["benchmark"]],
            output,
            vivado_exec=args.vivado_exec,
            python_exec=args.python_exec,
            repository_anchor=REPOSITORY_ROOT,
            scorer_script=REPOSITORY_ROOT / "scripts" / "e1_score_dcp.tcl",
            fake_startup_attestation_sha256=bundle["freeze"]["fake_startup_attestation_evidence_sha256"],
        )
        return 0
    if args.command == "run-queue":
        bundle = validate_e1v2_job_bundle(_read(args.bundle))
        matrix = bundle["protocol"]["matrix"]
        if args.repeat is not None and matrix != WP012_K_MATRIX:
            raise SystemExit("--repeat is only valid for the WP-012 K matrix")
        command_prefix = (
            _absolute_preserving_symlink(args.python_exec),
            os.fspath(Path(__file__).resolve()),
            "run-job",
            "--bundle",
            os.fspath(args.bundle.resolve()),
            "--roots-manifest",
            os.fspath(args.roots_manifest.resolve()),
            "--vivado-exec",
            os.fspath(args.vivado_exec.resolve()),
            "--python-exec",
            _absolute_preserving_symlink(args.python_exec),
        )
        selected_specs = [
            row
            for row in bundle["jobs"]
            if args.repeat is None or row["job"]["repeat"] == args.repeat
        ]
        if args.repeat is not None and len(selected_specs) != 72:
            raise SystemExit("WP-012 K repeat queue must contain exactly 72 jobs")
        jobs = [
            QueueJob(row["job"]["job_id"], command_prefix + ("--job-id", row["job"]["job_id"]))
            for row in selected_specs
        ]
        stochastic_total = sum(
            row["job"]["level"] in {"L2", "L3", "L4"}
            for row in bundle["jobs"]
        )
        stochastic_selected = sum(
            row["job"]["level"] in {"L2", "L3", "L4"}
            for row in selected_specs
        )
        queue_cost_cap = float(bundle["protocol"]["budgets"]["aggregate_cost_usd"])
        if args.cost_cap_usd is not None:
            if not 0.0 < args.cost_cap_usd <= queue_cost_cap:
                raise SystemExit("--cost-cap-usd exceeds frozen phase cap")
            queue_cost_cap = args.cost_cap_usd
        elif stochastic_selected != stochastic_total:
            queue_cost_cap *= stochastic_selected / stochastic_total
        CompleteJobQueue(
            repository_anchor=REPOSITORY_ROOT,
            output_root=args.output_root,
            max_concurrency=args.max_concurrency,
            provider_transport_failure_limit_per_model=(
                3
                if bundle["protocol"]["matrix"]
                in {
                    E1V2_REPEAT1_MATRIX,
                    WP012_K_MATRIX,
                    WP012_U_MATRIX,
                    WP012_H_MATRIX,
                }
                else None
            ),
            cost_upper_bound_usd=queue_cost_cap,
        ).run(jobs)
        return 0
    if args.command == "stop":
        marker = args.output_root.resolve() / "STOP"
        _write_new(marker, {"reason": args.reason})
        return 0
    if args.command == "aggregate":
        aggregate_trajectory_results(args.output_root)
        return 0
    raise AssertionError(args.command)


if __name__ == "__main__":
    raise SystemExit(main())
