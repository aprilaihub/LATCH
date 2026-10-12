#!/usr/bin/env python3
"""WP-016A-R10 typed-guard replay and directly authorised completion run."""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
import subprocess
import sys
from typing import Any, Mapping, Sequence

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from optimizer_pipeline.dof.wp016 import sha256  # noqa: E402
from optimizer_pipeline.dof.wp016_authority import (  # noqa: E402
    DurableJournal, consume_once, create_runtime_root, durable_write_exclusive,
)
from optimizer_pipeline.dof.wp016_canary import (  # noqa: E402
    budget_guard_receipt, clean_repository_identity, file_sha256,
    provider_binding_receipt, read_json, validate_credential_binding,
)
from optimizer_pipeline.dof.wp016_completion import (  # noqa: E402
    COMPLETION_REQUEST_CAP, R10_OFFLINE_REPLAY_NONCE,
    attempt2_bridge_receipt, combine_r10_completion_evidence,
    completion_identity_bindings, r09_bridge_receipt,
    r10_completion_manifest, r10_offline_replay_receipt, run_completion,
)
from optimizer_pipeline.dof.wp016_inputs import (  # noqa: E402
    verify_environment_manifest, verify_runtime_manifest,
    verify_scientific_input_manifest,
)
from optimizer_pipeline.dof.wp016_outcomes import taxonomy_manifest  # noqa: E402


TRACKED_AUTHORITY = (
    ROOT / "configs/wp016a/authority/WP016A_R10_DIRECT_EXECUTION_AUTHORITY.json"
)
R10_ATTEMPT2_BRIDGE = "WP-016A-R10-attempt2-bridge.json"
R10_R09_BRIDGE = "WP-016A-R10-r09-derived-bridge.json"
R10_MANIFEST = "WP-016A-R10-five-cell-manifest.json"
R10_TAXONOMY = "WP-016A-R10-terminal-taxonomy.json"
R10_PROVIDER_BINDING = "WP-016A-R10-provider-binding.json"
R10_BUDGET_GUARD = "WP-016A-R10-budget-guard.json"
R10_OFFLINE_REPLAY = "WP-016A-R10-five-cell-offline-replay.json"
R10_OFFLINE_LEDGER = "WP-016A-R10-combined-offline-ledger.json"
R10_ZERO_CALLS = "WP-016A-R10-zero-call-preflight.json"


def _write(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8",
    )


def _exact_authority(path: Path) -> dict[str, Any]:
    value = read_json(path)
    tracked = read_json(TRACKED_AUTHORITY)
    if value != tracked:
        raise RuntimeError("r10_direct_authority_drift")
    required = {
        "schema_version": "research-flow.wp016a-r10-direct-execution-authority.v1",
        "wp_id": "WP-016A",
        "revision": "R10",
        "decision": "AUTHORISE_IMPLEMENT_TEST_AND_EXECUTE_ONCE",
        "authority_mode": "WORK_PACKAGE_DIRECT_SMALL_EXPERIMENT",
        "base_commit": "9093a3f12b2bb6c4a08731e4266ef7b160c44dad",
        "base_tree": "dc3c67df32e840cfd058e2f13c5c4be885aa718e",
        "live_cells": 5,
        "maximum_provider_requests": 20,
        "literal_usd_cap": 0.5,
        "one_attempt_only": True,
        "separate_live_token_required": False,
        "scientific_analysis_eligible": False,
        "wp016b_authorised": False,
    }
    mismatch = {
        key: {"expected": expected, "observed": value.get(key)}
        for key, expected in required.items() if value.get(key) != expected
    }
    if mismatch:
        raise RuntimeError("r10_direct_authority_invalid:" + json.dumps(mismatch, sort_keys=True))
    return value


def _repository_identity(authority: Mapping[str, Any]) -> tuple[str, str]:
    commit, tree = clean_repository_identity()
    completed = subprocess.run(
        ["git", "merge-base", "--is-ancestor", str(authority["base_commit"]), commit],
        cwd=ROOT, check=False, capture_output=True, text=True,
    )
    if completed.returncode != 0:
        raise RuntimeError("r10_base_commit_not_ancestor")
    return commit, tree


def _r09_bridge(args: argparse.Namespace) -> dict[str, Any]:
    return r09_bridge_receipt(
        read_json(args.r09_completion_result),
        read_json(args.r09_partial_failure),
        read_json(args.r09_response_turn1),
        completion_result_sha256=file_sha256(args.r09_completion_result),
        partial_failure_sha256=file_sha256(args.r09_partial_failure),
        response_turn1_sha256=file_sha256(args.r09_response_turn1),
    )


def _attempt2_bridge(args: argparse.Namespace) -> dict[str, Any]:
    return attempt2_bridge_receipt(
        read_json(args.attempt2_canary_result),
        source_file_sha256=file_sha256(args.attempt2_canary_result),
    )


