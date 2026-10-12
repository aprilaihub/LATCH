#!/usr/bin/env python3
"""Zero-call gates and one-shot live execution for WP-016B Expansion."""

from __future__ import annotations

import argparse
import asyncio
import csv
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from typing import Any, Mapping

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from optimizer_pipeline.dof.wp016 import canonical_bytes, sha256  # noqa: E402
from optimizer_pipeline.dof.wp016_authority import (  # noqa: E402
    DurableJournal, durable_write_exclusive,
)
from optimizer_pipeline.dof.wp016_canary import (  # noqa: E402
    ConcurrentRequestTracker, LiteralBudget, ScientificInputs, file_sha256,
    read_json,
)
from optimizer_pipeline.dof.wp016b_expansion import (  # noqa: E402
    ANALYSIS_CONTRACT, AUTHORITY, CONFIG_ROOT, CORE_ROOTS, DERIVED_SEED,
    ExpansionController, LEADER_DECISION, MANIFEST, MAX_CONCURRENCY, MODELS,
    NEW_ROOTS, REQUEST_CAP, SCHEDULE, SOURCE_BUNDLE, SOURCE_LEDGER, USD_CAP,
    WALL_CLOCK_CAP_SECONDS, exact_authority, load_source_rows,
)
from scripts.run_wp016b_core import (  # noqa: E402
    ExactBodyTokenBounder, LABEL, MAX_INPUT_TOKENS_PER_REQUEST_PREFLIGHT,
    PRICING, REGISTRY, _decorate_scientific, _offline_envelope,
    maximum_request_cost,
)


ENVIRONMENT_CONTRACT = CONFIG_ROOT / "environment-contract.json"
PREFLIGHT_RECEIPT = CONFIG_ROOT / "preflight-receipt.json"
OFFLINE_REPLAY_RECEIPT = CONFIG_ROOT / "offline-replay-receipt.json"
CORE_RAW_SHA256 = "0453c94feba05fca47d1b67827e4832b1bf7096e6a39be321988a3b7a8b1509a"
CORE_DERIVED_SHA256 = "2521616250e3aaecd199255cdfffb8c4ae1777753b940088a89995ece1eebde1"


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def git(*args: str) -> str:
    return subprocess.check_output(["git", *args], cwd=ROOT, text=True).strip()


def frozen_hashes() -> dict[str, str]:
    paths = {
        "authority": AUTHORITY, "leader_decision": LEADER_DECISION,
        "source_ledger": SOURCE_LEDGER, "source_bundle": SOURCE_BUNDLE,
        "manifest": MANIFEST, "schedule": SCHEDULE,
        "analysis_contract": ANALYSIS_CONTRACT, "registry": REGISTRY,
        "pricing": PRICING, "environment_contract": ENVIRONMENT_CONTRACT,
        "preflight_receipt": PREFLIGHT_RECEIPT,
        "offline_replay_receipt": OFFLINE_REPLAY_RECEIPT,
        "controller": ROOT / "optimizer_pipeline/dof/wp016_canary.py",
        "core_support": ROOT / "optimizer_pipeline/dof/wp016b.py",
        "expansion_support": ROOT / "optimizer_pipeline/dof/wp016b_expansion.py",
        "runner": Path(__file__).resolve(),
    }
    return {name: file_sha256(path) for name, path in paths.items()}


def _load_core_ids(core_raw: Path) -> dict[str, set[str]]:
    value = read_json(core_raw)
    if file_sha256(core_raw) != CORE_RAW_SHA256 or value.get("status") != "PASS_WP016B_CORE":
        raise RuntimeError("accepted_core_raw_identity_invalid")
    return {
        key: {str(row[key]) for row in value["cells"]}
        for key in ("cell_id", "configuration_id", "session_id", "run_id", "log_id", "output_id")
    }


