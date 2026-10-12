from __future__ import annotations

from collections import Counter
import json
from pathlib import Path

from optimizer_pipeline.dof.e0 import (
    E0_BENCHMARKS,
    e0_jobs,
    e0_matrix_manifest,
    run_fake_e0_matrix,
    run_synthetic_openrouter_preflight,
)
from optimizer_pipeline.dof.openrouter import OPENROUTER_E0_MODELS
from optimizer_pipeline.dof.provider import (
    PROVIDER_TRANSPORT_CONTRACT_VERSION,
    RawProviderResponse,
)


class _PreflightTransport:
    transport_contract_version = PROVIDER_TRANSPORT_CONTRACT_VERSION
    internal_retries_disabled = True

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
            "route": {
                "requested_model": binding.model_id,
                "returned_model": binding.model_id,
                "selected_upstream_provider": "fake-upstream",
                "fallback_allowed": False,
                "attempt": 1,
            },
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15, "cost": 0.01},
        }
        payload = {
            "schema_version": "action_proposal.v1",
            "proposal_id": "proposal-preflight",
            "decision_id": binding.decision_id,
            "catalogue_version": binding.catalogue_version,
            "catalogue_hash": binding.catalogue_hash,
            "kind": "STOP",
            "steps": [],
        }
        import json

        return RawProviderResponse(
            json.dumps(payload, separators=(",", ":")).encode(),
            "openrouter",
            provider_request_id="fake-generation",
            returned_model_id=binding.model_id,
            returned_model_revision="fake-upstream",
            input_tokens=10,
            output_tokens=5,
            total_tokens=15,
            cost_usd=0.01,
            cost_source="openrouter_usage.cost",
            observed_request_contract_hash=binding.request_contract_hash,
        )


def test_e0_matrix_is_the_exact_twelve_condition_freeze() -> None:
    manifest = e0_matrix_manifest()
    jobs = e0_jobs()
    assert len(jobs) == 12
    assert len({job["job_id"] for job in jobs}) == 12
    assert Counter(job["benchmark"] for job in jobs) == {
        benchmark: 6 for benchmark in E0_BENCHMARKS
    }
    assert Counter(job["level"] for job in jobs) == {
        "L0": 2,
        "L1": 2,
        "L2": 4,
        "L3": 4,
    }
    assert {job["model"] for job in jobs if job["model"]} == set(
        OPENROUTER_E0_MODELS
    )
    assert manifest["regime"] == "known"
    assert manifest["repeats"] == 1
    assert manifest["max_heavy_calls_per_nonbaseline_job"] == 3
    assert manifest["max_model_requests_per_model_job"] == 3


def test_checked_in_matrix_is_byte_semantically_equal_to_builder() -> None:
    root = Path(__file__).resolve().parents[2]
    checked = json.loads(
        (root / "configs/experiments/e0_openrouter_matrix.json").read_text()
    )
    assert checked == e0_matrix_manifest()


def test_all_twelve_fake_shapes_traverse_the_expected_boundaries(tmp_path) -> None:
    result = run_fake_e0_matrix(tmp_path / "fake-e0")
    assert result["jobs_run"] == 12
    assert result["jobs_passed"] == 12
    assert result["provider_requests"] == 8
    for row in result["rows"]:
        assert row["endpoint"] == "VALID_COMPLETION"
        assert row["heavy_calls"] <= 3
        if row["level"] in {"L2", "L3"}:
            assert row["provider_requests"] == 1
            assert row["gate_accepted"] is True
            assert len(row["request_contract_hash"]) == 64
            assert len(row["outbound_binding_sha256"]) == 64
        elif row["level"] == "L0":
            assert row["provider_requests"] == 0
            assert row["heavy_calls"] == 0


def test_synthetic_preflight_persists_transport_and_gate_evidence(tmp_path) -> None:
    result = run_synthetic_openrouter_preflight(
        OPENROUTER_E0_MODELS[0],
        tmp_path / "provider-preflight",
        credential="private-test-key",
        transport_factory=_PreflightTransport,
    )
    assert result["transport_evidence_valid"] is True
    assert result["physical_requests"] == 1
    assert result["backend_dispatches"] == 0
    assert result["retries"] == result["fallbacks"] == result["repairs"] == 0
    assert result["gate_result"]["accepted"] is True
    combined = "".join(
        path.read_text()
        for path in (tmp_path / "provider-preflight").rglob("*.json")
    )
    assert "private-test-key" not in combined
