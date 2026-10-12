#!/usr/bin/env python3
"""Durable zero-call replay and one-shot live runner for WP-016B Expansion A2."""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass, field
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
import time
from typing import Any, Awaitable, Callable, Mapping

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from optimizer_pipeline.dof.wp016 import sha256  # noqa: E402
from optimizer_pipeline.dof.wp016_authority import (  # noqa: E402
    DurableJournal, durable_write_exclusive, durable_write_replace,
)
from optimizer_pipeline.dof.wp016_canary import (  # noqa: E402
    ConcurrentRequestTracker, LiteralBudget, ScientificInputs, file_sha256,
    read_json,
)
from optimizer_pipeline.dof.wp016b_expansion import (  # noqa: E402
    CORE_ROOTS, ExpansionController, NEW_ROOTS, SOURCE_BUNDLE,
    SOURCE_LEDGER, load_source_rows,
)
from optimizer_pipeline.dof.wp016b_expansion_a2 import (  # noqa: E402
    ANALYSIS_CONTRACT_SHA256, ATTEMPT1_ACCEPTANCE, ATTEMPT1_MANIFEST_SHA256,
    ATTEMPT1_PACKAGE_SHA256, ATTEMPT1_SCHEDULE_SHA256, ATTEMPT_LEDGER,
    AUTHORITY, AUTHORITY_LEDGER, DECISION_LEDGER, ENVIRONMENT_CONTRACT,
    MANIFEST, ORCHESTRATION_REPLAY_RECEIPT, PREFLIGHT_RECEIPT, SCHEDULE,
    exact_authority, joined_schedule_cells, verify_attempt1_treatment_sources,
)
from scripts.run_wp016b_core import (  # noqa: E402
    ExactBodyTokenBounder, LABEL, MAX_INPUT_TOKENS_PER_REQUEST_PREFLIGHT,
    PRICING, REGISTRY, _decorate_scientific, maximum_request_cost,
)


CORE_RAW_SHA256 = "0453c94feba05fca47d1b67827e4832b1bf7096e6a39be321988a3b7a8b1509a"
CORE_DERIVED_SHA256 = "2521616250e3aaecd199255cdfffb8c4ae1777753b940088a89995ece1eebde1"
ATTEMPT1_KNOWN_COST = Decimal("0.1278062")
ATTEMPT1_UNRESOLVED_LIABILITY = Decimal("0.6901780")
CUMULATIVE_USD_CAP = Decimal("50.00")
ATTEMPT2_USD_CAP = CUMULATIVE_USD_CAP - ATTEMPT1_KNOWN_COST - ATTEMPT1_UNRESOLVED_LIABILITY
REQUEST_CAP = 384
MAX_CONCURRENCY = 20
WALL_CLOCK_CAP_SECONDS = 4 * 3600


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def git(*args: str) -> str:
    return subprocess.check_output(["git", *args], cwd=ROOT, text=True).strip()


def frozen_hashes() -> dict[str, str]:
    paths = {
        "authority": AUTHORITY, "attempt1_acceptance": ATTEMPT1_ACCEPTANCE,
        "authority_ledger": AUTHORITY_LEDGER, "decision_ledger": DECISION_LEDGER,
        "attempt_ledger": ATTEMPT_LEDGER, "manifest": MANIFEST,
        "schedule": SCHEDULE, "environment_contract": ENVIRONMENT_CONTRACT,
        "orchestration_replay_receipt": ORCHESTRATION_REPLAY_RECEIPT,
        "preflight_receipt": PREFLIGHT_RECEIPT, "source_ledger": SOURCE_LEDGER,
        "source_bundle": SOURCE_BUNDLE, "registry": REGISTRY, "pricing": PRICING,
        "controller": ROOT / "optimizer_pipeline/dof/wp016_canary.py",
        "core_support": ROOT / "optimizer_pipeline/dof/wp016b.py",
        "expansion_support": ROOT / "optimizer_pipeline/dof/wp016b_expansion.py",
        "attempt2_support": ROOT / "optimizer_pipeline/dof/wp016b_expansion_a2.py",
        "runner": Path(__file__).resolve(),
    }
    return {name: file_sha256(path) for name, path in paths.items()}


