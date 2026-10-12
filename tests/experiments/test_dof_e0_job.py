from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import sys

import pytest

from optimizer_pipeline.dof import e0_job, real_mutation
from optimizer_pipeline.dof.artifacts import RootFingerprint
from optimizer_pipeline.dof.contracts import DoFLevel
from optimizer_pipeline.dof.e0 import e0_jobs
from optimizer_pipeline.dof.e0_job import (
    causal_evidence_for_dispatch,
    run_bound_provider_decision,
)
from optimizer_pipeline.dof.provider import (
    PROVIDER_TRANSPORT_CONTRACT_VERSION,
    RawProviderResponse,
)
from optimizer_pipeline.dof.real_mutation import (
    EXPECTED_ROOT_SHA256,
    EXPECTED_ROOT_SIZE,
    RealMutationPlanError,
    build_e0_dispatch_authorization,
    freeze_e0_real_action_plan,
    freeze_e0_real_gated_plan,
    validate_e0_dispatch_authorization,
)


def _hash(value) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _job_spec(job: dict) -> dict:
    core = {
        "schema_version": e0_job.E0_JOB_SPEC_SCHEMA,
        "study_status": "PILOT_NON_CLAIM",
        "job": job,
        "root": {
            "pseudonym": job["benchmark"],
            "sha256": EXPECTED_ROOT_SHA256,
            "size_bytes": EXPECTED_ROOT_SIZE,
        },
        "protocol_sha256": "a" * 64,
        "budgets": {
            "heavy_calls": 0 if job["level"] == "L0" else 3,
            "model_requests": 3 if job["level"] in {"L2", "L3"} else 0,
            "provider_cost_usd": 5.0,
        },
    }
    return core | {"job_spec_sha256": _hash(core)}


def _observation(spec: dict, index: int = 0, *, origin: str = "real_read_only_vivado") -> dict:
    payload = {
        "schema_version": e0_job.E0_OBSERVATION_SCHEMA,
        "origin": origin,
        "job_spec_sha256": spec["job_spec_sha256"],
        "decision_index": index,
        "current_artifact": {
            "sha256": EXPECTED_ROOT_SHA256,
            "size_bytes": EXPECTED_ROOT_SIZE,
        },
        "preflight_spec_hash": "b" * 64,
        "preflight_result_sha256": "c" * 64,
        "validation_sha256": "d" * 64,
        "source": {
            "condition": {"pseudonym": "condition-" + spec["job"]["benchmark"]},
            "state": {
                "pseudonym": f"state-{spec['job']['job_id']}-{index}",
                "lineage_pseudonym": "lineage-" + spec["job"]["job_id"],
                "stage": "ROUTED",
                "measurements": {
                    "timing": {"wns_ns": -1.0, "tns_ns": -10.0, "failing_endpoints": 4},
                    "route": {"status": "fully routed", "unrouted_nets": 0},
                    "utilization": {"lut": 100, "ff": 80, "dsp": 1, "bram": 2, "uram": 0},
                },
            },
            "history": [],
            "budget_remaining": {"IMPLEMENTATION_HEAVY": 3, "MUTATION_LIGHT": 3, "READ_ONLY": 20, "LLM_REQUEST": 3, "WALL_TIME_SECONDS": 1200.0},
            "validation": {"status": "CLEAN", "errors": [], "warnings": []},
            "stop_conditions": ["budget exhausted"],
            "known_priors": {},
        },
    }
    return payload | {"observation_sha256": _hash(payload)}


