#!/usr/bin/env python3
"""Freeze and execute the isolated WP-012-01 G diagnostic."""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import json
import os
from pathlib import Path
import sys
from typing import Any, Mapping, Sequence


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from optimizer_pipeline.dof.e1v2 import content_hash
from optimizer_pipeline.dof.e1v2_job import (
    execute_real_e1v2_job,
    run_bound_provider_decision_v2,
)
from optimizer_pipeline.dof.g_diagnostic import (
    GCue,
    G_DIAGNOSTIC_AGGREGATE_SCHEMA,
    G_DIAGNOSTIC_PROBE_SCHEMA,
    G_DIAGNOSTIC_PROTOCOL,
    freeze_g_diagnostic_bundle,
    overlay_manifest,
    rebind_probe_observation,
    validate_g_diagnostic_bundle,
)
from optimizer_pipeline.dof.job_queue import CompleteJobQueue, QueueJob
from optimizer_pipeline.dof.mutation_worker_binding import (
    verified_fake_attestation_evidence_sha256,
)


def _read(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text())
    if not isinstance(value, dict):
        raise SystemExit(f"JSON object required: {path}")
    return value


def _write_new(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as stream:
        stream.write(json.dumps(value, sort_keys=True, indent=2).encode() + b"\n")


def _roots(path: Path) -> dict[str, Path]:
    raw = _read(path)
    value = raw.get("roots", raw)
    if not isinstance(value, Mapping):
        raise SystemExit("roots manifest must contain an object")
    roots = {str(key): Path(str(item)).resolve() for key, item in value.items()}
    if any(not item.is_file() for item in roots.values()):
        raise SystemExit("one or more diagnostic roots are missing")
    return roots


def _absolute_preserving_symlink(path: Path) -> str:
    return os.path.abspath(os.fspath(path))


def _public_integrity_blocker(decision_dir: Path, exc: BaseException) -> dict[str, Any]:
    exchange_path = decision_dir / "evidence/openrouter-exchange.json"
    exchange = _read(exchange_path) if exchange_path.is_file() else {}
    route = exchange.get("route") if isinstance(exchange.get("route"), Mapping) else None
    return {
        "schema_version": "dof.g-diagnostic-provider-integrity-blocker-public.v1",
        "endpoint": "PROVIDER_INTEGRITY_BLOCKER",
        "exception_class": type(exc).__name__,
        "reason": str(exc),
        "failure_class": exchange.get("failure_class"),
        "http_status": exchange.get("http_status"),
        "raw_response_sha256": exchange.get("raw_response_sha256"),
        "raw_response_size_bytes": exchange.get("raw_response_size_bytes"),
        "route": route,
        "physical_requests": exchange.get("physical_requests"),
        "action_dispatches": 0,
        "retry": 0,
        "fallback": 0,
        "repair": 0,
        "private_response_included": False,
    }


def _probe_record(spec: Mapping[str, Any], bound: Any, phase: str) -> dict[str, Any]:
    gate = None if bound.gate is None else bound.gate.to_dict()
    diagnostic = dict(bound.result.get("g_diagnostic", {}))
    route = bound.result.get("route")
    route_admitted = bool(
        isinstance(route, Mapping)
        and route.get("requested_model") == spec["job"]["model"]
        and route.get("returned_model") == spec["job"]["model"]
        and route.get("selected_upstream_provider")
        and route.get("attempt") == 1
        and route.get("fallback_allowed") is False
        and bound.result.get("provider_transport_accounting_accepted") is True
        and bound.result.get("physical_requests") == 1
    )
    payload = {
        "schema_version": G_DIAGNOSTIC_PROBE_SCHEMA,
        "protocol": G_DIAGNOSTIC_PROTOCOL,
        "phase": phase,
        "job": spec["job"],
        "job_spec_sha256": spec["job_spec_sha256"],
        "decision_id": bound.binding.decision_id,
        "request_contract_hash": bound.binding.request_contract_hash,
        "base_observation_hash": bound.common_context.base_observation_hash,
        "prompt_sha256": bound.binding.prompt_hash,
        "context_sha256": bound.binding.context_hash,
        "provider_endpoint": bound.result["endpoint"],
        "route": route,
        "route_admitted": route_admitted,
        "g_diagnostic": diagnostic,
        "gate": gate,
        "provider_accepted": bound.decision.accepted,
        "zero_backend_dispatch": True,
        "physical_requests": bound.result["physical_requests"],
        "known_cost_usd": bound.result["known_cost_usd"],
        "reserved_unknown_cost_usd": bound.result["reserved_unknown_cost_usd"],
        "known_token_total": bound.result["known_token_total"],
        "retry": 0,
        "fallback": 0,
        "repair": 0,
    }
    return payload | {"result_sha256": content_hash(payload)}


def _run_provider_matrix(
    bundle: Mapping[str, Any], specs: Sequence[Mapping[str, Any]], output: Path, phase: str
) -> dict[str, Any]:
    output.mkdir(parents=True, exist_ok=False)
    rows = []
    stopped = False
    stop_reason = None
    for spec in specs:
        job_dir = output / "jobs" / spec["job"]["job_id"]
        observation = rebind_probe_observation(
            bundle["probe_observation_template"], spec
        )
        try:
            bound = run_bound_provider_decision_v2(
                spec,
                observation,
                job_dir / "provider",
                decision_index=0,
                heavy_remaining=4,
                remaining_cost_usd=0.4,
            )
        except BaseException as exc:
            blocker = _public_integrity_blocker(job_dir / "provider", exc)
            row = {
                "schema_version": G_DIAGNOSTIC_PROBE_SCHEMA,
                "protocol": G_DIAGNOSTIC_PROTOCOL,
                "phase": phase,
                "job": spec["job"],
                "job_spec_sha256": spec["job_spec_sha256"],
                "provider_integrity_blocker": blocker,
                "zero_backend_dispatch": True,
                "known_cost_usd": 0.0,
                "reserved_unknown_cost_usd": 0.1,
                "known_token_total": 0,
            }
            row = row | {"result_sha256": content_hash(row)}
            rows.append(row)
            _write_new(job_dir / "probe-result.json", row)
            stopped = True
            stop_reason = "provider_integrity_blocker:" + spec["job"]["job_id"]
            break
        row = _probe_record(spec, bound, phase)
        rows.append(row)
        _write_new(job_dir / "probe-result.json", row)
        cost_upper = sum(
            float(item.get("known_cost_usd", 0.0))
            + float(item.get("reserved_unknown_cost_usd", 0.0))
            for item in rows
        )
        if cost_upper > 4.0 + 1e-12:
            stopped = True
            stop_reason = "diagnostic_provider_cost_guard_exceeded"
            break
    payload = {
        "schema_version": "dof.g-diagnostic-provider-matrix-result.v1",
        "protocol": G_DIAGNOSTIC_PROTOCOL,
        "phase": phase,
        "planned": len(specs),
        "completed": len(rows),
        "stopped": stopped,
        "stop_reason": stop_reason,
        "known_cost_usd": sum(float(row.get("known_cost_usd", 0.0)) for row in rows),
        "reserved_unknown_cost_usd": sum(
            float(row.get("reserved_unknown_cost_usd", 0.0)) for row in rows
        ),
        "known_token_total": sum(int(row.get("known_token_total", 0)) for row in rows),
        "rows": rows,
    }
    result = payload | {"result_sha256": content_hash(payload)}
    _write_new(output / "aggregate.json", result)
    return result


def _aggregate_real(output: Path) -> dict[str, Any]:
    ledger = _read(output / "controller-ledger.json")
    ledger_core = {key: value for key, value in ledger.items() if key != "ledger_sha256"}
    if ledger.get("ledger_sha256") != content_hash(ledger_core):
        raise ValueError("g_diagnostic_queue_ledger_hash_invalid")
    rows = []
    for attempt in ledger["attempts"]:
        path = Path(attempt["output_directory"]) / "trajectory/trajectory-result.json"
        if not path.is_file():
            continue
        row = _read(path)
        core = {key: value for key, value in row.items() if key != "result_sha256"}
        if row.get("result_sha256") != content_hash(core):
            raise ValueError("g_diagnostic_trajectory_hash_invalid")
        if row.get("diagnostic", {}).get("primary_aggregate_eligible") is not False:
            raise ValueError("g_diagnostic_primary_boundary_missing")
        rows.append(row)
    funnel: dict[str, Counter[str]] = defaultdict(Counter)
    for row in rows:
        utilisation = row["diagnostic"]["utilisation"]
        keys = (
            "g_mentioned",
            "g_proposed",
            "g_gate_admitted",
            "g_executed",
            "g_valid_child",
            "g_followed_by_implementation",
            "g_scored_outcome",
        )
        for group in (
            "cue:" + row["job"]["cue"],
            "model:" + row["job"]["model"],
            "root:" + row["job"]["benchmark"],
        ):
            funnel[group]["jobs"] += 1
            for key in keys:
                funnel[group][key] += int(utilisation[key] is True)
    payload = {
        "schema_version": G_DIAGNOSTIC_AGGREGATE_SCHEMA,
        "protocol": G_DIAGNOSTIC_PROTOCOL,
        "queue_ledger_sha256": ledger["ledger_sha256"],
        "planned": 30,
        "canonical": len(rows),
        "endpoint_counts": dict(Counter(row["endpoint"] for row in rows)),
        "known_cost_usd": sum(float(row["known_cost_usd"]) for row in rows),
        "reserved_unknown_cost_usd": sum(
            float(row["reserved_unknown_cost_usd"]) for row in rows
        ),
        "known_token_total": sum(int(row["known_token_total"]) for row in rows),
        "heavy_calls": sum(int(row["heavy_calls"]) for row in rows),
        "light_mutation_calls": sum(int(row["light_mutation_calls"]) for row in rows),
        "funnel": {key: dict(value) for key, value in sorted(funnel.items())},
        "action_sequences_around_g": [
            {
                "job_id": row["job"]["job_id"],
                "root": row["job"]["benchmark"],
                "model": row["job"]["model"],
                "cue": row["job"]["cue"],
                "utilisation": row["diagnostic"]["utilisation"],
            }
            for row in rows
            if row["diagnostic"]["utilisation"]["g_proposed"]
        ],
        "jobs": rows,
    }
    result = payload | {"aggregate_sha256": content_hash(payload)}
    _write_new(output / "g-diagnostic-aggregate.json", result)
    return result


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    overlays = sub.add_parser("overlays")
    overlays.add_argument("--output", type=Path, required=True)
    freeze = sub.add_parser("freeze")
    freeze.add_argument("--roots-manifest", type=Path, required=True)
    freeze.add_argument("--root-audit", type=Path, required=True)
    freeze.add_argument("--provider-discovery", type=Path, required=True)
    freeze.add_argument("--probe-observation", type=Path, required=True)
    freeze.add_argument("--fake-attestation", type=Path, required=True)
    freeze.add_argument("--output", type=Path, required=True)
    validate = sub.add_parser("validate")
    validate.add_argument("--bundle", type=Path, required=True)
    noop = sub.add_parser("noop-audit")
    noop.add_argument("--bundle", type=Path, required=True)
    noop.add_argument("--output", type=Path, required=True)
    admission = sub.add_parser("provider-admission")
    admission.add_argument("--bundle", type=Path, required=True)
    admission.add_argument("--output-root", type=Path, required=True)
    probes = sub.add_parser("provider-probes")
    probes.add_argument("--bundle", type=Path, required=True)
    probes.add_argument("--output-root", type=Path, required=True)
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
    queue.add_argument("--cost-cap-usd", type=float, required=True)
    aggregate = sub.add_parser("aggregate")
    aggregate.add_argument("--output-root", type=Path, required=True)
    return parser


def main() -> int:
    args = _parser().parse_args()
    if args.command == "overlays":
        _write_new(args.output, overlay_manifest())
        return 0
    if args.command == "freeze":
        result = freeze_g_diagnostic_bundle(
            _roots(args.roots_manifest),
            _read(args.root_audit),
            _read(args.provider_discovery),
            _read(args.probe_observation),
            repository_anchor=REPOSITORY_ROOT,
            fake_startup_attestation_sha256=verified_fake_attestation_evidence_sha256(
                args.fake_attestation
            ),
        )
        _write_new(args.output, result)
        return 0
    if args.command == "validate":
        validate_g_diagnostic_bundle(_read(args.bundle))
        return 0
    if args.command == "noop-audit":
        from optimizer_pipeline.dof.g_diagnostic import build_g_diagnostic_policy_context
        from optimizer_pipeline.dof.common_observation import build_common_policy_context
        from optimizer_pipeline.dof.contracts import DoFLevel

        bundle = validate_g_diagnostic_bundle(_read(args.bundle))
        spec = bundle["provider_probes"][0]
        observation = rebind_probe_observation(bundle["probe_observation_template"], spec)
        primary = build_common_policy_context(
            level=DoFLevel.L4,
            source=observation["source"],
            current_artifact_sha256=observation["current_artifact"]["sha256"],
            decision_index=0,
        )
        contexts = {
            cue.value: build_g_diagnostic_policy_context(
                source=observation["source"],
                current_artifact_sha256=observation["current_artifact"]["sha256"],
                decision_index=0,
                cue=cue,
            )
            for cue in GCue
        }
        payload = {
            "schema_version": "dof.g-diagnostic-noop-audit.v1",
            "c0_prompt_bytes_equal": contexts[GCue.C0.value].common.provider_prompt_bytes
            == primary.provider_prompt_bytes,
            "c0_context_bytes_equal": contexts[GCue.C0.value].common.provider_context_bytes
            == primary.provider_context_bytes,
            "base_hashes": {
                cue: context.common.base_observation_hash for cue, context in contexts.items()
            },
            "primary_base_hash": primary.base_observation_hash,
            "prompt_hashes": {
                cue: context.common.provider_prompt_hash for cue, context in contexts.items()
            },
            "action_surfaces": {
                cue: list(context.action_surface) for cue, context in contexts.items()
            },
            "primary_aggregate_eligible": False,
            "sealed_holdouts_in_specs": sorted(
                {
                    row["job"]["benchmark"]
                    for row in bundle["real_jobs"]
                    if row["job"]["benchmark"].startswith("external_family_holdout")
                }
            ),
        }
        payload["accepted"] = bool(
            payload["c0_prompt_bytes_equal"]
            and payload["c0_context_bytes_equal"]
            and len(set(payload["base_hashes"].values())) == 1
            and not payload["sealed_holdouts_in_specs"]
        )
        _write_new(args.output, payload | {"audit_sha256": content_hash(payload)})
        return 0 if payload["accepted"] else 2
    if args.command in {"provider-admission", "provider-probes"}:
        bundle = validate_g_diagnostic_bundle(_read(args.bundle))
        specs = (
            bundle["provider_admissions"]
            if args.command == "provider-admission"
            else bundle["provider_probes"]
        )
        result = _run_provider_matrix(bundle, specs, args.output_root, args.command)
        if result["stopped"]:
            return 3
        if args.command == "provider-admission" and not all(
            row.get("route_admitted") is True for row in result["rows"]
        ):
            return 4
        return 0
    if args.command == "run-job":
        bundle = validate_g_diagnostic_bundle(_read(args.bundle))
        matches = [row for row in bundle["real_jobs"] if row["job"]["job_id"] == args.job_id]
        if len(matches) != 1:
            raise SystemExit("diagnostic job-id not unique")
        spec = matches[0]
        roots = _roots(args.roots_manifest)
        output = args.output
        if output is None:
            root = os.environ.get("DOF_JOB_OUTPUT")
            if not root:
                raise SystemExit("--output or DOF_JOB_OUTPUT required")
            output = Path(root) / "trajectory"
        execute_real_e1v2_job(
            spec,
            roots[spec["job"]["benchmark"]],
            output,
            vivado_exec=args.vivado_exec,
            python_exec=args.python_exec,
            repository_anchor=REPOSITORY_ROOT,
            scorer_script=REPOSITORY_ROOT / "scripts/e1_score_dcp.tcl",
            fake_startup_attestation_sha256=bundle["freeze"]["fake_startup_attestation_sha256"],
        )
        return 0
    if args.command == "run-queue":
        if not 0 < args.cost_cap_usd <= 4.0:
            raise SystemExit("diagnostic cost cap must be in (0, 4]")
        bundle = validate_g_diagnostic_bundle(_read(args.bundle))
        prefix = (
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
        jobs = [
            QueueJob(row["job"]["job_id"], prefix + ("--job-id", row["job"]["job_id"]))
            for row in bundle["real_jobs"]
        ]
        CompleteJobQueue(
            repository_anchor=REPOSITORY_ROOT,
            output_root=args.output_root,
            max_concurrency=args.max_concurrency,
            provider_transport_failure_limit_per_model=1,
            cost_upper_bound_usd=args.cost_cap_usd,
        ).run(jobs)
        return 0
    if args.command == "aggregate":
        _aggregate_real(args.output_root)
        return 0
    raise AssertionError(args.command)


if __name__ == "__main__":
    raise SystemExit(main())
