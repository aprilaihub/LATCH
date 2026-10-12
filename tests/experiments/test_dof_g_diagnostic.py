from __future__ import annotations

import json
from pathlib import Path

import pytest

from optimizer_pipeline.dof.catalogues import nested_action_catalogue
from optimizer_pipeline.dof.common_observation import build_common_policy_context
from optimizer_pipeline.dof.contracts import DoFLevel
from optimizer_pipeline.dof.e1v2 import content_hash
from optimizer_pipeline.dof.e1v2_job import run_bound_provider_decision_v2
from optimizer_pipeline.dof.g_diagnostic import (
    GCue,
    G_DIAGNOSTIC_MATRIX,
    G_DIAGNOSTIC_EXECUTION_EPOCH,
    G_DIAGNOSTIC_PROTOCOL,
    G_DIAGNOSTIC_RESULT_SCHEMA,
    G_DIAGNOSTIC_SPEC_SCHEMA,
    G_DIAGNOSTIC_STUDY_STATUS,
    build_g_diagnostic_policy_context,
    diagnostic_catalogue,
    g_only_catalogue,
    overlay_manifest,
    rebind_probe_observation,
    validate_diagnostic_spec,
)
from optimizer_pipeline.dof.generated import GENERATED_ACTION_KEY
from optimizer_pipeline.dof.job_queue import _hash, aggregate_trajectory_results
from optimizer_pipeline.dof.provider import (
    PROVIDER_TRANSPORT_CONTRACT_VERSION,
    RawProviderResponse,
)


def _source() -> dict:
    return {
        "condition": {"pseudonym": "condition-vexriscv_re-place"},
        "state": {
            "pseudonym": "state-vexriscv_re-place",
            "lineage_pseudonym": "lineage-vexriscv_re-place",
            "stage": "ROUTED",
            "measurements": {
                "timing": {
                    "wns_ns": -0.1,
                    "tns_ns": -1.0,
                    "failing_endpoints": 2,
                    "estimated_fmax_mhz": 300.0,
                },
                "route": {"status": "fully routed", "unrouted_nets": 0},
            },
        },
        "history": [],
        "budget_remaining": {
            "IMPLEMENTATION_HEAVY": 4,
            "MUTATION_LIGHT": 4,
            "READ_ONLY": 20,
            "LLM_REQUEST": 4,
            "WALL_TIME_SECONDS": 3700.0,
        },
        "validation": {"status": "CLEAN", "errors": [], "warnings": []},
        "stop_conditions": ["budget exhausted"],
        "known_priors": {},
    }


def _spec(cue: GCue = GCue.C4) -> dict:
    job = {
        "job_id": "vexriscv_re-place__l4__openai_gpt-5.6-luna__c4__provider_only_probe__gdiag_v1",
        "benchmark": "vexriscv_re-place",
        "level": "L4",
        "model": "openai/gpt-5.6-luna",
        "repeat": 1,
        "regime": "known",
        "phase": "PROVIDER_ONLY_PROBE",
        "cue": cue.value,
        "order": 0,
    }
    core = {
        "schema_version": G_DIAGNOSTIC_SPEC_SCHEMA,
        "protocol_version": G_DIAGNOSTIC_PROTOCOL,
        "matrix": G_DIAGNOSTIC_MATRIX,
        "study_status": G_DIAGNOSTIC_STUDY_STATUS,
        "job": job,
        "root": {
            "pseudonym": "vexriscv_re-place",
            "sha256": "a" * 64,
            "size_bytes": 10,
            "score": {"wns_ns": -0.1, "fmax_mhz": 300.0},
        },
        "freeze_sha256": "b" * 64,
        "repository": {},
        "diagnostic": {
            "protocol": G_DIAGNOSTIC_PROTOCOL,
            "execution_epoch": G_DIAGNOSTIC_EXECUTION_EPOCH,
            "cue": cue.value,
            "overlay_sha256": next(
                row["overlay_sha256"]
                for row in overlay_manifest()["rows"]
                if row["cue"] == cue.value
            ),
            "primary_aggregate_eligible": False,
            "sealed_holdout_access_allowed": False,
        },
        "budgets": {
            "heavy_calls": 4,
            "model_requests": 4,
            "trajectory_wall_seconds": 3700,
            "provider_cost_usd": 0.4,
        },
    }
    return core | {"job_spec_sha256": content_hash(core)}


