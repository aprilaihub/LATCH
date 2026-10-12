#!/usr/bin/env python3
"""Seven-model, three-language provider-only admission for frozen WP-014H G v2."""

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
sys.path[:0] = [str(REPO), str(REPO / "scripts")]

import run_wp014c_scientific as wp014c  # noqa: E402
import run_wp014g_model_usability as wp014g  # noqa: E402
from optimizer_pipeline.dof.multi_provider import (  # noqa: E402
    MAX_OUTPUT_TOKENS,
    PROFILES,
    StrictMultiProviderTransport,
    discover_openrouter_endpoint,
)
from optimizer_pipeline.dof.provider import ProviderRequestBinding, ProviderSettings  # noqa: E402
from optimizer_pipeline.dof.scientific_evidence import strict_boundary_certificate  # noqa: E402
from optimizer_pipeline.dof.single_submission.contract_v5 import (  # noqa: E402
    CONTRACT_VERSION,
    compile_candidate_v5,
)
from optimizer_pipeline.dof.single_submission.contract_v6 import (  # noqa: E402
    contract_for_cell,
    language_condition_error,
    prompt_for_cell,
)
from optimizer_pipeline.dof.single_submission.language import CompileContext  # noqa: E402
from optimizer_pipeline.dof.single_submission.specs import canonical_json_bytes  # noqa: E402


SCHEMA = "dof.wp014i-seven-model-language-admission.v1"
MODELS = tuple(PROFILES)
CONDITIONS = ("M_ONLY", "P_BOUNDARY", "G_STRICT_BOUNDARY")
OPENROUTER_TAG = "google-vertex/global"
MAX_TURNS = 4


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


def _binding(
    profile: Any,
    *,
    cell_id: str,
    turn: int,
    prompt: bytes,
    context: bytes,
    interface_hash: str,
) -> ProviderRequestBinding:
    return ProviderRequestBinding(
        run_id="wp014i-language-admission-v1",
        spec_hash=digest({"cell_id": cell_id, "model": profile.canonical_model_id}),
        experiment_id="WP-014I",
        condition_id=cell_id,
        repeat_id="NO_REPEAT",
        decision_id=f"{cell_id}-turn-{turn}",
        context_bytes=context,
        prompt_bytes=prompt,
        prompt_version="dof.wp014i-language-admission-prompt.v1",
        action_interface_version=CONTRACT_VERSION,
        action_interface_hash=interface_hash,
        context_interface_version="dof.wp014i-language-admission-context.v1",
        context_interface_hash=digest(context),
        catalogue_version="dof.action-surface.v4",
        catalogue_hash=interface_hash,
        capability_hash=digest({"provider": profile.provider, "adapter": profile.adapter_version}),
        mask_evidence_hash=digest({"provider_only": True, "no_qor": True}),
        provider_role="wp014i_language_admission",
        provider=profile.provider,
        model_id=profile.canonical_model_id,
        adapter_version=profile.adapter_version,
        settings=ProviderSettings(
            temperature=None,
            max_tokens=MAX_OUTPUT_TOKENS,
            seed=None,
            response_format="json_object",
            timeout_seconds=300.0,
            max_retries=0,
            retry_on=(),
            max_requests=1,
            unknown_cost_policy="reject",
            unknown_usage_policy="reject",
        ),
    )


def _classify(
    content: bytes,
    *,
    condition: str,
    compile_context: CompileContext,
) -> dict[str, Any]:
    try:
        message = json.loads(content.decode("utf-8"))
    except Exception as exc:
        return {"terminal": "MODEL_OUTPUT_NOT_STRICT_JSON", "reason": type(exc).__name__}
    if not isinstance(message, Mapping) or message.get("kind") != "COMMIT" or not isinstance(message.get("candidate"), Mapping):
        return {"terminal": "MODEL_OUTPUT_NOT_FINAL_COMMIT"}
    candidate = message["candidate"]
    protocol_condition = "G_BOUNDARY" if condition == "G_STRICT_BOUNDARY" else condition
    error = language_condition_error(candidate, protocol_condition)
    if error:
        return {"terminal": "MODEL_OUTPUT_LANGUAGE_CONDITION_REJECTION", "reason": error}
    try:
        compiled = compile_candidate_v5(candidate, compile_context)
    except Exception as exc:
        return {
            "terminal": "MODEL_OUTPUT_CONSTRUCTION_REJECTION",
            "reason": getattr(exc, "reason", type(exc).__name__),
            "candidate_sha256": digest(candidate),
        }
    strict = strict_boundary_certificate(candidate=candidate, execution_started=False, execution=None)
    if condition == "G_STRICT_BOUNDARY" and strict.get("mechanism_family") is None:
        return {
            "terminal": "MODEL_OUTPUT_STRICT_CONSTRUCT_REJECTION",
            "reason": "NO_CONSERVATIVE_STRICT_RULE",
            "candidate_sha256": digest(candidate),
        }
    return {
        "terminal": "MODEL_OUTPUT_COMPATIBLE",
        "candidate": candidate,
        "candidate_sha256": digest(candidate),
        "compiled_sha256": compiled.public_sha256,
        "selected_language": compiled.language,
        "heavy_calls": compiled.heavy_calls,
        "light_mutations": compiled.light_mutations,
        "strict_construct_family": strict.get("mechanism_family"),
        "strict_runtime_certificate": "NOT_RUN_PROVIDER_ONLY",
    }


