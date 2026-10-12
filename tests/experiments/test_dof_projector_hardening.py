from __future__ import annotations

import inspect
import json

import pytest

from optimizer_pipeline.dof.catalogues import (
    clean_macro_catalogue,
    strict_l3_catalogue,
)
from optimizer_pipeline.dof.contracts import DoFLevel, UnknownRegime
from optimizer_pipeline.dof.manifests import (
    CONTEXT_INTERFACE_SCHEMA_VERSION,
    context_interface_schema,
    interface_fingerprints,
)
from optimizer_pipeline.dof.masks import (
    MASK_MANIFEST_SCHEMA_VERSION,
    MaskRulesValidationError,
    validate_mask_rules,
)
from optimizer_pipeline.dof.projector import (
    PROJECTED_CONTEXT_SCHEMA_VERSION,
    DecisionContextProjector,
    ProjectionError,
)
from optimizer_pipeline.dof.spec import MaskSpec, MockObservationSpec


def _source() -> dict:
    return {
        "condition": {"pseudonym": "condition-001"},
        "state": {
            "pseudonym": "state-001",
            "lineage_pseudonym": "lineage-001",
            "stage": "ROUTED",
            "measurements": {
                "timing": {
                    "wns_ns": -0.125,
                    "tns_ns": -1.5,
                    "failing_endpoints": 7,
                    "clock_period_ns": 2.5,
                    "estimated_fmax_mhz": 380.0,
                },
                "route": {"status": "fully routed", "unrouted_nets": 0},
                "placement": {"status": "placed", "placed_cells": 1234},
                "utilization": {"lut": 1234, "ff": 2345},
                "congestion": {"level": "low", "hotspots": 1},
            },
        },
        "history": [
            {
                "decision_index": 0,
                "actions": [{"action_key": "manual.phys_opt_design", "parameters": {"directive": "Explore"}}],
                "parent_qor": {"fmax_mhz": 379.0, "wns_ns": -0.135},
                "child_qor": {"fmax_mhz": 380.0, "wns_ns": -0.125},
                "delta_qor": {"fmax_mhz": 1.0, "wns_ns": 0.01},
                "endpoint": "NO_IMPROVEMENT",
                "valid": True,
                "actual_heavy_calls": 1,
            }
        ],
        "budget_remaining": {
            "IMPLEMENTATION_HEAVY": 3,
            "MUTATION_LIGHT": 2,
            "READ_ONLY": 5,
            "LLM_REQUEST": 1,
            "WALL_TIME_SECONDS": 120.0,
        },
        "validation": {"status": "CLEAN", "errors": [], "warnings": []},
        "stop_conditions": ["heavy budget exhausted"],
        "known_priors": {},
    }


def test_mask_metadata_never_enters_context_but_full_manifest_remains() -> None:
    secret_version = "dof.mask.hide-retiming.UNIQUE-MASK-TOKEN.v1"
    mask = MaskSpec.create(
        version=secret_version,
        hidden_macro_families=("retiming",),
        forbidden_sentinels=("secret-benchmark",),
        known_prior_allowlist=("operator_note",),
    )
    source = _source()
    source["condition"]["pseudonym"] = "secret-benchmark"

    projected = DecisionContextProjector().project(
        level=DoFLevel.L2,
        regime=UnknownRegime.FORCED_UNKNOWN,
        source=source,
        catalogue=clean_macro_catalogue(DoFLevel.L2),
        mask=mask,
    )

    assert "mask_version" not in projected.context["condition"]
    assert secret_version not in projected.serialized
    assert "hide-retiming" not in projected.serialized
    assert "UNIQUE-MASK-TOKEN" not in projected.serialized
    assert mask.hash not in projected.serialized
    assert "secret-benchmark" not in projected.serialized
    assert "macro.retime" not in projected.context["legal_actions"]
    for key in ("pseudonym",):
        assert projected.context["condition"][key]
    for key in ("pseudonym", "lineage_pseudonym"):
        value = projected.context["current_state"][key]
        assert value
        assert not value.startswith("-")

    manifest = mask.to_manifest_dict()
    assert manifest == {
        "schema_version": MASK_MANIFEST_SCHEMA_VERSION,
        "version": secret_version,
        "hash": mask.hash,
        "hidden_macro_families": ["retiming"],
        "forbidden_sentinels": ["secret-benchmark"],
        "known_prior_allowlist": ["operator_note"],
    }
    assert validate_mask_rules(manifest).hash == mask.hash