async def _offline_values(args: argparse.Namespace) -> dict[str, Any]:
    attempt2 = _attempt2_bridge(args)
    r09 = _r09_bridge(args)
    result = await run_completion(
        live=False,
        scientific_input_manifest=args.scientific_input_manifest,
        run_nonce=R10_OFFLINE_REPLAY_NONCE,
    )
    replay = r10_offline_replay_receipt(result)
    ledger = combine_r10_completion_evidence(attempt2, r09, result)
    if ledger["status"] != "PASS_WP016A_INTEGRATION_COVERAGE":
        raise RuntimeError("r10_offline_combined_ledger_no_go")
    return {
        R10_ATTEMPT2_BRIDGE: attempt2,
        R10_R09_BRIDGE: r09,
        R10_MANIFEST: r10_completion_manifest(),
        R10_TAXONOMY: taxonomy_manifest(),
        R10_PROVIDER_BINDING: provider_binding_receipt(),
        R10_BUDGET_GUARD: budget_guard_receipt(COMPLETION_REQUEST_CAP),
        R10_OFFLINE_REPLAY: replay,
        R10_OFFLINE_LEDGER: ledger,
        R10_ZERO_CALLS: {
            "schema_version": "wp016a.r10.zero-call-preflight.v1",
            "provider_calls": 0,
            "backend_calls": 0,
            "maximum_provider_requests": 20,
            "literal_usd_cap": 0.5,
            "scientific_analysis_eligible": False,
            "wp016b_authorised": False,
        },
    }


def _common_preconditions(args: argparse.Namespace) -> tuple[dict[str, Any], str, str]:
    authority = _exact_authority(args.authority)
    commit, tree = _repository_identity(authority)
    verify_scientific_input_manifest(args.scientific_input_manifest)
    return authority, commit, tree


def _offline(args: argparse.Namespace) -> int:
    if args.output_dir is None or args.output_dir.exists():
        raise SystemExit("R10_OFFLINE_REQUIRES_NEW_OUTPUT_DIR")
    _common_preconditions(args)
    values = asyncio.run(_offline_values(args))
    args.output_dir.mkdir(parents=True)
    for name, value in values.items():
        _write(args.output_dir / name, value)
    print(json.dumps({
        "status": "PASS_R10_OFFLINE_PREFLIGHT",
        "provider_calls": 0,
        "backend_calls": 0,
        "output_dir": str(args.output_dir),
    }, sort_keys=True))
    return 0


def _verify_exact(path: Path, expected: Mapping[str, Any], reason: str) -> None:
    if read_json(path) != expected:
        raise RuntimeError(reason)