def exact_static_invariants(core_raw: Path, core_derived: Path) -> tuple[
    dict[str, Any], dict[str, Any], list[dict[str, str]],
]:
    exact_authority()
    if file_sha256(core_derived) != CORE_DERIVED_SHA256:
        raise RuntimeError("accepted_core_derived_identity_invalid")
    core_ids = _load_core_ids(core_raw)
    manifest, schedule, rows = read_json(MANIFEST), read_json(SCHEDULE), load_source_rows()
    cells = manifest["cells"]
    if len(cells) != 96 or len(schedule["blocks"]) != 32 or len(rows) != 48:
        raise RuntimeError("wp016b_expansion_inventory_arithmetic_invalid")
    if manifest.get("derived_seed") != DERIVED_SEED or schedule.get("derived_seed") != DERIVED_SEED:
        raise RuntimeError("wp016b_expansion_seed_drift")
    if schedule.get("maximum_concurrency") != MAX_CONCURRENCY:
        raise RuntimeError("wp016b_expansion_concurrency_drift")
    if sum(row["model_system"] == "openai/gpt-5.6-terra" and row["root_id"] in CORE_ROOTS for row in cells) != 24:
        raise RuntimeError("wp016b_expansion_terra_core_count_invalid")
    if sum(row["root_id"] in NEW_ROOTS for row in cells) != 72:
        raise RuntimeError("wp016b_expansion_new_root_count_invalid")
    for block in schedule["blocks"]:
        if len(block["cells"]) != 3 or {row["arm"] for row in block["cells"]} != {
            "LEGACY_STATIC", "COMPACT_RETRIEVAL_LOW_RESERVE",
            "COMPACT_RETRIEVAL_MATCHED_RESERVE",
        }:
            raise RuntimeError("wp016b_expansion_block_arm_inventory_invalid")
    identity_keys = ("configuration_id", "cell_id", "session_id", "run_id", "log_id", "output_id")
    for key in identity_keys:
        values = {str(row[key]) for row in cells}
        if len(values) != 96 or values & core_ids[key]:
            raise RuntimeError("wp016b_expansion_runtime_identity_collision:" + key)
    if any("canary" in json.dumps(row).lower() for row in cells):
        raise RuntimeError("wp016b_expansion_canary_identity_leak")
    import httpx
    import platform
    import tiktoken
    environment = read_json(ENVIRONMENT_CONTRACT)
    observed = {
        "python_version": platform.python_version(),
        "httpx_version": httpx.__version__, "tiktoken_version": tiktoken.__version__,
    }
    if any(environment.get(key) != value for key, value in observed.items()):
        raise RuntimeError("wp016b_expansion_environment_contract_drift")
    return manifest, schedule, rows


def whole_expansion_preflight_cost(schedule: Mapping[str, Any]) -> Decimal:
    return sum(
        maximum_request_cost(str(block["model_system"]), MAX_INPUT_TOKENS_PER_REQUEST_PREFLIGHT)
        * Decimal(12)
        for block in schedule["blocks"]
    )


def _scientific_inputs(capture_root: Path, hardblock_scan: Path) -> ScientificInputs:
    return ScientificInputs(
        manifest_path=SOURCE_BUNDLE, manifest_sha256=file_sha256(SOURCE_BUNDLE),
        scientific_bundle=SOURCE_BUNDLE, capture_root=capture_root,
        hardblock_scan=hardblock_scan, retrieval_registry=REGISTRY,
    )


async def offline_replay(core_raw: Path, core_derived: Path,
                         capture_root: Path, hardblock_scan: Path) -> dict[str, Any]:
    manifest, _schedule, rows = exact_static_invariants(core_raw, core_derived)
    inputs = _scientific_inputs(capture_root, hardblock_scan)
    controller = ExpansionController(
        registry=read_json(REGISTRY), inputs=inputs,
        run_nonce="wp016b-expansion-offline-replay",
        budget=LiteralBudget(request_cap=48, literal_usd_cap=USD_CAP,
                             request_bounder=lambda _body, _model: Decimal("0")),
    )
    cells = []
    for index, row in enumerate(rows, 1):
        endpoint = ({"endpoint": "NO_VALID_SUBMISSION", "reason_code": "INSUFFICIENT_VISIBLE_EVIDENCE"}
                    if row["arm"].startswith("COMPACT") else {"kind": "NO_VALID_SUBMISSION"})
        cells.append(await controller.run_cell(
            row=row, canned=[_offline_envelope(row["model_system"], endpoint, f"exp-{index}")],
            live=False,
        ))
    if len(cells) != 48 or any(cell["terminal"] != "NO_VALID_SUBMISSION_RECEIVED" for cell in cells):
        raise RuntimeError("wp016b_expansion_offline_replay_incomplete")
    identities = [{key: row[key] for key in (
        "configuration_id", "cell_id", "session_id", "run_id", "log_id", "output_id",
    )} for row in manifest["cells"]]
    return {
        "schema_version": "wp016b.expansion-offline-replay.v1",
        "status": "PASS_OFFLINE_REPLAY",
        "unique_configurations_replayed": 48,
        "scientific_identities_instantiated": 96,
        "all_scientific_identities_unique": all(
            len({row[key] for row in identities}) == 96 for key in identities[0]
        ),
        "typed_terminal_counts": {"NO_VALID_SUBMISSION_RECEIVED": 48},
        "adversarial_terminal_coverage": {
            "controller_replay": [
                "proposal commit", "no valid submission", "malformed retrieval",
                "retrieval not found", "validation required",
                "semantic reserve exhaustion", "context limit", "turn limit",
                "output limit", "truncation",
            ],
            "fail_closed_transport_and_taxonomy_tests": [
                "returned-model mismatch", "provider/transport failure",
                "unknown exception fail-closed",
            ],
            "family_unproven_commit": "certificate-classification test path",
            "test_modules": [
                "tests/test_wp016a_canary.py", "tests/test_wp016a_g0r2.py",
                "tests/test_wp016a_completion_g0.py", "tests/test_wp016a_r10.py",
                "tests/test_wp016b_expansion.py",
            ],
        },
        "provider_calls": 0, "backend_calls": 0,
        "scientific_analysis_eligible": False,
    }