def exact_static_invariants(
    core_raw: Path, core_derived: Path, attempt1_package: Path,
) -> tuple[dict[str, Any], dict[str, Any], list[dict[str, str]], list[tuple[int, Mapping[str, Any], dict[str, Any]]]]:
    authority = exact_authority()
    verify_attempt1_treatment_sources()
    if authority["base_commit"] != "3c913c6ea91264d2c4d3338f57930c2cd2d2aad3":
        raise RuntimeError("wp016b_expansion_a2_base_authority_drift")
    if file_sha256(core_raw) != CORE_RAW_SHA256 or file_sha256(core_derived) != CORE_DERIVED_SHA256:
        raise RuntimeError("wp016b_expansion_a2_accepted_core_hash_invalid")
    if file_sha256(attempt1_package) != ATTEMPT1_PACKAGE_SHA256:
        raise RuntimeError("wp016b_expansion_attempt1_package_hash_invalid")
    manifest, schedule, rows = read_json(MANIFEST), read_json(SCHEDULE), load_source_rows()
    joined = joined_schedule_cells(manifest, schedule)
    if len(rows) != 48 or len(joined) != 96 or len(schedule["blocks"]) != 32:
        raise RuntimeError("wp016b_expansion_a2_inventory_invalid")
    if manifest.get("logical_treatment_source_manifest_sha256") != ATTEMPT1_MANIFEST_SHA256:
        raise RuntimeError("wp016b_expansion_a2_treatment_source_invalid")
    if schedule.get("logical_order_source_schedule_sha256") != ATTEMPT1_SCHEDULE_SHA256:
        raise RuntimeError("wp016b_expansion_a2_order_source_invalid")
    if manifest.get("analysis_contract_sha256") != ANALYSIS_CONTRACT_SHA256:
        raise RuntimeError("wp016b_expansion_a2_analysis_contract_invalid")
    attempt1_ids = {
        key: {str(row[key]) for row in read_json(
            ROOT / "configs/wp016b-expansion/expansion96-manifest.json"
        )["cells"]}
        for key in ("configuration_id", "cell_id", "session_id", "run_id", "log_id", "output_id")
    }
    for key in attempt1_ids:
        current = {str(row[2][key]) for row in joined}
        if len(current) != 96 or current & attempt1_ids[key]:
            raise RuntimeError("wp016b_expansion_a2_identity_reuse:" + key)
    import httpx
    import tiktoken
    environment = read_json(ENVIRONMENT_CONTRACT)
    observed = {
        "python_version": platform.python_version(),
        "httpx_version": httpx.__version__, "tiktoken_version": tiktoken.__version__,
    }
    if any(environment.get(key) != value for key, value in observed.items()):
        raise RuntimeError("wp016b_expansion_a2_environment_contract_drift")
    return manifest, schedule, rows, joined


def whole_attempt2_preflight_cost(schedule: Mapping[str, Any]) -> Decimal:
    return sum(
        maximum_request_cost(str(block["model_system"]), MAX_INPUT_TOKENS_PER_REQUEST_PREFLIGHT)
        * Decimal(12)
        for block in schedule["blocks"]
    )


@dataclass(slots=True)
class ExecutionHooks:
    run_cell: Callable[[Mapping[str, Any]], Awaitable[dict[str, Any]]]
    decorate: Callable[[dict[str, Any], Mapping[str, Any]], dict[str, Any]]
    publish_cell: Callable[[dict[str, Any]], None]
    publish_block: Callable[[dict[str, Any]], None]
    finalise: Callable[[list[dict[str, Any]], list[dict[str, Any]]], None] = lambda _cells, _blocks: None


