#!/usr/bin/env python3
"""WP-016A controller entry point; offline is the only default mode."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from optimizer_pipeline.dof.wp016_canary import (  # noqa: E402
    load_source_rows, materialize_static_manifests, run_all, validate_live_token,
    runtime_identity_bindings, validate_credential_binding,
)
from optimizer_pipeline.dof.wp016_authority import (  # noqa: E402
    DurableJournal, consume_once, create_runtime_root, durable_write_exclusive,
)
from optimizer_pipeline.dof.wp016_inputs import (  # noqa: E402
    verify_environment_manifest, verify_runtime_manifest,
    verify_scientific_input_manifest,
)


def _failure_status(exc: BaseException) -> str:
    message = str(exc)
    if message.startswith("NO_GO_"):
        return message.split(":", 1)[0]
    if "credential" in message.lower():
        return "NO_GO_CREDENTIAL_UNAVAILABLE"
    if "cache" in message.lower():
        return "NO_GO_CACHE_OR_REUSE_POLICY"
    if "budget" in message.lower() or "request_cap" in message.lower():
        return "NO_GO_BUDGET_OR_REQUEST_CAP"
    if "provider" in message.lower() or "transport" in message.lower() or "http" in message.lower():
        return "NO_GO_PROVIDER_OR_TRANSPORT"
    return "NO_GO_CONTROLLER_OR_ADAPTER"


def prepare_live_runtime(*, token: dict[str, object], manifest_dir: Path,
                         runtime_base: Path) -> tuple[Path, DurableJournal]:
    """Perform credential and collision checks without consuming authority."""
    validate_credential_binding(manifest_dir / "provider-binding.json")
    identity_bindings = runtime_identity_bindings(
        load_source_rows(), str(token["run_nonce"]),
    )
    runtime_root = create_runtime_root(
        runtime_base=runtime_base, run_nonce=str(token["run_nonce"]),
        cell_bindings=identity_bindings,
    )
    return runtime_root, DurableJournal(runtime_root)


def main() -> int:
    parser = argparse.ArgumentParser()
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--offline-canned-only", action="store_true")
    modes.add_argument("--live-canary", action="store_true")
    parser.add_argument("--authority", type=Path)
    parser.add_argument("--offline-replay-receipt", type=Path)
    parser.add_argument("--scientific-input-manifest", type=Path)
    parser.add_argument("--environment-manifest", type=Path)
    parser.add_argument("--runtime-manifest", type=Path)
    parser.add_argument("--runtime-base", type=Path)
    parser.add_argument("--manifest-dir", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--describe", action="store_true")
    args = parser.parse_args()

    if args.describe:
        print(json.dumps({
            "default_mode": "OFFLINE_CANNED_ONLY",
            "live_mode_requires_hash_bound_authority": True,
            "provider_calls": 0,
            "backend_calls": 0,
        }, sort_keys=True))
        return 0

    live = bool(args.live_canary)
    if not live and args.authority is not None:
        raise SystemExit("AUTHORITY_NOT_ACCEPTED_IN_OFFLINE_MODE")
    if live:
        if not all((args.authority, args.offline_replay_receipt, args.manifest_dir,
                    args.scientific_input_manifest, args.environment_manifest,
                    args.runtime_manifest, args.runtime_base)):
            raise SystemExit("LIVE_REQUIRES_AUTHORITY_AND_ALL_HASH_BOUND_MANIFESTS")
        if args.output is not None:
            raise SystemExit("LIVE_OUTPUT_IS_TOKEN_DERIVED_NOT_CALLER_SELECTED")
        token = validate_live_token(
            args.authority, args.offline_replay_receipt, args.manifest_dir,
            Path(__file__).resolve(),
            scientific_input_manifest=args.scientific_input_manifest,
            environment_manifest=args.environment_manifest,
            runtime_manifest_path=args.runtime_manifest,
            runtime_base=args.runtime_base,
        )
        # Missing or drifted credentials are setup failures and must not burn
        # the one-shot authority.
        # A collision is rejected before consuming the one-shot token.  Once
        # consumption succeeds, every later failure is terminal and durable.
        runtime_root, journal = prepare_live_runtime(
            token=token, manifest_dir=args.manifest_dir,
            runtime_base=args.runtime_base,
        )
        lease = None
        try:
            lease = consume_once(token=token, authority_root=args.runtime_base / "authority")
            durable_write_exclusive(runtime_root / "authority" / "consumption-receipt.json", {
                "schema_version": "wp016a.runtime-authority-receipt.v1",
                "status": "CONSUMED_STARTING",
                "token_sha256": lease.token_sha256,
                "run_nonce": lease.run_nonce,
                "global_consumption_path_identity": lease.consumption_path.name,
            })
            journal.append(kind="authority-consumed", value={
                "status": "CONSUMED_BEFORE_REQUEST_CONSTRUCTION",
                "token_sha256": lease.token_sha256,
                "run_nonce_sha256": hashlib.sha256(token["run_nonce"].encode()).hexdigest(),
            })
            # Reverify after durable consumption and immediately before any
            # model-visible request can be constructed.
            verify_scientific_input_manifest(args.scientific_input_manifest)
            verify_environment_manifest(args.environment_manifest, repository_root=ROOT)
            verify_runtime_manifest(args.runtime_manifest, runtime_base=args.runtime_base)
            validate_credential_binding(args.manifest_dir / "provider-binding.json")
            result = asyncio.run(run_all(
                live=True,
                scientific_input_manifest=args.scientific_input_manifest,
                run_nonce=token["run_nonce"],
                journal=journal,
            ))
            journal.terminal(status=result["status"], value={
                "result": result,
                "provider_calls": result["provider_calls"],
                "backend_calls": result["backend_calls"],
            })
        except BaseException as exc:
            status = _failure_status(exc)
            journal.append(kind="terminal-failure", value={
                "status": status,
                "failure_type": type(exc).__name__,
                "failure_message": str(exc),
                "one_shot_remains_consumed": lease is not None,
            })
            journal.terminal(status=status, value={
                "failure_type": type(exc).__name__,
                "failure_message": str(exc),
                "one_shot_remains_consumed": lease is not None,
                "backend_calls": 0,
            })
            raise
        finally:
            if lease is not None:
                lease.close()
        print(json.dumps({
            "status": result["status"], "mode": result["mode"],
            "configurations": result["configurations_replayed"],
            "provider_calls": result["provider_calls"], "backend_calls": result["backend_calls"],
            "runtime_root": str(runtime_root),
        }, sort_keys=True))
        return 0 if result["status"] == "PASS_CANARY_INTEGRATION" else 2
    else:
        # Omitting a mode is deliberately identical to explicit offline mode.
        if args.offline_replay_receipt is not None:
            raise SystemExit("OFFLINE_REPLAY_INPUT_IS_LIVE_ONLY")
        if args.output is None or args.manifest_dir is None or args.scientific_input_manifest is None:
            raise SystemExit("OFFLINE_REQUIRES_MANIFEST_DIR_OUTPUT_AND_SCIENTIFIC_INPUT_MANIFEST")
        if any((args.environment_manifest, args.runtime_manifest, args.runtime_base)):
            raise SystemExit("LIVE_RUNTIME_INPUTS_NOT_ACCEPTED_IN_OFFLINE_MODE")

    assert args.output is not None and args.manifest_dir is not None
    if args.output.exists():
        raise SystemExit("OUTPUT_ALREADY_EXISTS")
    if not live:
        if args.manifest_dir.exists():
            raise SystemExit("MANIFEST_DIR_ALREADY_EXISTS")
        materialize_static_manifests(args.manifest_dir)
    result = asyncio.run(run_all(
        live=False, scientific_input_manifest=args.scientific_input_manifest,
    ))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({
        "status": result["status"], "mode": result["mode"],
        "configurations": result["configurations_replayed"],
        "provider_calls": result["provider_calls"], "backend_calls": result["backend_calls"],
    }, sort_keys=True))
    return 0 if result["status"] == "PASS_CANARY_INTEGRATION" else 2


if __name__ == "__main__":
    raise SystemExit(main())
