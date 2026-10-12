#!/usr/bin/env python3
"""Cost-bounded WP-014G construction-usability probes on direct providers."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import sys
import time
from typing import Any, Mapping


REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))
if str(REPO / "scripts") not in sys.path:
    sys.path.insert(0, str(REPO / "scripts"))

import run_wp014c_scientific as wp014c  # noqa: E402
from optimizer_pipeline.dof.multi_provider import (  # noqa: E402
    MAX_OUTPUT_TOKENS,
    PROFILES,
    StrictMultiProviderTransport,
)
from optimizer_pipeline.dof.provider import (  # noqa: E402
    ProviderRequestBinding,
    ProviderSettings,
)
from optimizer_pipeline.dof.scientific_evidence import causal_lineage  # noqa: E402
from optimizer_pipeline.dof.single_submission.contract_v5 import (  # noqa: E402
    CONTRACT_VERSION,
    compile_candidate_v5,
    contract_for_level,
)
from optimizer_pipeline.dof.single_submission.language import CompileContext  # noqa: E402
from optimizer_pipeline.dof.single_submission.observation_v4 import (  # noqa: E402
    CommonQuerySurfaceV4,
)
from optimizer_pipeline.dof.single_submission.specs import canonical_json_bytes  # noqa: E402


SCHEMA = "dof.wp014g-model-usability.v1"
MODELS = ("openai/gpt-5.6-terra", "deepseek/deepseek-v4-pro")
ARCHITECTURES = {
    "STRAIGHT_LINE_GBL": (
        "Use the executable straight-line G binding language exactly as documented. "
        "Create a generated value with a G-exclusive node, bind it, and consume that exact binding in an inherited P operation."
    ),
    "TYPED_SSA_CARD": (
        "Treat every bind as a typed single-assignment value. Construct an explicit def-use chain: "
        "G producer -> typed binding -> inherited P consumer. Do not create unused producers. Return the lowered executable G program."
    ),
    "MECHANISM_CAPSULE_CARD": (
        "Plan one bounded mechanism capsule, then lower it to the documented executable G steps. "
        "The capsule must contain a generated target/geometry/site and the same inherited P consumer; no hidden operation or new mechanism is allowed."
    ),
}
TASKS = {
    "SPATIAL": ("amd_mini-isp", "Generate a non-empty cell selection and generated region, assign the selection to that region, then end with legal place/route implementation."),
    "NET_ROUTE": ("logicnets_jscl", "Generate one non-clock high-fanout net target and consume it with ROUTE_NET using an allowed delay mode."),
    "PROPERTY": ("logicnets_jscl", "Generate one net target, consume it with SET_NET_DONT_TOUCH, then perform the legal follow-up implementation ending routed."),
    "HARDBLOCK_SITE": ("amd_mini-isp", "Generate one hardblock target and a bounded choice from published compatible site handles, relocate it, then end routed."),
}


def digest(value: Any) -> str:
    raw = value if isinstance(value, bytes) else canonical_json_bytes(value)
    return hashlib.sha256(raw).hexdigest()


def read(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text())
    if not isinstance(value, dict):
        raise ValueError("json_object_required:" + str(path))
    return value


def write(path: Path, value: Any) -> None:
    wp014c.write(path, value)


def _trial(root: Mapping[str, Any], capture: Path):
    observation, private = wp014c.common_observation_v3(root, capture)
    q = CommonQuerySurfaceV4(private["registry"])
    results = []
    for name, rows in private["rows"].items():
        result = q.materialize(
            query_name=name, top_k=min(8, max(1, len(rows))),
            private_rows=rows,
            tool_call_ids=("wp014g-frozen-development-capture",),
            latency_seconds=0.0,
            missing_reason=private["missing_reasons"].get(name),
        )
        results.append(json.loads(result.canonical_bytes))
    return observation, private, results, q.catalogue


def _binding(profile, *, cell_id: str, prompt: bytes, context: bytes, interface_hash: str):
    return ProviderRequestBinding(
        run_id="wp014g-usability-v1",
        spec_hash=digest({"cell_id": cell_id, "model": profile.canonical_model_id}),
        experiment_id="WP-014G", condition_id=cell_id,
        repeat_id="NO_REPEAT", decision_id=cell_id,
        context_bytes=context, prompt_bytes=prompt,
        prompt_version="dof.wp014g-usability-prompt.v1",
        action_interface_version=CONTRACT_VERSION,
        action_interface_hash=interface_hash,
        context_interface_version="dof.wp014g-usability-context.v1",
        context_interface_hash=digest(context),
        catalogue_version="dof.action-surface.v3",
        catalogue_hash=interface_hash,
        capability_hash=digest({"provider": profile.provider, "adapter": profile.adapter_version}),
        mask_evidence_hash=digest({"architecture_comparison": True, "same_OQ": True}),
        provider_role="wp014g_construction_usability",
        provider=profile.provider, model_id=profile.canonical_model_id,
        adapter_version=profile.adapter_version,
        settings=ProviderSettings(
            temperature=None, max_tokens=MAX_OUTPUT_TOKENS, seed=None,
            response_format="json_object", timeout_seconds=300.0,
            max_retries=0, retry_on=(), max_requests=1,
            unknown_cost_policy="reject", unknown_usage_policy="reject",
        ),
    )


def _classify(content: bytes, private: Mapping[str, Any]) -> dict[str, Any]:
    try:
        message = json.loads(content.decode("utf-8"))
    except Exception as exc:
        return {"terminal": "MODEL_OUTPUT_NOT_STRICT_JSON", "reason": type(exc).__name__}
    if not isinstance(message, Mapping) or message.get("kind") != "COMMIT" or not isinstance(message.get("candidate"), Mapping):
        return {"terminal": "MODEL_OUTPUT_NOT_FINAL_COMMIT"}
    candidate = message["candidate"]
    if candidate.get("language") != "G":
        return {"terminal": "MODEL_OUTPUT_LANGUAGE_REJECTION", "candidate": candidate}
    try:
        compiled = compile_candidate_v5(
            candidate,
            CompileContext(
                private["registry"], "ROUTED", private["predefined_regions"],
                private["geometry_bounds"], fanout_admitted=True,
                action_surface_version="v3",
            ),
        )
    except Exception as exc:
        return {
            "terminal": "MODEL_OUTPUT_CONSTRUCTION_REJECTION",
            "reason": getattr(exc, "reason", type(exc).__name__),
            "candidate": candidate,
        }
    lineage = causal_lineage(candidate=candidate, execution_started=False, execution=None)
    if not lineage["edges"]:
        terminal = "MODEL_OUTPUT_CAUSAL_BINDING_REJECTION"
    else:
        terminal = "MODEL_OUTPUT_COMPATIBLE"
    return {
        "terminal": terminal,
        "candidate": candidate,
        "candidate_sha256": digest(candidate),
        "compiled_sha256": compiled.public_sha256,
        "heavy_calls": compiled.heavy_calls,
        "light_mutations": compiled.light_mutations,
        "producer_count": sum(node["node_role"] == "PRODUCER" for node in lineage["nodes"]),
        "producer_consumer_edges": len(lineage["edges"]),
        "compile_lineage_sha256": lineage["lineage_sha256"],
    }


async def run(args: argparse.Namespace) -> int:
    if not 1 <= args.max_parallel <= 8:
        raise ValueError("weekday_parallelism_must_be_1_to_8")
    if args.out.exists():
        raise FileExistsError(args.out)
    for model in MODELS:
        credential = PROFILES[model].credential_env
        if not os.environ.get(credential):
            raise RuntimeError("required_direct_provider_credential_missing:" + credential)
    bundle = read(args.bundle)
    contract = contract_for_level("L4")
    interface_hash = digest(contract)
    args.out.mkdir(parents=True)
    semaphore = asyncio.Semaphore(args.max_parallel)
    started = time.monotonic()

    cells = []
    for model in MODELS:
        for architecture in ARCHITECTURES:
            for task in TASKS:
                cells.append((model, architecture, task))
    if len(cells) != 24:
        raise RuntimeError("wp014g_exact_24_usability_cells_required")

    async def one(cell):
        model, architecture, task = cell
        root_id, objective = TASKS[task]
        observation, private, query_results, catalogue = _trial(
            bundle["roots"][root_id], args.capture / root_id,
        )
        cell_id = "g14-" + digest({"model": model, "architecture": architecture, "task": task})[:24]
        prompt = (
            "Return exactly one JSON object with kind=COMMIT and candidate=<program>. "
            "No prose, QUERY, repair, retry, or fallback is allowed. "
            + ARCHITECTURES[architecture] + " Objective: " + objective
        ).encode()
        context = canonical_json_bytes({
            "schema_version": "dof.wp014g-usability-context.v1",
            "label": "DEVELOPMENT_ONLY_NOT_WP015",
            "architecture": architecture,
            "task": task,
            "common_observation": dict(observation.payload),
            "common_query_catalogue": catalogue,
            "materialized_query_results": query_results,
            "language_contract": contract,
            "qor_feedback_present": False,
            "sealed_holdout": False,
        })
        profile = PROFILES[model]
        binding = _binding(profile, cell_id=cell_id, prompt=prompt, context=context, interface_hash=interface_hash)
        transport = StrictMultiProviderTransport(
            profile=profile, api_key=os.environ[profile.credential_env],
        )
        error = None
        response = None
        async with semaphore:
            try:
                response = await transport.send(binding, 0)
            except Exception as exc:
                error = f"{type(exc).__name__}:{exc}"
        if response is None:
            classification = {"terminal": "PROVIDER_FAILURE", "reason": error}
            content = b""
        else:
            content = response.content_bytes
            classification = _classify(content, private)
        row = {
            "cell_id": cell_id, "model": model,
            "provider": profile.provider, "route": profile.route,
            "architecture": architecture, "task": task, "root_id": root_id,
            "physical_requests": transport.physical_requests,
            "retry_count": 0, "fallback_count": 0, "repair_count": 0,
            "cost_usd": float(response.cost_usd or 0.0) if response else 0.0,
            "input_tokens": response.input_tokens if response else None,
            "output_tokens": response.output_tokens if response else None,
            "response_sha256": digest(content),
            "exchange": transport.last_exchange,
            **classification,
        }
        write(args.out / "cells" / cell_id / "result.json", row)
        return row

    rows = await asyncio.gather(*(one(cell) for cell in cells))
    total_cost = sum(row["cost_usd"] for row in rows)
    if total_cost > 15.0:
        raise RuntimeError("wp014g_api_cost_cap_exceeded")
    terminals = sorted({row["terminal"] for row in rows})
    core = {
        "schema_version": SCHEMA,
        "not_a_wp015_run": True,
        "providers_used": sorted({row["provider"] for row in rows}),
        "anthropic_requests": 0,
        "openrouter_requests": 0,
        "physical_model_requests": sum(row["physical_requests"] for row in rows),
        "api_cost_usd": total_cost,
        "retry_count": 0, "fallback_count": 0, "repair_count": 0,
        "terminal_counts": {terminal: sum(row["terminal"] == terminal for row in rows) for terminal in terminals},
        "by_architecture": {
            architecture: {
                "sessions": sum(row["architecture"] == architecture for row in rows),
                "compatible": sum(row["architecture"] == architecture and row["terminal"] == "MODEL_OUTPUT_COMPATIBLE" for row in rows),
            }
            for architecture in ARCHITECTURES
        },
        "by_model": {
            model: {
                "sessions": sum(row["model"] == model for row in rows),
                "compatible": sum(row["model"] == model and row["terminal"] == "MODEL_OUTPUT_COMPATIBLE" for row in rows),
            }
            for model in MODELS
        },
        "queue_wall_seconds": time.monotonic() - started,
        "sealed_holdout_accesses": 0,
        "rows": rows,
    }
    value = core | {"aggregate_sha256": digest(core)}
    write(args.out / "aggregate.json", value)
    print(json.dumps({
        "physical_model_requests": value["physical_model_requests"],
        "api_cost_usd": value["api_cost_usd"],
        "providers_used": value["providers_used"],
        "terminal_counts": value["terminal_counts"],
        "by_architecture": value["by_architecture"],
    }, sort_keys=True))
    return 0


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(allow_abbrev=False)
    value.add_argument("--bundle", type=Path, required=True)
    value.add_argument("--capture", type=Path, required=True)
    value.add_argument("--out", type=Path, required=True)
    value.add_argument("--max-parallel", type=int, default=8)
    return value


if __name__ == "__main__":
    wp014c.configure()
    raise SystemExit(asyncio.run(run(parser().parse_args())))
