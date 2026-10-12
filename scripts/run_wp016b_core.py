#!/usr/bin/env python3
"""Offline preflight and one-shot live execution for WP-016B Core."""

from __future__ import annotations

import argparse
import asyncio
import csv
from datetime import datetime, timezone
from decimal import Decimal
import json
import hashlib
import math
import os
from pathlib import Path
import subprocess
import sys
import time
from typing import Any, Mapping

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from optimizer_pipeline.dof.wp016 import canonical_bytes, sha256  # noqa: E402
from optimizer_pipeline.dof.wp016_authority import DurableJournal, durable_write_exclusive  # noqa: E402
from optimizer_pipeline.dof.wp016_canary import (  # noqa: E402
    LiteralBudget, ScientificInputs, file_sha256, read_json,
)
from optimizer_pipeline.dof.wp016b import (  # noqa: E402
    ANALYSIS_CONTRACT, AUTHORITY, CONCURRENCY_AMENDMENT, CONFIG_ROOT,
    CoreController, INPUT_GUARD, MANIFEST, MAX_CONCURRENCY, REQUEST_CAP,
    ROOTS, SCHEDULE, SOURCE_BUNDLE, SOURCE_LEDGER, USD_CAP, exact_authority,
    load_source_rows,
)


PRICING = ROOT / ".research-flow/source/WP015-Pricing-Snapshot-2026-08-14.json"
REGISTRY = ROOT / "configs/wp016a/closed-retrieval-registry.json"
ENVIRONMENT_CONTRACT = CONFIG_ROOT / "environment-contract.json"
PREFLIGHT_RECEIPT = CONFIG_ROOT / "preflight-receipt.json"
OFFLINE_REPLAY_RECEIPT = CONFIG_ROOT / "offline-replay-receipt.json"
MAX_INPUT_TOKENS_PER_REQUEST_PREFLIGHT = 22_000
LABEL = "MODEL-ONLY PROPOSAL-CONSTRUCTION STUDY / NO PHYSICAL / FUNCTIONAL / FMAX OUTCOME"


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def git(*args: str) -> str:
    return subprocess.check_output(["git", *args], cwd=ROOT, text=True).strip()


def frozen_hashes() -> dict[str, str]:
    paths = {
        "authority": AUTHORITY, "source_ledger": SOURCE_LEDGER,
        "concurrency_amendment": CONCURRENCY_AMENDMENT,
        "source_bundle": SOURCE_BUNDLE, "manifest": MANIFEST,
        "schedule": SCHEDULE, "analysis_contract": ANALYSIS_CONTRACT,
        "registry": REGISTRY, "pricing": PRICING,
        "environment_contract": ENVIRONMENT_CONTRACT,
        "preflight_receipt": PREFLIGHT_RECEIPT,
        "offline_replay_receipt": OFFLINE_REPLAY_RECEIPT,
        "controller": ROOT / "optimizer_pipeline/dof/wp016_canary.py",
        "core": ROOT / "optimizer_pipeline/dof/wp016b.py",
        "runner": Path(__file__).resolve(),
    }
    return {name: file_sha256(path) for name, path in paths.items()}


def exact_static_invariants() -> tuple[dict[str, Any], dict[str, Any], list[dict[str, str]]]:
    exact_authority()
    manifest, schedule, rows = read_json(MANIFEST), read_json(SCHEDULE), load_source_rows()
    cells = manifest["cells"]
    if len(cells) != 48 or len(schedule["blocks"]) != 16 or len(rows) != 24:
        raise RuntimeError("wp016b_inventory_arithmetic_invalid")
    if schedule.get("maximum_concurrency") != MAX_CONCURRENCY:
        raise RuntimeError("wp016b_concurrency_amendment_not_frozen")
    if {row["root_id"] for row in cells} != set(ROOTS):
        raise RuntimeError("wp016b_root_inventory_invalid")
    if any(root in {"titan23_orig_gsm_x6", "mlcad_d181"} for root in (row["root_id"] for row in cells)):
        raise RuntimeError("wp016b_extension_root_leak")
    for block in schedule["blocks"]:
        if len(block["cells"]) != 3 or {row["arm"] for row in block["cells"]} != {
            "LEGACY_STATIC", "COMPACT_RETRIEVAL_LOW_RESERVE", "COMPACT_RETRIEVAL_MATCHED_RESERVE",
        }:
            raise RuntimeError("wp016b_block_arm_inventory_invalid")
    identity_keys = ("configuration_id", "cell_id", "session_id", "run_id", "log_id", "output_id")
    if any(len({row[key] for row in cells}) != 48 for key in identity_keys):
        raise RuntimeError("wp016b_runtime_identity_collision")
    if any("canary" in json.dumps(row).lower() for row in cells):
        raise RuntimeError("wp016b_canary_identity_leak")
    environment = read_json(ENVIRONMENT_CONTRACT)
    import platform
    import httpx
    try:
        import tiktoken
    except ImportError as exc:
        raise RuntimeError("wp016b_tiktoken_environment_missing") from exc
    observed = {
        "python_version": platform.python_version(), "httpx_version": httpx.__version__,
        "tiktoken_version": tiktoken.__version__,
    }
    if any(environment.get(key) != value for key, value in observed.items()):
        raise RuntimeError("wp016b_environment_contract_drift")
    return manifest, schedule, rows