class _Transport:
    transport_contract_version = PROVIDER_TRANSPORT_CONTRACT_VERSION
    internal_retries_disabled = True
    mode = "valid"

    def __init__(self, **kwargs):
        self.expected = kwargs["expected_outbound_binding"]
        self.send_attempts = 0
        self.physical_requests = 0
        self.cleanup_status = "SUCCEEDED"
        self.last_exchange = None

    async def send(self, binding, retry_index):
        self.send_attempts += 1
        self.physical_requests += 1
        self.last_exchange = {
            "schema_version": "dof.openrouter-exchange.v1",
            "route": {"requested_model": binding.model_id, "returned_model": binding.model_id, "selected_upstream_provider": "fake", "fallback_allowed": False, "attempt": 1},
            "usage": {"prompt_tokens": 20, "completion_tokens": 10, "total_tokens": 30, "cost": 0.01},
        }
        if type(self).mode == "invalid":
            raw = b"```json\n{}\n```"
        else:
            is_l3 = "strict" in binding.catalogue_version
            if type(self).mode == "stop":
                kind = "STOP"
                steps = []
            elif is_l3:
                kind = "PLAN"
                steps = [
                    {"step_id": "s0", "action_key": "manual.unroute_all", "parameters": {}},
                    {"step_id": "s1", "action_key": "manual.unplace_all", "parameters": {}},
                    {"step_id": "s2", "action_key": "manual.place_design", "parameters": {"directive": "Explore"}},
                    {"step_id": "s3", "action_key": "manual.route_design", "parameters": {"directive": "Default"}},
                    {"step_id": "s4", "action_key": "manual.phys_opt_design", "parameters": {"directive": "AddRetime"}},
                ]
            else:
                kind = "ACTION"
                steps = [{"step_id": "s0", "action_key": "macro.phys_opt", "parameters": {}}]
            raw = json.dumps({"schema_version": "action_proposal.v1", "proposal_id": "proposal-test", "decision_id": binding.decision_id, "catalogue_version": binding.catalogue_version, "catalogue_hash": binding.catalogue_hash, "kind": kind, "steps": steps}, separators=(",", ":")).encode()
        return RawProviderResponse(raw, "openrouter", provider_request_id="fake-request", returned_model_id=binding.model_id, returned_model_revision="fake", input_tokens=20, output_tokens=10, total_tokens=30, cost_usd=0.01, cost_source="openrouter_usage.cost", observed_request_contract_hash=binding.request_contract_hash)


@dataclass(frozen=True)
class _Repository:
    head_commit: str = "1" * 40
    head_tree: str = "2" * 40
    binary_diff_sha256: str = "3" * 64
    tracked_tree_dirty: bool = False
    branch_name: str = "phase0-policy-evidence-replan"