def _observation(spec: dict) -> dict:
    core = {
        "schema_version": "dof.e1v2-real-observation.v1",
        "origin": "real_read_only_vivado",
        "job_spec_sha256": spec["job_spec_sha256"],
        "current_artifact": {"sha256": "a" * 64, "size_bytes": 10},
        "source": _source(),
    }
    return core | {"observation_sha256": content_hash(core)}


def test_c0_is_primary_byte_exact_and_c1_c3_are_overlay_only() -> None:
    primary = build_common_policy_context(
        level=DoFLevel.L4,
        source=_source(),
        current_artifact_sha256="a" * 64,
        decision_index=0,
    )
    contexts = {
        cue: build_g_diagnostic_policy_context(
            source=_source(),
            current_artifact_sha256="a" * 64,
            decision_index=0,
            cue=cue,
        )
        for cue in GCue
    }
    assert contexts[GCue.C0].common.provider_prompt_bytes == primary.provider_prompt_bytes
    assert contexts[GCue.C0].common.provider_context_bytes == primary.provider_context_bytes
    for cue in (GCue.C1, GCue.C2, GCue.C3):
        item = contexts[cue]
        assert item.common.base_observation_bytes == primary.base_observation_bytes
        assert item.common.provider_context_bytes == primary.provider_context_bytes
        assert item.common.capability_appendix_bytes == primary.capability_appendix_bytes
        assert item.common.provider_prompt_bytes.startswith(
            primary.provider_prompt_bytes + b"\nG_DIAGNOSTIC_OVERLAY="
        )


def test_c4_changes_only_first_action_surface_and_restores_neutral_l4() -> None:
    first = build_g_diagnostic_policy_context(
        source=_source(),
        current_artifact_sha256="a" * 64,
        decision_index=0,
        cue=GCue.C4,
    )
    later = build_g_diagnostic_policy_context(
        source=_source(),
        current_artifact_sha256="a" * 64,
        decision_index=1,
        cue=GCue.C4,
    )
    assert first.action_surface == (GENERATED_ACTION_KEY,)
    assert later.action_surface == tuple(
        action.name for action in nested_action_catalogue(DoFLevel.L4).actions
    )
    assert later.overlay == b""
    assert (
        g_only_catalogue().action(GENERATED_ACTION_KEY).to_dict()
        == nested_action_catalogue(DoFLevel.L4).action(GENERATED_ACTION_KEY).to_dict()
    )


def test_diagnostic_spec_rejects_sealed_holdout_and_probe_rebind_is_hashed() -> None:
    spec = _spec()
    assert validate_diagnostic_spec(spec) is GCue.C4
    rebound = rebind_probe_observation(_observation(spec), spec)
    core = {key: value for key, value in rebound.items() if key != "observation_sha256"}
    assert rebound["observation_sha256"] == content_hash(core)
    context = build_g_diagnostic_policy_context(
        source=rebound["source"],
        current_artifact_sha256=rebound["current_artifact"]["sha256"],
        decision_index=0,
        cue=GCue.C4,
    )
    assert context.common.base_observation_hash
    bad = json.loads(json.dumps(spec))
    bad["job"]["benchmark"] = "external_family_holdout_a"
    bad_core = {key: value for key, value in bad.items() if key != "job_spec_sha256"}
    bad["job_spec_sha256"] = content_hash(bad_core)
    with pytest.raises(ValueError, match="root_not_allowed"):
        validate_diagnostic_spec(bad)


