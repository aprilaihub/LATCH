"""Common external observation and prompt-core contract for E1-v2."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from typing import Any, Mapping

from .catalogues import clean_macro_catalogue, nested_action_catalogue
from .contracts import DoFLevel, UnknownRegime
from .gate import ACTION_PROPOSAL_SCHEMA_VERSION
from .manifests import action_interface_schema_v2
from .masks import ValidatedMaskRules, create_mask_rules
from .projector import DecisionContextProjector


COMMON_BASE_SCHEMA = "dof.e1v2-common-base-observation.v1"
COMMON_PROMPT_CORE_VERSION = "dof.e1v2-common-prompt-core.v1"
CAPABILITY_APPENDIX_SCHEMA = "dof.e1v2-capability-appendix.v1"
POLICY_CONTEXT_SCHEMA = "dof.e1v2-policy-context.v1"


def _bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def _hash(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


@dataclass(frozen=True, slots=True)
class CommonPolicyContext:
    base_observation: Mapping[str, Any]
    base_observation_bytes: bytes
    base_observation_hash: str
    common_prompt_core_bytes: bytes
    common_prompt_core_hash: str
    capability_appendix: Mapping[str, Any]
    capability_appendix_bytes: bytes
    capability_appendix_hash: str
    provider_context_bytes: bytes
    provider_context_hash: str
    provider_prompt_bytes: bytes
    provider_prompt_hash: str
    decision_id: str

    def evidence(self) -> dict[str, Any]:
        return {
            "schema_version": "dof.e1v2-common-context-evidence.v1",
            "decision_id": self.decision_id,
            "base_observation": dict(self.base_observation),
            "base_observation_utf8": self.base_observation_bytes.decode("utf-8"),
            "base_observation_hash": self.base_observation_hash,
            "common_prompt_core_utf8": self.common_prompt_core_bytes.decode("utf-8"),
            "common_prompt_core_hash": self.common_prompt_core_hash,
            "capability_appendix": dict(self.capability_appendix),
            "capability_appendix_hash": self.capability_appendix_hash,
            "provider_context_hash": self.provider_context_hash,
            "provider_prompt_hash": self.provider_prompt_hash,
        }


def common_prompt_core(decision_id: str) -> bytes:
    return (
        "Return exactly one canonical JSON action-proposal object and no surrounding text. "
        "No markdown, commentary, paths, Tcl, shell, files, network access, aliases, retries, "
        "or invented fields. The envelope fields are schema_version, proposal_id, decision_id, "
        "catalogue_version, catalogue_hash, kind, and steps. Each step has step_id, action_key, "
        "and parameters. Use schema_version "
        f"{ACTION_PROPOSAL_SCHEMA_VERSION} and decision_id {decision_id}. "
        "The capability appendix supplies the exact catalogue binding and legal action schema."
    ).encode("utf-8")


def build_common_policy_context(
    *,
    level: DoFLevel | str,
    source: Mapping[str, Any],
    current_artifact_sha256: str,
    decision_index: int,
    regime: UnknownRegime | str = UnknownRegime.KNOWN,
    catalogue: Any | None = None,
    mask: ValidatedMaskRules | None = None,
) -> CommonPolicyContext:
    parsed = level if isinstance(level, DoFLevel) else DoFLevel(str(level))
    if parsed not in {DoFLevel.L2, DoFLevel.L3, DoFLevel.L4}:
        raise ValueError("common_observation_level_not_l2_l3_l4")
    parsed_regime = regime if isinstance(regime, UnknownRegime) else UnknownRegime(str(regime))
    selected_mask = mask or create_mask_rules(version="dof.e1v2-known-empty-mask.v1")
    # Reuse the extensively hardened path/identity sanitizer with one neutral
    # trusted catalogue, then remove every capability-specific field.
    neutral = DecisionContextProjector().project(
        level=DoFLevel.L2,
        regime=parsed_regime,
        source=source,
        catalogue=clean_macro_catalogue(DoFLevel.L2),
        mask=selected_mask,
    )
    context = json.loads(neutral.serialized)
    condition = context["condition"]
    # Job IDs contain level/model.  They are provenance, not external design
    # observation, so replace the two state identifiers with a neutral
    # condition/decision pseudonym before hashing the common base.
    current_state = dict(context["current_state"])
    neutral_state = f"state-{condition['pseudonym']}-d{decision_index}"
    current_state["pseudonym"] = neutral_state
    current_state["lineage_pseudonym"] = "lineage-" + condition["pseudonym"]
    base: dict[str, Any] = {
        "schema_version": COMMON_BASE_SCHEMA,
        "condition": {
            "pseudonym": condition["pseudonym"],
            "regime": condition["regime"],
        },
        "current_state": current_state,
        "history": context["history"],
        "budget_remaining": context["budget_remaining"],
        "validation": context["validation"],
        "stop_conditions": context["stop_conditions"],
    }
    if "known_priors" in context:
        base["known_priors"] = context["known_priors"]
    base_raw = _bytes(base)
    decision_id = "decision-" + hashlib.sha256(
        _bytes(
            {
                "base_observation_hash": _hash(base_raw),
                "current_artifact_sha256": current_artifact_sha256,
                "decision_index": decision_index,
            }
        )
    ).hexdigest()[:20]
    selected_catalogue = catalogue or nested_action_catalogue(parsed)
    action_schema = [
        {
            "action_key": action.name,
            "parameters_schema": action.to_dict()["parameters_schema"],
        }
        for action in selected_catalogue.actions
    ]
    appendix = {
        "schema_version": CAPABILITY_APPENDIX_SCHEMA,
        "level": parsed.value,
        "catalogue_version": selected_catalogue.version,
        "catalogue_hash": selected_catalogue.sha256,
        "allowed_kinds": list(action_interface_schema_v2(parsed)["allowed_kinds"]),
        "legal_actions": [row["action_key"] for row in action_schema],
        "action_schema": action_schema,
    }
    appendix_raw = _bytes(appendix)
    core = common_prompt_core(decision_id)
    provider_context = _bytes(
        {
            "schema_version": POLICY_CONTEXT_SCHEMA,
            "base_observation": base,
            "capability_appendix": appendix,
        }
    )
    provider_prompt = core + b"\nCAPABILITY_APPENDIX=" + appendix_raw
    return CommonPolicyContext(
        base,
        base_raw,
        _hash(base_raw),
        core,
        _hash(core),
        appendix,
        appendix_raw,
        _hash(appendix_raw),
        provider_context,
        _hash(provider_context),
        provider_prompt,
        _hash(provider_prompt),
        decision_id,
    )


def common_observation_equality_audit(
    *, source: Mapping[str, Any], current_artifact_sha256: str, decision_index: int
) -> dict[str, Any]:
    contexts = {
        level.value: build_common_policy_context(
            level=level,
            source=source,
            current_artifact_sha256=current_artifact_sha256,
            decision_index=decision_index,
        )
        for level in (DoFLevel.L2, DoFLevel.L3, DoFLevel.L4)
    }
    base_hashes = {level: item.base_observation_hash for level, item in contexts.items()}
    core_hashes = {level: item.common_prompt_core_hash for level, item in contexts.items()}
    accepted = len(set(base_hashes.values())) == 1 and len(set(core_hashes.values())) == 1
    payload = {
        "schema_version": "dof.e1v2-observation-equality-audit.v1",
        "base_observation_hashes": base_hashes,
        "common_prompt_core_hashes": core_hashes,
        "capability_appendix_hashes": {
            level: item.capability_appendix_hash for level, item in contexts.items()
        },
        "base_bytes_equal": len(
            {item.base_observation_bytes for item in contexts.values()}
        )
        == 1,
        "common_prompt_core_bytes_equal": len(
            {item.common_prompt_core_bytes for item in contexts.values()}
        )
        == 1,
        "capability_appendices_distinct": len(
            {item.capability_appendix_bytes for item in contexts.values()}
        )
        == 3,
        "accepted": accepted,
    }
    return payload | {"audit_sha256": _hash(_bytes(payload))}


__all__ = [
    "CAPABILITY_APPENDIX_SCHEMA",
    "COMMON_BASE_SCHEMA",
    "COMMON_PROMPT_CORE_VERSION",
    "CommonPolicyContext",
    "build_common_policy_context",
    "common_observation_equality_audit",
    "common_prompt_core",
]