def _write_executable(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\nexit 0\n")
    path.chmod(0o700)
    return path


def _gated_rows(gate) -> list[dict]:
    return [
        {"step_index": step.step_index, "step_id": step.step_id, "action_key": step.action_key, "parameters": dict(step.parameters), "stage_before": step.stage_before.value, "stage_after": step.stage_after.value}
        for step in gate.gated_steps
    ]


def test_synthetic_observation_is_rejected_before_provider_request(tmp_path: Path) -> None:
    spec = _job_spec(next(job for job in e0_jobs() if job["level"] == "L2"))
    with pytest.raises(ValueError, match="synthetic_observation"):
        run_bound_provider_decision(spec, _observation(spec, origin="synthetic"), tmp_path / "decision", decision_index=0, heavy_remaining=3, remaining_cost_usd=1.0, credential="private-test", transport_factory=_Transport)


@pytest.mark.parametrize("mode,endpoint", [("invalid", "INVALID_POLICY_OUTPUT"), ("stop", "PROVIDER_RESPONSE_BOUND")])
def test_invalid_or_stop_provider_output_has_zero_dispatchable_steps(tmp_path: Path, mode: str, endpoint: str) -> None:
    spec = _job_spec(next(job for job in e0_jobs() if job["level"] == "L2"))
    _Transport.mode = mode
    bound = run_bound_provider_decision(spec, _observation(spec), tmp_path / mode, decision_index=0, heavy_remaining=3, remaining_cost_usd=1.0, credential="private-test", transport_factory=_Transport)
    assert bound.result["endpoint"] == endpoint
    assert bound.result["physical_requests"] == 1
    assert bound.gate is None or not bound.gate.gated_steps
    _Transport.mode = "valid"


def test_all_twelve_shapes_reach_bound_authorization_or_valid_no_dispatch(tmp_path: Path, monkeypatch) -> None:
    repository = _Repository()
    monkeypatch.setattr(real_mutation, "observe_repository", lambda _path: repository)
    monkeypatch.setattr(real_mutation, "fingerprint_file", lambda _path: RootFingerprint(EXPECTED_ROOT_SHA256, EXPECTED_ROOT_SIZE))
    root = tmp_path / "root.dcp"
    root.write_bytes(b"fake root")
    vivado = _write_executable(tmp_path / "Vivado" / "2025.1" / "bin" / "vivado")
    authorized = 0
    no_dispatch = 0
    for index, job in enumerate(e0_jobs()):
        spec = _job_spec(job)
        observation = _observation(spec)
        level = DoFLevel(job["level"])
        if level is DoFLevel.L0:
            no_dispatch += 1
            continue
        if level is DoFLevel.L1:
            causal = e0_job._deterministic_l1_causal(spec, observation)
            plan = freeze_e0_real_action_plan(root, root_pseudonym=job["benchmark"], level=level, action_key="macro.phys_opt", parameters={}, run_id=f"fake-l1-{index}", repository_anchor=tmp_path, python_exec=sys.executable, vivado_exec=vivado, run_dir=tmp_path / f"run-{index}", fake_startup_attestation_sha256="9" * 64)
        else:
            bound = run_bound_provider_decision(spec, observation, tmp_path / f"provider-{index}", decision_index=0, heavy_remaining=3, remaining_cost_usd=1.0, credential="private-test", transport_factory=_Transport)
            assert bound.gate is not None and bound.gate.accepted
            causal = causal_evidence_for_dispatch(spec, observation, bound)
            proposal = bound.decision.bound_proposal.proposal
            plan = freeze_e0_real_gated_plan(root, root_pseudonym=job["benchmark"], level=level, decision_id=bound.binding.decision_id, proposal_id=proposal.proposal_id, proposal_hash=proposal.proposal_hash, gate_decision_sha256=_hash(bound.gate.to_dict()), gated_steps=_gated_rows(bound.gate), causal_evidence_sha256=causal["causal_evidence_sha256"], run_id=f"fake-bound-{index}", repository_anchor=tmp_path, python_exec=sys.executable, vivado_exec=vivado, run_dir=tmp_path / f"run-{index}", fake_startup_attestation_sha256="9" * 64)
        authorization = build_e0_dispatch_authorization(plan, causal)
        assert validate_e0_dispatch_authorization(plan, authorization, causal) == authorization
        authorized += 1
    assert authorized == 10
    assert no_dispatch == 2


def test_changed_causal_fields_fail_authorization_before_dispatch(tmp_path: Path, monkeypatch) -> None:
    repository = _Repository()
    monkeypatch.setattr(real_mutation, "observe_repository", lambda _path: repository)
    monkeypatch.setattr(real_mutation, "fingerprint_file", lambda _path: RootFingerprint(EXPECTED_ROOT_SHA256, EXPECTED_ROOT_SIZE))
    root = tmp_path / "root.dcp"
    root.write_bytes(b"fake root")
    vivado = _write_executable(tmp_path / "Vivado" / "2025.1" / "bin" / "vivado")
    plan = freeze_e0_real_action_plan(root, root_pseudonym="root", level=DoFLevel.L2, action_key="macro.phys_opt", parameters={}, run_id="tamper-test", repository_anchor=tmp_path, python_exec=sys.executable, vivado_exec=vivado, run_dir=tmp_path / "run", fake_startup_attestation_sha256="9" * 64)
    causal = {"parent_sha256": "1" * 64, "observation_sha256": "2" * 64, "context_sha256": "3" * 64, "decision_id": "decision-test", "response_sha256": "4" * 64, "proposal_sha256": "5" * 64}
    authorization = build_e0_dispatch_authorization(plan, causal)
    for field in tuple(causal):
        changed = dict(causal)
        changed[field] = "different" if field == "decision_id" else "f" * 64
        with pytest.raises(RealMutationPlanError, match="authorization_binding_mismatch"):
            validate_e0_dispatch_authorization(plan, authorization, changed)
