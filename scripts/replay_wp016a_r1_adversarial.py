#!/usr/bin/env python3
"""Replay both Attempt-1-like G-low paths through the exact controller."""

from __future__ import annotations

import argparse
import asyncio
import copy
import json
from pathlib import Path
import sys
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from optimizer_pipeline.dof.wp016 import canonical_bytes  # noqa: E402
from optimizer_pipeline.dof.wp016_canary import (  # noqa: E402
    _load_fixture_sequences, load_source_rows, run_all,
)


def _sequences(endpoints: list[dict[str, Any]]) -> dict[str, list[bytes]]:
    fixtures = {key: list(value) for key, value in _load_fixture_sequences().items()}
    source = load_source_rows()[0]["configuration_id"]
    template = json.loads(fixtures[source][0])
    rows = []
    for endpoint in endpoints:
        row = copy.deepcopy(template)
        row["choices"][0]["message"]["content"] = json.dumps(endpoint)
        row["choices"][0]["finish_reason"] = "stop"
        row["usage"]["completion_tokens"] = 8
        rows.append(canonical_bytes(row))
    fixtures[source] = rows
    return fixtures


async def replay(manifest: Path) -> dict[str, Any]:
    prefix = [
        {
            "endpoint": "GET_EXAMPLE",
            "content_id": "EXAMPLE/G/G_BOUNDARY_NONPREFIX_MULTI_NET",
        },
        {
            "endpoint": "GET_SCHEMA_FRAGMENT",
            "content_id": "SCHEMA/G/program_schema",
        },
    ]
    child = "SCHEMA/G/program_schema/properties/steps/items/oneOf/0"
    paths = {
        "CHILD_THEN_NVS": [
            *prefix,
            {"endpoint": "GET_SCHEMA_FRAGMENT", "content_id": child},
            {"endpoint": "NO_VALID_SUBMISSION", "reason_code": "DYNAMIC_BUDGET_EXHAUSTED"},
        ],
        "DIRECT_NVS": [
            *prefix,
            {"endpoint": "NO_VALID_SUBMISSION", "reason_code": "DYNAMIC_BUDGET_EXHAUSTED"},
        ],
    }
    results = {}
    for label, endpoints in paths.items():
        aggregate = await run_all(
            live=False, scientific_input_manifest=manifest,
            run_nonce="wp016a-r1-" + label.lower().replace("_", "-"),
            fixture_sequences=_sequences(endpoints),
        )
        first = aggregate["cells"][0]
        frontier = first["turns"][1]
        assert aggregate["status"] == "PASS_CANARY_INTEGRATION"
        assert first["terminal"] == "NO_VALID_SUBMISSION_RECEIVED"
        assert frontier["frontier_semantics"] == "RETRIEVABLE_DESCENDANT_FRONTIER"
        assert frontier["requested_content_bytes_charged"] == 0
        assert frontier["frontier_metadata_bytes"] > 0
        results[label] = aggregate
    return {
        "schema_version": "wp016a.r1-controller-adversarial-replay.v1",
        "status": "PASS_ZERO_CALL_CONTROLLER_ADVERSARIAL_REPLAY",
        "paths": results,
        "provider_calls": 0, "backend_calls": 0,
        "attempt2_token_issued": False, "wp016b_authorised": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--scientific-input-manifest", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        raise FileExistsError("adversarial_replay_output_already_exists")
    result = asyncio.run(replay(args.scientific_input_manifest))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({
        "status": result["status"], "paths": len(result["paths"]),
        "provider_calls": 0, "backend_calls": 0,
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