def create_runtime(runtime_base: Path, manifest: Mapping[str, Any], run_nonce: str) -> Path:
    root = runtime_base / ("wp016b-expansion__" + sha256(run_nonce.encode())[:24])
    root.mkdir(parents=True, exist_ok=False)
    for name in ("authority", "journal", "requests", "responses", "logs", "final", "blocks"):
        (root / name).mkdir()
    cells_root = root / "cells"; cells_root.mkdir()
    for row in manifest["cells"]:
        cell = cells_root / row["cell_id"]
        for part in ("session", "run", "log", "output"):
            (cell / part).mkdir(parents=True, exist_ok=True)
    durable_write_exclusive(root / "authority/identity-bindings.json", {
        "schema_version": "wp016b.expansion-runtime-identities.v1",
        "run_nonce_sha256": sha256(run_nonce.encode()), "cells": manifest["cells"],
    })
    return root


def _source_by_key(rows: list[dict[str, str]]) -> dict[tuple[str, str, str, str], dict[str, str]]:
    return {(row["root_id"], row["model_system"], row["capability_task"], row["arm"]): row
            for row in rows}


def _runtime_row(cell: Mapping[str, Any], source: Mapping[str, str]) -> dict[str, Any]:
    row = dict(source)
    row["source_configuration_id"] = source["configuration_id"]
    row["configuration_id"] = cell["configuration_id"]
    row["serialized_payload_path"] = str(ROOT / source["serialized_payload_relpath"])
    row["identity_overrides"] = {key: cell[key] for key in (
        "configuration_id", "cell_id", "session_id", "run_id", "log_id", "output_id",
    )}
    return row