def _pricing_row(model: str) -> Mapping[str, Any]:
    return read_json(PRICING)["providers"]["openai"]["per_million_tokens"][model.split("/", 1)[1]]


def maximum_request_cost(model: str, input_tokens: int) -> Decimal:
    row = _pricing_row(model)
    return (
        Decimal(input_tokens) * Decimal(str(row["input"]))
        + Decimal(4_096) * Decimal(str(row["output"]))
    ) / Decimal(1_000_000)


def whole_core_preflight_cost(schedule: Mapping[str, Any]) -> Decimal:
    return sum(
        maximum_request_cost(str(block["model_system"]), MAX_INPUT_TOKENS_PER_REQUEST_PREFLIGHT)
        * Decimal(12)
        for block in schedule["blocks"]
    )


class ExactBodyTokenBounder:
    def __init__(self) -> None:
        try:
            import tiktoken
        except ImportError as exc:
            raise RuntimeError("wp016b_tiktoken_required_for_live_budget_guard") from exc
        self.encoding = tiktoken.get_encoding("o200k_base")

    def __call__(self, body: Mapping[str, Any], model: str) -> Decimal:
        # Encoding the entire canonical request body (rather than only message
        # content) plus fixed framing headroom upper-bounds the observed prompt
        # count for the accepted OpenAI chat envelope.
        count = len(self.encoding.encode(canonical_bytes(body).decode("utf-8"))) + 64
        if count > MAX_INPUT_TOKENS_PER_REQUEST_PREFLIGHT:
            raise RuntimeError("wp016b_constructed_request_exceeds_frozen_token_preflight")
        return maximum_request_cost(model, count)


def _offline_envelope(model: str, content: Mapping[str, Any], suffix: str) -> bytes:
    return canonical_bytes({
        "id": "wp016b-offline-" + suffix, "model": model.split("/", 1)[1],
        "service_tier": "default", "system_fingerprint": "wp016b-offline",
        "choices": [{"finish_reason": "stop", "message": {
            "content": canonical_bytes(content).decode("utf-8"),
        }}],
        "usage": {"prompt_tokens": 512, "completion_tokens": 32,
                  "prompt_tokens_details": {"cached_tokens": 0},
                  "completion_tokens_details": {"reasoning_tokens": 0}},
    })


def _scientific_inputs(capture_root: Path, hardblock_scan: Path) -> ScientificInputs:
    return ScientificInputs(
        manifest_path=SOURCE_BUNDLE, manifest_sha256=file_sha256(SOURCE_BUNDLE),
        scientific_bundle=SOURCE_BUNDLE, capture_root=capture_root,
        hardblock_scan=hardblock_scan, retrieval_registry=REGISTRY,
    )


async def offline_replay(capture_root: Path, hardblock_scan: Path) -> dict[str, Any]:
    _manifest, _schedule, rows = exact_static_invariants()
    inputs = _scientific_inputs(capture_root, hardblock_scan)
    controller = CoreController(
        registry=read_json(REGISTRY), inputs=inputs, run_nonce="wp016b-offline-replay",
        budget=LiteralBudget(request_cap=24, literal_usd_cap=USD_CAP,
                             request_bounder=lambda _body, _model: Decimal("0")),
    )
    cells = []
    for index, row in enumerate(rows, 1):
        endpoint = ({"endpoint": "NO_VALID_SUBMISSION", "reason_code": "INSUFFICIENT_VISIBLE_EVIDENCE"}
                    if row["arm"].startswith("COMPACT") else {"kind": "NO_VALID_SUBMISSION"})
        cell = await controller.run_cell(
            row=row, canned=[_offline_envelope(row["model_system"], endpoint, str(index))], live=False,
        )
        cells.append(cell)
    if len(cells) != 24 or any(cell["terminal"] != "NO_VALID_SUBMISSION_RECEIVED" for cell in cells):
        raise RuntimeError("wp016b_offline_replay_incomplete")
    manifest = read_json(MANIFEST)
    identities = [{key: row[key] for key in (
        "configuration_id", "cell_id", "session_id", "run_id", "log_id", "output_id",
    )} for row in manifest["cells"]]
    return {
        "schema_version": "wp016b.offline-replay.v1", "status": "PASS_OFFLINE_REPLAY",
        "unique_configurations_replayed": len(cells), "scientific_identities_instantiated": len(identities),
        "all_scientific_identities_unique": all(len({row[key] for row in identities}) == 48 for key in identities[0]),
        "typed_terminal_counts": {"NO_VALID_SUBMISSION_RECEIVED": 24},
        "provider_calls": 0, "backend_calls": 0, "scientific_analysis_eligible": False,
    }