def test_l3_surface_is_independent_of_hidden_l2_macro_family() -> None:
    mask = MaskSpec.create(
        version="dof.mask.hide-retiming.v1",
        hidden_macro_families=("retiming",),
    )
    projected = DecisionContextProjector().project(
        level=DoFLevel.L3,
        regime=UnknownRegime.FORCED_UNKNOWN,
        source=_source(),
        catalogue=strict_l3_catalogue(),
        mask=mask,
    )

    assert "dof.mask.hide-retiming.v1" not in projected.serialized
    assert "manual.phys_opt_design" in projected.context["legal_actions"]
    phys_opt = next(
        item
        for item in projected.context["action_schema"]
        if item["action_key"] == "manual.phys_opt_design"
    )
    assert "AddRetime" in phys_opt["parameters_schema"]["properties"][
        "directive"
    ]["enum"]


def test_l3_source_cannot_reinject_bound_mask_version_or_hidden_semantics() -> None:
    version = "dof.mask.hide-retiming.v1"
    mask = MaskSpec.create(
        version=version,
        hidden_macro_families=("retiming",),
    )
    source = _source()
    source["state"]["measurements"]["route"]["status"] = version
    source["validation"]["warnings"] = ["macro.retime", "retiming"]
    projected = DecisionContextProjector().project(
        level=DoFLevel.L3,
        regime=UnknownRegime.FORCED_UNKNOWN,
        source=source,
        catalogue=strict_l3_catalogue(),
        mask=mask,
    )
    assert version not in projected.serialized
    assert "macro.retime" not in projected.serialized
    phys_opt = next(
        item
        for item in projected.context["action_schema"]
        if item["action_key"] == "manual.phys_opt_design"
    )
    assert "AddRetime" in phys_opt["parameters_schema"]["properties"][
        "directive"
    ]["enum"]


@pytest.mark.parametrize(
    "regime",
    [
        UnknownRegime.KNOWN,
        UnknownRegime.FORCED_UNKNOWN,
        UnknownRegime.NATURAL_UNKNOWN,
    ],
)
def test_path_and_repository_hygiene_applies_to_every_regime(
    regime: UnknownRegime,
) -> None:
    identity_token = "UNIQUE-UNKNOWN-IDENTITY"
    mask = MaskSpec.create(
        version="dof.mask.path-hygiene.v1",
        forbidden_sentinels=(identity_token,),
        known_prior_allowlist=("operator_note",),
    )
    source = _source()
    source["state"]["measurements"]["route"]["status"] = (
        "loaded /home/operator/secret/design.dcp"
    )
    source["history"][0]["actions"][0]["action_key"] = "repo/results/winner.json"
    source["history"][0]["endpoint"] = identity_token
    source["validation"]["errors"] = ["C:\\repo\\run\\place.tcl"]
    source["validation"]["warnings"] = ["file:///tmp/vivado.jou"]
    repository_evidence = [
        "inspect .research-flow/reports/result.md",
        "run.db",
        "results.sqlite",
        "evidence.parquet",
        "optimizer.py",
        "artifact.xml",
        "report.txt",
        "decision_events.jsonl",
        "report.pdf",
        "Makefile",
        "README",
        ".env",
        ".env.local",
        ".git",
        ".gitignore",
        ".dockerignore",
        ".research-flow",
        "__pycache__",
        ".pytest_cache",
        "design.sv",
        "design.vhd",
        "top.xci",
        "placed.bit",
        "project.xpr",
        "model.pkl",
        "analysis.ipynb",
        "features.npy",
        "estimator.joblib",
        "see .env,",
        "read README.",
        "open Makefile)",
        "0123456789abcdef0123456789abcdef01234567",
    ]
    source["stop_conditions"] = repository_evidence
    source["known_priors"] = {
        "operator_note": {
            "note": "operator supplied",
            "weight": 0.25,
            "source": "results/timing.rpt",
        }
    }

    projected = DecisionContextProjector().project(
        level=DoFLevel.L3,
        regime=regime,
        source=source,
        catalogue=strict_l3_catalogue(),
        mask=mask,
    )
    serialized = projected.serialized

    for leaked in (
        "/home/operator/secret/design.dcp",
        "repo/results/winner.json",
        "C:\\repo\\run\\place.tcl",
        "file:///tmp/vivado.jou",
        ".research-flow/reports/result.md",
        "results/timing.rpt",
        *repository_evidence,
    ):
        assert leaked not in serialized
    assert projected.context["current_state"]["measurements"]["route"][
        "status"
    ] == "[MASKED_PATH]"
    assert projected.context["current_state"]["measurements"]["timing"][
        "wns_ns"
    ] == -0.125
    assert projected.context["current_state"]["measurements"]["placement"][
        "status"
    ] == "placed"

    if regime is UnknownRegime.KNOWN:
        assert projected.context["history"][0]["endpoint"] == identity_token
        assert projected.context["known_priors"]["operator_note"] == {
            "note": "operator supplied",
            "weight": 0.25,
            "source": "[MASKED_PATH]",
        }
    else:
        assert identity_token not in serialized
        assert "known_priors" not in projected.context