def _input_hashes(capture_root: Path, hardblock_scan: Path) -> dict[str, str]:
    paths = [SOURCE_BUNDLE, hardblock_scan / "aggregate.json", hardblock_scan / "gate1-audit.json"]
    for root in (*CORE_ROOTS, *NEW_ROOTS):
        paths.extend([
            capture_root / root / "vivado-observation/real-observation.json",
            capture_root / root / "contract-probe/private/resolved-objects.json",
        ])
    return {str(path): file_sha256(path.resolve(strict=True)) for path in paths}


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
    manifest, schedule, rows = exact_static_invariants(args.core_raw, args.core_derived)
    if git("status", "--porcelain"):
        raise RuntimeError("wp016b_expansion_live_requires_clean_worktree")
    commit, tree = git("rev-parse", "HEAD"), git("rev-parse", "HEAD^{tree}")
    if subprocess.run(["git", "merge-base", "--is-ancestor", authority["base_commit"], commit],
                      cwd=ROOT, check=False).returncode != 0:
        raise RuntimeError("wp016b_expansion_base_not_ancestor")
    expected_hashes = frozen_hashes()
    protected_input_hashes = _input_hashes(args.capture_root, args.hardblock_scan)
    preflight_cost = whole_expansion_preflight_cost(schedule)
    if preflight_cost > USD_CAP:
        raise RuntimeError("NO_GO_BUDGET_PREFLIGHT")
    if not os.environ.get("OPENAI_API_KEY"):
        raise RuntimeError("wp016b_expansion_openai_credential_missing")
    runtime_base = args.runtime_base.resolve()
    protected_prefixes = _protected_prefixes()
    if any(runtime_base == path or runtime_base.is_relative_to(path) for path in protected_prefixes):
        raise RuntimeError("wp016b_expansion_runtime_overlaps_protected_wp015_path")
    credential_fingerprint = hashlib.sha256(
        b"WP016B_EXPANSION_OPENAI_CREDENTIAL_V1\0" + os.environ["OPENAI_API_KEY"].encode()
    ).hexdigest()
    run_nonce = "wp016b-expansion-live__" + sha256({
        "commit": commit, "tree": tree, "hashes": expected_hashes,
        "authority": file_sha256(AUTHORITY),
    })
    runtime = create_runtime(runtime_base, manifest, run_nonce)
    consumption = runtime_base / "authority" / ("consumed__" + file_sha256(AUTHORITY) + ".json")
    durable_write_exclusive(consumption, {
        "schema_version": "wp016b.expansion-one-shot-consumption.v1",
        "status": "CONSUMED_STARTING", "run_nonce_sha256": sha256(run_nonce.encode()),
        "commit": commit, "tree": tree,
    })
    journal = DurableJournal(runtime)
    journal.append(kind="scientific-freeze", value={
        "SCIENTIFIC_FREEZE_ACTIVE": True, "phase": "EXPANSION",
        "commit": commit, "tree": tree, "frozen_hashes": expected_hashes,
        "preflight_ceiling_usd": str(preflight_cost), "literal_usd_cap": str(USD_CAP),
        "configured_concurrency_cap": MAX_CONCURRENCY, "backend_calls": 0,
        "credential_identity_sha256": credential_fingerprint,
        "credential_value_persisted": False, "runtime_isolated_from_wp015c": True,
        "protected_input_hashes": protected_input_hashes,
    })
    inputs = _scientific_inputs(args.capture_root, args.hardblock_scan)
    budget = LiteralBudget(
        request_cap=REQUEST_CAP, literal_usd_cap=USD_CAP,
        request_bounder=ExactBodyTokenBounder(),
    )
    tracker = ConcurrentRequestTracker(cap=MAX_CONCURRENCY)
    sources = _source_by_key(rows)
    started = time.monotonic(); completed_blocks = []
    stop_event = asyncio.Event(); stop_records: list[dict[str, str]] = []
    semaphore = asyncio.Semaphore(MAX_CONCURRENCY)

    def pre_request_guard() -> None:
        if stop_event.is_set():
            raise RuntimeError("wp016b_expansion_cancelled_before_next_request_after_shared_stop")
        if time.monotonic() - started >= WALL_CLOCK_CAP_SECONDS:
            stop_event.set(); raise RuntimeError("wp016b_expansion_wall_clock_guard")
        if frozen_hashes() != expected_hashes:
            stop_event.set(); raise RuntimeError("wp016b_expansion_frozen_hash_drift")
        if _input_hashes(args.capture_root, args.hardblock_scan) != protected_input_hashes:
            stop_event.set(); raise RuntimeError("wp016b_expansion_protected_input_hash_drift")

    flattened = [(position, block, cell) for position, block in enumerate(schedule["blocks"], 1)
                 for cell in block["cells"]]

    async def run_one(position: int, block: Mapping[str, Any], scheduled: Mapping[str, Any]):
        async with semaphore:
            if stop_event.is_set():
                return {"kind": "CANCELLED_NOT_DISPATCHED", "scheduled": scheduled}
            source = sources[(scheduled["root_id"], scheduled["model_system"],
                              scheduled["capability"], scheduled["arm"])]
            row = _runtime_row(scheduled, source)
            controller = ExpansionController(
                registry=read_json(REGISTRY), inputs=inputs, run_nonce=run_nonce,
                journal=journal, budget=budget, pre_request_guard=pre_request_guard,
                request_tracker=tracker,
            )
            try:
                raw = await controller.run_cell(row=row, canned=None, live=True)
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
                    "configured_concurrency_cap": MAX_CONCURRENCY,
                    "measured_peak_inflight": tracker.measured_peak,
                })
                return {"kind": "SHARED_FAILURE", "scheduled": scheduled, "reason": reason}
            cell = _decorate_scientific(raw, scheduled, source["configuration_id"]) | {
                "schema_version": "wp016b.expansion-scientific-cell.v1",
                "phase": "EXPANSION", "launch_rank": scheduled["launch_rank"],
                "root_stratum": scheduled["root_stratum"],
            }
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
            "schema_version": "wp016b.expansion-completed-block.v1",
            "block_position": position, "block_id": block["block_id"],
            "cell_ids": [row["cell_id"] for row in block_cells],
            "terminals": [row["terminal"] for row in block_cells],
            "execution_mode": "CONCURRENCY20_LEADER_AUTHORISED",
        }
        durable_write_exclusive(runtime / "blocks" / f"{position:02d}__{block['block_id']}.json", block_result)
        completed_blocks.append(block_result)
    cell_cost = sum((Decimal(row["known_charged_cost_usd"]) for row in completed_cells), Decimal("0"))
    turn_cost = sum((Decimal(turn["charged_cost_usd"]) for row in completed_cells
                     for turn in row["turns"]), Decimal("0"))
    reconciliation = cell_cost == turn_cost == budget.charged
    if not reconciliation and not stop_records:
        stop_records.append({"cell_id": "GLOBAL", "reason": "concurrent_cost_reconciliation_failure",
                             "block_position": "GLOBAL"})
    if tracker.active or tracker.inflight != 0 or budget.pending_reservations:
        if not stop_records:
            stop_records.append({"cell_id": "GLOBAL", "reason": "concurrent_state_not_quiescent",
                                 "block_position": "GLOBAL"})
    if _input_hashes(args.capture_root, args.hardblock_scan) != protected_input_hashes:
        if not stop_records:
            stop_records.append({"cell_id": "GLOBAL", "reason": "protected_input_hash_drift",
                                 "block_position": "GLOBAL"})
    status = (
        "PASS_WP016B_EXPANSION" if len(completed_cells) == 96 and len(completed_blocks) == 32
        and not stop_records else "STOP_SHARED_INTEGRATION_DEFECT" if stop_records else "STOP_INCOMPLETE"
    )
    result = {
        "schema_version": "wp016b.expansion-live-result.v1", "status": status,
        "claim_boundary": LABEL, "phase": "EXPANSION", "commit": commit, "tree": tree,
        "scientific_freeze_active": True, "frozen_hashes": expected_hashes,
        "protected_input_hashes_before": protected_input_hashes,
        "protected_input_hashes_after": _input_hashes(args.capture_root, args.hardblock_scan),
        "cells": completed_cells, "completed_cells": len(completed_cells),
        "completed_blocks": len(completed_blocks), "provider_requests": budget.requests,
        "maximum_provider_requests": REQUEST_CAP,
        "known_charged_cost_usd": str(budget.charged),
        "cell_cost_sum_usd": str(cell_cost), "turn_cost_sum_usd": str(turn_cost),
        "cost_reconciliation_pass": reconciliation,
        "unknown_cost_attempts": budget.unknown_cost_attempts,
        "unknown_cost_liability_usd": str(budget.conservative_unknown_cost_liability),
        "configured_concurrency_cap": MAX_CONCURRENCY,
        "measured_peak_inflight_requests": tracker.measured_peak,
        "concurrency_events": tracker.events,
        "shared_stop_records": stop_records,
        "cancelled_not_dispatched_cells": sum(row["kind"] == "CANCELLED_NOT_DISPATCHED" for row in results),
        "cancelled_after_inflight_turn_cells": sum(row["kind"] == "CANCELLED_AFTER_INFLIGHT_TURN" for row in results),
        "backend_calls": 0, "retry_count": 0, "fallback_count": 0,
        "response_repair_count": 0, "cache_count": 0, "deduplication_count": 0,
        "wall_seconds": time.monotonic() - started, "runtime_root": str(runtime),
        "core_cells_rerun": False, "further_expansion_executed": False,
        "wp016c_executed": False,
    }
    durable_write_exclusive(runtime / "final/expansion-result.json", result)
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("offline", "live"))
    parser.add_argument("--core-raw", type=Path, required=True)
    parser.add_argument("--core-derived", type=Path, required=True)
    parser.add_argument("--capture-root", type=Path, required=True)
    parser.add_argument("--hardblock-scan", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--runtime-base", type=Path)
    args = parser.parse_args()
    for name in ("core_raw", "core_derived", "capture_root", "hardblock_scan"):
        setattr(args, name, getattr(args, name).resolve(strict=True))
    if args.mode == "offline":
        result = asyncio.run(offline_replay(
            args.core_raw, args.core_derived, args.capture_root, args.hardblock_scan,
        ))
        if args.output is not None:
            if args.output.exists(): raise FileExistsError(args.output)
            write_json(args.output, result)
    else:
        if args.runtime_base is None or args.output is not None:
            raise SystemExit("live_requires_runtime_base_and_forbids_output")
        result = asyncio.run(live_run(args))
    print(json.dumps({key: result.get(key) for key in (
        "status", "completed_cells", "completed_blocks", "provider_requests",
        "known_charged_cost_usd", "configured_concurrency_cap",
        "measured_peak_inflight_requests", "backend_calls", "runtime_root",
    ) if key in result}, sort_keys=True))
    return 0 if str(result["status"]).startswith("PASS") else 2


if __name__ == "__main__":
    raise SystemExit(main())