class _GTransport:
    transport_contract_version = PROVIDER_TRANSPORT_CONTRACT_VERSION
    internal_retries_disabled = True
    kind = "ACTION"

    def __init__(self, **kwargs):
        self.expected = kwargs["expected_outbound_binding"]
        self.send_attempts = 0
        self.physical_requests = 0
        self.cleanup_status = "SUCCEEDED"
        self.last_exchange = None

    async def send(self, binding, _retry_index):
        self.send_attempts = 1
        self.physical_requests = 1
        self.last_exchange = {
            "schema_version": "dof.openrouter-exchange.v1",
            "route": {
                "requested_model": binding.model_id,
                "returned_model": binding.model_id,
                "selected_upstream_provider": "OpenAI",
                "fallback_allowed": False,
                "attempt": 1,
            },
            "usage": {
                "prompt_tokens": 20,
                "completion_tokens": 10,
                "total_tokens": 30,
                "cost": 0.01,
            },
        }
        payload = {
            "schema_version": "action_proposal.v1",
            "proposal_id": "proposal-g-diagnostic",
            "decision_id": binding.decision_id,
            "catalogue_version": binding.catalogue_version,
            "catalogue_hash": binding.catalogue_hash,
            "kind": type(self).kind,
            "steps": [
                {
                    "step_id": "step-g",
                    "action_key": GENERATED_ACTION_KEY,
                    "parameters": {
                        "pblock_id": "g_test",
                        "selector_kind": "sequential_cells",
                        "cell_limit": 8,
                        "slice_x0": 0,
                        "slice_y0": 0,
                        "slice_x1": 10,
                        "slice_y1": 99,
                    },
                }
            ],
        }
        raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        return RawProviderResponse(
            raw,
            "openrouter",
            provider_request_id="fake-request",
            returned_model_id=binding.model_id,
            returned_model_revision="OpenAI",
            input_tokens=20,
            output_tokens=10,
            total_tokens=30,
            cost_usd=0.01,
            cost_source="openrouter_usage.cost",
            observed_request_contract_hash=binding.request_contract_hash,
        )


class _GPlanTransport(_GTransport):
    kind = "PLAN"


def test_c4_requires_one_action_step_after_strict_parse(tmp_path: Path) -> None:
    spec = _spec()
    valid = run_bound_provider_decision_v2(
        spec,
        _observation(spec),
        tmp_path / "valid",
        decision_index=0,
        heavy_remaining=4,
        remaining_cost_usd=0.4,
        credential="fake",
        transport_factory=_GTransport,
    )
    assert valid.gate.accepted is True
    assert valid.result["g_diagnostic"]["g_gate_admitted"] is True
    invalid = run_bound_provider_decision_v2(
        spec,
        _observation(spec),
        tmp_path / "invalid",
        decision_index=0,
        heavy_remaining=4,
        remaining_cost_usd=0.4,
        credential="fake",
        transport_factory=_GPlanTransport,
    )
    assert invalid.gate.accepted is False
    assert invalid.gate.reasons == (
        "g_diagnostic_c4_requires_exactly_one_g_action_step",
    )


def test_primary_aggregate_rejects_diagnostic_result(tmp_path: Path) -> None:
    attempt_dir = tmp_path / "attempt"
    trajectory = attempt_dir / "trajectory"
    trajectory.mkdir(parents=True)
    result_core = {
        "schema_version": G_DIAGNOSTIC_RESULT_SCHEMA,
        "job": {"job_id": "diagnostic-job"},
    }
    (trajectory / "trajectory-result.json").write_text(
        json.dumps(result_core | {"result_sha256": _hash(result_core)})
    )
    ledger_core = {
        "attempts": [
            {
                "job_id": "diagnostic-job",
                "attempt_index": 1,
                "output_directory": str(attempt_dir),
            }
        ]
    }
    (tmp_path / "controller-ledger.json").write_text(
        json.dumps(ledger_core | {"ledger_sha256": _hash(ledger_core)})
    )
    with pytest.raises(ValueError, match="rejected_from_primary"):
        aggregate_trajectory_results(tmp_path)
