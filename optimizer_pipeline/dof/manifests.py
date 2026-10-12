"""Content-addressed level and model-interface manifests for WP-002.

The manifest payloads in this module are intentionally independent of the
runtime implementation.  Experiment specs bind to these bytes, and the runner
recomputes the same digests before it opens a dispatch surface.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from typing import Any

from .contracts import DoFLevel, LevelStatus
from .gate import ACTION_PROPOSAL_SCHEMA_VERSION, DEFAULT_MAX_PLAN_STEPS
from .projector import PROJECTED_CONTEXT_SCHEMA_VERSION


CAPABILITY_MANIFEST_SCHEMA_VERSION = "dof.capability-manifest.v1"
CAPABILITY_VERSION = "dof.level-capability.v1"
ACTION_INTERFACE_SCHEMA_VERSION = "dof.action-interface-schema.v2"
CONTEXT_INTERFACE_SCHEMA_VERSION = "dof.context-interface-schema.v2"


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def content_hash(value: Any) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


@dataclass(frozen=True, slots=True)
class CapabilityManifest:
    level: DoFLevel
    status: LevelStatus
    decision_actor: str
    action_surface: str
    policy_kinds: tuple[str, ...]
    mutating_actions_allowed: bool
    llm_requests_allowed: bool
    common_gate_required: bool
    version: str = CAPABILITY_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": CAPABILITY_MANIFEST_SCHEMA_VERSION,
            "version": self.version,
            "level": self.level.value,
            "status": self.status.value,
            "decision_actor": self.decision_actor,
            "action_surface": self.action_surface,
            "policy_kinds": list(self.policy_kinds),
            "mutating_actions_allowed": self.mutating_actions_allowed,
            "llm_requests_allowed": self.llm_requests_allowed,
            "common_gate_required": self.common_gate_required,
            "fallback_level": None,
        }

    @property
    def sha256(self) -> str:
        return content_hash(self.to_dict())


def capability_manifest(level: DoFLevel | str) -> CapabilityManifest:
    parsed = level if isinstance(level, DoFLevel) else DoFLevel(str(level))
    values = {
        DoFLevel.L0: CapabilityManifest(
            parsed, LevelStatus.ACTIVE, "none", "none", (), False, False, False
        ),
        DoFLevel.L1: CapabilityManifest(
            parsed,
            LevelStatus.ACTIVE,
            "deterministic_schedule",
            "clean_named_macros",
            ("PLAN",),
            True,
            False,
            True,
        ),
        DoFLevel.L2: CapabilityManifest(
            parsed,
            LevelStatus.ACTIVE,
            "catalogue_agent",
            "clean_named_macros",
            ("ACTION", "STOP"),
            True,
            True,
            True,
        ),
        DoFLevel.L3: CapabilityManifest(
            parsed,
            LevelStatus.ACTIVE,
            "strict_primitive_agent",
            "strict_typed_manual_primitives",
            ("ACTION", "PLAN", "STOP"),
            True,
            True,
            True,
        ),
        DoFLevel.L4: CapabilityManifest(
            parsed,
            LevelStatus.REFERENCE_PENDING,
            "unrestricted_agent_pending",
            "unavailable",
            (),
            False,
            False,
            False,
        ),
    }
    return values[parsed]


def capability_manifest_v2(level: DoFLevel | str) -> CapabilityManifest:
    """Protocol E1-v2 nested capability declaration."""

    parsed = level if isinstance(level, DoFLevel) else DoFLevel(str(level))
    values = {
        DoFLevel.L0: capability_manifest(DoFLevel.L0),
        DoFLevel.L1: capability_manifest(DoFLevel.L1),
        DoFLevel.L2: CapabilityManifest(
            parsed,
            LevelStatus.ACTIVE,
            "catalogue_agent",
            "M",
            ("ACTION", "STOP"),
            True,
            True,
            True,
            version="dof.level-capability.e1v2.v1",
        ),
        DoFLevel.L3: CapabilityManifest(
            parsed,
            LevelStatus.ACTIVE,
            "catalogue_agent",
            "M_PLUS_P",
            ("ACTION", "PLAN", "STOP"),
            True,
            True,
            True,
            version="dof.level-capability.e1v2.v1",
        ),
        DoFLevel.L4: CapabilityManifest(
            parsed,
            LevelStatus.ACTIVE,
            "generated_intervention_agent",
            "M_PLUS_P_PLUS_G",
            ("ACTION", "PLAN", "STOP"),
            True,
            True,
            True,
            version="dof.level-capability.e1v2.v1",
        ),
    }
    return values[parsed]


def action_interface_schema_v2(level: DoFLevel | str) -> dict[str, Any]:
    parsed = level if isinstance(level, DoFLevel) else DoFLevel(str(level))
    kinds = list(capability_manifest_v2(parsed).policy_kinds)
    cardinalities = {
        "STOP": {"min_steps": 0, "max_steps": 0},
        "ACTION": {"min_steps": 1, "max_steps": 1},
        "PLAN": {"min_steps": 1, "max_steps": DEFAULT_MAX_PLAN_STEPS},
    }
    return {
        "schema_version": "dof.action-interface-schema.e1v2.v1",
        "proposal_schema_version": ACTION_PROPOSAL_SCHEMA_VERSION,
        "level": parsed.value,
        "allowed_kinds": kinds,
        "required_envelope_fields": action_interface_schema(parsed)[
            "required_envelope_fields"
        ],
        "required_step_fields": action_interface_schema(parsed)[
            "required_step_fields"
        ],
        "decision_id_must_match_active_decision": True,
        "proposal_revalidation": "canonical_strict_parser",
        "max_plan_steps": DEFAULT_MAX_PLAN_STEPS,
        "kind_step_cardinality": {kind: cardinalities[kind] for kind in kinds},
        "additional_properties": False,
    }


def interface_fingerprints_v2(level: DoFLevel | str) -> dict[str, str]:
    parsed = level if isinstance(level, DoFLevel) else DoFLevel(str(level))
    if parsed not in {DoFLevel.L2, DoFLevel.L3, DoFLevel.L4}:
        return interface_fingerprints(parsed)
    action = action_interface_schema_v2(parsed)
    return {
        "prompt_version": "dof.e1v2-common-core-plus-capability.v1",
        "action_schema_version": action["schema_version"],
        "action_schema_hash": content_hash(action),
        "context_version": "dof.e1v2-policy-context.v1",
        "context_hash": content_hash(
            {
                "base": "dof.e1v2-common-base-observation.v1",
                "capability": action["schema_version"],
            }
        ),
    }


_PROMPTS = {
    DoFLevel.L2: (
        "Select exactly one action_key from legal_actions, or STOP. "
        "Return only the strict action-proposal JSON envelope. Do not emit "
        "typed primitives, raw tool calls, paths, aliases, or plans."
    ),
    DoFLevel.L3: (
        "Select a stage-legal strict typed primitive ACTION, a bounded PLAN of "
        "strict primitives, or STOP. Return only the strict action-proposal "
        "JSON envelope. Raw Tcl, shell, files, flow aliases, and meta-actions "
        "are forbidden."
    ),
}


def prompt_text(level: DoFLevel | str) -> str:
    parsed = level if isinstance(level, DoFLevel) else DoFLevel(str(level))
    return _PROMPTS.get(parsed, "")


def action_interface_schema(level: DoFLevel | str) -> dict[str, Any]:
    parsed = level if isinstance(level, DoFLevel) else DoFLevel(str(level))
    kinds = list(capability_manifest(parsed).policy_kinds)
    cardinalities = {
        "STOP": {"min_steps": 0, "max_steps": 0},
        "ACTION": {"min_steps": 1, "max_steps": 1},
        "PLAN": {"min_steps": 1, "max_steps": DEFAULT_MAX_PLAN_STEPS},
    }
    return {
        "schema_version": ACTION_INTERFACE_SCHEMA_VERSION,
        "proposal_schema_version": ACTION_PROPOSAL_SCHEMA_VERSION,
        "level": parsed.value,
        "allowed_kinds": kinds,
        "required_envelope_fields": [
            "schema_version",
            "proposal_id",
            "decision_id",
            "catalogue_version",
            "catalogue_hash",
            "kind",
            "steps",
        ],
        "required_step_fields": ["step_id", "action_key", "parameters"],
        "decision_id_must_match_active_decision": True,
        "proposal_revalidation": "canonical_strict_parser",
        "max_plan_steps": DEFAULT_MAX_PLAN_STEPS,
        "kind_step_cardinality": {
            kind: cardinalities[kind] for kind in kinds
        },
        "additional_properties": False,
    }


def context_interface_schema() -> dict[str, Any]:
    """Document the closed projector output without exposing runtime data."""

    return {
        "schema_version": CONTEXT_INTERFACE_SCHEMA_VERSION,
        "projected_context_schema_version": PROJECTED_CONTEXT_SCHEMA_VERSION,
        "top_level_fields": [
            "schema_version",
            "condition",
            "current_state",
            "legal_actions",
            "action_schema",
            "history",
            "budget_remaining",
            "validation",
            "stop_conditions",
            "known_priors",
        ],
        "condition_fields": [
            "pseudonym",
            "regime",
            "level",
            "catalogue_version",
        ],
        "known_priors_optional_and_allowlisted": True,
        "mask_metadata_model_visible": False,
        "universal_path_repository_hygiene": True,
        "unknown_identity_hygiene_regimes": [
            "forced_unknown",
            "natural_unknown",
        ],
        "additional_properties": False,
    }


def interface_fingerprints(level: DoFLevel | str) -> dict[str, str]:
    parsed = level if isinstance(level, DoFLevel) else DoFLevel(str(level))
    if parsed not in {DoFLevel.L2, DoFLevel.L3}:
        return {
            "prompt_version": "NOT_RUN",
            "prompt_hash": "NOT_RUN",
            "action_schema_version": "NOT_RUN",
            "action_schema_hash": "NOT_RUN",
            "context_version": "NOT_RUN",
            "context_hash": "NOT_RUN",
        }
    return {
        "prompt_version": f"dof.{parsed.value.lower()}-prompt.v1",
        "prompt_hash": hashlib.sha256(prompt_text(parsed).encode("utf-8")).hexdigest(),
        "action_schema_version": ACTION_INTERFACE_SCHEMA_VERSION,
        "action_schema_hash": content_hash(action_interface_schema(parsed)),
        "context_version": CONTEXT_INTERFACE_SCHEMA_VERSION,
        "context_hash": content_hash(context_interface_schema()),
    }
