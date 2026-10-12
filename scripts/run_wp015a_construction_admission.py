#!/usr/bin/env python3
"""Run 7-model x 3-family provider-only WP-015A construction admission."""

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
from optimizer_pipeline.dof.wp015_provider import (  # noqa: E402
    MODEL_RESERVES_USD, PROVIDER_CAPS_USD, runtime_profile,
)
from optimizer_pipeline.dof.wp015a_strict_g import STRICT_FAMILIES  # noqa: E402


ROOT_BY_FAMILY = {
    "SPATIAL_GENERATED_GEOMETRY": "amd_mini-isp",
    "NONPREFIX_MULTI_NET_BATCH": "logicnets_jscl",
    "COUPLED_HARDBLOCK_SITE_ALLOCATION": "amd_mini-isp",
}


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


def jobs(inventory: Mapping[str, Any]) -> list[dict[str, Any]]:
    result = []
    for model in MODEL_RESERVES_USD:
        for family in STRICT_FAMILIES:
            root = ROOT_BY_FAMILY[family]
            source = next(
                row for row in inventory["jobs"]
                if row["model_key"] == model and row["root_id"] == root
                and row["condition"] == "G_STRICT_BOUNDARY"
            )
            result.append(dict(source) | {
                "job_id": "wp015a-gate2__" + cells.digest({"model": model, "family": family})[:28],
                "repeat_id": "WP015A_CONSTRUCTION_ADMISSION_NO_REPEAT",
                "model": model,
                "study_track": source["track"],
                "strict_mechanism": family,
                "not_a_scientific_repeat": True,
                "backend_dispatch_allowed": False,
            })
    if len(result) != 21 or len({row["job_id"] for row in result}) != 21:
        raise ValueError("wp015a_gate2_requires_21_unique_sessions")
    return result


async def run(args: argparse.Namespace) -> int:
    cells.configure()
    bundle, inventory = read(args.bundle), read(args.inventory)
    rows = jobs(inventory)
    args.output.mkdir(parents=True, exist_ok=False)
    costs = {provider: base.SharedCost(cap) for provider, cap in PROVIDER_CAPS_USD.items()}
    semaphore = asyncio.Semaphore(args.max_parallel)

    async def one(job: Mapping[str, Any]) -> dict[str, Any]:
        provider = runtime_profile(job["model_key"])["provider"]
        key = os.environ.get(runtime_profile(job["model_key"])["credential_env"], "")
        if not key:
            raise RuntimeError("provider_credential_missing:" + provider)
        async with semaphore:
            planned, _registry, _compiled, candidate = await legacy._plan_with_context(
                job, bundle, args.capture, args.output, key, costs[provider],
            )
        exchanges = cells._public_exchange_accounting(args.output, job["job_id"])
        valid = planned.get("canonical_terminal") == "PLANNED_COMMIT"
        return dict(job) | {
            "canonical_terminal": planned.get("canonical_terminal"),
            "provider_requests": int(planned.get("provider_requests") or 0),
            "cost_usd": float(planned.get("cost_usd") or 0.0),
            "strict_candidate_valid": valid,
            "candidate_sha256": cells.digest(candidate) if candidate is not None else None,
            "provider_exchange_accounting": exchanges,
            "backend_dispatches": 0,
        }

    results = await asyncio.gather(*(one(job) for job in rows))
    all_exchanges = [exchange for row in results for exchange in row["provider_exchange_accounting"]]
    profile_envelope = {}
    for model in MODEL_RESERVES_USD:
        exchanges = [exchange for row in results if row["model_key"] == model for exchange in row["provider_exchange_accounting"]]
        profile_envelope[model] = any(
            exchange.get("final_text_present") is True
            and exchange.get("final_text_truncated") is False
            and exchange.get("silent_truncation_detected") is False
            for exchange in exchanges
        )
    family_model_families = {
        family: sorted({
            model_family(row["model_key"]) for row in results
            if row["strict_mechanism"] == family and row["strict_candidate_valid"]
        }) for family in STRICT_FAMILIES
    }
    checks = {
        "exact_21_sessions": len(results) == 21,
        "zero_backend_dispatch": all(row["backend_dispatches"] == 0 for row in results),
        "every_profile_nontruncated_final": all(profile_envelope.values()),
        "every_family_two_model_families": all(len(value) >= 2 for value in family_model_families.values()),
        "zero_retry_fallback_repair": all(
            exchange.get("retry_count") == exchange.get("fallback_count") == exchange.get("repair_count") == 0
            for exchange in all_exchanges
        ),
        "request_bound": sum(row["provider_requests"] for row in results) <= 84,
        "cost_bound": sum(row["cost_usd"] for row in results) <= 12.0,
    }
    core = {
        "schema_version": "dof.wp015a-construction-admission.v1",
        "status": "PASS" if all(checks.values()) else "STOP_SHARED_GATE",
        "checks": checks,
        "session_count": len(results),
        "terminal_counts": dict(collections.Counter(str(row["canonical_terminal"]) for row in results)),
        "physical_model_requests": sum(row["provider_requests"] for row in results),
        "api_cost_usd": sum(row["cost_usd"] for row in results),
        "backend_dispatches": 0,
        "profile_nontruncated_final": profile_envelope,
        "valid_model_families_by_strict_family": family_model_families,
        "rows": results,
    }
    result = core | {"aggregate_sha256": cells.digest(core)}
    cells.write(args.output / "aggregate.json", result)
    print(json.dumps({key: result[key] for key in ("status", "terminal_counts", "physical_model_requests", "api_cost_usd", "valid_model_families_by_strict_family")}, sort_keys=True))
    return 0 if result["status"] == "PASS" else 2


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(allow_abbrev=False)
    value.add_argument("--bundle", type=Path, required=True)
    value.add_argument("--inventory", type=Path, required=True)
    value.add_argument("--capture", type=Path, required=True)
    value.add_argument("--output", type=Path, required=True)
    value.add_argument("--max-parallel", type=int, default=7)
    return value


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run(parser().parse_args())))
