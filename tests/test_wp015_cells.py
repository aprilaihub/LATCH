import importlib.util
import argparse
import json
from pathlib import Path

import pytest

from optimizer_pipeline.dof.single_submission.contract_v6 import validate_protocol_cell
from optimizer_pipeline.dof.single_submission.macro_authority import (
    M10_ACTION_KEYS as M_ACTION_KEYS,
    macro_definition,
)


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("run_wp015_cells", ROOT / "scripts/run_wp015_cells.py")
module = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(module)

VALIDATION_SPEC = importlib.util.spec_from_file_location(
    "finalize_wp015_cell_validation", ROOT / "scripts/finalize_wp015_cell_validation.py"
)
validation_module = importlib.util.module_from_spec(VALIDATION_SPEC)
assert VALIDATION_SPEC.loader is not None
VALIDATION_SPEC.loader.exec_module(validation_module)

REFERENCE_SPEC = importlib.util.spec_from_file_location(
    "run_wp015_post_agent_references", ROOT / "scripts/run_wp015_post_agent_references.py"
)
reference_module = importlib.util.module_from_spec(REFERENCE_SPEC)
assert REFERENCE_SPEC.loader is not None
REFERENCE_SPEC.loader.exec_module(reference_module)

ANALYSIS_SPEC = importlib.util.spec_from_file_location(
    "analyze_wp015", ROOT / "scripts/analyze_wp015.py"
)
analysis_module = importlib.util.module_from_spec(ANALYSIS_SPEC)
assert ANALYSIS_SPEC.loader is not None
ANALYSIS_SPEC.loader.exec_module(analysis_module)

BUNDLE_SPEC = importlib.util.spec_from_file_location(
    "prepare_wp015_bundle", ROOT / "scripts/prepare_wp015_bundle.py"
)
bundle_module = importlib.util.module_from_spec(BUNDLE_SPEC)
assert BUNDLE_SPEC.loader is not None
BUNDLE_SPEC.loader.exec_module(bundle_module)

RECONCILE_SPEC = importlib.util.spec_from_file_location(
    "reconcile_wp015_physical_aggregate",
    ROOT / "scripts/reconcile_wp015_physical_aggregate.py",
)
reconcile_module = importlib.util.module_from_spec(RECONCILE_SPEC)
assert RECONCILE_SPEC.loader is not None
RECONCILE_SPEC.loader.exec_module(reconcile_module)


def test_m10_authority_uses_observed_exact_one_route_costs() -> None:
    assert len(M_ACTION_KEYS) == 10
    assert M_ACTION_KEYS[-2:] == ("macro.fanout", "macro.lut_cone")
    for action_key in M_ACTION_KEYS[-2:]:
        definition = macro_definition(action_key)
        assert definition.heavy_call_cost == 1
        assert definition.heavy_call_sequence == ("route_design",)
        assert definition.light_mutation_cost == 0


@pytest.mark.parametrize("level", ["L2", "L3", "L4"])
def test_wp015_full_m10_neutral_cells_are_admitted(level: str) -> None:
    validate_protocol_cell(
        study_track="N_NEUTRAL", coverage_regime="FULL_M10",
        language_condition="FREE_CHOICE", level=level,
    )


def test_full_m10_does_not_weaken_track_firewalls() -> None:
    with pytest.raises(ValueError, match="neutral_full_cell_invalid"):
        validate_protocol_cell(
            study_track="N_NEUTRAL", coverage_regime="FULL_M10",
            language_condition="P_BOUNDARY", level="L3",
        )
    with pytest.raises(ValueError, match="neutral_stress_cell_invalid"):
        validate_protocol_cell(
            study_track="S_NEUTRAL_REDUCED", coverage_regime="FULL_M10",
            language_condition="FREE_CHOICE", level="L3",
        )


def test_canary_is_exact_balanced_and_matches_any_strict_family_semantics():
    jobs = []
    for root in ("amd_mini-isp", "r1", "r2", "r3"):
        for model in module.MODEL_RESERVES_USD:
            for condition, track, language, level, agent in (
                ("L4", "N_NEUTRAL", "FREE_CHOICE", "L4", "L4"),
                ("M_ONLY", "C_CONDITIONED", "M_ONLY", "L2", "C"),
                ("P_BOUNDARY", "C_CONDITIONED", "P_BOUNDARY", "L3", "C"),
                ("G_STRICT_BOUNDARY", "C_CONDITIONED", "G_BOUNDARY", "L4", "C"),
            ):
                jobs.append({
                    "job_id": f"{root}-{model}-{condition}", "root_id": root,
                    "root_pseudonym": root, "split": "DEVELOPMENT_KNOWN",
                    "model_key": model, "condition": condition, "track": track,
                    "language_condition": language, "level": level,
                    "agent_level": agent, "repeat_id": "R1", "coverage_regime": "FULL_M10",
                    "mask_evidence_hash": "a" * 64, "admitted_action_keys": ["macro.phys_opt"],
                    "strict_g_required": condition == "G_STRICT_BOUNDARY",
                })
    rows = module.canary_jobs({"jobs": jobs})
    assert len(rows) == len({row["job_id"] for row in rows}) == 28
    assert all(row["job_id"].startswith("wp015b-canary__") for row in rows)
    assert {row["repeat_id"] for row in rows} == {"WP015B_FINAL_CANARY_NO_REPEAT"}
    assert {row["condition"] for row in rows} == {"L4", "M_ONLY", "P_BOUNDARY", "G_STRICT_BOUNDARY"}
    assert {row["root_id"] for row in rows if row["condition"] == "G_STRICT_BOUNDARY"} == {"amd_mini-isp"}
    assert {row["strict_mechanism"] for row in rows if row["condition"] == "G_STRICT_BOUNDARY"} == {None}
    assert all(row["model"] == row["model_key"] for row in rows)