@dataclass(slots=True)
class SharedStop:
    event: asyncio.Event
    journal: DurableJournal | None = None
    records: list[dict[str, Any]] = field(default_factory=list)

    def record(self, *, stage: str, cell_id: str, exc: BaseException) -> None:
        self.event.set()
        value = {
            "status": "STOP_SHARED_INTEGRATION_DEFECT",
            "stage": stage, "cell_id": cell_id,
            "exception_type": type(exc).__name__, "reason": str(exc),
            "scientific_semantics_changed": False,
        }
        self.records.append(value)
        if self.journal is not None:
            self.journal.append(kind="shared-stop", value=value)


async def orchestrate(
    *, joined: list[tuple[int, Mapping[str, Any], dict[str, Any]]],
    schedule: Mapping[str, Any], hooks: ExecutionHooks,
    shared: SharedStop, maximum_concurrency: int = MAX_CONCURRENCY,
) -> dict[str, Any]:
    """Run the full path; no exception may escape without a typed stop record."""
    semaphore = asyncio.Semaphore(maximum_concurrency)

    async def run_one(position: int, block: Mapping[str, Any], cell: Mapping[str, Any]) -> dict[str, Any]:
        stage = "semaphore_wait"
        try:
            async with semaphore:
                if shared.event.is_set():
                    return {"kind": "CANCELLED_NOT_DISPATCHED", "scheduled": dict(cell)}
                stage = "controller"
                raw = await hooks.run_cell(cell)
                stage = "scientific_decoration"
                scientific = hooks.decorate(raw, cell)
                stage = "durable_cell_publication"
                hooks.publish_cell(scientific)
                return {"kind": "CELL", "cell": scientific, "scheduled": dict(cell)}
        except Exception as exc:
            reason = str(exc)
            if shared.event.is_set() and "cancelled_before_next_request_after_shared_stop" in reason:
                return {"kind": "CANCELLED_AFTER_INFLIGHT_TURN", "scheduled": dict(cell), "reason": reason}
            shared.record(stage=stage, cell_id=str(cell.get("cell_id")), exc=exc)
            return {"kind": "SHARED_FAILURE", "scheduled": dict(cell), "reason": reason, "stage": stage}

    results = await asyncio.gather(*(run_one(*item) for item in joined), return_exceptions=False)
    completed_cells = [row["cell"] for row in results if row["kind"] == "CELL"]
    by_cell = {row["cell_id"]: row for row in completed_cells}
    completed_blocks: list[dict[str, Any]] = []
    for position, block in enumerate(schedule["blocks"], 1):
        block_cells = [by_cell[cell["cell_id"]] for cell in block["cells"] if cell["cell_id"] in by_cell]
        if len(block_cells) != 3:
            continue
        block_result = {
            "schema_version": "wp016b.expansion-a2-completed-block.v1",
            "attempt": 2, "block_position": position, "block_id": block["block_id"],
            "cell_ids": [row["cell_id"] for row in block_cells],
            "terminals": [row["terminal"] for row in block_cells],
            "execution_mode": "CONCURRENCY20_LEADER_AUTHORISED",
        }
        try:
            hooks.publish_block(block_result)
            completed_blocks.append(block_result)
        except Exception as exc:
            shared.record(stage="block_aggregation", cell_id=str(block["block_id"]), exc=exc)
            break
    try:
        hooks.finalise(completed_cells, completed_blocks)
    except Exception as exc:
        shared.record(stage="final_aggregation", cell_id="GLOBAL", exc=exc)
    return {
        "results": results, "completed_cells": completed_cells,
        "completed_blocks": completed_blocks, "shared_stop_records": shared.records,
        "cancelled_not_dispatched_cells": sum(row["kind"] == "CANCELLED_NOT_DISPATCHED" for row in results),
        "cancelled_after_inflight_turn_cells": sum(row["kind"] == "CANCELLED_AFTER_INFLIGHT_TURN" for row in results),
    }