def _live(args: argparse.Namespace) -> int:
    required = (
        args.preflight_dir, args.environment_manifest, args.runtime_manifest,
        args.runtime_base,
    )
    if not all(required) or args.output_dir is not None:
        raise SystemExit("R10_LIVE_REQUIRES_PREFLIGHT_ENVIRONMENT_AND_RUNTIME")
    authority, commit, tree = _common_preconditions(args)
    verify_environment_manifest(args.environment_manifest, repository_root=ROOT)
    runtime = verify_runtime_manifest(
        args.runtime_manifest, runtime_base=args.runtime_base,
    )
    if runtime.get("cell_subdirectory_count") != 5:
        raise RuntimeError("r10_runtime_manifest_not_five_cells")
    values = asyncio.run(_offline_values(args))
    for name, expected in values.items():
        _verify_exact(args.preflight_dir / name, expected, "r10_preflight_drift:" + name)
    validate_credential_binding(args.preflight_dir / R10_PROVIDER_BINDING)

    controller_hash = sha256({
        "runner": file_sha256(Path(__file__).resolve()),
        "canary": file_sha256(ROOT / "optimizer_pipeline/dof/wp016_canary.py"),
        "completion": file_sha256(ROOT / "optimizer_pipeline/dof/wp016_completion.py"),
        "outcomes": file_sha256(ROOT / "optimizer_pipeline/dof/wp016_outcomes.py"),
        "provider": file_sha256(ROOT / "optimizer_pipeline/dof/multi_provider.py"),
    })
    authority_sha = file_sha256(args.authority)
    run_nonce = "wp016a-r10-five-cell-live-" + sha256({
        "authority_sha256": authority_sha,
        "commit": commit,
        "tree": tree,
        "controller_sha256": controller_hash,
    })
    runtime_root = create_runtime_root(
        runtime_base=args.runtime_base,
        run_nonce=run_nonce,
        cell_bindings=completion_identity_bindings(run_nonce),
        expected_cell_count=5,
    )
    journal = DurableJournal(runtime_root)
    consumption_token = dict(authority) | {
        "run_nonce": "wp016a-r10-direct-authority-one-shot",
    }
    lease = None
    result: dict[str, Any] | None = None
    ledger: dict[str, Any] | None = None
    try:
        lease = consume_once(
            token=consumption_token,
            authority_root=args.runtime_base / "authority",
        )
        authority_receipt = {
            "schema_version": "wp016a.r10.direct-authority-consumption.v1",
            "status": "CONSUMED_STARTING",
            "authority_sha256": authority_sha,
            "consumption_token_sha256": lease.token_sha256,
            "run_nonce": run_nonce,
            "commit": commit,
            "tree": tree,
            "controller_sha256": controller_hash,
            "maximum_provider_requests": 20,
            "literal_usd_cap": 0.5,
            "one_attempt_only": True,
            "backend_calls_authorised": 0,
            "wp016b_authorised": False,
        }
        durable_write_exclusive(
            runtime_root / "authority/direct-authority-receipt.json",
            authority_receipt,
        )
        journal.append(kind="authority-consumed", value=authority_receipt)

        # Recheck every precondition after durable consumption and before the
        # first request is constructed.
        _common_preconditions(args)
        verify_environment_manifest(args.environment_manifest, repository_root=ROOT)
        verify_runtime_manifest(args.runtime_manifest, runtime_base=args.runtime_base)
        validate_credential_binding(args.preflight_dir / R10_PROVIDER_BINDING)

        result = asyncio.run(run_completion(
            live=True,
            scientific_input_manifest=args.scientific_input_manifest,
            run_nonce=run_nonce,
            journal=journal,
        ))
        attempt2 = read_json(args.preflight_dir / R10_ATTEMPT2_BRIDGE)
        r09 = read_json(args.preflight_dir / R10_R09_BRIDGE)
        ledger = combine_r10_completion_evidence(attempt2, r09, result)
        durable_write_exclusive(
            runtime_root / "final/combined-six-cell-ledger.json", ledger,
        )
        journal.completion_terminal(status=result["status"], value={
            "revision": "R10",
            "result": result,
            "combined_ledger": ledger,
            "direct_authority_receipt": authority_receipt,
            "provider_calls": result["provider_calls"],
            "backend_calls": result["backend_calls"],
            "one_shot_remains_consumed": True,
            "scientific_analysis_eligible": False,
            "wp016b_authorised": False,
        })
    except BaseException as exc:
        if lease is not None:
            journal.append(kind="terminal-failure", value={
                "status": "BLOCKED_FINAL_R10_SHARED_INTEGRATION_FAILURE",
                "failure_type": type(exc).__name__,
                "failure_message": str(exc),
                "provider_calls": 0 if result is None else result.get("provider_calls", 0),
                "backend_calls": 0,
                "one_shot_remains_consumed": True,
            })
            journal.completion_terminal(
                status="BLOCKED_FINAL_R10_SHARED_INTEGRATION_FAILURE",
                value={
                    "revision": "R10",
                    "failure_type": type(exc).__name__,
                    "failure_message": str(exc),
                    "partial_result": result,
                    "partial_combined_ledger": ledger,
                    "backend_calls": 0,
                    "one_shot_remains_consumed": True,
                    "scientific_analysis_eligible": False,
                    "wp016b_authorised": False,
                },
            )
        raise
    finally:
        if lease is not None:
            lease.close()

    assert result is not None and ledger is not None
    print(json.dumps({
        "status": result["status"],
        "combined_status": ledger["status"],
        "configurations": result["configurations_replayed"],
        "provider_calls": result["provider_calls"],
        "backend_calls": result["backend_calls"],
        "known_cost_usd": result["known_charged_cost_usd"],
        "runtime_root": str(runtime_root),
    }, sort_keys=True))
    return 0 if ledger["status"] == "PASS_WP016A_INTEGRATION_COVERAGE" else 2


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument("--offline-preflight", action="store_true")
    modes.add_argument("--live-completion", action="store_true")
    parser.add_argument("--authority", type=Path, required=True)
    parser.add_argument("--scientific-input-manifest", type=Path, required=True)
    parser.add_argument("--attempt2-canary-result", type=Path, required=True)
    parser.add_argument("--r09-completion-result", type=Path, required=True)
    parser.add_argument("--r09-partial-failure", type=Path, required=True)
    parser.add_argument("--r09-response-turn1", type=Path, required=True)
    parser.add_argument("--preflight-dir", type=Path)
    parser.add_argument("--environment-manifest", type=Path)
    parser.add_argument("--runtime-manifest", type=Path)
    parser.add_argument("--runtime-base", type=Path)
    parser.add_argument("--output-dir", type=Path)
    args = parser.parse_args(argv)
    return _live(args) if args.live_completion else _offline(args)


if __name__ == "__main__":
    raise SystemExit(main())
