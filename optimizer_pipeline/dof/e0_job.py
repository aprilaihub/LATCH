"""Minimal observation-bound serial E0 trajectory orchestration.

The module joins existing accepted components without creating a scheduler or
new persistence framework.  Every provider-driven dispatch is preceded by an
immutable causal record, a deterministically materialized plan, and an exact
dispatch authorization verified again at the real executor boundary.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import time
from typing import Any, Callable, Mapping

from .artifacts import fingerprint_file
from .catalogues import clean_macro_catalogue, strict_l3_catalogue
from .contracts import DesignStage, DoFLevel, EventClass, UnknownRegime
from .e0 import E0_BENCHMARKS, e0_jobs, e0_matrix_manifest
from .evidence import ImmutableEvidenceWriter
from .e1 import action_aware_history_row
from .gate import ACTION_PROPOSAL_SCHEMA_VERSION, ActionGate, ProposalKind
from .manifests import capability_manifest, interface_fingerprints
from .masks import create_mask_rules
from .mutation_worker_binding import verified_fake_attestation_evidence_sha256
from .openrouter import (
    OPENROUTER_ADAPTER_VERSION,
    OPENROUTER_E0_MODELS,
    OpenRouterChatCompletionsTransport,
    build_openrouter_outbound,
)
from .preflight import (
    freeze_e0_preflight_plan,
    freeze_e1_observation_preflight_plan,
    run_l0_real_preflight,
    write_e0_preflight_plan,
)
from .projector import DecisionContextProjector
from .provider import (
    DoFProviderObserver,
    ProviderRequestBinding,
    ProviderRunLedger,
    ProviderSettings,
    verify_provider_attempts,
    write_provider_attempt_trace,
)
from .provenance import observe_repository
from .real_mutation import (
    build_e0_dispatch_authorization,
    execute_e0_real_action,
    execute_e0_real_gated_plan,
    freeze_e0_real_action_plan,
    freeze_e0_real_gated_plan,
    validate_e0_dispatch_authorization,
    write_e0_frozen_plan,
    write_e0_gated_plan,
)


E0_JOB_BUNDLE_SCHEMA = "dof.e0-bound-job-bundle.v1"
E0_JOB_SPEC_SCHEMA = "dof.e0-bound-job-spec.v1"
E0_OBSERVATION_SCHEMA = "dof.e0-real-observation.v1"
E0_CAUSAL_EVIDENCE_SCHEMA = "dof.e0-causal-decision-evidence.v1"
E0_TRAJECTORY_RESULT_SCHEMA = "dof.e0-bound-trajectory-result.v1"
E0_MATRIX_RESULT_SCHEMA = "dof.e0-bound-matrix-result.v1"
E0_MASK_VERSION = "dof.e0-known-empty-mask.v1"
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def _hash(value: Any) -> str:
    return hashlib.sha256(
        value if isinstance(value, bytes) else _canonical_bytes(value)
    ).hexdigest()


def _write_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as stream:
        stream.write(json.dumps(value, sort_keys=True, indent=2).encode() + b"\n")


def _file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _semantic_file_hashes(repository: Path) -> dict[str, str]:
    relative = (
        "configs/experiments/e0_openrouter_matrix.json",
        "configs/providers/openrouter_e0.json",
        "optimizer_pipeline/dof/e0_job.py",
        "optimizer_pipeline/dof/artifacts.py",
        "optimizer_pipeline/dof/catalogues.py",
        "optimizer_pipeline/dof/evidence.py",
        "optimizer_pipeline/dof/openrouter.py",
        "optimizer_pipeline/dof/provider.py",
        "optimizer_pipeline/dof/projector.py",
        "optimizer_pipeline/dof/gate.py",
        "optimizer_pipeline/dof/manifests.py",
        "optimizer_pipeline/dof/masks.py",
        "optimizer_pipeline/dof/expansions.py",
        "optimizer_pipeline/dof/mutation_worker_binding.py",
        "optimizer_pipeline/dof/preflight.py",
        "optimizer_pipeline/dof/real_mutation.py",
        "optimizer_pipeline/dof/technical.py",
        "optimizer_pipeline/dof/toolcalls.py",
        "optimizer_pipeline/dof/vivado_mutation_worker.py",
        "optimizer_pipeline/core/domain/__init__.py",
        "optimizer_pipeline/core/domain/enums.py",
        "optimizer_pipeline/core/domain/facts.py",
        "optimizer_pipeline/core/domain/value.py",
        "optimizer_pipeline/core/store/__init__.py",
        "optimizer_pipeline/core/store/contracts.py",
        "optimizer_pipeline/core/store/schema.py",
        "optimizer_pipeline/core/store/sqlite.py",
    )
    return {item: _file_hash(repository / item) for item in relative}


def freeze_e0_job_bundle(
    roots: Mapping[str, str | Path],
    *,
    repository_anchor: str | Path,
    fake_startup_attestation: str | Path,
) -> dict[str, Any]:
    repository_root = Path(repository_anchor).resolve()
    repository = observe_repository(repository_root)
    if repository.tracked_tree_dirty:
        raise RuntimeError("tracked_product_tree_not_clean")
    if set(roots) != set(E0_BENCHMARKS):
        raise ValueError("e0_root_set_mismatch")
    attestation_hash = verified_fake_attestation_evidence_sha256(
        fake_startup_attestation
    )
    matrix = e0_matrix_manifest()
    root_rows = {}
    for benchmark in E0_BENCHMARKS:
        fingerprint = fingerprint_file(Path(roots[benchmark]).resolve())
        root_rows[benchmark] = {
            "pseudonym": benchmark,
            "sha256": fingerprint.sha256,
            "size_bytes": fingerprint.size_bytes,
        }
    protocol = {
        "matrix_sha256": matrix["matrix_sha256"],
        "repository": {
            "head_commit": repository.head_commit,
            "head_tree": repository.head_tree,
            "binary_diff_sha256": repository.binary_diff_sha256,
            "branch_name": repository.branch_name,
        },
        "semantic_file_hashes": _semantic_file_hashes(repository_root),
        "mask_version": E0_MASK_VERSION,
        "provider_models": list(OPENROUTER_E0_MODELS),
        "provider_request_cap": 24,
        "provider_total_cost_cap_usd": 5.0,
        "heavy_call_cap_per_nonbaseline_trajectory": 3,
        "selection_policy": "best_valid_wns_root_fallback.pilot.v1",
        "fake_startup_attestation_evidence_sha256": attestation_hash,
        "tool_versions": {
            "python": platform.python_version(),
            "vivado": "2025.1",
        },
    }
    protocol_sha = _hash(protocol)
    specs = []
    for job in matrix["jobs"]:
        spec = {
            "schema_version": E0_JOB_SPEC_SCHEMA,
            "study_status": "PILOT_NON_CLAIM",
            "job": job,
            "root": root_rows[job["benchmark"]],
            "protocol_sha256": protocol_sha,
            "budgets": {
                "heavy_calls": 0 if job["level"] == "L0" else 3,
                "model_requests": 3 if job["level"] in {"L2", "L3"} else 0,
                "provider_cost_usd": 5.0,
            },
        }
        specs.append(spec | {"job_spec_sha256": _hash(spec)})
    payload = {
        "schema_version": E0_JOB_BUNDLE_SCHEMA,
        "study_status": "PILOT_NON_CLAIM",
        "matrix": matrix,
        "roots": root_rows,
        "protocol": protocol,
        "protocol_sha256": protocol_sha,
        "jobs": specs,
    }
    return payload | {"bundle_sha256": _hash(payload)}


def validate_e0_job_bundle(bundle: Mapping[str, Any]) -> dict[str, Any]:
    value = json.loads(_canonical_bytes(dict(bundle)))
    claimed = value.pop("bundle_sha256", None)
    if value.get("schema_version") != E0_JOB_BUNDLE_SCHEMA or claimed != _hash(value):
        raise ValueError("e0_job_bundle_invalid")
    if value.get("matrix") != e0_matrix_manifest() or len(value.get("jobs", [])) != 12:
        raise ValueError("e0_job_matrix_mismatch")
    if value.get("protocol_sha256") != _hash(value.get("protocol")):
        raise ValueError("e0_job_protocol_hash_mismatch")
    expected_ids = {job["job_id"] for job in e0_jobs()}
    observed_ids = set()
    for spec in value["jobs"]:
        if spec.get("schema_version") != E0_JOB_SPEC_SCHEMA:
            raise ValueError("e0_job_spec_schema_mismatch")
        claimed_spec = spec.get("job_spec_sha256")
        core = {key: item for key, item in spec.items() if key != "job_spec_sha256"}
        if claimed_spec != _hash(core):
            raise ValueError("e0_job_spec_hash_mismatch")
        if spec.get("protocol_sha256") != value["protocol_sha256"]:
            raise ValueError("e0_job_protocol_binding_mismatch")
        observed_ids.add(spec["job"]["job_id"])
    if observed_ids != expected_ids:
        raise ValueError("e0_job_ids_mismatch")
    return value | {"bundle_sha256": claimed}


def _source_from_validation(
    job_spec: Mapping[str, Any],
    validation: Mapping[str, Any],
    *,
    decision_index: int,
    history: list[dict[str, Any]],
    heavy_remaining: int,
    model_requests_remaining: int,
    frozen_timing_score: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    timing = dict(validation.get("timing", {}))
    route = dict(validation.get("route_status", {}))
    resources = dict(validation.get("utilization", {}).get("resources", {}))
    measurements: dict[str, Any] = {
        "timing": {
            key: timing.get(key)
            for key in ("wns_ns", "tns_ns", "failing_endpoints")
            if key in timing
        },
        "route": {
            "status": (
                "fully routed" if route.get("is_fully_routed") is True else "not fully routed"
            ),
            "unrouted_nets": route.get("unrouted_nets"),
        },
        "utilization": {
            "lut": resources.get("CLB LUTs"),
            "ff": resources.get("CLB Registers"),
            "dsp": resources.get("DSPs"),
            "bram": resources.get("Block RAM Tile"),
            "uram": resources.get("URAM"),
        },
    }
    if frozen_timing_score is not None:
        observed_wns = measurements["timing"].get("wns_ns")
        frozen_wns = frozen_timing_score.get("wns_ns")
        if (
            not isinstance(observed_wns, (int, float))
            or not isinstance(frozen_wns, (int, float))
            or abs(float(observed_wns) - float(frozen_wns)) > 0.001
        ):
            raise RuntimeError("frozen_timing_score_observation_mismatch")
        measurements["timing"].update(
            {
                "clock_period_ns": frozen_timing_score.get("target_period_ns"),
                "estimated_fmax_mhz": frozen_timing_score.get("fmax_mhz"),
            }
        )
    return {
        "condition": {"pseudonym": "condition-" + job_spec["job"]["benchmark"]},
        "state": {
            "pseudonym": f"state-{job_spec['job']['job_id']}-{decision_index}",
            "lineage_pseudonym": "lineage-" + job_spec["job"]["job_id"],
            "stage": "ROUTED",
            "measurements": measurements,
        },
        "history": list(history),
        "budget_remaining": {
            "IMPLEMENTATION_HEAVY": heavy_remaining,
            "MUTATION_LIGHT": 3,
            "READ_ONLY": 20,
            "LLM_REQUEST": model_requests_remaining,
            "WALL_TIME_SECONDS": 1200.0,
        },
        "validation": {
            "status": "CLEAN" if validation.get("valid") is True else "INVALID",
            "errors": list(validation.get("missing_reasons", [])),
            "warnings": [],
        },
        "stop_conditions": [
            "implementation-heavy budget exhausted",
            "model-request budget exhausted",
        ],
        "known_priors": {},
    }


def capture_real_observation(
    job_spec: Mapping[str, Any],
    current_dcp: str | Path,
    observation_dir: str | Path,
    *,
    decision_index: int,
    history: list[dict[str, Any]],
    heavy_remaining: int,
    model_requests_remaining: int,
    vivado_exec: str | Path,
    python_exec: str | Path,
    repository_anchor: str | Path,
    frozen_timing_score: Mapping[str, Any] | None = None,
    e1_protocol: bool = False,
) -> dict[str, Any]:
    output = Path(observation_dir).resolve()
    if output.exists():
        raise FileExistsError(output)
    output.mkdir(parents=True)
    current = fingerprint_file(Path(current_dcp).resolve())
    plan_factory = (
        freeze_e1_observation_preflight_plan
        if e1_protocol
        else freeze_e0_preflight_plan
    )
    plan = plan_factory(
        current_dcp,
        dcp_pseudonym=f"{job_spec['job']['benchmark']}-d{decision_index}",
        run_id=f"obs-{_hash((job_spec['job_spec_sha256'], decision_index, current.sha256))[:24]}",
        repository_anchor=repository_anchor,
    )
    plan_path = output / "observation-plan.json"
    write_e0_preflight_plan(plan, plan_path)
    run_dir = output / "read-only-run"
    summary = run_l0_real_preflight(
        plan_path,
        dcp_path=current_dcp,
        run_dir=run_dir,
        vivado_exec=vivado_exec,
        python_exec=python_exec,
        repository_anchor=repository_anchor,
    )
    validation_path = run_dir / "evidence" / "validation_summary.json"
    validation = json.loads(validation_path.read_text())
    if summary.get("endpoint") != "VALID_COMPLETION" or validation.get("valid") is not True:
        raise RuntimeError("real_observation_not_valid")
    source = _source_from_validation(
        job_spec,
        validation,
        decision_index=decision_index,
        history=history,
        heavy_remaining=heavy_remaining,
        model_requests_remaining=model_requests_remaining,
        frozen_timing_score=frozen_timing_score,
    )
    payload = {
        "schema_version": E0_OBSERVATION_SCHEMA,
        "origin": "real_read_only_vivado",
        "job_spec_sha256": job_spec["job_spec_sha256"],
        "decision_index": decision_index,
        "current_artifact": {
            "sha256": current.sha256,
            "size_bytes": current.size_bytes,
        },
        "preflight_spec_hash": plan.spec_hash,
        "preflight_result_sha256": _hash(summary),
        "validation_sha256": _file_hash(validation_path),
        "source": source,
    }
    result = payload | {"observation_sha256": _hash(payload)}
    _write_json(output / "real-observation.json", result)
    return result


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


def _provider_binding(
    job_spec: Mapping[str, Any],
    observation: Mapping[str, Any],
    *,
    decision_index: int,
    remaining_cost_usd: float,
) -> tuple[ProviderRequestBinding, Any, Any]:
    if observation.get("origin") != "real_read_only_vivado":
        raise ValueError("synthetic_observation_for_real_decision_rejected")
    if observation.get("job_spec_sha256") != job_spec.get("job_spec_sha256"):
        raise ValueError("observation_job_binding_mismatch")
    if observation.get("observation_sha256") != _hash(
        {key: item for key, item in observation.items() if key != "observation_sha256"}
    ):
        raise ValueError("observation_hash_mismatch")
    level = DoFLevel(job_spec["job"]["level"])
    catalogue = clean_macro_catalogue(DoFLevel.L2) if level is DoFLevel.L2 else strict_l3_catalogue()
    mask = create_mask_rules(version=E0_MASK_VERSION)
    projected = DecisionContextProjector().project(
        level=level,
        regime=UnknownRegime.KNOWN,
        source=observation["source"],
        catalogue=catalogue,
        mask=mask,
    )
    decision_id = "decision-" + _hash(
        (job_spec["job_spec_sha256"], observation["observation_sha256"], decision_index)
    )[:20]
    prompt = _prompt(decision_id, catalogue, level)
    interfaces = interface_fingerprints(level)
    binding = ProviderRequestBinding(
        run_id="e0-" + _hash((job_spec["job_spec_sha256"], decision_index))[:24],
        spec_hash=job_spec["job_spec_sha256"],
        experiment_id="e0-openrouter-pilot",
        condition_id=job_spec["job"]["job_id"],
        repeat_id="repeat-0",
        decision_id=decision_id,
        context_bytes=projected.serialized.encode("utf-8"),
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
        model_id=job_spec["job"]["model"],
        adapter_version=OPENROUTER_ADAPTER_VERSION,
        settings=ProviderSettings(
            temperature=None,
            max_tokens=4096,
            seed=None,
            timeout_seconds=120.0,
            max_retries=0,
            retry_on=(),
            max_requests=1,
            max_usd=remaining_cost_usd,
            max_cost_per_request_usd=remaining_cost_usd,
            unknown_cost_policy="reject",
            unknown_usage_policy="reject",
        ),
    )
    return binding, catalogue, mask


@dataclass(frozen=True)
class BoundDecision:
    binding: ProviderRequestBinding
    decision: Any
    gate: Any
    result: Mapping[str, Any]


def run_bound_provider_decision(
    job_spec: Mapping[str, Any],
    observation: Mapping[str, Any],
    decision_dir: str | Path,
    *,
    decision_index: int,
    heavy_remaining: int,
    remaining_cost_usd: float,
    credential: str | None = None,
    transport_factory: Any = OpenRouterChatCompletionsTransport,
) -> BoundDecision:
    binding, catalogue, mask = _provider_binding(
        job_spec,
        observation,
        decision_index=decision_index,
        remaining_cost_usd=remaining_cost_usd,
    )
    output = Path(decision_dir).resolve()
    output.mkdir(parents=True, exist_ok=True)
    writer = ImmutableEvidenceWriter(output, binding.run_id, binding.spec_hash)
    outbound = build_openrouter_outbound(binding)
    writer.write_json(
        "evidence/real-observation-binding.json",
        observation,
        role="e0_real_observation",
        schema_version=E0_OBSERVATION_SCHEMA,
    )
    writer.write_json(
        "evidence/provider-request.json",
        {
            "schema_version": "dof.e0-bound-provider-request.v1",
            "binding": binding.to_evidence_dict(),
            "outbound": outbound.to_dict(),
        },
        role="e0_provider_request",
        schema_version="dof.e0-bound-provider-request.v1",
    )
    key = credential or os.environ.get("OPENROUTER_API_KEY")
    if not key:
        raise RuntimeError("openrouter_credential_unavailable")
    transport = transport_factory(api_key=key, expected_outbound_binding=outbound)
    decision = DoFProviderObserver(
        run_ledger=ProviderRunLedger.for_binding(binding)
    ).request(binding, transport)
    errors = verify_provider_attempts(decision.attempts, binding)
    if errors:
        raise RuntimeError("provider_attempt_evidence_invalid:" + ";".join(errors))
    exchange = transport.last_exchange
    route = None if exchange is None else exchange.get("route")
    if (
        transport.physical_requests != 1
        or transport.send_attempts != 1
        or transport.cleanup_status != "SUCCEEDED"
        or not isinstance(route, Mapping)
        or route.get("requested_model") != binding.model_id
        or route.get("returned_model") != binding.model_id
        or route.get("fallback_allowed") is not False
        or route.get("attempt") != 1
    ):
        raise RuntimeError("openrouter_transport_evidence_invalid")
    write_provider_attempt_trace(writer, decision.attempts, binding=binding)
    writer.write_json(
        "evidence/openrouter-exchange.json",
        exchange if exchange is not None else {"status": "NOT_AVAILABLE"},
        role="openrouter_exchange",
        schema_version="dof.openrouter-exchange.v1",
    )
    gate = None
    if decision.accepted and decision.bound_proposal is not None:
        proposal = decision.bound_proposal.verify_for_dispatch(binding)
        gate = ActionGate().evaluate(
            proposal,
            level=DoFLevel(job_spec["job"]["level"]),
            expected_decision_id=binding.decision_id,
            catalogue=catalogue,
            expected_catalogue_hash=catalogue.sha256,
            current_stage=DesignStage.ROUTED,
            budget_remaining={
                event: (heavy_remaining if event is EventClass.IMPLEMENTATION_HEAVY else 3)
                for event in EventClass
            },
            mask=mask,
        )
    last = decision.attempts[-1] if decision.attempts else None
    result = {
        "schema_version": "dof.e0-bound-provider-decision.v1",
        "decision_id": binding.decision_id,
        "request_contract_hash": binding.request_contract_hash,
        "context_sha256": binding.context_hash,
        "prompt_sha256": binding.prompt_hash,
        "endpoint": decision.endpoint,
        "reasons": list(decision.reasons),
        "provider_accepted": decision.accepted,
        "gate": None if gate is None else gate.to_dict(),
        "raw_response_sha256": None if last is None else last.raw_response_hash,
        "cost_usd": None if last is None else last.cost_usd,
        "input_tokens": None if last is None else last.input_tokens,
        "output_tokens": None if last is None else last.output_tokens,
        "total_tokens": None if last is None else last.total_tokens,
        "route": None if exchange is None else exchange.get("route"),
        "physical_requests": transport.physical_requests,
        "retries": 0,
        "fallbacks": 0,
        "repairs": 0,
    }
    result = result | {"decision_result_sha256": _hash(result)}
    writer.write_json(
        "evidence/provider-decision.json",
        result,
        role="e0_bound_provider_decision",
        schema_version="dof.e0-bound-provider-decision.v1",
    )
    return BoundDecision(binding, decision, gate, result)


def causal_evidence_for_dispatch(
    job_spec: Mapping[str, Any],
    observation: Mapping[str, Any],
    bound: BoundDecision,
) -> dict[str, Any]:
    if bound.gate is None or not bound.gate.accepted or not bound.gate.gated_steps:
        raise ValueError("accepted_gated_steps_required")
    proposal = bound.decision.bound_proposal
    if proposal is None:
        raise ValueError("bound_provider_proposal_required")
    payload = {
        "schema_version": E0_CAUSAL_EVIDENCE_SCHEMA,
        "policy_source": "openrouter_real_observation",
        "job_spec_sha256": job_spec["job_spec_sha256"],
        "current_artifact": observation["current_artifact"],
        "observation_sha256": observation["observation_sha256"],
        "context_sha256": bound.binding.context_hash,
        "decision_id": bound.binding.decision_id,
        "request_contract_hash": bound.binding.request_contract_hash,
        "raw_response_sha256": proposal.raw_response_hash,
        "proposal_sha256": proposal.parsed_proposal_hash,
        "gate_decision_sha256": _hash(bound.gate.to_dict()),
    }
    return payload | {"causal_evidence_sha256": _hash(payload)}


def _gated_step_rows(gate: Any) -> list[dict[str, Any]]:
    return [
        {
            "step_index": step.step_index,
            "step_id": step.step_id,
            "action_key": step.action_key,
            "parameters": dict(step.parameters),
            "stage_before": step.stage_before.value,
            "stage_after": step.stage_after.value,
        }
        for step in gate.gated_steps
    ]


def _technical_metrics(action_dir: Path, phase: str = "child") -> dict[str, Any]:
    path = action_dir / "evidence" / "technical-reports.json"
    if not path.exists():
        return {}
    return json.loads(path.read_text()).get("phases", {}).get(phase, {}).get("parsed", {})


def _deterministic_l1_causal(
    job_spec: Mapping[str, Any], observation: Mapping[str, Any]
) -> dict[str, Any]:
    payload = {
        "schema_version": E0_CAUSAL_EVIDENCE_SCHEMA,
        "policy_source": "deterministic_l1_pilot",
        "job_spec_sha256": job_spec["job_spec_sha256"],
        "current_artifact": observation["current_artifact"],
        "observation_sha256": observation["observation_sha256"],
        "context_sha256": None,
        "decision_id": "decision-" + job_spec["job_spec_sha256"][:20],
        "request_contract_hash": None,
        "raw_response_sha256": None,
        "proposal_sha256": _hash({"action_key": "macro.phys_opt", "parameters": {}}),
        "gate_decision_sha256": _hash({"accepted": True, "policy": "deterministic_l1_pilot"}),
    }
    return payload | {"causal_evidence_sha256": _hash(payload)}


def execute_real_e0_job(
    job_spec: Mapping[str, Any],
    root_dcp: str | Path,
    output_dir: str | Path,
    *,
    vivado_exec: str | Path,
    python_exec: str | Path,
    repository_anchor: str | Path,
    fake_startup_attestation_sha256: str,
    remaining_program_cost_usd: float,
    credential: str | None = None,
) -> dict[str, Any]:
    start = time.monotonic()
    output = Path(output_dir).resolve()
    output.mkdir(parents=True, exist_ok=False)
    level = DoFLevel(job_spec["job"]["level"])
    expected_root = job_spec["root"]
    observed_root = fingerprint_file(Path(root_dcp).resolve())
    if (observed_root.sha256, observed_root.size_bytes) != (
        expected_root["sha256"], expected_root["size_bytes"]
    ):
        raise RuntimeError("ROOT_IDENTITY_BLOCKER")
    current_dcp = Path(root_dcp).resolve()
    best = {
        "artifact": dict(expected_root),
        "wns_ns": None,
        "source": "root",
    }
    history: list[dict[str, Any]] = []
    decisions: list[dict[str, Any]] = []
    heavy_used = 0
    provider_cost = 0.0
    model_requests = 0
    endpoint = "VALID_COMPLETION"
    reasons: list[str] = []
    max_decisions = 1 if level in {DoFLevel.L0, DoFLevel.L1} else 3
    for decision_index in range(max_decisions):
        decision_root = output / f"decision-{decision_index:02d}"
        observation = capture_real_observation(
            job_spec,
            current_dcp,
            decision_root / "observation",
            decision_index=decision_index,
            history=history,
            heavy_remaining=max(0, 3 - heavy_used),
            model_requests_remaining=max(0, 3 - model_requests),
            vivado_exec=vivado_exec,
            python_exec=python_exec,
            repository_anchor=repository_anchor,
        )
        timing = observation["source"]["state"]["measurements"]["timing"]
        if best["wns_ns"] is None and isinstance(timing.get("wns_ns"), (int, float)):
            best["wns_ns"] = timing["wns_ns"]
        if level is DoFLevel.L0:
            decisions.append({"observation_sha256": observation["observation_sha256"], "dispatches": 0})
            break
        if level is DoFLevel.L1:
            causal = _deterministic_l1_causal(job_spec, observation)
            plan_dir = decision_root / "dispatch"
            plan_path = plan_dir / "materialized-plan.json"
            run_dir = decision_root / "action-run"
            plan = freeze_e0_real_action_plan(
                current_dcp,
                root_pseudonym=job_spec["job"]["benchmark"],
                level=level,
                action_key="macro.phys_opt",
                parameters={},
                run_id="action-" + _hash((job_spec["job_spec_sha256"], decision_index))[:24],
                repository_anchor=repository_anchor,
                python_exec=python_exec,
                vivado_exec=vivado_exec,
                run_dir=run_dir,
                fake_startup_attestation_sha256=fake_startup_attestation_sha256,
            )
            write_e0_frozen_plan(plan, plan_path)
            authorization = build_e0_dispatch_authorization(plan, causal)
            _write_json(plan_dir / "causal-evidence.json", causal)
            _write_json(plan_dir / "dispatch-authorization.json", authorization)
            for step_auth in authorization["step_authorizations"]:
                _write_json(
                    plan_dir
                    / "step-authorizations"
                    / f"{step_auth['step']['step_index']:03d}.json",
                    step_auth,
                )
            result = execute_e0_real_action(
                plan,
                current_dcp,
                run_dir,
                dispatch_authorization=authorization,
                causal_evidence=causal,
                vivado_exec=vivado_exec,
                python_exec=python_exec,
                repository_anchor=repository_anchor,
            )
            heavy_used += int(result.get("real_mutation_invocations", 0))
            decisions.append({"causal_evidence_sha256": causal["causal_evidence_sha256"], "authorization_sha256": authorization["authorization_sha256"], "action_result": result})
            if not result.get("endpoint_success"):
                endpoint = result.get("endpoint", "TOOL_EXECUTION_FAILURE")
                reasons.extend(result.get("reasons", []))
            else:
                metrics = _technical_metrics(run_dir)
                child_wns = metrics.get("timing", {}).get("wns_ns")
                transaction = result.get("transaction") or {}
                if transaction.get("valid") and isinstance(child_wns, (int, float)) and (best["wns_ns"] is None or child_wns > best["wns_ns"]):
                    best = {"artifact": {"sha256": transaction["sha256"], "size_bytes": transaction["size_bytes"]}, "wns_ns": child_wns, "source": "action-child"}
            break
        bound = run_bound_provider_decision(
            job_spec,
            observation,
            decision_root / "provider",
            decision_index=decision_index,
            heavy_remaining=max(0, 3 - heavy_used),
            remaining_cost_usd=max(0.0, remaining_program_cost_usd - provider_cost),
            credential=credential,
        )
        model_requests += int(bound.result["physical_requests"])
        provider_cost += float(bound.result.get("cost_usd") or 0.0)
        decision_row: dict[str, Any] = {"provider": dict(bound.result), "dispatches": 0}
        decisions.append(decision_row)
        if not bound.decision.accepted:
            endpoint = bound.decision.endpoint
            reasons.extend(bound.decision.reasons)
            break
        proposal = bound.decision.bound_proposal.proposal
        if proposal.kind is ProposalKind.STOP:
            endpoint = "POLICY_STOP"
            reasons.append("provider_returned_stop")
            break
        if bound.gate is None or not bound.gate.accepted or not bound.gate.gated_steps:
            endpoint = "CAPABILITY_ACTION_REJECTION"
            reasons.extend(() if bound.gate is None else bound.gate.reasons)
            break
        causal = causal_evidence_for_dispatch(job_spec, observation, bound)
        plan_dir = decision_root / "dispatch"
        run_dir = decision_root / "action-run"
        plan = freeze_e0_real_gated_plan(
            current_dcp,
            root_pseudonym=job_spec["job"]["benchmark"],
            level=level,
            decision_id=bound.binding.decision_id,
            proposal_id=proposal.proposal_id,
            proposal_hash=proposal.proposal_hash,
            gate_decision_sha256=_hash(bound.gate.to_dict()),
            gated_steps=_gated_step_rows(bound.gate),
            causal_evidence_sha256=causal["causal_evidence_sha256"],
            run_id="action-" + _hash((job_spec["job_spec_sha256"], decision_index, proposal.proposal_hash))[:24],
            repository_anchor=repository_anchor,
            python_exec=python_exec,
            vivado_exec=vivado_exec,
            run_dir=run_dir,
            fake_startup_attestation_sha256=fake_startup_attestation_sha256,
        )
        write_e0_gated_plan(plan, plan_dir / "materialized-plan.json")
        authorization = build_e0_dispatch_authorization(plan, causal)
        _write_json(plan_dir / "causal-evidence.json", causal)
        _write_json(plan_dir / "dispatch-authorization.json", authorization)
        for step_auth in authorization["step_authorizations"]:
            _write_json(
                plan_dir
                / "step-authorizations"
                / f"{step_auth['step']['step_index']:03d}.json",
                step_auth,
            )
        validate_e0_dispatch_authorization(plan, authorization, causal)
        result = execute_e0_real_gated_plan(
            plan,
            current_dcp,
            run_dir,
            dispatch_authorization=authorization,
            causal_evidence=causal,
            vivado_exec=vivado_exec,
            python_exec=python_exec,
            repository_anchor=repository_anchor,
        )
        decision_row.update({"dispatches": int(bool(result.get("backend_dispatches"))), "causal_evidence_sha256": causal["causal_evidence_sha256"], "authorization_sha256": authorization["authorization_sha256"], "action_result": result})
        heavy = int(result.get("real_mutation_invocations", 0))
        heavy_used += heavy
        transaction = result.get("transaction") or {}
        valid_child = bool(result.get("endpoint_success") and transaction.get("valid"))
        metrics = _technical_metrics(run_dir)
        child_wns = metrics.get("timing", {}).get("wns_ns")
        history.append(
            action_aware_history_row(
                decision_index=decision_index,
                actions=(
                    {
                        "action_key": step.action_key,
                        "parameters": dict(step.parameters),
                    }
                    for step in bound.gate.gated_steps
                ),
                parent_qor={"fmax_mhz": None, "wns_ns": best["wns_ns"]},
                child_qor={"fmax_mhz": None, "wns_ns": child_wns},
                valid=valid_child,
                endpoint=result.get("endpoint", "UNKNOWN"),
                actual_heavy_calls=heavy,
            )
        )
        if not valid_child:
            endpoint = result.get("endpoint", "TOOL_EXECUTION_FAILURE")
            reasons.extend(result.get("reasons", []))
            break
        child_path = run_dir / "artifacts" / "sha256" / f"{transaction['sha256']}.dcp"
        if not child_path.is_file():
            raise RuntimeError("validated_child_artifact_missing")
        current_dcp = child_path
        if isinstance(child_wns, (int, float)) and (best["wns_ns"] is None or child_wns > best["wns_ns"]):
            best = {"artifact": {"sha256": transaction["sha256"], "size_bytes": transaction["size_bytes"]}, "wns_ns": child_wns, "source": f"decision-{decision_index:02d}"}
        if heavy_used >= 3:
            reasons.append("heavy_call_budget_exhausted")
            break
    result = {
        "schema_version": E0_TRAJECTORY_RESULT_SCHEMA,
        "study_status": "PILOT_NON_CLAIM",
        "job": job_spec["job"],
        "job_spec_sha256": job_spec["job_spec_sha256"],
        "endpoint": endpoint,
        "reasons": reasons,
        "decisions": decisions,
        "model_requests": model_requests,
        "provider_cost_usd": provider_cost,
        "heavy_calls": heavy_used,
        "best_valid_selection": best,
        "wall_seconds": time.monotonic() - start,
    }
    result = result | {"result_sha256": _hash(result)}
    _write_json(output / "trajectory-result.json", result)
    return result


def execute_real_e0_matrix(
    bundle: Mapping[str, Any],
    roots: Mapping[str, str | Path],
    output_dir: str | Path,
    *,
    vivado_exec: str | Path,
    python_exec: str | Path,
    repository_anchor: str | Path,
    initial_program_cost_usd: float = 0.0057207,
    credential: str | None = None,
) -> dict[str, Any]:
    validated = validate_e0_job_bundle(bundle)
    repository = observe_repository(repository_anchor)
    frozen_repository = validated["protocol"]["repository"]
    if repository.tracked_tree_dirty or any(
        getattr(repository, key) != frozen_repository[key]
        for key in ("head_commit", "head_tree", "binary_diff_sha256")
    ):
        raise RuntimeError("frozen_repository_identity_mismatch")
    output = Path(output_dir).resolve()
    output.mkdir(parents=True, exist_ok=False)
    rows = []
    cost = float(initial_program_cost_usd)
    physical_requests = 0
    for spec in validated["jobs"]:
        if cost >= 5.0:
            raise RuntimeError("OPENROUTER_PROGRAM_COST_CAP_EXHAUSTED")
        row = execute_real_e0_job(
            spec,
            roots[spec["job"]["benchmark"]],
            output / spec["job"]["job_id"],
            vivado_exec=vivado_exec,
            python_exec=python_exec,
            repository_anchor=repository_anchor,
            fake_startup_attestation_sha256=validated["protocol"]["fake_startup_attestation_evidence_sha256"],
            remaining_program_cost_usd=5.0 - cost,
            credential=credential,
        )
        rows.append(row)
        cost += float(row["provider_cost_usd"])
        physical_requests += int(row["model_requests"])
        if physical_requests > 24 or cost > 5.0:
            raise RuntimeError("E0_AGGREGATE_PROVIDER_CAP_EXCEEDED")
    result = {
        "schema_version": E0_MATRIX_RESULT_SCHEMA,
        "study_status": "PILOT_NON_CLAIM",
        "bundle_sha256": validated["bundle_sha256"],
        "jobs_planned": 12,
        "jobs_completed": len(rows),
        "fresh_provider_requests": physical_requests,
        "combined_wp005_wp005a_cost_usd": cost,
        "rows": rows,
    }
    result = result | {"result_sha256": _hash(result)}
    _write_json(output / "e0-matrix-result.json", result)
    return result


__all__ = [
    "BoundDecision",
    "E0_CAUSAL_EVIDENCE_SCHEMA",
    "E0_JOB_BUNDLE_SCHEMA",
    "E0_JOB_SPEC_SCHEMA",
    "E0_MATRIX_RESULT_SCHEMA",
    "E0_OBSERVATION_SCHEMA",
    "E0_TRAJECTORY_RESULT_SCHEMA",
    "capture_real_observation",
    "causal_evidence_for_dispatch",
    "execute_real_e0_job",
    "execute_real_e0_matrix",
    "freeze_e0_job_bundle",
    "run_bound_provider_decision",
    "validate_e0_job_bundle",
]