def test_path_bearing_object_key_fails_closed_instead_of_colliding() -> None:
    path_key = "reports/winner.json"
    mask = MaskSpec.create(
        version="dof.mask.path-key.v1",
        known_prior_allowlist=(path_key,),
    )
    source = _source()
    source["known_priors"] = {path_key: "operator supplied"}

    with pytest.raises(ProjectionError, match="policy_view_path_in_object_key"):
        DecisionContextProjector().project(
            level=DoFLevel.L3,
            regime=UnknownRegime.KNOWN,
            source=source,
            catalogue=strict_l3_catalogue(),
            mask=mask,
        )


@pytest.mark.parametrize(
    "regime",
    [
        UnknownRegime.KNOWN,
        UnknownRegime.FORCED_UNKNOWN,
        UnknownRegime.NATURAL_UNKNOWN,
    ],
)
def test_bare_repository_filename_pseudonym_stays_canonical(
    regime: UnknownRegime,
) -> None:
    mask = MaskSpec.create(version="dof.mask.pseudonym-path.v1")
    source = _source()
    source["condition"]["pseudonym"] = "design.json"
    projected = DecisionContextProjector().project(
        level=DoFLevel.L3,
        regime=regime,
        source=source,
        catalogue=strict_l3_catalogue(),
        mask=mask,
    )
    pseudonym = projected.context["condition"]["pseudonym"]
    assert pseudonym != "[MASKED_PATH]"
    assert pseudonym
    assert "design.json" not in projected.serialized


@pytest.mark.parametrize(
    ("level", "sentinel"),
    [
        (DoFLevel.L2, "macro"),
        (DoFLevel.L3, "manual"),
        (DoFLevel.L2, "dof"),
    ],
)
def test_unknown_sentinel_cannot_rewrite_trusted_protocol_or_actions(
    level: DoFLevel,
    sentinel: str,
) -> None:
    mask = MaskSpec.create(
        version=f"mask.structural-{sentinel}.v1",
        forbidden_sentinels=(sentinel,),
    )
    source = _source()
    source["condition"]["pseudonym"] = f"{sentinel}-identity"
    catalogue = (
        clean_macro_catalogue(level)
        if level is DoFLevel.L2
        else strict_l3_catalogue()
    )
    with pytest.raises(ProjectionError, match="unknown_sentinel_survived"):
        DecisionContextProjector().project(
            level=level,
            regime=UnknownRegime.FORCED_UNKNOWN,
            source=source,
            catalogue=catalogue,
            mask=mask,
        )


