#!/usr/bin/env python3
"""No-network full-orchestrator adversarial gate for Expansion Attempt 2."""

from __future__ import annotations

import argparse
import asyncio
from decimal import Decimal
import json
from pathlib import Path
import sys
from typing import Any, Mapping

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from optimizer_pipeline.dof.wp016_authority import DurableJournal, durable_write_exclusive  # noqa: E402
from optimizer_pipeline.dof.wp016_canary import ConcurrentRequestTracker, read_json  # noqa: E402
from optimizer_pipeline.dof.wp016b_expansion_a2 import (  # noqa: E402
    MANIFEST, SCHEDULE, joined_schedule_cells,
)
from scripts.run_wp016b_core import LABEL, _decorate_scientific  # noqa: E402
from scripts.run_wp016b_expansion_a2 import (  # noqa: E402
    ExecutionHooks, SharedStop, create_runtime, orchestrate,
)


class InjectedDefect(RuntimeError):
    pass


def _raw(cell: Mapping[str, Any], cost: str) -> dict[str, Any]:
    return {
        "schema_version": "wp016b.expansion-a2-mock-cell.v1",
        **{key: cell[key] for key in (
            "configuration_id", "cell_id", "session_id", "run_id", "log_id", "output_id",
        )},
        "root_id": cell["root_id"], "model_system": cell["model_system"],
        "capability": cell["capability"], "arm": cell["arm"],
        "terminal": "NO_VALID_SUBMISSION_RECEIVED",
        "proposal_outcome": "NO_PROPOSAL_INSUFFICIENT_VISIBLE_EVIDENCE",
        "proposal_boundary_certificate_v3": None,
        "turns": [{"charged_cost_usd": cost}],
        "known_charged_cost_usd": cost,
        "request_count": 1, "provider_calls": 0, "backend_calls": 0,
        "claim_boundary": LABEL,
    }


async def _scenario(
    *, name: str, output_root: Path, joined: list[tuple[int, Mapping[str, Any], dict[str, Any]]],
    schedule: Mapping[str, Any], failure_stage: str | None = None,
) -> dict[str, Any]:
    runtime = create_runtime(output_root, read_json(MANIFEST), "offline-a2-" + name)
    journal = DurableJournal(runtime)
    tracker = ConcurrentRequestTracker(cap=20)
    seen_requests: set[str] = set()
    cell_costs: dict[str, Decimal] = {}

    async def run_cell(cell: Mapping[str, Any]) -> dict[str, Any]:
        request_id = "offline-mock-request__" + str(cell["cell_id"])
        if request_id in seen_requests:
            raise RuntimeError("offline_cross_cell_request_reuse")
        seen_requests.add(request_id)
        started = tracker.start(
            request_id=request_id, cell_id=str(cell["cell_id"]), turn=1,
        )
        # A mixed duration profile forces interleaved completion while the
        # initial 20 tasks overlap before the first one returns.
        delay = 0.012 if int(cell["launch_rank"]) % 11 == 0 else 0.003
        await asyncio.sleep(delay)
        tracker.finish(
            request_id=request_id, cell_id=str(cell["cell_id"]), turn=1,
            started=started,
        )
        if failure_stage == "controller" and int(cell["launch_rank"]) == 5:
            raise InjectedDefect("injected_controller_defect")
        cost = Decimal("0.0001") * Decimal(1 + int(cell["launch_rank"]) % 7)
        cell_costs[str(cell["cell_id"])] = cost
        return _raw(cell, str(cost))

    def decorate(raw: dict[str, Any], cell: Mapping[str, Any]) -> dict[str, Any]:
        if failure_stage == "scientific_decoration" and int(cell["launch_rank"]) == 5:
            raise InjectedDefect("injected_scientific_decoration_defect")
        value = _decorate_scientific(raw, cell, str(cell["source"])) | {
            "schema_version": "wp016b.expansion-a2-offline-scientific-cell.v1",
            "phase": "EXPANSION", "attempt": 2,
            "launch_rank": cell["launch_rank"], "root_stratum": cell["root_stratum"],
            "scientific_analysis_eligible": False,
        }
        return value

    def publish_cell(cell: dict[str, Any]) -> None:
        if failure_stage == "durable_cell_publication" and int(cell["launch_rank"]) == 5:
            raise InjectedDefect("injected_durable_publication_defect")
        durable_write_exclusive(
            runtime / "cells" / cell["cell_id"] / "output/scientific-cell-result.json", cell,
        )

    def publish_block(block: dict[str, Any]) -> None:
        if failure_stage == "block_aggregation" and int(block["block_position"]) == 1:
            raise InjectedDefect("injected_block_aggregation_defect")
        durable_write_exclusive(
            runtime / "blocks" / f"{block['block_position']:02d}__{block['block_id']}.json", block,
        )

    def finalise(cells: list[dict[str, Any]], blocks: list[dict[str, Any]]) -> None:
        if failure_stage == "final_aggregation":
            raise InjectedDefect("injected_final_aggregation_defect")
        durable_write_exclusive(runtime / "final/mock-finaliser-receipt.json", {
            "schema_version": "wp016b.expansion-a2-mock-finaliser.v1",
            "cells": len(cells), "blocks": len(blocks),
        })

    shared = SharedStop(asyncio.Event(), journal)
    outcome = await orchestrate(
        joined=joined, schedule=schedule,
        hooks=ExecutionHooks(run_cell, decorate, publish_cell, publish_block, finalise),
        shared=shared,
    )
    completed = outcome["completed_cells"]
    cell_sum = sum((Decimal(row["known_charged_cost_usd"]) for row in completed), Decimal("0"))
    turn_sum = sum((Decimal(turn["charged_cost_usd"]) for row in completed
                    for turn in row["turns"]), Decimal("0"))
    # The partial result write is deliberately outside the injected hooks so
    # every failure scenario proves durable terminal packaging.
    partial = {
        "schema_version": "wp016b.expansion-a2-mock-partial.v1",
        "scenario": name, "failure_stage": failure_stage,
        "status": (
            "PASS_WP016B_EXPANSION" if len(completed) == 96
            and len(outcome["completed_blocks"]) == 32 and not shared.records
            else "STOP_SHARED_INTEGRATION_DEFECT"
        ),
        "completed_cells": len(completed),
        "completed_blocks": len(outcome["completed_blocks"]),
        "shared_stop_records": shared.records,
        "configured_concurrency_cap": 20,
        "measured_peak_inflight_requests": tracker.measured_peak,
        "active_request_count_after": len(tracker.active),
        "cell_cost_sum_usd": str(cell_sum), "turn_cost_sum_usd": str(turn_sum),
        "cost_reconciliation_pass": cell_sum == turn_sum,
        "unique_mock_request_ids": len(seen_requests),
        "provider_calls": 0, "backend_calls": 0,
    }
    durable_write_exclusive(runtime / "final/full-orchestrator-result.json", partial)
    return partial


