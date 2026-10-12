#!/usr/bin/env python3
"""Run the fresh seven-model provider-only WP-015B hardblock admission."""

from __future__ import annotations

import argparse
import asyncio
import collections
import json
import os
from pathlib import Path
import sys
from typing import Any, Mapping


REPO = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(REPO), str(REPO / "scripts")]

import run_wp013a_e0 as base  # noqa: E402
import run_wp014 as legacy  # noqa: E402
import run_wp015_cells as cells  # noqa: E402
from optimizer_pipeline.dof.hardblock_compatibility import validate_allocation  # noqa: E402
from optimizer_pipeline.dof.wp015_provider import (  # noqa: E402
    MODEL_RESERVES_USD, PROVIDER_CAPS_USD, runtime_profile,
)


FAMILY = "COUPLED_HARDBLOCK_SITE_ALLOCATION"
PROTOCOL = "WP015B_HARDBLOCK_PROVIDER_ADMISSION_V1"


def read(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("json_object_required:" + str(path))
    return value


def model_family(model: str) -> str:
    if model.startswith("openai/"):
        return "openai_gpt56"
    if model.startswith("deepseek/"):
        return "deepseek_v4"
    return model.split("/", 1)[0]


def jobs(inventory: Mapping[str, Any], selected_root: str) -> list[dict[str, Any]]:
    result = []
    for model in MODEL_RESERVES_USD:
        source = next(
            row for row in inventory["jobs"]
            if row["model_key"] == model and row["root_id"] == selected_root
            and row["condition"] == "G_STRICT_BOUNDARY"
        )
        result.append(dict(source) | {
            "job_id": "wp015b-gate3__" + cells.digest({"protocol": PROTOCOL, "model": model, "root": selected_root})[:28],
            "repeat_id": "WP015B_HARDBLOCK_ADMISSION_NO_REPEAT",
            "model": model, "study_track": source["track"],
            "strict_mechanism": FAMILY, "not_a_scientific_repeat": True,
            "backend_dispatch_allowed": False,
        })
    if len(result) != 7 or len({row["job_id"] for row in result}) != 7:
        raise ValueError("wp015b_gate3_requires_seven_unique_sessions")
    return result


def allocation_proof(compiled: Any, registry: Any, relation: Mapping[str, Any]) -> dict[str, Any]:
    steps = [step for step in compiled.host_steps if step.operation == "vivado.wp014h_apply_allocation"]
    if len(steps) != 1:
        raise ValueError("wp015b_compiled_allocation_dispatch_not_unique")
    allocation = registry.resolve(str(steps[0].public_args["allocation"]), expected_type="allocation")
    validate_allocation(
        allocation.attributes, relation, root_sha256=registry.root_sha256,
        stage=str(relation["stage"]),
    )
    return {
        "allocation_sha256": allocation.attributes["allocation_sha256"],
        "compatibility_relation_sha256": allocation.attributes["compatibility_relation_sha256"],
        "feasibility_query_sha256": allocation.attributes["feasibility_query_sha256"],
        "matching_size": allocation.attributes["matching_size"],
        "target_site_pair_hashes": list(allocation.attributes["target_site_pair_hashes"]),
        "all_pairs_membership_validated": True,
    }


async def run(args: argparse.Namespace) -> int:
    cells.configure()
    cells.configure_wp015b_hardblock_compatibility(args.scan)
    bundle, inventory, scan = read(args.bundle), read(args.inventory), read(args.scan / "aggregate.json")
    selected_root = str(scan["selected_admission_root"])
    rows = jobs(inventory, selected_root)
    args.output.mkdir(parents=True, exist_ok=False)
    costs = {provider: base.SharedCost(cap) for provider, cap in PROVIDER_CAPS_USD.items()}
    semaphore = asyncio.Semaphore(args.max_parallel)

    async def one(job: Mapping[str, Any]) -> dict[str, Any]:
        profile = runtime_profile(job["model_key"])
        provider = profile["provider"]
        key = os.environ.get(profile["credential_env"], "")
        if not key:
            raise RuntimeError("provider_credential_missing:" + provider)
        async with semaphore:
            planned, registry, compiled, candidate = await legacy._plan_with_context(
                job, bundle, args.capture, args.output, key, costs[provider],
            )
        exchanges = cells._public_exchange_accounting(args.output, job["job_id"])
        valid = planned.get("canonical_terminal") == "PLANNED_COMMIT"
        proof = None
        if valid:
            if compiled is None or registry is None or candidate is None:
                raise RuntimeError("wp015b_planned_commit_missing_compiled_evidence")
            relation_objects = registry.objects(object_type="compatibility")
            if len(relation_objects) != 1:
                raise RuntimeError("wp015b_planned_relation_object_missing")
            proof = allocation_proof(compiled, registry, relation_objects[0].attributes["relation"])
        return dict(job) | {
            "canonical_terminal": planned.get("canonical_terminal"),
            "provider_requests": int(planned.get("provider_requests") or 0),
            "cost_usd": float(planned.get("cost_usd") or 0.0),
            "strict_candidate_valid": valid,
            "candidate_sha256": cells.digest(candidate) if candidate is not None else None,
            "provider_exchange_accounting": exchanges,
            "allocation_proof": proof,
            "backend_dispatches": 0,
        }

    results = await asyncio.gather(*(one(job) for job in rows))
    exchanges = [item for row in results for item in row["provider_exchange_accounting"]]
    valid_families = sorted({
        model_family(row["model_key"]) for row in results if row["strict_candidate_valid"]
    })
    checks = {
        "exact_7_sessions": len(results) == 7,
        "zero_backend_dispatch": all(row["backend_dispatches"] == 0 for row in results),
        "at_least_two_independent_model_families": len(valid_families) >= 2,
        "all_planned_pairs_exactly_validated": all(
            not row["strict_candidate_valid"] or row["allocation_proof"]["all_pairs_membership_validated"]
            for row in results
        ),
        "zero_retry_fallback_repair": all(
            item.get("retry_count") == item.get("fallback_count") == item.get("repair_count") == 0
            for item in exchanges
        ),
        "request_bound": sum(row["provider_requests"] for row in results) <= 28,
        "cost_bound": sum(row["cost_usd"] for row in results) <= 8.0,
        "zero_shared_truncation": not any(
            item.get("silent_truncation_detected") is True for item in exchanges
        ),
    }
    core = {
        "schema_version": "dof.wp015b-hardblock-construction-admission.v1",
        "protocol_id": PROTOCOL,
        "status": "PASS" if all(checks.values()) else "STOP_SHARED_GATE",
        "checks": checks, "session_count": len(results),
        "selected_admission_root": selected_root,
        "terminal_counts": dict(collections.Counter(str(row["canonical_terminal"]) for row in results)),
        "physical_model_requests": sum(row["provider_requests"] for row in results),
        "api_cost_usd": sum(row["cost_usd"] for row in results),
        "backend_dispatches": 0,
        "valid_model_families": valid_families,
        "rows": results,
    }
    result = core | {"aggregate_sha256": cells.digest(core)}
    cells.write(args.output / "aggregate.json", result)
    print(json.dumps({key: result[key] for key in ("status", "terminal_counts", "physical_model_requests", "api_cost_usd", "valid_model_families")}, sort_keys=True))
    return 0 if result["status"] == "PASS" else 2


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(allow_abbrev=False)
    value.add_argument("--bundle", type=Path, required=True)
    value.add_argument("--inventory", type=Path, required=True)
    value.add_argument("--capture", type=Path, required=True)
    value.add_argument("--scan", type=Path, required=True)
    value.add_argument("--output", type=Path, required=True)
    value.add_argument("--max-parallel", type=int, default=7)
    return value


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run(parser().parse_args())))