def test_cell_validation_plan_and_apply_are_hash_bound(tmp_path):
    root = tmp_path / "root.dcp"
    child = tmp_path / "child.dcp"
    root.write_bytes(b"root")
    child.write_bytes(b"child")
    root_sha = validation_module.file_sha256(root)
    child_sha = validation_module.file_sha256(child)
    bundle = {
        "bundle_sha256": "b" * 64, "shared_scientific_core_hash": "c" * 64,
        "roots": {"r": {"root_path": str(root), "sha256": root_sha}},
    }
    aggregate_core = {
        "bundle_sha256": bundle["bundle_sha256"],
        "shared_scientific_core_hash": bundle["shared_scientific_core_hash"],
        "scientific_source_commit": "d" * 40, "scientific_source_tree": "e" * 40,
        "jobs": [{
            "job_id": "j", "root_id": "r", "resource_class": "CORE",
            "selected_language": "P", "functional_terminal": "PENDING_V1",
            "resolved_child_path": str(child), "resolved_child_sha256": child_sha,
        }],
    }
    aggregate = aggregate_core | {"aggregate_sha256": validation_module.digest(aggregate_core)}
    bundle_path, aggregate_path, plan_path = (
        tmp_path / "bundle.json", tmp_path / "aggregate.json", tmp_path / "plan.json"
    )
    bundle_path.write_text(json.dumps(bundle))
    aggregate_path.write_text(json.dumps(aggregate))
    assert validation_module.plan(argparse.Namespace(
        aggregate=aggregate_path, bundle=bundle_path, output=plan_path,
    )) == 0
    plan_value = json.loads(plan_path.read_text())
    case_id = plan_value["cases"][0]["case_id"]
    validation_core = {
        "plan_sha256": plan_value["plan_sha256"],
        "case_aggregates": {case_id: {
            "terminal": "MULTISEED_FUNCTIONAL_PASS",
            "semantic_result_sha256": "f" * 64,
            "all_seeds_present_exactly_once": True,
        }},
    }
    validation = validation_core | {"aggregate_sha256": validation_module.digest(validation_core)}
    validation_path, output_path = tmp_path / "validation.json", tmp_path / "final.json"
    validation_path.write_text(json.dumps(validation))
    assert validation_module.apply(argparse.Namespace(
        aggregate=aggregate_path, plan=plan_path,
        validation=validation_path, output=output_path,
    )) == 0
    final = json.loads(output_path.read_text())
    assert final["functional_valid_children"] == 1
    assert final["jobs"][0]["functional_valid"] is True


def test_physical_aggregate_reconciliation_is_append_only_and_hash_bound(tmp_path, monkeypatch):
    run_root = tmp_path / "run"
    execution_dir = run_root / "jobs" / "j" / "execution"
    child = execution_dir / "suffix" / "vivado" / "child.dcp"
    child.parent.mkdir(parents=True)
    child.write_bytes(b"child")
    child_sha = reconcile_module.file_sha256(child)
    execution_summary = {
        "canonical_endpoint": "VALID_COMPLETION",
        "child": {"sha256": child_sha, "size_bytes": child.stat().st_size},
    }
    (execution_dir / "execution-summary.json").write_text(json.dumps(execution_summary))
    aggregate_core = {
        "schema_version": "dof.wp015-cell-aggregate.v1",
        "physical_valid_children": 0,
        "jobs": [{
            "job_id": "j", "canonical_terminal": "VALID_COMPLETION",
            "functional_terminal": None, "resolved_child_path": None,
            "resolved_child_sha256": None,
        }],
    }
    aggregate = aggregate_core | {"aggregate_sha256": reconcile_module.digest(aggregate_core)}
    aggregate_path = tmp_path / "aggregate.json"
    output_path = tmp_path / "aggregate-reconciled.json"
    aggregate_path.write_text(json.dumps(aggregate))
    monkeypatch.setattr(reconcile_module, "git", lambda *args: "a" * 40)
    assert reconcile_module.reconcile(argparse.Namespace(
        aggregate=aggregate_path, run_root=run_root, output=output_path,
        expected_count=1,
    )) == 0
    result = json.loads(output_path.read_text())
    assert json.loads(aggregate_path.read_text()) == aggregate
    assert result["source_physical_aggregate_sha256"] == aggregate["aggregate_sha256"]
    assert result["physical_valid_children"] == 1
    assert result["jobs"][0]["functional_terminal"] == "PENDING_V1"
    assert result["jobs"][0]["resolved_child_sha256"] == child_sha
    assert result["child_path_reconciliation"]["source_records_modified"] is False