def create_runtime(runtime_base: Path, manifest: Mapping[str, Any], run_nonce: str) -> Path:
    root = runtime_base / ("wp016b-core__" + sha256(run_nonce.encode())[:24])
    root.mkdir(parents=True, exist_ok=False)
    for name in ("authority", "journal", "requests", "responses", "logs", "final", "blocks"):
        (root / name).mkdir()
    cells_root = root / "cells"; cells_root.mkdir()
    for row in manifest["cells"]:
        cell = cells_root / row["cell_id"]
        for part in ("session", "run", "log", "output"):
            (cell / part).mkdir(parents=True, exist_ok=True)
    durable_write_exclusive(root / "authority/identity-bindings.json", {
        "schema_version": "wp016b.runtime-identities.v1", "run_nonce_sha256": sha256(run_nonce.encode()),
        "cells": manifest["cells"],
    })
    return root


def _source_by_key(rows: list[dict[str, str]]) -> dict[tuple[str, str, str, str], dict[str, str]]:
    return {(row["root_id"], row["model_system"], row["capability_task"], row["arm"]): row for row in rows}


def _runtime_row(cell: Mapping[str, Any], source: Mapping[str, str]) -> dict[str, Any]:
    row = dict(source)
    row["source_configuration_id"] = source["configuration_id"]
    row["configuration_id"] = cell["configuration_id"]
    row["serialized_payload_path"] = str(ROOT / source["serialized_payload_relpath"])
    row["identity_overrides"] = {key: cell[key] for key in (
        "configuration_id", "cell_id", "session_id", "run_id", "log_id", "output_id",
    )}
    return row


def _decorate_scientific(cell: dict[str, Any], scheduled: Mapping[str, Any], source_id: str) -> dict[str, Any]:
    certificate = cell.get("proposal_boundary_certificate_v3")
    primary = bool(
        cell.get("terminal") == "COMMIT_RECEIVED"
        and isinstance(certificate, Mapping)
        and certificate.get("proposal_named_family_valid_commit_v4") is True
    )
    return cell | {
        "schema_version": "wp016b.scientific-cell.v1", "wp_id": "WP-016B",
        "source_configuration_id": source_id, "block_id": scheduled["block_id"],
        "repeat": scheduled["repeat"], "scientific_analysis_eligible": True,
        "proposal_named_family_valid_commit_v4": primary,
        "claim_boundary": LABEL, "runtime_boundary_execution": "NOT_MEASURED",
        "physical_validity": "NOT_MEASURED", "functional_validity": "NOT_MEASURED",
        "fmax_qor": "NOT_MEASURED", "backend_calls": 0,
    }


def _protected_prefixes() -> tuple[Path, ...]:
    workspace = os.environ.get("LATCH_HISTORICAL_WORKSPACE")
    if not workspace:
        raise RuntimeError("LATCH_HISTORICAL_WORKSPACE is required for historical input protection")
    root = Path(workspace).resolve(strict=True)
    if not root.is_dir():
        raise RuntimeError("historical_workspace_must_be_a_directory")
    return tuple((root / value).resolve(strict=True) for value in (
        "wp015-runs", "wp015c-runs",
        "wp015c-runtime-deps", "wp015-scientific-source-32d852a",
    ))