def test_redaction_marker_collision_selects_another_canonical_marker() -> None:
    mask = MaskSpec.create(
        version="mask.marker-collision.v1",
        forbidden_sentinels=("opaque",),
    )
    source = _source()
    source["condition"]["pseudonym"] = "opaque-identity"
    catalogue = strict_l3_catalogue()
    projected = DecisionContextProjector().project(
        level=DoFLevel.L3,
        regime=UnknownRegime.FORCED_UNKNOWN,
        source=source,
        catalogue=catalogue,
        mask=mask,
    )
    assert "opaque" not in projected.serialized.lower()
    assert projected.context["schema_version"] == PROJECTED_CONTEXT_SCHEMA_VERSION
    assert projected.context["legal_actions"] == tuple(
        action.name for action in catalogue.actions
    )
    assert [item["action_key"] for item in projected.context["action_schema"]] == [
        action.name for action in catalogue.actions
    ]


def test_l2_hidden_family_semantics_are_removed_from_source_only() -> None:
    mask = MaskSpec.create(
        version="dof.mask.hide-retiming.v1",
        hidden_macro_families=("retiming",),
    )
    source = _source()
    source["history"][0]["actions"][0]["action_key"] = "macro.retime"
    source["history"][0]["endpoint"] = "retiming observed"
    source["state"]["measurements"]["route"]["status"] = "retiming"
    source["validation"]["warnings"] = ["dof.mask.hide-retiming.v1"]
    projected = DecisionContextProjector().project(
        level=DoFLevel.L2,
        regime=UnknownRegime.FORCED_UNKNOWN,
        source=source,
        catalogue=clean_macro_catalogue(DoFLevel.L2),
        mask=mask,
    )
    lowered = projected.serialized.lower()
    assert "retiming" not in lowered
    assert "macro.retime" not in lowered
    assert "retime" not in lowered
    assert "macro.phys_opt" in projected.context["legal_actions"]


@pytest.mark.parametrize("family", ["retimng", "Retiming", " retiming "])
def test_unknown_or_noncanonical_hidden_family_is_rejected(family: str) -> None:
    if family != family.strip():
        with pytest.raises(
            Exception, match="hidden_macro_families_contains_noncanonical_string"
        ):
            MaskSpec.create(
                version="mask.invalid-family.v1",
                hidden_macro_families=(family,),
            )
        return
    mask = MaskSpec.create(
        version="mask.invalid-family.v1",
        hidden_macro_families=(family,),
    )
    with pytest.raises(
        ProjectionError, match="mask.hidden_macro_families_unknown"
    ):
        DecisionContextProjector().project(
            level=DoFLevel.L2,
            regime=UnknownRegime.FORCED_UNKNOWN,
            source=_source(),
            catalogue=clean_macro_catalogue(DoFLevel.L2),
            mask=mask,
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("forbidden_sentinels", ["\t"]),
        ("known_prior_allowlist", [" padded "]),
    ],
)
def test_mask_rule_strings_must_be_nonempty_and_canonical(
    field: str,
    value: list[str],
) -> None:
    payload = MaskSpec.create(version="mask.base.v1").to_dict()
    payload[field] = value
    from optimizer_pipeline.dof.masks import mask_content_hash, mask_manifest_payload

    payload["hash"] = mask_content_hash(
        mask_manifest_payload(
            payload["version"],
            payload["hidden_macro_families"],
            payload["forbidden_sentinels"],
            payload["known_prior_allowlist"],
        )
    )
    with pytest.raises(MaskRulesValidationError):
        validate_mask_rules(payload)


def test_explicit_null_mask_manifest_schema_is_rejected() -> None:
    payload = MaskSpec.create(version="mask.null-schema.v1").to_manifest_dict()
    payload["schema_version"] = None
    with pytest.raises(
        MaskRulesValidationError, match="mask.schema_version_unsupported:None"
    ):
        validate_mask_rules(payload)


