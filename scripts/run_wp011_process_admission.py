#!/usr/bin/env python3
"""Bounded real production-path process-stop admission for WP-011."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import threading
import time
from typing import Any

from optimizer_pipeline.dof.artifacts import fingerprint_file
from optimizer_pipeline.dof.e0_job import capture_real_observation
from optimizer_pipeline.dof.e1v2 import content_hash
from optimizer_pipeline.dof.e1v2_job import (
    execute_real_e1v2_job,
    validate_e1v2_job_bundle,
)
from optimizer_pipeline.dof.job_queue import CompleteJobQueue, QueueJob
from optimizer_pipeline.dof.openrouter import OPENROUTER_E1_PROFILES
from optimizer_pipeline.dof.provider import (
    PROVIDER_TRANSPORT_CONTRACT_VERSION,
    RawProviderResponse,
)


REPOSITORY = Path(__file__).resolve().parents[1]
SCHEMA = "research-flow.wp011-process-stop-admission.v1"
KINDS = ("l0-readonly", "l2-macro", "l3-typed", "l4-generated")
QUEUE_KEYS = (
    "DOF_PROCESS_REGISTRY_DIR",
    "DOF_JOB_ID",
    "DOF_ATTEMPT_ID",
)


def _read(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text())
    if not isinstance(value, dict):
        raise ValueError("JSON object required")
    return value


def _write_new(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as handle:
        handle.write(json.dumps(value, sort_keys=True, indent=2).encode() + b"\n")
        handle.flush()
        os.fsync(handle.fileno())


def _roots(path: Path) -> dict[str, str]:
    value = _read(path)
    source = value.get("roots", value)
    if not isinstance(source, dict):
        raise ValueError("roots object required")
    return {str(key): str(item) for key, item in source.items()}


def _proposal(kind: str, binding: Any) -> dict[str, Any]:
    action_key, parameters = {
        "l2-macro": ("macro.phys_opt", {}),
        "l3-typed": ("manual.phys_opt_design", {"directive": "Explore"}),
        "l4-generated": (
            "generated.pblock_region",
            {
                "pblock_id": "g_admission_region",
                "selector_kind": "sequential_cells",
                "cell_limit": 16,
                "slice_x0": 0,
                "slice_y0": 0,
                "slice_x1": 12,
                "slice_y1": 99,
            },
        ),
    }[kind]
    return {
        "schema_version": "action_proposal.v1",
        "proposal_id": "wp011-" + kind,
        "decision_id": binding.decision_id,
        "catalogue_version": binding.catalogue_version,
        "catalogue_hash": binding.catalogue_hash,
        "kind": "ACTION",
        "steps": [
            {
                "step_id": "wp011-step-0",
                "action_key": action_key,
                "parameters": parameters,
            }
        ],
    }


class ForcedAdmissionTransport:
    transport_contract_version = PROVIDER_TRANSPORT_CONTRACT_VERSION
    internal_retries_disabled = True
    admission_kind = "l2-macro"

    def __init__(self, *, expected_outbound_binding: Any, **_kwargs: Any) -> None:
        self.expected = expected_outbound_binding
        self.physical_requests = 0
        self.send_attempts = 0
        self.cleanup_status = "NOT_ATTEMPTED"
        self.last_exchange: dict[str, Any] | None = None
        self.reconciled_accounting = None
        self.accounting_lookup_requests = 0

    async def send(self, binding: Any, retry_index: int) -> RawProviderResponse:
        if retry_index != 0 or self.send_attempts:
            raise RuntimeError("technical_admission_single_request_exhausted")
        self.send_attempts = 1
        self.physical_requests = 1
        self.cleanup_status = "SUCCEEDED"
        profile = OPENROUTER_E1_PROFILES[binding.model_id]
        payload = _proposal(self.admission_kind, binding)
        raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        self.last_exchange = {
            "schema_version": "dof.openrouter-exchange.v1",
            "route": {
                "attempt": 1,
                "requested_model": binding.model_id,
                "returned_model": binding.model_id,
                "selected_upstream_provider": profile["upstream_provider"],
                "required_endpoint_tag": profile["endpoint_tag"],
                "fallback_allowed": False,
            },
            "usage": {
                "prompt_tokens": 1,
                "completion_tokens": 2,
                "total_tokens": 3,
                "cost": 0.0,
            },
            "finish_reason": "stop",
            "failure_class": None,
        }
        return RawProviderResponse(
            raw,
            "openrouter",
            provider_request_id="wp011-technical-admission",
            returned_model_id=binding.model_id,
            returned_model_revision=profile["upstream_provider"],
            input_tokens=1,
            output_tokens=2,
            total_tokens=3,
            cost_usd=0.0,
            cost_source="wp011_technical_admission_fake",
            observed_request_contract_hash=binding.request_contract_hash,
        )


def _transport_for(kind: str) -> type[ForcedAdmissionTransport]:
    return type(
        "ForcedAdmissionTransport_" + kind.replace("-", "_"),
        (ForcedAdmissionTransport,),
        {"admission_kind": kind},
    )


def _spec(bundle: dict[str, Any], benchmark: str, level: str) -> dict[str, Any]:
    rows = [
        row
        for row in bundle["jobs"]
        if row["job"]["benchmark"] == benchmark
        and row["job"]["level"] == level
        and (level in {"L0", "L1"} or row["job"]["model"] == "openai/gpt-5.6-luna")
    ]
    if len(rows) != 1:
        raise RuntimeError("technical_admission_job_selection_not_unique")
    return rows[0]


def run_job(args: argparse.Namespace) -> int:
    bundle = validate_e1v2_job_bundle(_read(args.bundle))
    roots = _roots(args.roots_manifest)
    level = {
        "l0-readonly": "L0",
        "l2-macro": "L2",
        "l3-typed": "L3",
        "l4-generated": "L4",
    }[args.kind]
    spec = _spec(bundle, args.benchmark, level)
    root = Path(roots[args.benchmark]).resolve()
    output = Path(os.environ["DOF_JOB_OUTPUT"]) / "trajectory"
    if args.kind == "l0-readonly":
        output.mkdir(parents=True, exist_ok=False)
        capture_real_observation(
            spec,
            root,
            output / "root-observation",
            decision_index=0,
            history=[],
            heavy_remaining=0,
            model_requests_remaining=0,
            vivado_exec=args.vivado_exec,
            python_exec=args.python_exec,
            repository_anchor=REPOSITORY,
            frozen_timing_score=spec["root"]["score"],
            e1_protocol=True,
        )
        core = {
            "schema_version": spec["schema_version"].replace("bound-job-spec", "trajectory-result"),
            "protocol_version": spec["protocol_version"],
            "matrix": spec["matrix"],
            "study_status": "WP011_PROCESS_ADMISSION_NON_CLAIM",
            "job": spec["job"],
            "job_spec_sha256": spec["job_spec_sha256"],
            "endpoint": "VALID_COMPLETION",
            "model_requests": 0,
            "known_cost_usd": 0.0,
            "unknown_cost_attempt_count": 0,
            "reserved_unknown_cost_usd": 0.0,
            "cost_lower_bound_usd": 0.0,
            "cost_upper_bound_usd": 0.0,
            "known_token_total": 0,
            "unknown_usage_attempt_count": 0,
            "heavy_calls": 0,
            "light_mutation_calls": 0,
            "design_mutated": False,
            "root_unchanged": fingerprint_file(root).sha256 == spec["root"]["sha256"],
        }
        _write_new(output / "trajectory-result.json", core | {"result_sha256": content_hash(core)})
        return 0
    execute_real_e1v2_job(
        spec,
        root,
        output,
        vivado_exec=args.vivado_exec,
        python_exec=args.python_exec,
        repository_anchor=REPOSITORY,
        scorer_script=REPOSITORY / "scripts/e1_score_dcp.tcl",
        fake_startup_attestation_sha256=bundle["freeze"]["fake_startup_attestation_evidence_sha256"],
        credential="wp011-technical-admission-no-network",
        transport_factory=_transport_for(args.kind),
    )
    return 0


def run_admission(args: argparse.Namespace) -> dict[str, Any]:
    bundle = validate_e1v2_job_bundle(_read(args.bundle))
    roots = _roots(args.roots_manifest)
    root = Path(roots[args.benchmark]).resolve()
    root_before = fingerprint_file(root)
    output = args.output.resolve()
    command_prefix = (
        os.fspath(args.python_exec.resolve()),
        "-m",
        "scripts.run_wp011_process_admission",
        "job",
        "--bundle",
        os.fspath(args.bundle.resolve()),
        "--roots-manifest",
        os.fspath(args.roots_manifest.resolve()),
        "--vivado-exec",
        os.fspath(args.vivado_exec.resolve()),
        "--python-exec",
        os.fspath(args.python_exec.resolve()),
        "--benchmark",
        args.benchmark,
    )
    jobs = [
        QueueJob("wp011-" + kind, command_prefix + ("--kind", kind))
        for kind in KINDS
    ]
    queue = CompleteJobQueue(
        repository_anchor=REPOSITORY,
        output_root=output,
        max_concurrency=4,
        poll_seconds=0.05,
        terminate_grace_seconds=1.0,
        kill_grace_seconds=3.0,
    )
    returned: list[dict[str, Any]] = []
    thread = threading.Thread(target=lambda: returned.append(queue.run(jobs)))
    thread.start()
    observed: dict[str, int] = {}
    thresholds = {
        "l0-readonly": 1,
        "l2-macro": 2,
        "l3-typed": 2,
        "l4-generated": 2,
    }
    deadline = time.monotonic() + args.observation_timeout_seconds
    while time.monotonic() < deadline:
        for kind in KINDS:
            job_id = "wp011-" + kind
            records = output / "jobs" / job_id / f"{job_id}--attempt-01/process-ownership/records"
            observed[kind] = len(list(records.glob("*.json"))) if records.is_dir() else 0
        if all(observed.get(kind, 0) >= thresholds[kind] for kind in KINDS):
            queue.request_stop("wp011_predeclared_process_stop_admission")
            break
        time.sleep(0.05)
    else:
        queue.request_stop("wp011_process_registration_observation_timeout")
    thread.join(timeout=30)
    if thread.is_alive() or not returned:
        raise RuntimeError("process_admission_queue_did_not_stop_boundedly")
    ledger = _read(queue.ledger_file)
    root_after = fingerprint_file(root)
    attempt_rows = ledger.get("attempts", [])
    registry_complete = all(
        row.get("nested_process_registry", {})
        .get("attempt_path_audits", [{}])[0]
        .get("registry_complete")
        is True
        for row in attempt_rows
    )
    two_zero = all(
        len(row.get("nested_process_registry", {}).get("attempt_path_audits", [])) == 3
        and all(
            audit.get("live_supervisor_count") == 0
            and audit.get("quiescent") is True
            for audit in row["nested_process_registry"]["attempt_path_audits"][1:]
        )
        for row in attempt_rows
    )
    core = {
        "schema_version": SCHEMA,
        "benchmark": args.benchmark,
        "bundle_sha256": bundle["bundle_sha256"],
        "queue_result": returned[0],
        "records_observed_before_stop": observed,
        "required_record_thresholds": thresholds,
        "all_required_supervisors_observed": all(
            observed.get(kind, 0) >= thresholds[kind] for kind in KINDS
        ),
        "attempt_count": len(attempt_rows),
        "registry_complete": registry_complete,
        "two_attempt_path_zero_live_audits": two_zero,
        "automatic_cleanup": True,
        "manual_kill_commands": 0,
        "target_queue_keys": list(QUEUE_KEYS),
        "target_queue_keys_absent_by_structural_boundary": True,
        "root_unchanged": (
            root_before.sha256 == root_after.sha256
            and root_before.size_bytes == root_after.size_bytes
        ),
        "repository_unchanged": ledger.get("repository_unchanged") is True,
    }
    accepted = bool(
        core["all_required_supervisors_observed"]
        and core["registry_complete"]
        and core["two_attempt_path_zero_live_audits"]
        and core["root_unchanged"]
        and core["repository_unchanged"]
        and returned[0].get("cleanup_quiescent") is True
    )
    summary = core | {"accepted": accepted}
    summary["summary_sha256"] = content_hash(summary)
    _write_new(output / "process-admission-public.json", summary)
    if not accepted:
        raise RuntimeError("wp011_process_stop_admission_failed")
    return summary


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser()
    sub = root.add_subparsers(dest="command", required=True)
    for name in ("run", "job"):
        item = sub.add_parser(name)
        item.add_argument("--bundle", type=Path, required=True)
        item.add_argument("--roots-manifest", type=Path, required=True)
        item.add_argument("--vivado-exec", type=Path, required=True)
        item.add_argument("--python-exec", type=Path, required=True)
        item.add_argument("--benchmark", default="vexriscv_re-place")
        if name == "run":
            item.add_argument("--output", type=Path, required=True)
            item.add_argument("--observation-timeout-seconds", type=float, default=300.0)
        else:
            item.add_argument("--kind", choices=KINDS, required=True)
    return root


def main() -> int:
    args = parser().parse_args()
    if args.command == "job":
        return run_job(args)
    result = run_admission(args)
    print(json.dumps(result, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