def _join_adversarial(manifest: Mapping[str, Any], schedule: Mapping[str, Any]) -> dict[str, str]:
    results: dict[str, str] = {}
    cases: dict[str, tuple[dict[str, Any], dict[str, Any]]] = {}
    missing = json.loads(json.dumps(schedule)); missing["blocks"][0]["cells"][0].pop("launch_rank")
    cases["missing_launch_metadata"] = (dict(manifest), missing)
    duplicate = json.loads(json.dumps(schedule)); duplicate["blocks"][0]["cells"][1] = dict(duplicate["blocks"][0]["cells"][0])
    cases["duplicate_cell_id"] = (dict(manifest), duplicate)
    mismatch = json.loads(json.dumps(schedule)); mismatch["blocks"][0]["cells"][0]["root_id"] = "MISMATCH"
    cases["mismatched_metadata"] = (dict(manifest), mismatch)
    for name, (candidate_manifest, candidate_schedule) in cases.items():
        try:
            joined_schedule_cells(candidate_manifest, candidate_schedule)
        except RuntimeError as exc:
            results[name] = str(exc)
        else:
            raise RuntimeError("adversarial_join_case_did_not_fail_closed:" + name)
    return results


async def replay(output_root: Path) -> dict[str, Any]:
    manifest, schedule = read_json(MANIFEST), read_json(SCHEDULE)
    joined = joined_schedule_cells(manifest, schedule)
    output_root.mkdir(parents=True, exist_ok=False)
    success = await _scenario(
        name="success_96", output_root=output_root, joined=joined, schedule=schedule,
    )
    if (
        success["status"] != "PASS_WP016B_EXPANSION"
        or success["completed_cells"] != 96 or success["completed_blocks"] != 32
        or success["measured_peak_inflight_requests"] != 20
        or not success["cost_reconciliation_pass"]
        or success["active_request_count_after"] != 0
        or success["unique_mock_request_ids"] != 96
    ):
        raise RuntimeError("wp016b_expansion_a2_full_success_replay_failed")
    failures = {}
    for stage in (
        "controller", "scientific_decoration", "durable_cell_publication",
        "block_aggregation", "final_aggregation",
    ):
        result = await _scenario(
            name="failure__" + stage, output_root=output_root,
            joined=joined, schedule=schedule, failure_stage=stage,
        )
        if result["status"] != "STOP_SHARED_INTEGRATION_DEFECT" or not result["shared_stop_records"]:
            raise RuntimeError("wp016b_expansion_a2_failure_replay_not_fail_closed:" + stage)
        if result["active_request_count_after"] != 0:
            raise RuntimeError("wp016b_expansion_a2_failure_replay_left_active_request:" + stage)
        failures[stage] = result
    value = {
        "schema_version": "wp016b.expansion-a2-full-orchestrator-replay.v1",
        "status": "PASS_FULL_ORCHESTRATOR_ZERO_CALL_GATE",
        "success_fixture": success,
        "failure_fixtures": failures,
        "join_adversarial_cases": _join_adversarial(manifest, schedule),
        "requirements_covered": [
            "96 scheduled cells", "32 blocks", "concurrency cap 20",
            "controller completion", "scientific decoration", "durable cell publication",
            "block aggregation", "final aggregation", "interleaved completion",
            "one long and many short cells", "missing launch metadata",
            "duplicate/mismatched cell ID", "exception in controller",
            "exception during scientific decoration", "exception during durable publication",
            "exception during block aggregation", "shared stop with tasks in flight",
            "partial final result durability", "per-cell/global cost reconciliation",
            "measured in-flight peak", "no cross-cell state/session/request contamination",
        ],
        "scientific_analysis_eligible": False,
        "provider_calls": 0, "backend_calls": 0,
    }
    return value


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runtime-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        raise FileExistsError(args.output)
    result = asyncio.run(replay(args.runtime_root.resolve()))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({
        "status": result["status"], "provider_calls": 0, "backend_calls": 0,
        "success_cells": result["success_fixture"]["completed_cells"],
        "success_blocks": result["success_fixture"]["completed_blocks"],
        "measured_peak": result["success_fixture"]["measured_peak_inflight_requests"],
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
