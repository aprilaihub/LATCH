"""Frozen E0 matrix shape and deterministic fake contract runner.

This module deliberately stops short of a scheduler.  It defines the twelve
calibration jobs and proves that each level/model shape can traverse the
accepted projector, provider, parser, gate and technical-host boundaries.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any, Mapping

from .catalogues import clean_macro_catalogue, strict_l3_catalogue
from .contracts import DesignStage, DoFLevel, EventClass, UnknownRegime
from .gate import ACTION_PROPOSAL_SCHEMA_VERSION, ActionGate
from .manifests import capability_manifest, interface_fingerprints
from .masks import create_mask_rules
from .openrouter import (
    OPENROUTER_ADAPTER_VERSION,
    OPENROUTER_E0_MODELS,
    OpenRouterChatCompletionsTransport,
    build_openrouter_outbound,
)
from .evidence import ImmutableEvidenceWriter
from .projector import DecisionContextProjector
from .provider import (
    DoFProviderObserver,
    ProviderRequestBinding,
    ProviderRunLedger,
    ProviderSettings,
    RawProviderResponse,
    ScriptedFakeProvider,
    verify_provider_attempts,
    write_provider_attempt_trace,
)
from .technical import run_fake_technical_mutation


E0_MATRIX_SCHEMA = "dof.e0-matrix.v1"
E0_FAKE_RESULT_SCHEMA = "dof.e0-fake-matrix-result.v1"
E0_BENCHMARKS = ("vexriscv_re-place", "logicnets_jscl")


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def _hash(value: Any) -> str:
    raw = value if isinstance(value, bytes) else _canonical_bytes(value)
    return hashlib.sha256(raw).hexdigest()


def e0_jobs() -> list[dict[str, Any]]:
    jobs: list[dict[str, Any]] = []
    for benchmark in E0_BENCHMARKS:
        jobs.extend(
            [
                {
                    "job_id": f"{benchmark}__l0",
                    "benchmark": benchmark,
                    "level": "L0",
                    "model": None,
                    "policy": "none",
                },
                {
                    "job_id": f"{benchmark}__l1",
                    "benchmark": benchmark,
                    "level": "L1",
                    "model": None,
                    "policy": "deterministic",
                },
            ]
        )
        for level in ("L2", "L3"):
            for model in OPENROUTER_E0_MODELS:
                model_slug = model.replace("/", "_").replace(".", "-")
                jobs.append(
                    {
                        "job_id": f"{benchmark}__{level.lower()}__{model_slug}",
                        "benchmark": benchmark,
                        "level": level,
                        "model": model,
                        "policy": "openrouter",
                    }
                )
    payload = {
        "schema_version": E0_MATRIX_SCHEMA,
        "study_status": "PILOT_NON_CLAIM",
        "regime": "known",
        "repeats": 1,
        "jobs": jobs,
    }
    payload["matrix_sha256"] = _hash(payload)
    return payload["jobs"]


def e0_matrix_manifest() -> dict[str, Any]:
    payload = {
        "schema_version": E0_MATRIX_SCHEMA,
        "study_status": "PILOT_NON_CLAIM",
        "regime": "known",
        "repeats": 1,
        "benchmarks": list(E0_BENCHMARKS),
        "models": list(OPENROUTER_E0_MODELS),
        "levels": ["L0", "L1", "L2", "L3"],
        "max_heavy_calls_per_nonbaseline_job": 3,
        "max_model_requests_per_model_job": 3,
        "controller": "serial_iterative_bounded.v1",
        "jobs": e0_jobs(),
    }
    return payload | {"matrix_sha256": _hash(payload)}


def e0_action_variants(level: DoFLevel) -> list[dict[str, Any]]:
    """Enumerate the exact root-stage action/parameter plans frozen for E0."""

    if level in {DoFLevel.L1, DoFLevel.L2}:
        actions = clean_macro_catalogue(level).actions
    elif level is DoFLevel.L3:
        actions = strict_l3_catalogue().actions
    else:
        return []
    variants: list[dict[str, Any]] = []
    for action in actions:
        if level is DoFLevel.L3 and DesignStage.ROUTED not in action.allowed_stages:
            # These actions remain visible and are rejected by the common gate
            # at a routed root; freezing an unreachable backend plan would
            # falsely imply structural legality.
            continue
        schema = action.to_dict()["parameters_schema"]
        directive_values = (
            schema.get("properties", {}).get("directive", {}).get("enum", [])
        )
        parameter_rows = (
            [{"directive": value} for value in directive_values]
            if directive_values
            else [{}]
        )
        for parameters in parameter_rows:
            variants.append(
                {
                    "action_key": action.name,
                    "parameters": parameters,
                    "variant_sha256": _hash(
                        {"action_key": action.name, "parameters": parameters}
                    ),
                }
            )
    return variants


def _source(benchmark: str) -> dict[str, Any]:
    return {
        "condition": {"pseudonym": f"condition-{benchmark}"},
        "state": {
            "pseudonym": f"state-{benchmark}-root",
            "lineage_pseudonym": f"lineage-{benchmark}",
            "stage": "ROUTED",
            "measurements": {
                "timing": {
                    "wns_ns": -0.1,
                    "tns_ns": -1.0,
                    "failing_endpoints": 1,
                    "clock_period_ns": 2.5,
                    "estimated_fmax_mhz": 384.6,
                },
                "route": {"status": "fully routed", "unrouted_nets": 0},
                "placement": {"status": "placed", "placed_cells": 1000},
                "utilization": {"lut": 1000, "ff": 1200, "dsp": 2, "bram": 4},
                "congestion": {"level": "unknown", "hotspots": 0},
            },
        },
        "history": [],
        "budget_remaining": {
            "IMPLEMENTATION_HEAVY": 3,
            "MUTATION_LIGHT": 3,
            "READ_ONLY": 20,
            "LLM_REQUEST": 3,
            "WALL_TIME_SECONDS": 1200.0,
        },
        "validation": {"status": "CLEAN", "errors": [], "warnings": []},
        "stop_conditions": ["implementation-heavy budget exhausted"],
        "known_priors": {},
    }


def _prompt(decision_id: str, catalogue: Any, level: DoFLevel) -> bytes:
    kind = "ACTION with exactly one step" if level is DoFLevel.L2 else "ACTION or PLAN"
    return (
        "Return exactly one JSON object and no surrounding text. No markdown, commentary, "
        "tools, paths, Tcl, shell, or invented fields. The object must contain canonical "
        "fields schema_version, proposal_id, decision_id, catalogue_version, catalogue_hash, "
        "kind, and steps; every step must contain canonical step_id, action_key, and parameters. "
        f"Use schema_version {ACTION_PROPOSAL_SCHEMA_VERSION}, decision_id {decision_id}, "
        f"catalogue_version {catalogue.version}, catalogue_hash {catalogue.sha256}. "
        f"kind may be STOP with steps [] or {kind} using only the legal action schema in context."
    ).encode("utf-8")


def _binding(job: Mapping[str, Any]) -> tuple[ProviderRequestBinding, Any, Any]:
    level = DoFLevel(str(job["level"]))
    catalogue = (
        clean_macro_catalogue(DoFLevel.L2)
        if level is DoFLevel.L2
        else strict_l3_catalogue()
    )
    mask = create_mask_rules(version="dof.e0-known-empty-mask.v1")
    projected = DecisionContextProjector().project(
        level=level,
        regime=UnknownRegime.KNOWN,
        source=_source(str(job["benchmark"])),
        catalogue=catalogue,
        mask=mask,
    )
    interfaces = interface_fingerprints(level)
    decision_id = "decision-" + _hash(job)[:20]
    context_bytes = projected.serialized.encode("utf-8")
    prompt = _prompt(decision_id, catalogue, level)
    base = {
        "job": dict(job),
        "decision_id": decision_id,
        "context_sha256": _hash(context_bytes),
        "prompt_sha256": _hash(prompt),
    }
    binding = ProviderRequestBinding(
        run_id="e0-" + _hash(job)[:24],
        spec_hash=_hash(base),
        experiment_id="e0-openrouter-pilot",
        condition_id=str(job["job_id"]),
        repeat_id="repeat-0",
        decision_id=decision_id,
        context_bytes=context_bytes,
        prompt_bytes=prompt,
        prompt_version=interfaces["prompt_version"],
        action_interface_version=interfaces["action_schema_version"],
        action_interface_hash=interfaces["action_schema_hash"],
        context_interface_version=interfaces["context_version"],
        context_interface_hash=interfaces["context_hash"],
        catalogue_version=catalogue.version,
        catalogue_hash=catalogue.sha256,
        capability_hash=capability_manifest(level).sha256,
        mask_evidence_hash=mask.hash,
        provider_role="e0_policy",
        provider="openrouter",
        model_id=str(job["model"]),
        adapter_version=OPENROUTER_ADAPTER_VERSION,
        settings=ProviderSettings(
            temperature=None,
            max_tokens=4096,
            seed=None,
            timeout_seconds=120.0,
            max_retries=0,
            retry_on=(),
            max_requests=1,
            max_usd=5.0,
            max_cost_per_request_usd=5.0,
            unknown_cost_policy="reject",
            unknown_usage_policy="reject",
        ),
    )
    return binding, catalogue, mask


def _fake_payload(binding: ProviderRequestBinding, level: DoFLevel) -> dict[str, Any]:
    if level is DoFLevel.L2:
        action_key = "macro.phys_opt"
        parameters: dict[str, Any] = {}
    else:
        action_key = "manual.phys_opt_design"
        parameters = {"directive": "Explore"}
    return {
        "schema_version": ACTION_PROPOSAL_SCHEMA_VERSION,
        "proposal_id": "proposal-" + binding.request_contract_hash[:20],
        "decision_id": binding.decision_id,
        "catalogue_version": binding.catalogue_version,
        "catalogue_hash": binding.catalogue_hash,
        "kind": "ACTION",
        "steps": [
            {
                "step_id": "step-001",
                "action_key": action_key,
                "parameters": parameters,
            }
        ],
    }


def run_fake_e0_matrix(output_dir: str | Path) -> dict[str, Any]:
    """Run all twelve shapes without provider or Vivado network entry."""

    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=False)
    rows: list[dict[str, Any]] = []
    for job in e0_jobs():
        job_dir = output / str(job["job_id"])
        level = DoFLevel(str(job["level"]))
        if level is DoFLevel.L0:
            rows.append(
                dict(job)
                | {
                    "endpoint": "VALID_COMPLETION",
                    "provider_requests": 0,
                    "heavy_calls": 0,
                    "gate_accepted": None,
                    "technical_endpoint": None,
                }
            )
            continue
        if level is DoFLevel.L1:
            technical = run_fake_technical_mutation(
                job_dir,
                level=level,
                action_key="macro.phys_opt",
                parameters={},
                initial_stage=DesignStage.ROUTED,
            )
            rows.append(
                dict(job)
                | {
                    "endpoint": "VALID_COMPLETION",
                    "provider_requests": 0,
                    "heavy_calls": technical["budget"]["used"][
                        EventClass.IMPLEMENTATION_HEAVY.value
                    ],
                    "gate_accepted": True,
                    "technical_endpoint": technical["endpoint"],
                }
            )
            continue
        binding, catalogue, mask = _binding(job)
        payload = _fake_payload(binding, level)
        response = RawProviderResponse(
            content_bytes=_canonical_bytes(payload),
            actual_provider="openrouter",
            provider_request_id="fake-request-" + binding.request_contract_hash[:12],
            returned_model_id=binding.model_id,
            returned_model_revision="fake-upstream",
            input_tokens=100,
            output_tokens=40,
            total_tokens=140,
            cost_usd=0.01,
            cost_source="fake-openrouter",
            observed_request_contract_hash=binding.request_contract_hash,
        )
        decision = DoFProviderObserver(
            run_ledger=ProviderRunLedger.for_binding(binding)
        ).request(binding, ScriptedFakeProvider([response]))
        if not decision.accepted or decision.bound_proposal is None:
            raise RuntimeError("fake_e0_provider_decision_rejected")
        if verify_provider_attempts(decision.attempts, binding):
            raise RuntimeError("fake_e0_provider_attempt_invalid")
        proposal = decision.bound_proposal.verify_for_dispatch(binding)
        gate = ActionGate().evaluate(
            proposal,
            level=level,
            expected_decision_id=binding.decision_id,
            catalogue=catalogue,
            expected_catalogue_hash=catalogue.sha256,
            current_stage=DesignStage.ROUTED,
            budget_remaining={event_class: 3 for event_class in EventClass},
            mask=mask,
        )
        if not gate.accepted or len(gate.gated_steps) != 1:
            raise RuntimeError("fake_e0_gate_rejected")
        step = gate.gated_steps[0]
        technical = run_fake_technical_mutation(
            job_dir,
            level=level,
            action_key=step.action_key,
            parameters=dict(step.parameters),
            initial_stage=DesignStage.ROUTED,
        )
        rows.append(
            dict(job)
            | {
                "endpoint": "VALID_COMPLETION",
                "provider_requests": 1,
                "request_contract_hash": binding.request_contract_hash,
                "outbound_binding_sha256": build_openrouter_outbound(
                    binding
                ).binding_sha256,
                "heavy_calls": technical["budget"]["used"][
                    EventClass.IMPLEMENTATION_HEAVY.value
                ],
                "gate_accepted": gate.accepted,
                "technical_endpoint": technical["endpoint"],
            }
        )
    result = {
        "schema_version": E0_FAKE_RESULT_SCHEMA,
        "matrix": e0_matrix_manifest(),
        "jobs_run": len(rows),
        "jobs_passed": sum(row["endpoint"] == "VALID_COMPLETION" for row in rows),
        "provider_requests": sum(row["provider_requests"] for row in rows),
        "rows": rows,
    }
    result["result_sha256"] = _hash(result)
    (output / "fake-e0-result.json").write_bytes(
        json.dumps(result, sort_keys=True, indent=2).encode() + b"\n"
    )
    return result


def run_synthetic_openrouter_preflight(
    model: str,
    output_dir: str | Path,
    *,
    credential: str | None = None,
    transport_factory: Any = OpenRouterChatCompletionsTransport,
) -> dict[str, Any]:
    """Issue the one authorised no-backend synthetic request for ``model``."""

    if model not in OPENROUTER_E0_MODELS:
        raise ValueError("openrouter_exact_e0_model_required")
    job = {
        "job_id": "synthetic-preflight-" + _hash(model)[:16],
        "benchmark": "synthetic-provider-preflight",
        "level": "L2",
        "model": model,
        "policy": "openrouter",
    }
    binding, catalogue, mask = _binding(job)
    outbound = build_openrouter_outbound(binding)
    key = credential or os.environ.get("OPENROUTER_API_KEY")
    if not key:
        raise RuntimeError("openrouter_credential_unavailable")
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=False)
    writer = ImmutableEvidenceWriter(output, binding.run_id, binding.spec_hash)
    request_sidecar = writer.write_json(
        "evidence/provider-request.json",
        {
            "schema_version": "dof.e0-provider-request.v1",
            "binding": binding.to_evidence_dict(),
            "outbound": outbound.to_dict(),
            "backend_dispatches": 0,
        },
        role="e0_provider_request",
        schema_version="dof.e0-provider-request.v1",
    )
    transport = transport_factory(
        api_key=key,
        expected_outbound_binding=outbound,
    )
    decision = DoFProviderObserver(
        run_ledger=ProviderRunLedger.for_binding(binding)
    ).request(binding, transport)
    errors = verify_provider_attempts(decision.attempts, binding)
    if errors:
        raise RuntimeError("e0_provider_attempt_evidence_invalid:" + ";".join(errors))
    attempt_sidecar = write_provider_attempt_trace(
        writer,
        decision.attempts,
        binding=binding,
    )
    exchange = transport.last_exchange
    exchange_sidecar = writer.write_json(
        "evidence/openrouter-exchange.json",
        exchange
        if exchange is not None
        else {
            "schema_version": "dof.openrouter-exchange.v1",
            "status": "NOT_AVAILABLE",
        },
        role="openrouter_exchange",
        schema_version="dof.openrouter-exchange.v1",
    )
    gate_result: dict[str, Any] = {
        "accepted": False,
        "endpoint": "NOT_RUN",
        "reasons": ["strict parser did not produce a proposal"],
    }
    if decision.accepted and decision.bound_proposal is not None:
        proposal = decision.bound_proposal.verify_for_dispatch(binding)
        gate_result = ActionGate().evaluate(
            proposal,
            level=DoFLevel.L2,
            expected_decision_id=binding.decision_id,
            catalogue=catalogue,
            expected_catalogue_hash=catalogue.sha256,
            current_stage=DesignStage.ROUTED,
            budget_remaining={event_class: 3 for event_class in EventClass},
            mask=mask,
        ).to_dict()
    last_attempt = decision.attempts[-1] if decision.attempts else None
    transport_valid = bool(
        len(decision.attempts) == 1
        and transport.physical_requests == 1
        and transport.send_attempts == 1
        and transport.cleanup_status == "SUCCEEDED"
        and exchange is not None
        and isinstance(exchange.get("route"), dict)
        and isinstance(exchange.get("usage"), dict)
        and exchange["route"].get("requested_model") == model
        and exchange["route"].get("returned_model") == model
        and exchange["route"].get("fallback_allowed") is False
        and exchange["route"].get("attempt") == 1
        and last_attempt is not None
        and last_attempt.cost_usd is not None
        and last_attempt.input_tokens is not None
    )
    result = {
        "schema_version": "dof.e0-provider-preflight-result.v1",
        "study_status": "PILOT_NON_CLAIM",
        "model": model,
        "endpoint": decision.endpoint,
        "reasons": list(decision.reasons),
        "provider_decision_accepted": decision.accepted,
        "gate_result": gate_result,
        "transport_evidence_valid": transport_valid,
        "physical_requests": transport.physical_requests,
        "retries": 0,
        "fallbacks": 0,
        "repairs": 0,
        "backend_dispatches": 0,
        "reported_cost_usd": None if last_attempt is None else last_attempt.cost_usd,
        "input_tokens": None if last_attempt is None else last_attempt.input_tokens,
        "output_tokens": None if last_attempt is None else last_attempt.output_tokens,
        "total_tokens": None if last_attempt is None else last_attempt.total_tokens,
        "latency_seconds": None if last_attempt is None else last_attempt.latency_seconds,
        "evidence": [
            {"role": item.role, "uri": item.uri, "sha256": item.sha256}
            for item in (request_sidecar, attempt_sidecar, exchange_sidecar)
        ],
        "secrets_persisted": False,
    }
    result["result_sha256"] = _hash(result)
    writer.write_json(
        "evidence/provider-preflight-result.json",
        result,
        role="e0_provider_preflight_result",
        schema_version="dof.e0-provider-preflight-result.v1",
    )
    return result


__all__ = [
    "E0_BENCHMARKS",
    "E0_FAKE_RESULT_SCHEMA",
    "E0_MATRIX_SCHEMA",
    "e0_action_variants",
    "e0_jobs",
    "e0_matrix_manifest",
    "run_fake_e0_matrix",
    "run_synthetic_openrouter_preflight",
]