async def live_run(args: argparse.Namespace) -> dict[str, Any]:
    authority = exact_authority()
    manifest, schedule, rows = exact_static_invariants()
    if git("status", "--porcelain"):
        raise RuntimeError("wp016b_live_requires_clean_worktree")
    commit, tree = git("rev-parse", "HEAD"), git("rev-parse", "HEAD^{tree}")
    if subprocess.run(["git", "merge-base", "--is-ancestor", authority["base_commit"], commit],
                      cwd=ROOT, check=False).returncode != 0:
        raise RuntimeError("wp016b_base_not_ancestor")
    expected_hashes = frozen_hashes()
    preflight_cost = whole_core_preflight_cost(schedule)
    if preflight_cost > USD_CAP:
        raise RuntimeError("NO_GO_BUDGET_PREFLIGHT")
    if not os.environ.get("OPENAI_API_KEY"):
        raise RuntimeError("wp016b_openai_credential_missing")
    runtime_base = args.runtime_base.resolve()
    protected_prefixes = _protected_prefixes()
    if any(runtime_base == path or runtime_base.is_relative_to(path) for path in protected_prefixes):
        raise RuntimeError("wp016b_runtime_overlaps_protected_wp015_path")
    credential_fingerprint = hashlib.sha256(
        b"WP016B_OPENAI_CREDENTIAL_V1\0" + os.environ["OPENAI_API_KEY"].encode("utf-8")
    ).hexdigest()
    run_nonce = "wp016b-core-live__" + sha256({
        "commit": commit, "tree": tree, "hashes": expected_hashes,
        "authority": file_sha256(AUTHORITY),
    })
    runtime = create_runtime(runtime_base, manifest, run_nonce)
    consumption = runtime_base / "authority" / ("consumed__" + file_sha256(AUTHORITY) + ".json")
    durable_write_exclusive(consumption, {
        "schema_version": "wp016b.one-shot-consumption.v1", "status": "CONSUMED_STARTING",
        "run_nonce_sha256": sha256(run_nonce.encode()), "commit": commit, "tree": tree,
    })
    journal = DurableJournal(runtime)
    journal.append(kind="scientific-freeze", value={
        "SCIENTIFIC_FREEZE_ACTIVE": True, "commit": commit, "tree": tree,
        "frozen_hashes": expected_hashes, "preflight_ceiling_usd": str(preflight_cost),
        "literal_usd_cap": str(USD_CAP), "backend_calls": 0,
        "credential_identity_sha256": credential_fingerprint,
        "credential_value_persisted": False,
        "runtime_isolated_from_wp015c": True,
        "protected_roots_write_operations": 0,
    })
    inputs = _scientific_inputs(args.capture_root, args.hardblock_scan)
    bounder = ExactBodyTokenBounder()
    budget = LiteralBudget(request_cap=REQUEST_CAP, literal_usd_cap=USD_CAP, request_bounder=bounder)
    sources = _source_by_key(rows)
    started = time.monotonic(); completed_blocks = []
    stop_event = asyncio.Event()
    stop_records: list[dict[str, str]] = []
    semaphore = asyncio.Semaphore(MAX_CONCURRENCY)

    def pre_request_guard() -> None:
        if stop_event.is_set():
            raise RuntimeError("wp016b_cancelled_before_next_request_after_shared_stop")
        if time.monotonic() - started >= 12 * 3600:
            stop_event.set()
            raise RuntimeError("wp016b_wall_clock_guard")
        if frozen_hashes() != expected_hashes:
            stop_event.set()
            raise RuntimeError("wp016b_frozen_hash_drift")

    flattened = [
        (position, block, cell)
        for position, block in enumerate(schedule["blocks"], 1)
        for cell in block["cells"]
    ]

    async def run_one(position: int, block: Mapping[str, Any], scheduled: Mapping[str, Any]):
        async with semaphore:
            if stop_event.is_set():
                return {"kind": "CANCELLED_NOT_DISPATCHED", "scheduled": scheduled}
            source = sources[(scheduled["root_id"], scheduled["model_system"],
                              scheduled["capability"], scheduled["arm"])]
            row = _runtime_row(scheduled, source)
            cell_controller = CoreController(
                registry=read_json(REGISTRY), inputs=inputs, run_nonce=run_nonce,
                journal=journal, budget=budget, pre_request_guard=pre_request_guard,
            )
            try:
                raw = await cell_controller.run_cell(row=row, canned=None, live=True)
            except Exception as exc:
                reason = type(exc).__name__ + ":" + str(exc)
                if "cancelled_before_next_request_after_shared_stop" in reason:
                    return {"kind": "CANCELLED_AFTER_INFLIGHT_TURN", "scheduled": scheduled}
                stop_event.set(); stop_records.append({
                    "cell_id": str(scheduled["cell_id"]), "reason": reason,
                    "block_position": str(position),
                })
                journal.append(kind="shared-stop", value={
                    "status": "STOP_SHARED_INTEGRATION_DEFECT", "reason": reason,
                    "cell_id": scheduled["cell_id"], "block_position": position,
                    "concurrency_inflight_bound": MAX_CONCURRENCY,
                })
                return {"kind": "SHARED_FAILURE", "scheduled": scheduled, "reason": reason}
            cell = _decorate_scientific(raw, scheduled, source["configuration_id"])
            durable_write_exclusive(
                runtime / "cells" / scheduled["cell_id"] / "output/scientific-cell-result.json", cell,
            )
            return {"kind": "CELL", "cell": cell, "scheduled": scheduled}

    results = await asyncio.gather(*(run_one(*item) for item in flattened))
    completed_cells = [row["cell"] for row in results if row["kind"] == "CELL"]
    by_cell = {row["cell_id"]: row for row in completed_cells}
    for position, block in enumerate(schedule["blocks"], 1):
        block_cells = [by_cell[row["cell_id"]] for row in block["cells"] if row["cell_id"] in by_cell]
        if len(block_cells) != 3:
            continue
        block_result = {
            "schema_version": "wp016b.completed-block.v1", "block_position": position,
            "block_id": block["block_id"], "cell_ids": [row["cell_id"] for row in block_cells],
            "terminals": [row["terminal"] for row in block_cells],
            "execution_mode": "CONCURRENCY20_USER_AMENDMENT",
        }
        durable_write_exclusive(runtime / "blocks" / f"{position:02d}__{block['block_id']}.json", block_result)
        completed_blocks.append(block_result)
    stop_status = "STOP_SHARED_INTEGRATION_DEFECT" if stop_records else None
    stop_reason = stop_records[0]["reason"] if stop_records else None
    status = "PASS_WP016B_CORE" if len(completed_cells) == 48 and len(completed_blocks) == 16 else (stop_status or "STOP_INCOMPLETE")
    result = {
        "schema_version": "wp016b.core-live-result.v1", "status": status,
        "claim_boundary": LABEL, "commit": commit, "tree": tree,
        "scientific_freeze_active": True, "frozen_hashes": expected_hashes,
        "cells": completed_cells, "completed_cells": len(completed_cells),
        "completed_blocks": len(completed_blocks), "provider_requests": budget.requests,
        "maximum_provider_requests": REQUEST_CAP, "known_charged_cost_usd": str(budget.charged),
        "unknown_cost_attempts": budget.unknown_cost_attempts,
        "unknown_cost_liability_usd": str(budget.conservative_unknown_cost_liability),
        "maximum_concurrent_provider_requests": MAX_CONCURRENCY,
        "execution_amendment_sha256": file_sha256(CONCURRENCY_AMENDMENT),
        "shared_stop_records": stop_records,
        "cancelled_not_dispatched_cells": sum(
            row["kind"] == "CANCELLED_NOT_DISPATCHED" for row in results
        ),
        "cancelled_after_inflight_turn_cells": sum(
            row["kind"] == "CANCELLED_AFTER_INFLIGHT_TURN" for row in results
        ),
        "backend_calls": 0, "retry_count": 0, "fallback_count": 0,
        "response_repair_count": 0, "cache_count": 0, "deduplication_count": 0,
        "stop_reason": stop_reason, "wall_seconds": time.monotonic() - started,
        "runtime_root": str(runtime),
    }
    durable_write_exclusive(runtime / "final/core-result.json", result)
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("offline", "live"))
    parser.add_argument("--capture-root", type=Path, required=True)
    parser.add_argument("--hardblock-scan", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--runtime-base", type=Path)
    args = parser.parse_args()
    args.capture_root = args.capture_root.resolve(strict=True)
    args.hardblock_scan = args.hardblock_scan.resolve(strict=True)
    if args.mode == "offline":
        result = asyncio.run(offline_replay(args.capture_root, args.hardblock_scan))
        if args.output is not None:
            if args.output.exists(): raise FileExistsError(args.output)
            write_json(args.output, result)
    else:
        if args.runtime_base is None or args.output is not None:
            raise SystemExit("live_requires_runtime_base_and_forbids_output")
        result = asyncio.run(live_run(args))
    print(json.dumps({key: result.get(key) for key in (
        "status", "completed_cells", "completed_blocks", "provider_requests",
        "known_charged_cost_usd", "backend_calls", "runtime_root",
    ) if key in result}, sort_keys=True))
    return 0 if str(result["status"]).startswith("PASS") else 2


if __name__ == "__main__":
    raise SystemExit(main())
