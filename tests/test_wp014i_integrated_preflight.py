import importlib.util
import json
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "run_wp014i_integrated_preflight", ROOT / "scripts/run_wp014i_integrated_preflight.py"
)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)


def plan():
    return json.loads((ROOT / "configs/wp014i/integrated-preflight-v1.json").read_text())


def test_exact_28_job_inventory_and_rotation():
    rows = MODULE.jobs(plan())
    assert len(rows) == 28
    assert len({row["job_id"] for row in rows}) == 28
    assert set(row["preflight_class"] for row in rows) == {
        "NEUTRAL_L4", "M_ONLY", "P_BOUNDARY", "G_STRICT_BOUNDARY"
    }
    assert set(row["strict_mechanism"] for row in rows if row["strict_mechanism"]) == {
        "SPATIAL_GENERATED_GEOMETRY", "NONPREFIX_MULTI_NET_BATCH",
        "COUPLED_HARDBLOCK_SITE_ALLOCATION",
    }


def test_gemini_profile_is_terminal_without_retry_or_fallback():
    value = plan()
    assert value["profile_not_ready"] == ["google/gemini-3.6-flash"]
    assert value["limits"]["provider_retries"] == 0
    assert value["limits"]["fallbacks"] == 0
    assert value["limits"]["response_repairs"] == 0


def test_only_development_roots_are_assigned():
    development = {
        "amd_mini-isp", "corescore_500_mod", "finn_radioml", "logicnets_jscl",
        "rosetta_3d-rendering", "rosetta_digit-recognition", "rosetta_optical-flow",
        "rosetta_spam-filter", "vexriscv_re-place", "vexriscv_re-place_v2",
    }
    assert {row["root_id"] for row in MODULE.jobs(plan())} <= development


def test_embedded_hash_verification_rejects_tampering():
    core = {"schema_version": "test.v1", "rows": [1, 2]}
    value = core | {"aggregate_sha256": MODULE.digest(core)}
    MODULE.verify_embedded_hash(value, "aggregate_sha256")
    value["rows"].append(3)
    with pytest.raises(ValueError, match="embedded_hash_mismatch"):
        MODULE.verify_embedded_hash(value, "aggregate_sha256")


def test_superseded_attempt_accounting_is_hash_bound_and_total(tmp_path):
    private = tmp_path / "planning/job/private"
    private.mkdir(parents=True)
    (private / "turn-1-exchange.json").write_text(json.dumps({
        "physical_requests": 1, "derived_cost_usd": "0.125",
        "canonical_provider": "openai",
    }))
    (private / "turn-2-exchange.json").write_text(json.dumps({
        "physical_requests": 0, "provider_reported_cost_usd": 0,
        "canonical_provider": "openai",
    }))
    value = MODULE.superseded_attempt_accounting(tmp_path)
    assert value["exchange_count"] == 2
    assert value["physical_requests"] == 1
    assert value["cost_usd"] == 0.125
    assert value["backend_started"] is False
    assert len(value["exchange_sha256"]) == 2


def test_execution_child_resolution_requires_recorded_hash(tmp_path):
    output = tmp_path / "job"
    (output / "vivado").mkdir(parents=True)
    child = output / "vivado/child.dcp"
    child.write_bytes(b"canonical child")
    value = {"child": {"sha256": MODULE.file_sha256(child)}}
    assert MODULE.resolve_execution_child(value, output) == child.resolve()
    value["child"]["sha256"] = "0" * 64
    assert MODULE.resolve_execution_child(value, output) is None


def test_execution_child_resolution_accepts_embedded_macro_suffix_vivado_path(tmp_path):
    output = tmp_path / "job"
    (output / "suffix" / "vivado").mkdir(parents=True)
    child = output / "suffix" / "vivado" / "child.dcp"
    child.write_bytes(b"embedded macro suffix child")
    value = {"child": {"sha256": MODULE.file_sha256(child)}}
    assert MODULE.resolve_execution_child(value, output) == child.resolve()
