from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path

import pytest

from optimizer_pipeline.dof.catalogues import (
    L4_REFERENCE_CATALOGUE_HASH,
    L4_REFERENCE_CATALOGUE_VERSION,
    NO_ACTION_CATALOGUE_HASH,
    NO_ACTION_CATALOGUE_VERSION,
    clean_macro_catalogue,
    strict_l3_catalogue,
)
from optimizer_pipeline.dof.contracts import DoFLevel, EventClass, LevelStatus, UnknownRegime
from optimizer_pipeline.dof.manifests import capability_manifest, interface_fingerprints
from optimizer_pipeline.dof.spec import (
    EXPERIMENT_SPEC_SCHEMA_VERSION,
    ExperimentSpec,
    MaskSpec,
    PolicySpec,
    SpecValidationError,
    content_hash,
    load_experiment_spec,
)


def observation() -> dict:
    return {
        "condition": {"pseudonym": "condition-001"},
        "state": {
            "pseudonym": "state-001",
            "lineage_pseudonym": "lineage-001",
            "stage": "ROUTED",
            "measurements": {
                "timing": {"wns_ns": -0.2, "tns_ns": -1.0},
                "route": {"status": "routed", "unrouted_nets": 0},
                "placement": {"status": "placed", "placed_cells": 100},
                "utilization": {"lut": 100, "ff": 200},
                "congestion": {"level": "low", "hotspots": 0},
            },
        },
        "history": [],
        "budget_remaining": {
            "IMPLEMENTATION_HEAVY": 4,
            "MUTATION_LIGHT": 4,
            "READ_ONLY": 8,
            "VALIDATION": 4,
            "LLM_REQUEST": 1,
            "OUTER_ATTEMPT": 1,
            "WALL_TIME_SECONDS": 60.0,
        },
        "validation": {"status": "CLEAN", "errors": [], "warnings": []},
        "stop_conditions": ["budget exhausted"],
        "known_priors": {},
    }


def base_spec(level: DoFLevel = DoFLevel.L2) -> dict:
    mask = MaskSpec.create(version="mask.v1")
    policy_response: dict
    if level in {DoFLevel.L0, DoFLevel.L4}:
        policy_response = {}
    elif level is DoFLevel.L1:
        policy_response = {
            "kind": "PLAN",
            "steps": [{"action_key": "macro.phys_opt", "parameters": {}}],
        }
    elif level is DoFLevel.L2:
        policy_response = {
            "kind": "ACTION",
            "steps": [{"action_key": "macro.phys_opt", "parameters": {}}],
        }
    else:
        policy_response = {
            "kind": "ACTION",
            "steps": [
                {
                    "action_key": "manual.phys_opt_design",
                    "parameters": {"directive": "Explore"},
                }
            ],
        }
    mode = {
        DoFLevel.L0: "baseline",
        DoFLevel.L1: "deterministic_schedule",
        DoFLevel.L2: "mock_adapter",
        DoFLevel.L3: "mock_adapter",
        DoFLevel.L4: "reference_pending",
    }[level]
    policy = PolicySpec.create(mode=mode, version="policy.v1", mock_response=policy_response)
    if level is DoFLevel.L0:
        catalogue_version, catalogue_hash = NO_ACTION_CATALOGUE_VERSION, NO_ACTION_CATALOGUE_HASH
    elif level in {DoFLevel.L1, DoFLevel.L2}:
        catalogue = clean_macro_catalogue(level)
        catalogue_version, catalogue_hash = catalogue.version, catalogue.sha256
    elif level is DoFLevel.L3:
        catalogue = strict_l3_catalogue()
        catalogue_version, catalogue_hash = catalogue.version, catalogue.sha256
    else:
        catalogue_version, catalogue_hash = L4_REFERENCE_CATALOGUE_VERSION, L4_REFERENCE_CATALOGUE_HASH
    capability = capability_manifest(level)
    return {
        "schema_version": EXPERIMENT_SPEC_SCHEMA_VERSION,
        "experiment_id": "experiment-001",
        "condition_id": "condition-001",
        "repeat_id": "repeat-001",
        "root_dcp": {
            "internal_ref": "fixtures/root.dcp",
            "sha256": "2" * 64,
            "model_pseudonym": "design-001",
        },
        "level": level.value,
        "unknown_regime": "forced_unknown",
        "model": {
            "provider_role": "none",
            "provider": "none",
            "model_id": "none",
            "model_version": "NOT_RUN",
            "decision_adapter": "mock_adapter" if level in {DoFLevel.L2, DoFLevel.L3} else "none",
            "adapter_version": "adapter.v1" if level in {DoFLevel.L2, DoFLevel.L3} else "NOT_RUN",
        },
        "validation": {
            "validation_id": "mock-validation",
            "validation_version": "validation.v1",
            "scoring_id": "mock-score",
            "scoring_version": "score.v1",
        },
        "budget": {
            "max_heavy_calls": 0 if level is DoFLevel.L0 else 4,
            "max_light_mutation_calls": 0 if level is DoFLevel.L0 else 4,
            "max_read_calls": 8,
            "max_validation_calls": 4,
            "max_llm_calls": 0,
            "max_outer_attempts": 1,
            "whole_run_wall_time_seconds": 60,
            "max_usd": None,
            "unknown_pricing_policy": "allow_not_run",
        },
        "catalogue": {
            "capability_version": capability.version,
            "capability_hash": capability.sha256,
            "catalogue_version": catalogue_version,
            "catalogue_hash": catalogue_hash,
        },
        "interface": interface_fingerprints(level),
        "mask": mask.to_dict(),
        "provenance": {
            "repository_constraint": {
                "repository_mode": "git",
                "required_head": None,
                "require_tracked_clean": False,
                "untracked_policy": "record_only",
            },
            "tool_versions": [
                {"name": "vivado", "version": "NOT_RUN"},
                {"name": "rapidwright", "version": "NOT_RUN"},
            ],
            "environment_manifest_version": "mock-env.v1",
            "environment_manifest_hash": "4" * 64,
            "execution_mode": "mock",
        },
        "policy": policy.to_dict(),
        "mock_observation": observation(),
    }


