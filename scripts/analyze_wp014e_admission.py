#!/usr/bin/env python3
"""Build a public-safe, hash-bound ledger from the WP-014E admission run."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any


def canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def digest(value: Any) -> str:
    return hashlib.sha256(value if isinstance(value, bytes) else canonical(value)).hexdigest()


def read(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text())
    if not isinstance(value, dict):
        raise ValueError("json_object_required:" + str(path))
    return value


def main() -> int:
    parser = argparse.ArgumentParser(allow_abbrev=False)
    parser.add_argument("--run", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    aggregate = read(args.run / "provider-admission.json")
    claimed = aggregate.pop("aggregate_sha256")
    if digest(aggregate) != claimed:
        raise SystemExit("provider_admission_hash_invalid")
    aggregate["aggregate_sha256"] = claimed
    rows = []
    errors = []
    for admission in aggregate["inference_admissions"]:
        exchange_path = (
            args.run / "private" / admission["model"].replace("/", "_")
            / f"{admission['admission']}.json"
        )
        if admission["transport_terminal"] == "COMPATIBLE":
            exchange = read(exchange_path)
            if digest(exchange) != admission["exchange_sha256"]:
                errors.append("exchange_hash_mismatch:" + admission["model"] + ":" + admission["admission"])
            request_id = exchange.get("provider_request_id")
            rows.append({
                "model": admission["model"],
                "admission": admission["admission"],
                "provider": admission["provider"],
                "route": admission["route"],
                "transport_terminal": admission["transport_terminal"],
                "technical_error_category": None,
                "requested_api_model_id": exchange["requested_api_model_id"],
                "returned_api_model_id": exchange["returned_api_model_id"],
                "returned_upstream_provider": exchange["returned_upstream_provider"],
                "provider_request_id_sha256": digest(str(request_id).encode()) if request_id else None,
                "request_contract_hash": exchange["request_contract_hash"],
                "prompt_sha256": exchange["prompt_sha256"],
                "context_sha256": exchange["context_sha256"],
                "request_body_sha256": exchange["request_body_sha256"],
                "response_sha256": exchange["response_sha256"],
                "final_text_sha256": exchange["final_text_sha256"],
                "physical_requests": exchange["physical_requests"],
                "native_usage": exchange["native_usage"],
                "provider_reported_cost_usd": exchange["provider_reported_cost_usd"],
                "derived_cost_usd": exchange["derived_cost_usd"],
                "accounted_cost_usd": admission["accounted_cost_usd"],
                "cost_source": admission["cost_source"],
                "pricing_snapshot_id": exchange["pricing_snapshot_id"],
                "cost_formula_version": exchange["cost_formula_version"],
                "billing_reconciliation_status": exchange["billing_reconciliation_status"],
                "latency_seconds": admission["latency_seconds"],
                "retry_count": exchange["retry_count"],
                "fallback_count": exchange["fallback_count"],
                "repair_count": exchange["repair_count"],
                "raw_reasoning_persisted": exchange["raw_reasoning_persisted"],
                "policy_compatibility": admission["policy_compatibility"],
            })
        else:
            rows.append({
                "model": admission["model"],
                "admission": admission["admission"],
                "provider": admission["provider"],
                "route": admission["route"],
                "transport_terminal": admission["transport_terminal"],
                "technical_error_category": admission["technical_error"],
                "requested_api_model_id": None,
                "returned_api_model_id": None,
                "returned_upstream_provider": None,
                "provider_request_id_sha256": None,
                "request_contract_hash": admission["request_contract_hash"],
                "prompt_sha256": admission["prompt_sha256"],
                "context_sha256": admission["context_sha256"],
                "request_body_sha256": None,
                "response_sha256": None,
                "final_text_sha256": None,
                "physical_requests": admission["physical_requests"],
                "native_usage": None,
                "provider_reported_cost_usd": None,
                "derived_cost_usd": None,
                "accounted_cost_usd": None,
                "cost_source": None,
                "pricing_snapshot_id": "provider-pricing-2026-08-12-v1",
                "cost_formula_version": "dof.provider-cost-formula.v1",
                "billing_reconciliation_status": "NOT_AVAILABLE_TECHNICAL_FAILURE",
                "latency_seconds": admission["latency_seconds"],
                "retry_count": admission["retry_count"],
                "fallback_count": admission["fallback_count"],
                "repair_count": admission["repair_count"],
                "raw_reasoning_persisted": False,
                "policy_compatibility": admission["policy_compatibility"],
            })
    core = {
        "schema_version": "dof.wp014e-public-provider-ledger.v1",
        "provider_admission_sha256": claimed,
        "rows": rows,
        "row_count": len(rows),
        "physical_inference_requests": sum(row["physical_requests"] for row in rows),
        "transport_compatible": sum(row["transport_terminal"] == "COMPATIBLE" for row in rows),
        "transport_failures": sum(row["transport_terminal"] != "COMPATIBLE" for row in rows),
        "total_accounted_cost_usd": sum(float(row["accounted_cost_usd"] or 0.0) for row in rows),
        "retry_count": sum(row["retry_count"] for row in rows),
        "fallback_count": sum(row["fallback_count"] for row in rows),
        "repair_count": sum(row["repair_count"] for row in rows),
        "secret_values_recorded": False,
        "raw_reasoning_recorded": False,
        "errors": errors,
        "accepted": len(rows) == 14 and not errors,
    }
    value = core | {"ledger_sha256": digest(core)}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(value, sort_keys=True, indent=2) + "\n")
    print(json.dumps({key: value[key] for key in (
        "accepted", "row_count", "physical_inference_requests",
        "transport_compatible", "transport_failures", "total_accounted_cost_usd",
    )}, sort_keys=True))
    return 0 if value["accepted"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