def create_runtime(runtime_base: Path, manifest: Mapping[str, Any], run_nonce: str) -> Path:
    root = runtime_base / ("wp016b-expansion-a2__" + sha256(run_nonce.encode())[:24])
    root.mkdir(parents=True, exist_ok=False)
    for name in ("authority", "journal", "requests", "responses", "logs", "final", "blocks"):
        (root / name).mkdir()
    cells_root = root / "cells"; cells_root.mkdir()
    for row in manifest["cells"]:
        cell = cells_root / row["cell_id"]
        for part in ("session", "run", "log", "output"):
            (cell / part).mkdir(parents=True, exist_ok=True)
    durable_write_exclusive(root / "authority/identity-bindings.json", {
        "schema_version": "wp016b.expansion-a2-runtime-identities.v1",
        "attempt": 2, "run_nonce_sha256": sha256(run_nonce.encode()),
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


def _scientific_inputs(capture_root: Path, hardblock_scan: Path) -> ScientificInputs:
    return ScientificInputs(
        manifest_path=SOURCE_BUNDLE, manifest_sha256=file_sha256(SOURCE_BUNDLE),
        scientific_bundle=SOURCE_BUNDLE, capture_root=capture_root,
        hardblock_scan=hardblock_scan, retrieval_registry=REGISTRY,
    )


def _input_hashes(capture_root: Path, hardblock_scan: Path) -> dict[str, str]:
    paths = [SOURCE_BUNDLE, hardblock_scan / "aggregate.json", hardblock_scan / "gate1-audit.json"]
    for root in (*CORE_ROOTS, *NEW_ROOTS):
        paths.extend([
            capture_root / root / "vivado-observation/real-observation.json",
            capture_root / root / "contract-probe/private/resolved-objects.json",
        ])
    return {str(path): file_sha256(path.resolve(strict=True)) for path in paths}


def _journal_request_sets(runtime: Path) -> tuple[set[str], set[str]]:
    sent, responded = set(), set()
    for path in (runtime / "journal").glob("*__request-sent.json"):
        sent.add(str(read_json(path)["client_request_id"]))
    for path in (runtime / "journal").glob("*__response-received.json"):
        responded.add(str(read_json(path)["client_request_id"]))
    for path in (runtime / "journal").glob("*__request-failure.json"):
        responded.add(str(read_json(path)["client_request_id"]))
    return sent, responded


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
        "wp016b-expansion-runtime",
    ))