def test_spec_round_trip_stable_hash_run_id_and_relative_resolution(tmp_path: Path) -> None:
    left_path = tmp_path / "one" / "spec.json"
    right_path = tmp_path / "two" / "spec.json"
    left = ExperimentSpec.from_dict(base_spec(), source_path=left_path)
    right = ExperimentSpec.from_dict(base_spec(), source_path=right_path)
    assert left.to_dict() == right.to_dict()
    assert left.canonical_bytes() == right.canonical_bytes()
    assert left.spec_hash == right.spec_hash
    assert left.run_id == right.run_id
    assert left.resolve_root() == left_path.parent / "fixtures/root.dcp"
    assert right.resolve_root() == right_path.parent / "fixtures/root.dcp"
    assert ExperimentSpec.from_dict(left.to_dict()).spec_hash == left.spec_hash


def test_load_json_records_source_but_not_identity(tmp_path: Path) -> None:
    path = tmp_path / "condition.json"
    path.write_text(json.dumps(base_spec()))
    loaded = load_experiment_spec(path)
    assert loaded.source_path == path
    assert loaded.resolve_root() == tmp_path / "fixtures/root.dcp"


def test_absolute_parent_and_uri_root_references_fail_closed() -> None:
    for value in ("/tmp/root.dcp", "../root.dcp", "file://root.dcp", r"fixtures\root.dcp"):
        payload = base_spec()
        payload["root_dcp"]["internal_ref"] = value
        with pytest.raises(SpecValidationError, match="internal_ref_must_be_relative"):
            ExperimentSpec.from_dict(payload)


@pytest.mark.parametrize(
    "path,key",
    [
        (("root_dcp",), "unexpected"),
        (("model",), "temperature"),
        (("budget",), "extra_budget"),
        (("mask",), "benchmark_name"),
        (("provenance", "tool_versions", 0), "path"),
        (("mock_observation", "state", "measurements", "timing"), "winner"),
    ],
)
def test_unknown_fields_fail_closed_at_every_nested_boundary(path, key) -> None:
    payload = base_spec()
    target = payload
    for component in path:
        target = target[component]
    target[key] = "leak"
    with pytest.raises(SpecValidationError, match="unknown_fields"):
        ExperimentSpec.from_dict(payload)


@pytest.mark.parametrize("legacy_field", ("git_commit", "dirty_tree"))
def test_config_cannot_supply_observed_repository_facts(legacy_field: str) -> None:
    payload = base_spec()
    payload["provenance"][legacy_field] = (
        "3f121acb882c7a813f5d48e606eb93f67aa87dc3"
        if legacy_field == "git_commit"
        else False
    )
    with pytest.raises(SpecValidationError, match="provenance.unknown_fields"):
        ExperimentSpec.from_dict(payload)