def test_post_agent_reference_firewall_requires_42_terminal_cells_per_root():
    roots = {"transfer": {}}
    complete = {
        "job_count": 1134,
        "jobs": [
            {"root_id": "transfer", "canonical_terminal": "VALID_COMPLETION"}
            for _ in range(42)
        ] + [
            {"root_id": "other", "canonical_terminal": "NO_VALID_SUBMISSION"}
            for _ in range(1092)
        ],
    }
    reference_module._agent_firewall(complete, roots)
    complete["jobs"][0]["canonical_terminal"] = None
    try:
        reference_module._agent_firewall(complete, roots)
    except ValueError as exc:
        assert "not_terminal" in str(exc)
    else:
        raise AssertionError("firewall accepted a non-terminal agent cell")


def test_prior_provider_accounting_accepts_admission_rows(tmp_path):
    path = tmp_path / "admission.json"
    path.write_text(json.dumps({"rows": [{
        "model_key": "openai/gpt-5.6-luna", "provider_requests": 3,
        "cost_usd": 0.25,
    }]}))
    spent, requests, evidence = module.prior_provider_accounting([path])
    assert spent == {"openai": 0.25}
    assert requests == 3
    assert evidence[0]["path"] == str(path)


def test_prior_provider_accounting_binds_admission_and_cell_aggregates(tmp_path):
    admission = tmp_path / "admission.json"
    cells = tmp_path / "cells.json"
    admission.write_text(json.dumps({
        "provider": "google_gemini", "api_cost_usd": 0.05,
        "physical_model_requests": 3,
    }))
    cells.write_text(json.dumps({"jobs": [{
        "model_key": "openai/gpt-5.6-luna", "cost_usd": 0.02,
        "provider_requests": 2,
    }]}))
    spent, requests, evidence = module.prior_provider_accounting([admission, cells])
    assert spent == {"google_gemini": 0.05, "openai": 0.02}
    assert requests == 5
    assert len(evidence) == 2 and all(len(row["sha256"]) == 64 for row in evidence)


def test_typed_execution_timing_uses_frozen_root_period():
    result = analysis_module.timing({
        "execution": {"pre_timing": {"wns": -1.0}, "post_timing": {"wns": -0.5}},
    }, {"period_ns": 4.0})
    assert result["initial_fmax_mhz"] == 200.0
    assert result["final_fmax_mhz"] == 1000.0 / 4.5
    assert result["delta_fmax_mhz"] > 0


def test_wp015_timing_contract_is_complete_and_preserves_strata():
    contract = json.loads((ROOT / "configs/wp015/benchmark-timing-contract-v1.json").read_text())
    rows = contract["roots"]
    assert len(rows) == len({row["root_id"] for row in rows}) == 25
    counts = {}
    for row in rows:
        counts[row["timing_stratum"]] = counts.get(row["timing_stratum"], 0) + 1
        assert row["target_period_ns"] > 0
        assert len(row["evidence_sha256"]) == 64
    assert counts == {"SOURCE_TIMED": 19, "SYNTHETIC_TIMING": 6}
    synthetic = [row for row in rows if row["timing_stratum"] == "SYNTHETIC_TIMING"]
    assert all("experiment_defined_synthetic" in row["target_period_evidence"] for row in synthetic)
    assert contract["policy"]["wns_inference_forbidden"] is True


def test_wp015_bundle_digest_accepts_canonical_prompt_bytes():
    value = b"frozen prompt bytes"
    import hashlib
    assert bundle_module.digest(value) == hashlib.sha256(value).hexdigest()


def test_summary_is_descriptive_and_handles_singleton():
    assert analysis_module._summary([])["n"] == 0
    assert analysis_module._summary([2.0]) == {
        "n": 1, "mean": 2.0, "median": 2.0, "std": 0.0, "q1": 2.0, "q3": 2.0,
    }
    assert analysis_module.bootstrap_mean_ci([1.0, 2.0, 3.0], seed="frozen") == analysis_module.bootstrap_mean_ci([1.0, 2.0, 3.0], seed="frozen")