async def live_run(args: argparse.Namespace) -> dict[str, Any]:
    authority = exact_authority()
    manifest, schedule, rows, joined = exact_static_invariants(
        args.core_raw, args.core_derived, args.attempt1_package,
    )
    if git("status", "--porcelain"):
        raise RuntimeError("wp016b_expansion_a2_live_requires_clean_worktree")
    commit, tree = git("rev-parse", "HEAD"), git("rev-parse", "HEAD^{tree}")
    if subprocess.run(["git", "merge-base", "--is-ancestor", authority["base_commit"], commit],
                      cwd=ROOT, check=False).returncode != 0:
        raise RuntimeError("wp016b_expansion_a2_base_not_ancestor")
    expected_hashes = frozen_hashes()
    protected_input_hashes = _input_hashes(args.capture_root, args.hardblock_scan)
    preflight_cost = whole_attempt2_preflight_cost(schedule)
    if preflight_cost > ATTEMPT2_USD_CAP:
        raise RuntimeError("NO_GO_CUMULATIVE_BUDGET_PREFLIGHT")
    if not os.environ.get("OPENAI_API_KEY"):
        raise RuntimeError("wp016b_expansion_a2_openai_credential_missing")
    runtime_base = args.runtime_base.resolve()
    protected_prefixes = _protected_prefixes()
    if any(runtime_base == path or runtime_base.is_relative_to(path) for path in protected_prefixes):
        raise RuntimeError("wp016b_expansion_a2_runtime_overlaps_protected_path")
    credential_fingerprint = hashlib.sha256(
        b"WP016B_EXPANSION_A2_OPENAI_CREDENTIAL_V1\0" + os.environ["OPENAI_API_KEY"].encode()
    ).hexdigest()
    run_nonce = "wp016b-expansion-a2-live__" + sha256({
        "attempt": 2, "commit": commit, "tree": tree, "hashes": expected_hashes,
        "authority": file_sha256(AUTHORITY),
    })
    runtime = create_runtime(runtime_base, manifest, run_nonce)
    consumption = runtime_base / "authority" / ("consumed__" + file_sha256(AUTHORITY) + ".json")
    durable_write_exclusive(consumption, {
        "schema_version": "wp016b.expansion-a2-one-shot-consumption.v1",
        "status": "CONSUMED_STARTING", "attempt": 2,
        "run_nonce_sha256": sha256(run_nonce.encode()), "commit": commit, "tree": tree,
    })
    journal = DurableJournal(runtime)
    journal.append(kind="scientific-freeze", value={
        "SCIENTIFIC_FREEZE_ACTIVE": True, "phase": "EXPANSION", "attempt": 2,
        "commit": commit, "tree": tree, "frozen_hashes": expected_hashes,
        "attempt2_preflight_ceiling_usd": str(preflight_cost),
        "attempt2_literal_usd_cap": str(ATTEMPT2_USD_CAP),
        "cumulative_literal_usd_cap": str(CUMULATIVE_USD_CAP),
        "attempt1_known_plus_unresolved_ceiling_usd": str(
            ATTEMPT1_KNOWN_COST + ATTEMPT1_UNRESOLVED_LIABILITY
        ),
        "configured_concurrency_cap": MAX_CONCURRENCY, "backend_calls": 0,
        "credential_identity_sha256": credential_fingerprint,
        "credential_value_persisted": False, "runtime_isolated_from_attempt1": True,
        "protected_input_hashes": protected_input_hashes,
    })
    inputs = _scientific_inputs(args.capture_root, args.hardblock_scan)
    budget = LiteralBudget(
        request_cap=REQUEST_CAP, literal_usd_cap=ATTEMPT2_USD_CAP,
        request_bounder=ExactBodyTokenBounder(),
    )
    tracker = ConcurrentRequestTracker(cap=MAX_CONCURRENCY)
    sources = _source_by_key(rows)
    started = time.monotonic()
    shared = SharedStop(asyncio.Event(), journal)

    def pre_request_guard() -> None:
        if shared.event.is_set():
            raise RuntimeError("wp016b_expansion_a2_cancelled_before_next_request_after_shared_stop")
        if time.monotonic() - started >= WALL_CLOCK_CAP_SECONDS:
            shared.event.set(); raise RuntimeError("wp016b_expansion_a2_wall_clock_guard")
        if frozen_hashes() != expected_hashes:
            shared.event.set(); raise RuntimeError("wp016b_expansion_a2_frozen_hash_drift")
        if _input_hashes(args.capture_root, args.hardblock_scan) != protected_input_hashes:
            shared.event.set(); raise RuntimeError("wp016b_expansion_a2_protected_input_hash_drift")

    async def run_cell(scheduled: Mapping[str, Any]) -> dict[str, Any]:
        source = sources[(scheduled["root_id"], scheduled["model_system"],
                          scheduled["capability"], scheduled["arm"])]
        row = _runtime_row(scheduled, source)
        controller = ExpansionController(
            registry=read_json(REGISTRY), inputs=inputs, run_nonce=run_nonce,
            journal=journal, budget=budget, pre_request_guard=pre_request_guard,
            request_tracker=tracker,
        )
        return await controller.run_cell(row=row, canned=None, live=True)

    def decorate(raw: dict[str, Any], scheduled: Mapping[str, Any]) -> dict[str, Any]:
        source = sources[(scheduled["root_id"], scheduled["model_system"],
                          scheduled["capability"], scheduled["arm"])]
        return _decorate_scientific(raw, scheduled, source["configuration_id"]) | {
            "schema_version": "wp016b.expansion-a2-scientific-cell.v1",
            "phase": "EXPANSION", "attempt": 2,
            "launch_rank": scheduled["launch_rank"],
            "root_stratum": scheduled["root_stratum"],
            "attempt1_partial_traffic_reused": False,
        }

    def publish_cell(cell: dict[str, Any]) -> None:
        durable_write_exclusive(
            runtime / "cells" / cell["cell_id"] / "output/scientific-cell-result.json", cell,
        )

    def publish_block(block: dict[str, Any]) -> None:
        durable_write_exclusive(
            runtime / "blocks" / f"{block['block_position']:02d}__{block['block_id']}.json", block,
        )

    outcome = await orchestrate(
        joined=joined, schedule=schedule,
        hooks=ExecutionHooks(run_cell, decorate, publish_cell, publish_block),
        shared=shared,
    )
    completed_cells = outcome["completed_cells"]
    completed_blocks = outcome["completed_blocks"]
    cell_cost = sum((Decimal(row["known_charged_cost_usd"]) for row in completed_cells), Decimal("0"))
    turn_cost = sum((Decimal(turn["charged_cost_usd"]) for row in completed_cells
                     for turn in row["turns"]), Decimal("0"))
    sent, responded = _journal_request_sets(runtime)
    unresolved_sent = sorted(sent - responded)
    reconciliation = (
        len(completed_cells) == 96
        and cell_cost == turn_cost == budget.charged
        and not budget.pending_reservations and not unresolved_sent
    )
    if len(completed_cells) == 96 and len(completed_blocks) == 32 and not outcome["shared_stop_records"] and not reconciliation:
        exc = RuntimeError("wp016b_expansion_a2_cost_or_request_reconciliation_failure")
        shared.record(stage="final_reconciliation", cell_id="GLOBAL", exc=exc)
    if tracker.active or tracker.inflight != 0 or budget.pending_reservations:
        exc = RuntimeError("wp016b_expansion_a2_concurrent_state_not_quiescent")
        shared.record(stage="final_reconciliation", cell_id="GLOBAL", exc=exc)
    protected_after = _input_hashes(args.capture_root, args.hardblock_scan)
    if protected_after != protected_input_hashes:
        exc = RuntimeError("wp016b_expansion_a2_protected_input_hash_drift")
        shared.record(stage="final_reconciliation", cell_id="GLOBAL", exc=exc)
    status = (
        "PASS_WP016B_EXPANSION_A2"
        if len(completed_cells) == 96 and len(completed_blocks) == 32
        and not shared.records and reconciliation
        else "STOP_SHARED_INTEGRATION_DEFECT"
    )
    result = {
        "schema_version": "wp016b.expansion-a2-live-result.v1", "status": status,
        "claim_boundary": LABEL, "phase": "EXPANSION", "attempt": 2,
        "commit": commit, "tree": tree, "scientific_freeze_active": True,
        "frozen_hashes": expected_hashes,
        "protected_input_hashes_before": protected_input_hashes,
        "protected_input_hashes_after": protected_after,
        "cells": completed_cells, "completed_cells": len(completed_cells),
        "completed_blocks": len(completed_blocks), "provider_requests": budget.requests,
        "maximum_provider_requests": REQUEST_CAP,
        "known_charged_cost_usd": str(budget.charged),
        "cell_cost_sum_usd": str(cell_cost), "turn_cost_sum_usd": str(turn_cost),
        "cost_reconciliation_pass": reconciliation,
        "unknown_cost_attempts": budget.unknown_cost_attempts,
        "unknown_cost_liability_usd": str(budget.conservative_unknown_cost_liability),
        "attempt1_known_plus_unresolved_ceiling_usd": str(
            ATTEMPT1_KNOWN_COST + ATTEMPT1_UNRESOLVED_LIABILITY
        ),
        "cumulative_known_plus_conservative_liability_usd": str(
            ATTEMPT1_KNOWN_COST + ATTEMPT1_UNRESOLVED_LIABILITY
            + budget.charged + budget.conservative_unknown_cost_liability
        ),
        "configured_concurrency_cap": MAX_CONCURRENCY,
        "measured_peak_inflight_requests": tracker.measured_peak,
        "concurrency_events": tracker.events,
        "shared_stop_records": shared.records,
        "sent_request_count": len(sent), "response_or_failure_receipt_count": len(responded),
        "sent_without_complete_response_evidence": unresolved_sent,
        "cancelled_not_dispatched_cells": outcome["cancelled_not_dispatched_cells"],
        "cancelled_after_inflight_turn_cells": outcome["cancelled_after_inflight_turn_cells"],
        "backend_calls": 0, "retry_count": 0, "fallback_count": 0,
        "response_repair_count": 0, "cache_count": 0, "deduplication_count": 0,
        "wall_seconds": time.monotonic() - started, "runtime_root": str(runtime),
        "core_cells_rerun": False, "attempt1_partial_traffic_reused": False,
        "further_expansion_executed": False, "wp016c_executed": False,
    }
    try:
        durable_write_exclusive(runtime / "final/expansion-a2-result.json", result)
    except Exception as exc:
        # The primary finaliser is itself inside the engineering firewall.  A
        # distinct filename prevents an existing/partial primary result from
        # hiding the stop classification.
        shared.record(stage="durable_final_result", cell_id="GLOBAL", exc=exc)
        result["status"] = "STOP_SHARED_INTEGRATION_DEFECT"
        result["shared_stop_records"] = shared.records
        durable_write_exclusive(runtime / "final/emergency-partial-result.json", {
            "schema_version": "wp016b.expansion-a2-emergency-partial.v1",
            "status": result["status"], "attempt": 2,
            "completed_cells": result["completed_cells"],
            "completed_blocks": result["completed_blocks"],
            "provider_requests": result["provider_requests"],
            "known_charged_cost_usd": result["known_charged_cost_usd"],
            "unknown_cost_liability_usd": result["unknown_cost_liability_usd"],
            "shared_stop_records": shared.records,
        })
    durable_write_replace(consumption, {
        "schema_version": "wp016b.expansion-a2-one-shot-consumption.v1",
        "status": "CONSUMED_TERMINAL", "attempt": 2,
        "terminal_status": status, "run_nonce_sha256": sha256(run_nonce.encode()),
        "commit": commit, "tree": tree, "provider_requests": budget.requests,
        "known_charged_cost_usd": str(budget.charged),
    })
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("live",))
    parser.add_argument("--core-raw", type=Path, required=True)
    parser.add_argument("--core-derived", type=Path, required=True)
    parser.add_argument("--attempt1-package", type=Path, required=True)
    parser.add_argument("--capture-root", type=Path, required=True)
    parser.add_argument("--hardblock-scan", type=Path, required=True)
    parser.add_argument("--runtime-base", type=Path, required=True)
    args = parser.parse_args()
    for name in ("core_raw", "core_derived", "attempt1_package", "capture_root", "hardblock_scan"):
        setattr(args, name, getattr(args, name).resolve(strict=True))
    result = asyncio.run(live_run(args))
    print(json.dumps({key: result.get(key) for key in (
        "status", "completed_cells", "completed_blocks", "provider_requests",
        "known_charged_cost_usd", "configured_concurrency_cap",
        "measured_peak_inflight_requests", "backend_calls", "runtime_root",
    )}, sort_keys=True))
    return 0 if result["status"] == "PASS_WP016B_EXPANSION_A2" else 2


if __name__ == "__main__":
    raise SystemExit(main())