def test_repository_constraint_is_strict_and_versioned() -> None:
    payload = base_spec()
    payload["provenance"]["repository_constraint"]["required_head"] = "short"
    with pytest.raises(SpecValidationError, match="required_head_invalid"):
        ExperimentSpec.from_dict(payload)

    payload = base_spec()
    payload["provenance"]["repository_constraint"]["unknown"] = True
    with pytest.raises(SpecValidationError, match="unknown_fields"):
        ExperimentSpec.from_dict(payload)


def test_invalid_level_model_adapter_and_budget_combinations_fail_closed() -> None:
    l0 = base_spec(DoFLevel.L0)
    l0["model"]["provider_role"] = "policy_model"
    with pytest.raises(SpecValidationError, match="L0_model_or_adapter_must_be_none"):
        ExperimentSpec.from_dict(l0)

    l2 = base_spec(DoFLevel.L2)
    l2["model"]["decision_adapter"] = "none"
    with pytest.raises(SpecValidationError, match="L2_requires_decision_adapter"):
        ExperimentSpec.from_dict(l2)

    bad_baseline = base_spec(DoFLevel.L0)
    bad_baseline["budget"]["max_heavy_calls"] = 1
    with pytest.raises(SpecValidationError, match="L0_heavy_budget_must_be_zero"):
        ExperimentSpec.from_dict(bad_baseline)


def test_catalogue_binding_is_level_exact_and_l1_l2_are_byte_identical() -> None:
    l1 = clean_macro_catalogue(DoFLevel.L1)
    l2 = clean_macro_catalogue(DoFLevel.L2)
    assert l1.canonical_bytes() == l2.canonical_bytes()
    assert l1.sha256 == l2.sha256

    payload = base_spec(DoFLevel.L3)
    payload["catalogue"]["catalogue_hash"] = l1.sha256
    with pytest.raises(SpecValidationError, match="L3_catalogue_hash_mismatch"):
        ExperimentSpec.from_dict(payload)


def test_strict_l3_catalogue_exact_semantics_do_not_serialize_macro_or_raw_tcl() -> None:
    catalogue = strict_l3_catalogue()
    assert {item.name for item in catalogue.actions} == {
        "manual.unroute_all",
        "manual.unplace_all",
        "manual.place_design",
        "manual.route_design",
        "manual.phys_opt_design",
    }
    encoded = catalogue.canonical_bytes().decode()
    assert "macro_command" not in encoded
    assert "route_design -unroute" not in encoded
    by_name = {item.name: item for item in catalogue.actions}
    assert by_name["manual.unroute_all"].internal_events[0].event_class is EventClass.MUTATION_LIGHT
    assert by_name["manual.place_design"].internal_events[0].event_class is EventClass.IMPLEMENTATION_HEAVY


def test_budget_event_limits_and_mask_policy_hashes_are_content_bound() -> None:
    parsed = ExperimentSpec.from_dict(base_spec())
    limits = parsed.budget.limits_by_event_class()
    assert limits[EventClass.IMPLEMENTATION_HEAVY] == 4
    assert limits[EventClass.OUTER_ATTEMPT] == 1

    tampered_mask = base_spec()
    tampered_mask["mask"]["hidden_macro_families"] = ["retiming"]
    with pytest.raises(SpecValidationError, match="mask.hash_content_mismatch"):
        ExperimentSpec.from_dict(tampered_mask)

    tampered_policy = base_spec()
    tampered_policy["policy"]["mock_response"]["steps"][0]["action_key"] = "macro.retime"
    with pytest.raises(SpecValidationError, match="policy.config_hash_content_mismatch"):
        ExperimentSpec.from_dict(tampered_policy)


def test_l4_parses_only_as_reference_pending() -> None:
    parsed = ExperimentSpec.from_dict(base_spec(DoFLevel.L4))
    assert parsed.status is LevelStatus.REFERENCE_PENDING
    assert parsed.level is DoFLevel.L4


def test_lower_and_upper_unknown_regime_spellings_are_canonicalized() -> None:
    lower = ExperimentSpec.from_dict(base_spec())
    upper_payload = base_spec()
    upper_payload["unknown_regime"] = "FORCED_UNKNOWN"
    upper = ExperimentSpec.from_dict(upper_payload)
    assert lower.unknown_regime is UnknownRegime.FORCED_UNKNOWN
    assert upper.unknown_regime is UnknownRegime.FORCED_UNKNOWN
    assert lower.to_dict()["unknown_regime"] == "forced_unknown"