def test_projector_and_source_validator_use_only_the_bound_mask() -> None:
    signature = inspect.signature(DecisionContextProjector.project)
    assert "known_prior_allowlist" not in signature.parameters
    assert "forbidden_sentinels" not in signature.parameters

    mask = MaskSpec.create(
        version="dof.mask.single-source.v1",
        forbidden_sentinels=("BOUND-SECRET",),
        known_prior_allowlist=("declared_note",),
    )
    source = _source()
    source["known_priors"] = {"declared_note": "operator supplied"}
    observation = MockObservationSpec.from_dict(
        source,
        regime=UnknownRegime.KNOWN,
        mask=mask,
    )
    projected = DecisionContextProjector().project(
        level=DoFLevel.L3,
        regime=UnknownRegime.KNOWN,
        source=observation.to_projector_source(),
        catalogue=strict_l3_catalogue(),
        mask=mask,
    )
    assert projected.context["known_priors"] == {
        "declared_note": "operator supplied"
    }

    undeclared = _source()
    undeclared["known_priors"] = {"weaker_override": "must not pass"}
    with pytest.raises(ProjectionError, match="known_priors_not_allowlisted"):
        DecisionContextProjector().project(
            level=DoFLevel.L3,
            regime=UnknownRegime.KNOWN,
            source=undeclared,
            catalogue=strict_l3_catalogue(),
            mask=mask,
        )


def test_content_invalid_mask_cannot_cross_projection_or_evidence_boundary() -> None:
    forged = MaskSpec(
        version="dof.mask.forged.v1",
        hash="a" * 64,
        hidden_macro_families=("retiming",),
        forbidden_sentinels=(),
        known_prior_allowlist=(),
    )
    with pytest.raises(ProjectionError, match="mask.hash_content_mismatch"):
        DecisionContextProjector().project(
            level=DoFLevel.L2,
            regime=UnknownRegime.FORCED_UNKNOWN,
            source=_source(),
            catalogue=clean_macro_catalogue(DoFLevel.L2),
            mask=forged,
        )
    with pytest.raises(
        MaskRulesValidationError, match="mask.hash_content_mismatch"
    ):
        forged.to_manifest_dict()

    class BrokenMask:
        def to_dict(self) -> dict:
            raise RuntimeError("untrusted adapter failure")

    with pytest.raises(ProjectionError, match="mask.to_dict_failed:RuntimeError"):
        DecisionContextProjector().project(
            level=DoFLevel.L2,
            regime=UnknownRegime.FORCED_UNKNOWN,
            source=_source(),
            catalogue=clean_macro_catalogue(DoFLevel.L2),
            mask=BrokenMask(),
        )


def test_context_interface_versions_and_hash_bind_the_hardened_shape() -> None:
    assert PROJECTED_CONTEXT_SCHEMA_VERSION == "dof_decision_context.v2"
    assert CONTEXT_INTERFACE_SCHEMA_VERSION == "dof.context-interface-schema.v2"
    schema = context_interface_schema()
    assert schema["condition_fields"] == [
        "pseudonym",
        "regime",
        "level",
        "catalogue_version",
    ]
    assert schema["mask_metadata_model_visible"] is False
    assert schema["universal_path_repository_hygiene"] is True
    assert schema["unknown_identity_hygiene_regimes"] == [
        "forced_unknown",
        "natural_unknown",
    ]
    assert interface_fingerprints(DoFLevel.L2)["context_hash"] == (
        "54252b1e90ab19fc024ac47b2b9ece9e99467b2e979b6a5139fd53ea2c6942af"
    )


def test_serialized_context_is_canonical_json_after_redaction() -> None:
    mask = MaskSpec.create(version="dof.mask.none.v1")
    source = _source()
    source["validation"]["warnings"] = ["see scripts/run.sh"]
    projected = DecisionContextProjector().project(
        level=DoFLevel.L3,
        regime=UnknownRegime.KNOWN,
        source=source,
        catalogue=strict_l3_catalogue(),
        mask=mask,
    )
    assert json.loads(projected.serialized)["validation"]["warnings"] == [
        "[MASKED_PATH]"
    ]
    assert len(projected.context_hash) == 64
