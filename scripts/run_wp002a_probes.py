#!/usr/bin/env python3
"""Run the offline independent-style WP-002A boundary probes.

This command only constructs in-memory mock catalogues, contexts, and gate
decisions.  It never starts a backend, MCP server, external model, or real DCP
flow.  A non-zero exit means at least one corrected boundary is open again.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from optimizer_pipeline.dof.catalogues import (  # noqa: E402
    clean_macro_catalogue,
    strict_l3_catalogue,
)
from optimizer_pipeline.dof.contracts import (  # noqa: E402
    DesignStage,
    DoFLevel,
    EventClass,
    UnknownRegime,
)
from optimizer_pipeline.dof.gate import (  # noqa: E402
    ACTION_PROPOSAL_SCHEMA_VERSION,
    ActionProposal,
    ActionStepProposal,
    ExperimentActionGate,
    ProposalKind,
)
from optimizer_pipeline.dof.manifests import (  # noqa: E402
    ACTION_INTERFACE_SCHEMA_VERSION,
    action_interface_schema,
)
from optimizer_pipeline.dof.projector import (  # noqa: E402
    DecisionContextProjector,
    ProjectionError,
)
from optimizer_pipeline.dof.spec import MaskSpec  # noqa: E402


SCHEMA_VERSION = "research-flow.wp002a-corrected-probes.v2"
DECISION_ID = "decision-current"


def _source() -> dict[str, Any]:
    return {
        "condition": {"pseudonym": "condition-probe"},
        "state": {
            "pseudonym": "state-probe",
            "lineage_pseudonym": "lineage-probe",
            "stage": "ROUTED",
            "measurements": {
                "timing": {
                    "wns_ns": -0.2,
                    "tns_ns": -1.0,
                    "failing_endpoints": 2,
                    "clock_period_ns": 2.5,
                    "estimated_fmax_mhz": 380.0,
                },
                "route": {"status": "routed", "unrouted_nets": 0},
                "placement": {"status": "placed", "placed_cells": 100},
                "utilization": {"lut": 100, "ff": 200, "dsp": 4, "bram": 2},
                "congestion": {"level": "low", "hotspots": 0},
            },
        },
        "history": [],
        "budget_remaining": {
            event.value: 20 for event in EventClass
        }
        | {"WALL_TIME_SECONDS": 60.0},
        "validation": {"status": "CLEAN", "errors": [], "warnings": []},
        "stop_conditions": ["declared budget exhausted"],
        "known_priors": {},
    }


def _catalogue(level: DoFLevel):
    if level in {DoFLevel.L1, DoFLevel.L2}:
        return clean_macro_catalogue(level)
    return strict_l3_catalogue()


def _typed_proposal(
    level: DoFLevel,
    kind: ProposalKind,
    steps: tuple[ActionStepProposal, ...],
    *,
    decision_id: str = DECISION_ID,
) -> ActionProposal:
    catalogue = _catalogue(level)
    return ActionProposal(
        schema_version=ACTION_PROPOSAL_SCHEMA_VERSION,
        proposal_id=f"proposal-{level.value.lower()}",
        decision_id=decision_id,
        catalogue_version=catalogue.version,
        catalogue_hash=catalogue.sha256,
        kind=kind,
        steps=steps,
    )


def _gate(
    level: DoFLevel,
    proposal: ActionProposal,
    *,
    mask: MaskSpec | None = None,
) -> dict[str, Any]:
    catalogue = _catalogue(level)
    decision = ExperimentActionGate().evaluate(
        proposal,
        level=level,
        expected_decision_id=DECISION_ID,
        catalogue=catalogue,
        expected_catalogue_hash=catalogue.sha256,
        current_stage=DesignStage.ROUTED,
        budget_remaining={event: 20 for event in EventClass},
        mask=mask or MaskSpec.create(version="dof.mask.probe-none.v1"),
    )
    return {
        "accepted": decision.accepted,
        "endpoint": decision.endpoint.value if decision.endpoint else None,
        "reasons": list(decision.reasons),
        "gated_steps": len(decision.gated_steps),
    }


def run_probes() -> dict[str, Any]:
    l2_catalogue = clean_macro_catalogue(DoFLevel.L2)
    semantic_mask = MaskSpec.create(
        version="dof.mask.hide-retiming.v1",
        hidden_macro_families=("retiming",),
        forbidden_sentinels=("secret-benchmark",),
    )
    forced_source = _source()
    forced_source["condition"]["pseudonym"] = "secret-benchmark"
    forced_source["state"]["pseudonym"] = "secret-benchmark-state"
    forced_source["history"] = [
        {
            "decision_index": 0,
            "actions": [{"action_key": "macro.retime", "parameters": {}}],
            "parent_qor": {"fmax_mhz": 379.0, "wns_ns": -0.135},
            "child_qor": {"fmax_mhz": 380.0, "wns_ns": -0.125},
            "delta_qor": {"fmax_mhz": 1.0, "wns_ns": 0.01},
            "endpoint": "retiming observed",
            "valid": True,
            "actual_heavy_calls": 1,
        }
    ]
    forced_source["validation"]["warnings"] = [
        "dof.mask.hide-retiming.v1"
    ]
    forced = DecisionContextProjector().project(
        level=DoFLevel.L2,
        regime=UnknownRegime.FORCED_UNKNOWN,
        source=forced_source,
        catalogue=l2_catalogue,
        mask=semantic_mask,
    )
    semantic_visible = any(
        token in forced.serialized.lower()
        for token in ("hide-retiming", "retiming", "macro.retime", "retime")
    )
    forced_pseudonym_invalid = any(
        not value or value.startswith("-") or "secret-benchmark" in value.lower()
        for value in (
            forced.context["condition"]["pseudonym"],
            forced.context["current_state"]["pseudonym"],
            forced.context["current_state"]["lineage_pseudonym"],
        )
    )

    synthetic_token = "UNIQUE-WP002A-MASK-SECRET"
    synthetic_mask = MaskSpec.create(
        version=f"dof.mask.synthetic.{synthetic_token}.v1",
        hidden_macro_families=("retiming",),
        forbidden_sentinels=("synthetic-hidden-identity",),
    )
    synthetic = DecisionContextProjector().project(
        level=DoFLevel.L2,
        regime=UnknownRegime.FORCED_UNKNOWN,
        source=_source(),
        catalogue=l2_catalogue,
        mask=synthetic_mask,
    )
    synthetic_token_visible = synthetic_token in synthetic.serialized

    known_source = _source()
    known_source["state"]["measurements"]["route"]["status"] = (
        "/tmp/secret/design.dcp"
    )
    known_source["validation"]["warnings"] = ["repo/results/winner.json"]
    repository_tokens = (
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
    )
    known_source["stop_conditions"] = list(repository_tokens)
    known_source["known_priors"] = {
        "operator_note": "read .research-flow/reports/result.md"
    }
    known = DecisionContextProjector().project(
        level=DoFLevel.L3,
        regime=UnknownRegime.KNOWN,
        source=known_source,
        catalogue=strict_l3_catalogue(),
        mask=MaskSpec.create(
            version="dof.mask.probe-known.v1",
            known_prior_allowlist=("operator_note",),
        ),
    )
    path_tokens = (
        "/tmp/secret/design.dcp",
        "repo/results/winner.json",
        ".research-flow/reports/result.md",
        *repository_tokens,
    )
    known_path_visible = any(token in known.serialized for token in path_tokens)

    l1_action = _gate(
        DoFLevel.L1,
        _typed_proposal(
            DoFLevel.L1,
            ProposalKind.ACTION,
            (ActionStepProposal("macro", "macro.phys_opt", {}),),
        ),
    )
    l2_multi = _gate(
        DoFLevel.L2,
        _typed_proposal(
            DoFLevel.L2,
            ProposalKind.ACTION,
            (
                ActionStepProposal("macro-1", "macro.phys_opt", {}),
                ActionStepProposal("macro-2", "macro.phys_opt", {}),
            ),
        ),
    )
    l3_overlong = _gate(
        DoFLevel.L3,
        _typed_proposal(
            DoFLevel.L3,
            ProposalKind.PLAN,
            tuple(
                ActionStepProposal(
                    f"step-{index}",
                    "manual.phys_opt_design",
                    {"directive": "Explore"},
                )
                for index in range(9)
            ),
        ),
    )
    wrong_decision = _gate(
        DoFLevel.L2,
        _typed_proposal(
            DoFLevel.L2,
            ProposalKind.ACTION,
            (ActionStepProposal("macro", "macro.phys_opt", {}),),
            decision_id="decision-stale",
        ),
    )

    typo_mask = MaskSpec.create(
        version="dof.mask.typo-family.v1",
        hidden_macro_families=("retimng",),
    )
    typo_family = _gate(
        DoFLevel.L2,
        _typed_proposal(
            DoFLevel.L2,
            ProposalKind.ACTION,
            (ActionStepProposal("macro", "macro.phys_opt", {}),),
        ),
        mask=typo_mask,
    )

    try:
        ExperimentActionGate(max_plan_steps=7)
    except ValueError:
        narrower_gate_accepted = False
    else:  # pragma: no cover - regression probe
        narrower_gate_accepted = True

    structural_mask = MaskSpec.create(
        version="dof.mask.structural-collision.v1",
        forbidden_sentinels=("dof",),
    )
    structural_source = _source()
    structural_source["condition"]["pseudonym"] = "dof-identity"
    try:
        DecisionContextProjector().project(
            level=DoFLevel.L2,
            regime=UnknownRegime.FORCED_UNKNOWN,
            source=structural_source,
            catalogue=l2_catalogue,
            mask=structural_mask,
        )
    except ProjectionError:
        structural_mask_collision_accepted = False
    else:  # pragma: no cover - regression probe
        structural_mask_collision_accepted = True

    l2_action_schema = action_interface_schema(DoFLevel.L2)
    action_interface_v2_missing = not (
        ACTION_INTERFACE_SCHEMA_VERSION == "dof.action-interface-schema.v2"
        and l2_action_schema["decision_id_must_match_active_decision"] is True
        and l2_action_schema["max_plan_steps"] == 8
        and l2_action_schema["kind_step_cardinality"]["ACTION"]
        == {"min_steps": 1, "max_steps": 1}
        and l2_action_schema["kind_step_cardinality"]["STOP"]
        == {"min_steps": 0, "max_steps": 0}
    )

    checks = {
        "forced_unknown_semantic_mask_token_visible": semantic_visible,
        "forced_unknown_pseudonym_invalid": forced_pseudonym_invalid,
        "synthetic_mask_secret_token_visible": synthetic_token_visible,
        "known_mode_absolute_or_repository_path_visible": known_path_visible,
        "l1_typed_action_accepted": l1_action["accepted"],
        "l2_typed_multi_step_action_accepted": l2_multi["accepted"],
        "l3_typed_overlong_plan_accepted": l3_overlong["accepted"],
        "wrong_decision_id_accepted": wrong_decision["accepted"],
        "unknown_hidden_family_accepted": typo_family["accepted"],
        "narrower_gate_than_manifest_accepted": narrower_gate_accepted,
        "structural_mask_collision_accepted": structural_mask_collision_accepted,
        "action_interface_v2_binding_missing": action_interface_v2_missing,
    }
    details = {
        "l1_typed_action": l1_action,
        "l2_typed_multi_step_action": l2_multi,
        "l3_typed_overlong_plan": l3_overlong,
        "wrong_decision_id": wrong_decision,
        "unknown_hidden_family": typo_family,
        "forced_unknown_legal_actions": list(forced.context["legal_actions"]),
        "synthetic_mask_manifest": synthetic_mask.to_manifest_dict(),
        "known_route_status": known.context["current_state"]["measurements"][
            "route"
        ]["status"],
    }
    no_dispatch = all(
        details[name]["gated_steps"] == 0
        for name in (
            "l1_typed_action",
            "l2_typed_multi_step_action",
            "l3_typed_overlong_plan",
            "wrong_decision_id",
            "unknown_hidden_family",
        )
    )
    result = {
        "schema_version": SCHEMA_VERSION,
        "work_package": "WP-002A",
        "execution_mode": "offline_mock_boundary_probe",
        "checks": checks,
        "details": details,
        "all_corrected": not any(checks.values()) and no_dispatch,
        "all_rejections_have_zero_gated_steps": no_dispatch,
        "execution_boundary": {
            "real_dcp_attempts": 0,
            "vivado_starts": 0,
            "rapidwright_starts": 0,
            "mcp_starts": 0,
            "external_model_requests": 0,
            "l4_execution": False,
            "schema_migration": False,
        },
    }
    if not result["all_corrected"]:
        raise AssertionError(json.dumps(result, sort_keys=True))
    return result


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run offline WP-002A boundary probes."
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="Optional JSON output path; parent directory must already exist.",
    )
    args = parser.parse_args()
    result = run_probes()
    rendered = json.dumps(result, indent=2, sort_keys=True) + "\n"
    if args.output is not None:
        args.output.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
