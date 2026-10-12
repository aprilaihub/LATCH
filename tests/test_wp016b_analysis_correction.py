from __future__ import annotations

import csv
from decimal import Decimal
import json
from pathlib import Path
import os

import pytest

from scripts.analyze_wp016b_expansion import (
    _groups,
    analyze,
    flatten,
    load_expansion_reasoning_receipts,
)


def _historical_path(relative: str) -> Path | None:
    workspace = os.environ.get("LATCH_HISTORICAL_WORKSPACE")
    return Path(workspace) / relative if workspace else None


CORE_RAW = _historical_path('wp016b-core-runtime/wp016b-core__a037ad943f3c1103798638fa/final/core-result.json')
CORE_DERIVED = _historical_path('wp016b-core-derived/core-result.per-turn-cost-attribution-v1.json')
EXPANSION_RUNTIME_BASE = _historical_path('wp016b-expansion-a2-runtime')


def _cell(*, latency: float | None = None) -> dict:
    turn = {
        "turn": 1,
        "client_request_id": "request-1",
        "request_latency_seconds": latency,
        "input_tokens": 10,
        "output_tokens": 5,
        "parsed_public_endpoint": {},
    }
    return {
        "cell_id": "cell-1",
        "block_id": "block-1",
        "root_id": "root-1",
        "model_system": "model-1",
        "capability": "P",
        "arm": "LEGACY_STATIC",
        "repeat": 1,
        "terminal": "COMMIT_RECEIVED",
        "proposal_outcome": "PROPOSAL_COMMIT",
        "proposal_named_family_valid_commit_v4": True,
        "proposal_boundary_certificate_v3": {},
        "turns": [turn],
        "known_charged_cost_usd": "0.1000001",
        "request_count": 1,
        "claim_boundary": "MODEL_ONLY",
    }


def test_missing_latency_and_reasoning_remain_missing_not_zero() -> None:
    row = flatten(
        _cell(), phase="CORE_48", root_stratum="SOURCE_TIMED_TRANSFER",
    )
    assert row["request_latency_seconds"] is None
    assert row["request_latency_turns_observed"] == 0
    assert row["request_latency_turns_missing"] == 1
    assert row["reasoning_tokens"] is None
    assert row["reasoning_token_turns_missing"] == 1
    assert Decimal(row["cost_usd"]) == Decimal("0.1000001")


def test_group_does_not_pool_partially_missing_latency_or_reasoning() -> None:
    missing = flatten(
        _cell(), phase="CORE_48", root_stratum="SOURCE_TIMED_TRANSFER",
    )
    observed = flatten(
        _cell(latency=2.5), phase="EXPANSION_96",
        root_stratum="SOURCE_TIMED_TRANSFER",
        reasoning_by_turn={("cell-1", 1, "request-1"): 17},
    )
    group = _groups([missing, observed], ("capability",))[0]
    assert group["mean_request_latency_seconds"] is None
    assert group["request_latency_scope"] == "PARTIAL_NOT_POOLED"
    assert group["request_latency_observed_cells"] == 1
    assert group["request_latency_missing_cells"] == 1
    assert group["reasoning_tokens_sum"] is None
    assert group["reasoning_tokens_observed_cells"] == 1
    assert group["reasoning_tokens_missing_cells"] == 1


def test_reasoning_receipts_are_joined_by_full_request_identity(tmp_path: Path) -> None:
    receipt = {
        "kind": "request-complete",
        "cell_id": "cell-1",
        "turn": 1,
        "client_request_id": "request-1",
        "native_usage": {"reasoning_tokens": 23},
    }
    path = tmp_path / "0001__request-complete.json"
    path.write_text(json.dumps(receipt), encoding="utf-8")
    mapping, rows, digest = load_expansion_reasoning_receipts(tmp_path)
    assert mapping == {("cell-1", 1, "request-1"): 23}
    assert rows == [{
        "cell_id": "cell-1", "turn": 1,
        "client_request_id": "request-1", "reasoning_tokens": 23,
    }]
    assert len(digest) == 64


def _expansion_runtime() -> Path | None:
    if EXPANSION_RUNTIME_BASE is None:
        return None
    matches = sorted(EXPANSION_RUNTIME_BASE.glob("wp016b-expansion-a2__*"))
    return matches[0] if len(matches) == 1 else None


@pytest.mark.skipif(
    CORE_RAW is None or CORE_DERIVED is None or not CORE_RAW.is_file() or not CORE_DERIVED.is_file() or _expansion_runtime() is None,
    reason="accepted local Core/Expansion evidence unavailable",
)
def test_full_correction_preserves_scientific_invariants(tmp_path: Path) -> None:
    runtime = _expansion_runtime()
    assert runtime is not None
    summary = analyze(
        CORE_RAW, CORE_DERIVED, runtime / "final/expansion-a2-result.json",
        runtime / "journal", tmp_path,
    )
    assert summary["primary_successes"] == {
        "core48": 12, "expansion96": 20, "combined144": 32,
    }
    assert summary["cost_usd"] == {
        "core48_corrected": "3.3779738",
        "expansion96": "5.5489672",
        "combined144": "8.9269410",
    }
    assert summary["latency_seconds"]["core48"] is None
    assert summary["latency_seconds"]["combined144"] is None
    assert summary["reasoning_tokens"]["core48"] is None
    assert summary["reasoning_tokens"]["expansion96"] == 133_190

    receipt = json.loads(
        (tmp_path / "WP016B_ANALYSIS_CORRECTION_RECEIPT.json").read_text()
    )
    assert receipt["raw_evidence_hashes_unchanged"] is True
    assert receipt["primary_endpoint_counts_unchanged"] is True
    assert receipt["paired_contrasts_unchanged"] is True
    assert receipt["reasoning_token_reconciliation"] == {
        "turn_sum": 133_190, "cell_sum": 133_190,
        "aggregate_sum": 133_190,
    }

    with (tmp_path / "tables/combined144_root_model_small_multiples.csv").open() as handle:
        small = list(csv.DictReader(handle))
    assert len(small) == 72
    assert {row["capability"] for row in small} == {"P", "G"}

    assert not (tmp_path / "tables/combined144_turn_latency_token_cost.csv").exists()
    assert not (tmp_path / "figures/combined144_turn_latency_token_cost.png").exists()
    assert (tmp_path / "tables/combined144_turn_token_cost.csv").is_file()
    assert (tmp_path / "figures/expansion96_latency_distribution.png").is_file()