async def run(args: argparse.Namespace) -> int:
    if not 1 <= args.max_parallel <= 8:
        raise ValueError("weekday_provider_parallelism_must_be_1_to_8")
    if args.out.exists():
        raise FileExistsError(args.out)
    missing = sorted({profile.credential_env for profile in PROFILES.values() if not os.environ.get(profile.credential_env)})
    if missing:
        raise RuntimeError("required_provider_credentials_missing:" + ",".join(missing))
    if set(MODELS) != {
        "openai/gpt-5.6-luna", "openai/gpt-5.6-terra", "openai/gpt-5.6-sol",
        "anthropic/claude-sonnet-5", "deepseek/deepseek-v4-flash",
        "deepseek/deepseek-v4-pro", "google/gemini-3.6-flash",
    }:
        raise RuntimeError("wp014i_exact_seven_model_panel_required")

    bundle = read(args.bundle)
    root = bundle["roots"]["amd_mini-isp"]
    observation, private, query_results, catalogue = wp014g._trial(root, args.capture / "amd_mini-isp")
    compile_context = CompileContext(
        private["registry"], "ROUTED", private["predefined_regions"],
        private["geometry_bounds"], fanout_admitted=True, action_surface_version="v3",
    )
    endpoint = await discover_openrouter_endpoint(os.environ["OPENROUTER_API_KEY"])
    if endpoint["endpoint_tag"] != OPENROUTER_TAG:
        raise RuntimeError("openrouter_endpoint_tag_drift")
    args.out.mkdir(parents=True)
    write(args.out / "openrouter-endpoint-freeze.json", endpoint)
    semaphore = asyncio.Semaphore(args.max_parallel)
    started = time.monotonic()

    async def one(model: str, condition: str) -> dict[str, Any]:
        profile = PROFILES[model]
        protocol_condition = "G_BOUNDARY" if condition == "G_STRICT_BOUNDARY" else condition
        contract = contract_for_cell(
            study_track="C_CONDITIONED", coverage_regime="FULL_M8",
            language_condition=protocol_condition, level="C",
        )
        interface_hash = digest(contract)
        objective = {
            "M_ONLY": "Return one valid M control COMMIT.",
            "P_BOUNDARY": "Return one valid P COMMIT using a P-exclusive operation and ending in a scoreable routed state.",
            "G_STRICT_BOUNDARY": (
                "Return one valid G COMMIT satisfying a conservative strict rule: either generated non-predefined geometry consumed by ASSIGN_REGION, "
                "a DIFF-produced multi-net collection consumed by BATCH_ROUTE_NETS, or a COUPLE_SITES allocation consumed by APPLY_ALLOCATION. "
                "Use at most 8 statements and 4 heavy calls."
            ),
        }[condition]
        prompt = prompt_for_cell(
            study_track="C_CONDITIONED", language_condition=protocol_condition, level="C",
        ) + b" Provider-only development admission; no backend or QoR is available. " + objective.encode()
        feedback: list[dict[str, Any]] = []
        exchanges: list[dict[str, Any]] = []
        final: dict[str, Any] = {"terminal": "NO_VALID_SUBMISSION"}
        cell_id = "i14-" + digest({"model": model, "condition": condition})[:24]
        for turn in range(1, MAX_TURNS + 1):
            context = canonical_json_bytes({
                "schema_version": "dof.wp014i-language-admission-context.v1",
                "label": "WP014I_PROVIDER_ONLY_NOT_SCIENTIFIC_REPEAT",
                "condition": condition,
                "common_observation": dict(observation.payload),
                "common_query_catalogue": catalogue,
                "materialized_query_results": query_results if condition == "G_STRICT_BOUNDARY" else [],
                "language_contract": contract,
                "geometry_bounds": list(private["geometry_bounds"]),
                "construction_feedback": feedback[-1:] if feedback else [],
                "turn": turn,
                "turns_remaining": MAX_TURNS - turn,
                "qor_feedback_present": False,
                "sealed_holdout": False,
            })
            if len(prompt) + len(context) > 65_536:
                raise RuntimeError("wp014i_language_admission_input_too_large")
            transport = StrictMultiProviderTransport(
                profile=profile,
                api_key=os.environ[profile.credential_env],
                pinned_endpoint_tag=OPENROUTER_TAG if profile.provider == "openrouter" else None,
            )
            binding = _binding(
                profile, cell_id=cell_id, turn=turn, prompt=prompt,
                context=context, interface_hash=interface_hash,
            )
            response = None
            error = None
            async with semaphore:
                try:
                    response = await transport.send(binding, 0)
                except Exception as exc:
                    error = f"{type(exc).__name__}:{exc}"
            if response is None:
                final = {"terminal": "PROVIDER_FAILURE", "reason": error}
                exchanges.append({"turn": turn, "physical_requests": transport.physical_requests, "exchange": transport.last_exchange, **final})
                break
            classified = _classify(response.content_bytes, condition=condition, compile_context=compile_context)
            exchanges.append({
                "turn": turn,
                "physical_requests": transport.physical_requests,
                "cost_usd": float(response.cost_usd or 0.0),
                "input_tokens": response.input_tokens,
                "output_tokens": response.output_tokens,
                "response_sha256": digest(response.content_bytes),
                "exchange": transport.last_exchange,
                **{key: value for key, value in classified.items() if key != "candidate"},
            })
            final = classified
            if classified["terminal"] == "MODEL_OUTPUT_COMPATIBLE":
                break
            feedback.append({
                "feedback_round": len(feedback) + 1,
                "error_code": classified["terminal"],
                "reason": classified.get("reason"),
                "qor_feedback_present": False,
            })
        row = {
            "cell_id": cell_id,
            "model": model,
            "provider": profile.provider,
            "route": profile.route,
            "condition": condition,
            "turns_used": len(exchanges),
            "construction_feedback_rounds": len(feedback),
            "physical_requests": sum(int(row["physical_requests"]) for row in exchanges),
            "cost_usd": sum(float(row.get("cost_usd") or 0.0) for row in exchanges),
            "retry_count": 0,
            "fallback_count": 0,
            "repair_count": 0,
            "exchanges": exchanges,
            **final,
        }
        write(args.out / "cells" / cell_id / "result.json", row)
        return row

    rows = await asyncio.gather(*(one(model, condition) for model in MODELS for condition in CONDITIONS))
    total_cost = sum(float(row["cost_usd"]) for row in rows)
    total_requests = sum(int(row["physical_requests"]) for row in rows)
    if total_requests > 84 or total_cost > 20.0:
        raise RuntimeError("wp014i_language_admission_cap_exceeded")
    terminals = sorted({str(row["terminal"]) for row in rows})
    core = {
        "schema_version": SCHEMA,
        "label": "WP014I_PROVIDER_ONLY_NOT_SCIENTIFIC_REPEAT",
        "models": list(MODELS),
        "conditions": list(CONDITIONS),
        "sessions": len(rows),
        "physical_model_requests": total_requests,
        "api_cost_usd": total_cost,
        "terminal_counts": {value: sum(row["terminal"] == value for row in rows) for value in terminals},
        "compatible_by_condition": {
            condition: sum(row["condition"] == condition and row["terminal"] == "MODEL_OUTPUT_COMPATIBLE" for row in rows)
            for condition in CONDITIONS
        },
        "retry_count": 0,
        "fallback_count": 0,
        "repair_count": 0,
        "queue_wall_seconds": time.monotonic() - started,
        "provider_metadata_requests": endpoint["physical_metadata_requests"],
        "sealed_holdout_accesses": 0,
        "provider_or_qor_model_selection": False,
        "rows": rows,
    }
    result = core | {"aggregate_sha256": digest(core)}
    write(args.out / "aggregate.json", result)
    print(json.dumps({key: result[key] for key in ("sessions", "physical_model_requests", "api_cost_usd", "terminal_counts", "compatible_by_condition")}, sort_keys=True))
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
