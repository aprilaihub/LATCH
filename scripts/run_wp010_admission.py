#!/usr/bin/env python3
"""Deterministic no-network WP-010 policy-terminal and cleanup admission."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
from typing import Any

from optimizer_pipeline.dof.e1v2 import content_hash
from optimizer_pipeline.dof.job_queue import (
    CompleteJobQueue,
    QueueJob,
    aggregate_trajectory_results,
)
from optimizer_pipeline.dof.provider import (
    PROVIDER_TRANSPORT_CONTRACT_VERSION,
    DoFProviderObserver,
    ProviderAttemptStatus,
    ProviderRequestBinding,
    ProviderRunLedger,
    ProviderSettings,
    RawProviderResponse,
    verify_provider_attempts,
)


REPOSITORY = Path(__file__).resolve().parents[1]
SUPERVISOR = REPOSITORY / "optimizer_pipeline/dof/process_supervisor.py"


def _write(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, sort_keys=True, indent=2) + "\n")
    os.replace(temporary, path)


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


class ProposalMismatchTransport:
    transport_contract_version = PROVIDER_TRANSPORT_CONTRACT_VERSION
    internal_retries_disabled = True

    def __init__(self, *, expected_outbound_binding, **_kwargs):
        self.expected = expected_outbound_binding
        self.physical_requests = 0
        self.send_attempts = 0
        self.last_exchange = None
        self.reconciled_accounting = None

    async def send(self, binding, _retry_index):
        self.physical_requests = 1
        self.send_attempts = 1
        self.last_exchange = {
            "schema_version": "dof.openrouter-exchange.v1",
            "route": {
                "attempt": 1,
                "requested_model": binding.model_id,
                "returned_model": binding.model_id,
                "selected_upstream_provider": "fake-pinned-openai",
                "fallback_allowed": False,
            },
            "usage": {
                "prompt_tokens": 20,
                "completion_tokens": 10,
                "total_tokens": 30,
                "cost": 0.01,
            },
        }
        payload = {
            "schema_version": "action_proposal.v1",
            "proposal_id": "wp010-admission-proposal",
            "decision_id": binding.decision_id + "x",
            "catalogue_version": binding.catalogue_version,
            "catalogue_hash": binding.catalogue_hash,
            "kind": "ACTION",
            "steps": [
                {
                    "step_id": "step-001",
                    "action_key": "macro.place_explore",
                    "parameters": {},
                }
            ],
        }
        return RawProviderResponse(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode(),
            "openrouter",
            provider_request_id="wp010-fake-request",
            returned_model_id=binding.model_id,
            returned_model_revision="fake-pinned-openai",
            input_tokens=20,
            output_tokens=10,
            total_tokens=30,
            cost_usd=0.01,
            cost_source="deterministic_fake",
            observed_request_contract_hash=binding.request_contract_hash,
        )


def _trajectory_result(job_id: str, endpoint: str, **provider: Any) -> dict[str, Any]:
    core = {
        "schema_version": "dof.e1v2-trajectory-result.v3",
        "job": {"job_id": job_id, "model": "openai/gpt-5.6-luna"},
        "endpoint": endpoint,
        "model_requests": int(provider.get("physical_requests", 0)),
        "known_cost_usd": float(provider.get("known_cost_usd", 0.0)),
        "unknown_cost_attempt_count": int(provider.get("unknown_cost_attempt_count", 0)),
        "reserved_unknown_cost_usd": float(provider.get("reserved_unknown_cost_usd", 0.0)),
        "cost_lower_bound_usd": float(provider.get("cost_lower_bound_usd", 0.0)),
        "cost_upper_bound_usd": float(provider.get("cost_upper_bound_usd", 0.0)),
        "known_token_total": int(provider.get("known_token_total", 0)),
        "unknown_usage_attempt_count": int(provider.get("unknown_usage_attempt_count", 0)),
        "heavy_calls": 0,
        "light_mutation_calls": 0,
        "design_mutated": False,
    }
    return core | {"result_sha256": content_hash(core)}


def proposal_job() -> None:
    binding = ProviderRequestBinding(
        run_id="wp010-admission-run",
        spec_hash=_sha("wp010-admission-spec"),
        experiment_id="wp010-admission",
        condition_id=os.environ["DOF_JOB_ID"],
        repeat_id="repeat-0",
        decision_id="wp010-admission-decision",
        context_bytes=b'{"admission":"deterministic_fake"}',
        prompt_bytes=b"Return one exact action proposal JSON object.",
        prompt_version="dof.prompt.v2",
        action_interface_version="dof.action-interface-schema.v2",
        action_interface_hash=_sha("action-interface"),
        context_interface_version="dof.context-interface-schema.v2",
        context_interface_hash=_sha("context-interface"),
        catalogue_version="e1v2-nested-M.v1",
        catalogue_hash=_sha("catalogue"),
        capability_hash=_sha("capability"),
        mask_evidence_hash=_sha("mask"),
        provider_role="wp010_admission_policy",
        provider="openrouter",
        model_id="openai/gpt-5.6-luna",
        adapter_version="wp010-deterministic-fake.v1",
        settings=ProviderSettings(
            temperature=None,
            max_tokens=1024,
            timeout_seconds=10,
            max_retries=0,
            retry_on=(),
            max_requests=1,
            max_usd=0.40,
            max_cost_per_request_usd=0.10,
            unknown_cost_policy="reject",
            unknown_usage_policy="reject",
        ),
    )
    transport = ProposalMismatchTransport(expected_outbound_binding=None)
    decision = DoFProviderObserver(
        run_ledger=ProviderRunLedger.for_binding(binding)
    ).request(binding, transport)
    evidence_errors = verify_provider_attempts(decision.attempts, binding)
    if evidence_errors:
        raise RuntimeError("proposal_terminal_evidence_invalid:" + ";".join(evidence_errors))
    attempt = decision.attempts[-1]
    if (
        decision.endpoint != "INVALID_POLICY_OUTPUT"
        or attempt.status is not ProviderAttemptStatus.POLICY_OUTPUT_REJECTED
        or decision.reasons != ("proposal_decision_id_mismatch",)
    ):
        raise RuntimeError("proposal_terminal_admission_failed")
    provider = {
        "physical_requests": transport.physical_requests,
        "known_cost_usd": float(attempt.cost_usd or 0.0),
        "unknown_cost_attempt_count": int(attempt.cost_usd is None),
        "reserved_unknown_cost_usd": (
            float(attempt.accounted_cost_usd) if attempt.cost_usd is None else 0.0
        ),
        "cost_lower_bound_usd": float(attempt.cost_usd or 0.0),
        "cost_upper_bound_usd": float(attempt.accounted_cost_usd),
        "known_token_total": int(attempt.total_tokens or 0),
        "unknown_usage_attempt_count": int(attempt.total_tokens is None),
    }
    result = _trajectory_result(
        os.environ["DOF_JOB_ID"], decision.endpoint, **provider
    )
    result["policy_output_subtype"] = "PROPOSAL_BINDING_MISMATCH"
    result["reasons"] = list(decision.reasons)
    result["provider_transport_accounting_accepted"] = True
    result["proposal_dispatchable"] = False
    result["action_dispatches"] = 0
    result["vivado_dispatches"] = 0
    result["retry_count"] = 0
    result["fallback_count"] = 0
    result["repair_count"] = 0
    core = {key: value for key, value in result.items() if key != "result_sha256"}
    result["result_sha256"] = content_hash(core)
    output = Path(os.environ["DOF_JOB_OUTPUT"])
    _write(output / "provider-attempt.json", attempt.to_dict())
    _write(output / "trajectory-result.json", result)


def success_job() -> None:
    _write(
        Path(os.environ["DOF_JOB_OUTPUT"]) / "trajectory-result.json",
        _trajectory_result(os.environ["DOF_JOB_ID"], "VALID_COMPLETION"),
    )


def nested_job() -> None:
    child = (
        "import signal,subprocess,sys,time;"
        "signal.signal(signal.SIGTERM,signal.SIG_IGN);"
        "subprocess.Popen([sys.executable,'-c',"
        "'import signal,time;signal.signal(signal.SIGTERM,signal.SIG_IGN);time.sleep(120)'],"
        "start_new_session=True);time.sleep(120)"
    )
    subprocess.Popen(
        [sys.executable, "-I", "-S", str(SUPERVISOR), "--", sys.executable, "-c", child],
        start_new_session=True,
    )
    registry = Path(os.environ["DOF_PROCESS_REGISTRY_DIR"]) / "records"
    deadline = time.monotonic() + 10
    while not list(registry.glob("*.json")) and time.monotonic() < deadline:
        time.sleep(0.01)
    if not list(registry.glob("*.json")):
        raise RuntimeError("nested_admission_registration_missing")
    time.sleep(120)


def run_admission(output: Path) -> dict[str, Any]:
    proposal_root = output / "proposal-terminal"
    proposal_queue = CompleteJobQueue(
        repository_anchor=REPOSITORY,
        output_root=proposal_root,
        max_concurrency=1,
        poll_seconds=0.01,
    )
    proposal_result = proposal_queue.run(
        [
            QueueJob("proposal-mismatch", (sys.executable, __file__, "proposal-job")),
            QueueJob("subsequent-fake", (sys.executable, __file__, "success-job")),
        ]
    )
    proposal_aggregate = aggregate_trajectory_results(proposal_root)

    nested_root = output / "nested-cleanup"
    nested_queue = CompleteJobQueue(
        repository_anchor=REPOSITORY,
        output_root=nested_root,
        max_concurrency=1,
        poll_seconds=0.01,
        terminate_grace_seconds=0.2,
        kill_grace_seconds=0.5,
    )
    nested_results: list[dict[str, Any]] = []
    thread = threading.Thread(
        target=lambda: nested_results.append(
            nested_queue.run(
                [QueueJob("nested-retaining", (sys.executable, __file__, "nested-job"))]
            )
        )
    )
    thread.start()
    records = (
        nested_root
        / "jobs/nested-retaining/nested-retaining--attempt-01/process-ownership/records"
    )
    deadline = time.monotonic() + 10
    while not list(records.glob("*.json")) if records.is_dir() else True:
        if time.monotonic() >= deadline:
            raise RuntimeError("nested_admission_registration_timeout")
        time.sleep(0.01)
    nested_queue.request_stop("wp010_nested_cleanup_admission")
    thread.join(timeout=15)
    if thread.is_alive() or not nested_results:
        raise RuntimeError("nested_admission_queue_did_not_stop")
    nested_ledger = json.loads(nested_queue.ledger_file.read_text())
    audits = nested_ledger["attempts"][0]["nested_process_registry"]["audits"]
    summary_core = {
        "schema_version": "research-flow.wp010-deterministic-admission.v1",
        "real_openrouter_requests": 0,
        "real_vivado_calls": 0,
        "proposal_terminal": {
            "queue_stopped": proposal_result["stopped"],
            "terminal_job_count": proposal_result["terminal_job_count"],
            "endpoint_counts": proposal_aggregate["endpoint_counts"],
            "queue_continued_after_mismatch": (
                proposal_result["stopped"] is False
                and proposal_result["terminal_job_count"] == 2
            ),
        },
        "nested_cleanup": {
            "automatic_cleanup": True,
            "manual_kill_commands": 0,
            "cleanup_quiescent": nested_results[0]["cleanup_quiescent"],
            "audit_count": len(audits),
            "audit_quiescent": [audit["quiescent"] for audit in audits],
        },
    }
    if (
        summary_core["proposal_terminal"]["queue_continued_after_mismatch"] is not True
        or summary_core["nested_cleanup"]["cleanup_quiescent"] is not True
        or summary_core["nested_cleanup"]["audit_quiescent"] != [True, True]
    ):
        raise RuntimeError("wp010_deterministic_admission_failed")
    summary = summary_core | {"summary_sha256": content_hash(summary_core)}
    _write(output / "admission-summary.json", summary)
    return summary


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run")
    run.add_argument("--output", type=Path, required=True)
    sub.add_parser("proposal-job")
    sub.add_parser("success-job")
    sub.add_parser("nested-job")
    args = parser.parse_args()
    if args.command == "proposal-job":
        proposal_job()
    elif args.command == "success-job":
        success_job()
    elif args.command == "nested-job":
        nested_job()
    else:
        print(json.dumps(run_admission(args.output.resolve()), sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
