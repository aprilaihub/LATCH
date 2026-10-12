#!/usr/bin/env python3
"""WP-016A-R08 five-cell completion runner; offline unless token-authorised."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
from pathlib import Path
import sys
from typing import Any, Sequence

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from optimizer_pipeline.dof.wp016_authority import (  # noqa: E402
    DurableJournal, consume_once, create_runtime_root, durable_write_exclusive,
)
from optimizer_pipeline.dof.wp016_canary import (  # noqa: E402
    file_sha256, read_json, validate_credential_binding,
)
from optimizer_pipeline.dof.wp016_completion import (  # noqa: E402
    OFFLINE_REPLAY_NONCE, R08_OFFLINE_REPLAY, R08_PROVIDER_BINDING,
    attempt2_bridge_receipt, combine_completion_evidence,
    completion_identity_bindings, offline_replay_receipt,
    r08_static_manifests, run_completion, validate_completion_live_token,
)


R08_COMBINED_OFFLINE_LEDGER = "WP-016A-R08-combined-offline-ledger-v2.json"
R08_ZERO_CALL_COUNTERS = "WP-016A-R08-zero-call-counters.json"


def _write(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8",
    )


def _failure_status(exc: BaseException) -> str:
    message = str(exc)
    if message.startswith("NO_GO_"):
        return message.split(":", 1)[0]
    lowered = message.lower()
    if "credential" in lowered:
        return "NO_GO_CREDENTIAL_UNAVAILABLE"
    if "cache" in lowered:
        return "NO_GO_CACHE_OR_REUSE_POLICY"
    if "budget" in lowered or "request_cap" in lowered:
        return "NO_GO_BUDGET_OR_REQUEST_CAP"
    if any(word in lowered for word in ("provider", "transport", "http")):
        return "NO_GO_PROVIDER_OR_TRANSPORT"
    return "NO_GO_CONTROLLER_OR_ADAPTER"


def prepare_completion_live_runtime(
    *, token: dict[str, Any], manifest_dir: Path, runtime_base: Path,
) -> tuple[Path, DurableJournal]:
    """Check credential and collision, then create exactly five runtime cells."""
    validate_credential_binding(manifest_dir / R08_PROVIDER_BINDING)
    bindings = completion_identity_bindings(str(token["run_nonce"]))
    runtime_root = create_runtime_root(
        runtime_base=runtime_base,
        run_nonce=str(token["run_nonce"]),
        cell_bindings=bindings,
        expected_cell_count=5,
    )
    return runtime_root, DurableJournal(runtime_root)


def _validation_kwargs(args: argparse.Namespace) -> dict[str, Any]:
    return {
        "controller_path": Path(__file__).resolve(),
        "manifest_dir": args.manifest_dir,
        "attempt2_canary_result": args.attempt2_canary_result,
        "attempt2_bridge_path": args.attempt2_bridge,
        "combined_ledger_template_path": args.combined_ledger_template,
        "offline_replay_receipt_path": args.offline_replay_receipt,
        "scientific_input_manifest": args.scientific_input_manifest,
        "environment_manifest": args.environment_manifest,
        "runtime_manifest_path": args.runtime_manifest,
        "runtime_base": args.runtime_base,
    }


def _run_offline(args: argparse.Namespace) -> int:
    required = (
        args.scientific_input_manifest, args.attempt2_canary_result,
        args.output_dir, args.run_nonce,
    )
    if not all(required):
        raise SystemExit("R08_OFFLINE_REQUIRES_SOURCE_INPUT_OUTPUT_AND_NONCE")
    if args.run_nonce != OFFLINE_REPLAY_NONCE:
        raise SystemExit("R08_OFFLINE_REPLAY_NONCE_MISMATCH")
    if any((
        args.authority, args.manifest_dir, args.attempt2_bridge,
        args.combined_ledger_template, args.offline_replay_receipt,
        args.environment_manifest, args.runtime_manifest, args.runtime_base,
    )):
        raise SystemExit("R08_LIVE_INPUTS_NOT_ACCEPTED_IN_OFFLINE_MODE")
    if args.output_dir.exists():
        raise SystemExit("R08_OUTPUT_DIR_ALREADY_EXISTS")

    source = read_json(args.attempt2_canary_result)
    bridge = attempt2_bridge_receipt(
        source, source_file_sha256=file_sha256(args.attempt2_canary_result),
    )
    result = asyncio.run(run_completion(
        live=False,
        scientific_input_manifest=args.scientific_input_manifest,
        run_nonce=OFFLINE_REPLAY_NONCE,
    ))
    if result["provider_calls"] != 0 or result["backend_calls"] != 0:
        raise RuntimeError("r08_zero_call_firewall_breached")

    args.output_dir.mkdir(parents=True)
    for name, value in r08_static_manifests(bridge=bridge).items():
        _write(args.output_dir / name, value)
    replay = offline_replay_receipt(result)
    _write(args.output_dir / R08_OFFLINE_REPLAY, replay)
    _write(
        args.output_dir / R08_COMBINED_OFFLINE_LEDGER,
        combine_completion_evidence(bridge, result),
    )
    _write(args.output_dir / R08_ZERO_CALL_COUNTERS, {
        "schema_version": "wp016a.r08.zero-call-counters.v1",
        "revision": "R08",
        "provider_calls": 0,
        "backend_calls": 0,
        "completion_live_token_issued": False,
        "wp016b_authorised": False,
    })
    print(json.dumps({
        "status": "READY_FOR_FIVE_CELL_LIVE_TOKEN",
        "offline_replay_status": result["status"],
        "offline_replay_nonce": OFFLINE_REPLAY_NONCE,
        "configurations": result["configurations_replayed"],
        "provider_calls": 0,
        "backend_calls": 0,
        "output_dir": str(args.output_dir),
    }, sort_keys=True))
    return 0


def _run_live(args: argparse.Namespace) -> int:
    required = (
        args.authority, args.manifest_dir, args.attempt2_canary_result,
        args.attempt2_bridge, args.combined_ledger_template,
        args.offline_replay_receipt, args.scientific_input_manifest,
        args.environment_manifest, args.runtime_manifest, args.runtime_base,
    )
    if not all(required):
        raise SystemExit("R08_LIVE_REQUIRES_ALL_HASH_BOUND_INPUTS")
    if args.output_dir is not None or args.run_nonce is not None:
        raise SystemExit("R08_LIVE_OUTPUT_AND_NONCE_ARE_TOKEN_DERIVED")

    token = validate_completion_live_token(
        args.authority, **_validation_kwargs(args),
    )
    runtime_root, journal = prepare_completion_live_runtime(
        token=token, manifest_dir=args.manifest_dir,
        runtime_base=args.runtime_base,
    )
    lease = None
    result: dict[str, Any] | None = None
    ledger: dict[str, Any] | None = None
    try:
        lease = consume_once(
            token=token, authority_root=args.runtime_base / "authority",
        )
        durable_write_exclusive(
            runtime_root / "authority" / "consumption-receipt.json", {
                "schema_version": "wp016a.r08.runtime-authority-receipt.v1",
                "status": "CONSUMED_STARTING",
                "token_sha256": lease.token_sha256,
                "run_nonce": lease.run_nonce,
                "global_consumption_path_identity": lease.consumption_path.name,
            },
        )
        journal.append(kind="authority-consumed", value={
            "status": "CONSUMED_BEFORE_REQUEST_CONSTRUCTION",
            "token_sha256": lease.token_sha256,
            "run_nonce_sha256": hashlib.sha256(
                str(token["run_nonce"]).encode()
            ).hexdigest(),
        })

        # Revalidate every bound input after durable consumption and before
        # construction of the first provider request.
        validate_completion_live_token(
            args.authority, **_validation_kwargs(args),
        )
        bridge = read_json(args.attempt2_bridge)
        result = asyncio.run(run_completion(
            live=True,
            scientific_input_manifest=args.scientific_input_manifest,
            run_nonce=str(token["run_nonce"]),
            journal=journal,
        ))
        ledger = combine_completion_evidence(bridge, result)
        durable_write_exclusive(
            runtime_root / "final" / "combined-six-cell-ledger.json", ledger,
        )
        journal.completion_terminal(status=result["status"], value={
            "result": result,
            "combined_ledger": ledger,
            "provider_calls": result["provider_calls"],
            "backend_calls": result["backend_calls"],
            "one_shot_remains_consumed": True,
        })
    except BaseException as exc:
        status = _failure_status(exc)
        journal.append(kind="terminal-failure", value={
            "status": status,
            "failure_type": type(exc).__name__,
            "failure_message": str(exc),
            "one_shot_remains_consumed": lease is not None,
        })
        journal.completion_terminal(status=status, value={
            "failure_type": type(exc).__name__,
            "failure_message": str(exc),
            "partial_result": result,
            "partial_combined_ledger": ledger,
            "one_shot_remains_consumed": lease is not None,
            "backend_calls": 0,
        })
        raise
    finally:
        if lease is not None:
            lease.close()

    assert result is not None and ledger is not None
    print(json.dumps({
        "status": result["status"],
        "combined_status": ledger["status"],
        "mode": result["mode"],
        "configurations": result["configurations_replayed"],
        "provider_calls": result["provider_calls"],
        "backend_calls": result["backend_calls"],
        "runtime_root": str(runtime_root),
    }, sort_keys=True))
    return 0 if ledger["status"] == "PASS_WP016A_INTEGRATION_COVERAGE" else 2


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--offline-canned-only", action="store_true")
    modes.add_argument("--live-completion", action="store_true")
    parser.add_argument("--authority", type=Path)
    parser.add_argument("--manifest-dir", type=Path)
    parser.add_argument("--attempt2-canary-result", type=Path)
    parser.add_argument("--attempt2-bridge", type=Path)
    parser.add_argument("--combined-ledger-template", type=Path)
    parser.add_argument("--offline-replay-receipt", type=Path)
    parser.add_argument("--scientific-input-manifest", type=Path)
    parser.add_argument("--environment-manifest", type=Path)
    parser.add_argument("--runtime-manifest", type=Path)
    parser.add_argument("--runtime-base", type=Path)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--run-nonce")
    parser.add_argument("--describe", action="store_true")
    args = parser.parse_args(argv)

    if args.describe:
        print(json.dumps({
            "wp_id": "WP-016A",
            "revision": "R08",
            "default_mode": "OFFLINE_CANNED_ONLY",
            "live_mode": "TOKEN_REQUIRED_NOT_ISSUED",
            "completion_cells": 5,
            "maximum_provider_requests": 20,
            "literal_usd_cap": 0.5,
            "offline_replay_nonce": OFFLINE_REPLAY_NONCE,
            "provider_calls_at_r08": 0,
            "backend_calls_at_r08": 0,
            "wp016b_authorised": False,
        }, sort_keys=True))
        return 0
    if args.live_completion:
        return _run_live(args)
    return _run_offline(args)


if __name__ == "__main__":
    raise SystemExit(main())
