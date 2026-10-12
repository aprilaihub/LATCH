"""Fail-closed proposal parsing and execution gating for DoF experiments.

This module is deliberately independent of the legacy contest dispatch surface.
It turns untrusted policy JSON into a small typed proposal, binds that proposal
to one immutable catalogue, and returns dispatchable steps only after every
capability, mask, stage, and declared-event budget check succeeds.

The returned :class:`GatedStep` is evidence for the mock runner; it is not a
claim that the legacy Vivado backend already exposes equivalent call-level
accounting.  That binding belongs to the real-tool instrumentation phase.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
import hashlib
import json
import math
import re
from types import MappingProxyType
from typing import Any, Iterable, Mapping, Sequence

from .contracts import DesignStage, DoFLevel, EndpointCode, EventClass
from .masks import MaskRulesValidationError, validate_mask_rules


ACTION_PROPOSAL_SCHEMA_VERSION = "action_proposal.v1"
DEFAULT_MAX_PLAN_STEPS = 8

_PROPOSAL_FIELDS = frozenset(
    {
        "schema_version",
        "proposal_id",
        "decision_id",
        "catalogue_version",
        "catalogue_hash",
        "kind",
        "steps",
    }
)
_STEP_FIELDS = frozenset({"step_id", "action_key", "parameters"})
_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_PATH_VALUE_PATTERN = re.compile(
    r"(?:^|\s)(?:/|~[/\\]|[A-Za-z]:[/\\]|file://)|"
    r"(?:^|[/\\])[^\s/\\]+\.(?:dcp|tcl|sh|bash|json|csv|yaml|yml)(?:$|\s)",
    flags=re.IGNORECASE,
)
_FORBIDDEN_PARAMETER_PARTS = (
    "command",
    "tcl",
    "shell",
    "script",
    "path",
    "file",
    "checkpoint",
    "dcp",
)
_FORBIDDEN_RAW_ACTIONS = frozenset(
    {
        "run_tcl",
        "raw_tcl",
        "shell",
        "bash",
        "sh",
        "write_checkpoint",
    }
)
_FORBIDDEN_META_OR_ALIASES = frozenset(
    {
        "auto_top3",
        "pblock",
        "auto_pblock",
        "vexriscv",
        "retime",
        "phys_opt",
        "rescue_place_reset_retime",
        "synthesized_recipe",
        "manual_action",
    }
)

_STRICT_L3_DIRECTIVES: dict[str, tuple[str, ...]] = {
    "manual.unroute_all": (),
    "manual.unplace_all": (),
    "manual.place_design": ("Default", "Explore"),
    "manual.route_design": ("Default", "Explore"),
    "manual.phys_opt_design": (
        "Explore",
        "AddRetime",
        "AlternateFlowWithRetiming",
    ),
}
_STRICT_L3_STAGES: dict[str, frozenset[DesignStage]] = {
    "manual.unroute_all": frozenset({DesignStage.ROUTED}),
    "manual.unplace_all": frozenset({DesignStage.PLACED}),
    "manual.place_design": frozenset({DesignStage.UNPLACED}),
    "manual.route_design": frozenset({DesignStage.PLACED}),
    "manual.phys_opt_design": frozenset({DesignStage.PLACED, DesignStage.ROUTED}),
}
_STRICT_L3_EVENT: dict[str, EventClass] = {
    "manual.unroute_all": EventClass.MUTATION_LIGHT,
    "manual.unplace_all": EventClass.MUTATION_LIGHT,
    "manual.place_design": EventClass.IMPLEMENTATION_HEAVY,
    "manual.route_design": EventClass.IMPLEMENTATION_HEAVY,
    "manual.phys_opt_design": EventClass.IMPLEMENTATION_HEAVY,
}


class ProposalKind(str, Enum):
    STOP = "STOP"
    ACTION = "ACTION"
    PLAN = "PLAN"


class ProposalParseError(ValueError):
    """One or more fail-closed proposal-schema violations."""

    def __init__(self, reasons: Iterable[str]) -> None:
        self.reasons = tuple(str(reason) for reason in reasons)
        super().__init__("; ".join(self.reasons))


@dataclass(frozen=True, slots=True)
class ActionStepProposal:
    step_id: str
    action_key: str
    parameters: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "step_id": self.step_id,
            "action_key": self.action_key,
            "parameters": _thaw_json(self.parameters),
        }


@dataclass(frozen=True, slots=True)
class ActionProposal:
    schema_version: str
    proposal_id: str
    decision_id: str
    catalogue_version: str
    catalogue_hash: str
    kind: ProposalKind
    steps: tuple[ActionStepProposal, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "proposal_id": self.proposal_id,
            "decision_id": self.decision_id,
            "catalogue_version": self.catalogue_version,
            "catalogue_hash": self.catalogue_hash,
            "kind": self.kind.value,
            "steps": [step.to_dict() for step in self.steps],
        }

    def canonical_bytes(self) -> bytes:
        return _canonical_bytes(self.to_dict())

    @property
    def proposal_hash(self) -> str:
        return hashlib.sha256(self.canonical_bytes()).hexdigest()


@dataclass(frozen=True, slots=True)
class GatedEvent:
    event_class: EventClass
    operation: str
    units: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_class": self.event_class.value,
            "operation": self.operation,
            "units": self.units,
        }


@dataclass(frozen=True, slots=True)
class GatedStep:
    proposal_id: str
    proposal_hash: str
    catalogue_hash: str
    step_index: int
    step_id: str
    action_key: str
    family: str
    parameters: Mapping[str, Any]
    stage_before: DesignStage | None
    stage_after: DesignStage | None
    events: tuple[GatedEvent, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "proposal_id": self.proposal_id,
            "proposal_hash": self.proposal_hash,
            "catalogue_hash": self.catalogue_hash,
            "step_index": self.step_index,
            "step_id": self.step_id,
            "action_key": self.action_key,
            "family": self.family,
            "parameters": _thaw_json(self.parameters),
            "stage_before": self.stage_before.value if self.stage_before else None,
            "stage_after": self.stage_after.value if self.stage_after else None,
            "events": [event.to_dict() for event in self.events],
        }


@dataclass(frozen=True, slots=True)
class GateDecision:
    accepted: bool
    endpoint: EndpointCode | None
    reasons: tuple[str, ...]
    proposal: ActionProposal | None = None
    gated_steps: tuple[GatedStep, ...] = ()
    required_units: Mapping[str, int] = field(default_factory=dict)
    final_stage: DesignStage | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "accepted": self.accepted,
            "endpoint": self.endpoint.value if self.endpoint else None,
            "reasons": list(self.reasons),
            "proposal": self.proposal.to_dict() if self.proposal else None,
            "gated_steps": [step.to_dict() for step in self.gated_steps],
            "required_units": dict(self.required_units),
            "final_stage": self.final_stage.value if self.final_stage else None,
            "fallback_used": False,
        }


def parse_action_proposal(
    payload: Mapping[str, Any],
    *,
    max_plan_steps: int = DEFAULT_MAX_PLAN_STEPS,
) -> ActionProposal:
    """Parse the only accepted policy-to-gate JSON shape.

    No legacy aliases (``name``, ``action``, ``primitive`` or ``args``) are
    accepted.  Unknown fields are errors, including fields that would otherwise
    be harmless, because permissive parsing makes capability audits ambiguous.
    """

    if not isinstance(payload, Mapping):
        raise ProposalParseError(("payload_not_object",))
    max_steps = int(max_plan_steps)
    if max_steps <= 0:
        raise ValueError("max_plan_steps must be positive")
    if max_steps > DEFAULT_MAX_PLAN_STEPS:
        raise ValueError(
            "max_plan_steps cannot exceed the declared action-interface maximum "
            f"of {DEFAULT_MAX_PLAN_STEPS}"
        )

    reasons: list[str] = []
    raw_keys = tuple(payload)
    if not all(isinstance(key, str) for key in raw_keys):
        reasons.append("payload_has_non_string_key")
    keys = {key for key in raw_keys if isinstance(key, str)}
    unknown = sorted(keys - _PROPOSAL_FIELDS)
    missing = sorted(_PROPOSAL_FIELDS - keys)
    if unknown:
        reasons.append("unknown_fields:" + ",".join(unknown))
    if missing:
        reasons.append("missing_fields:" + ",".join(missing))
    if reasons:
        raise ProposalParseError(reasons)

    schema_version = payload.get("schema_version")
    if schema_version != ACTION_PROPOSAL_SCHEMA_VERSION:
        reasons.append(f"unsupported_schema_version:{schema_version}")
    proposal_id = _parse_id(payload.get("proposal_id"), "proposal_id", reasons)
    decision_id = _parse_id(payload.get("decision_id"), "decision_id", reasons)
    catalogue_version = _parse_id(
        payload.get("catalogue_version"), "catalogue_version", reasons
    )
    catalogue_hash = payload.get("catalogue_hash")
    if not isinstance(catalogue_hash, str) or not _SHA256_PATTERN.fullmatch(
        catalogue_hash
    ):
        reasons.append("catalogue_hash_not_sha256")
        catalogue_hash = ""
    try:
        kind = ProposalKind(payload.get("kind"))
    except (TypeError, ValueError):
        reasons.append(f"unknown_proposal_kind:{payload.get('kind')}")
        kind = ProposalKind.STOP

    raw_steps = payload.get("steps")
    if not isinstance(raw_steps, list):
        reasons.append("steps_not_array")
        raw_steps = []

    steps: list[ActionStepProposal] = []
    for index, raw_step in enumerate(raw_steps):
        prefix = f"step_{index}"
        if not isinstance(raw_step, Mapping):
            reasons.append(f"{prefix}_not_object")
            continue
        raw_step_keys = tuple(raw_step)
        if not all(isinstance(key, str) for key in raw_step_keys):
            reasons.append(f"{prefix}_has_non_string_key")
        step_keys = {
            key for key in raw_step_keys if isinstance(key, str)
        }
        unknown_step = sorted(step_keys - _STEP_FIELDS)
        missing_step = sorted(_STEP_FIELDS - step_keys)
        if unknown_step:
            reasons.append(f"{prefix}_unknown_fields:" + ",".join(unknown_step))
        if missing_step:
            reasons.append(f"{prefix}_missing_fields:" + ",".join(missing_step))
        step_id = _parse_id(raw_step.get("step_id"), f"{prefix}_step_id", reasons)
        action_key = raw_step.get("action_key")
        if not isinstance(action_key, str) or not action_key.strip():
            reasons.append(f"{prefix}_action_key_not_nonempty_string")
            action_key = ""
        elif action_key != action_key.strip() or len(action_key) > 128:
            reasons.append(f"{prefix}_action_key_not_canonical")
            action_key = action_key.strip()
        parameters = raw_step.get("parameters")
        if not isinstance(parameters, Mapping):
            reasons.append(f"{prefix}_parameters_not_object")
            parameters = {}
        else:
            try:
                _validate_json(parameters)
            except (TypeError, ValueError) as exc:
                reasons.append(f"{prefix}_parameters_not_json:{exc}")
                parameters = {}
        steps.append(
            ActionStepProposal(
                step_id=step_id,
                action_key=action_key,
                parameters=_freeze_json(dict(parameters)),
            )
        )

    if kind is ProposalKind.STOP and steps:
        reasons.append("stop_must_have_no_steps")
    elif kind is ProposalKind.ACTION and len(steps) != 1:
        reasons.append(f"action_requires_exactly_one_step:{len(steps)}")
    elif kind is ProposalKind.PLAN:
        if not steps:
            reasons.append("plan_requires_at_least_one_step")
        if len(steps) > max_steps:
            reasons.append(f"plan_too_long:{len(steps)}>{max_steps}")

    ids = [step.step_id for step in steps]
    if len(ids) != len(set(ids)):
        reasons.append("duplicate_step_ids")
    if reasons:
        raise ProposalParseError(reasons)
    return ActionProposal(
        schema_version=ACTION_PROPOSAL_SCHEMA_VERSION,
        proposal_id=proposal_id,
        decision_id=decision_id,
        catalogue_version=catalogue_version,
        catalogue_hash=catalogue_hash,
        kind=kind,
        steps=tuple(steps),
    )


class ExperimentActionGate:
    """Validate one proposal against a trusted experiment condition."""

    def __init__(self, *, max_plan_steps: int = DEFAULT_MAX_PLAN_STEPS) -> None:
        if int(max_plan_steps) != DEFAULT_MAX_PLAN_STEPS:
            raise ValueError(
                "gate max_plan_steps must equal the content-addressed "
                f"action-interface value of {DEFAULT_MAX_PLAN_STEPS}"
            )
        self.max_plan_steps = int(max_plan_steps)

    def evaluate(
        self,
        payload: Mapping[str, Any] | ActionProposal,
        *,
        level: DoFLevel | str,
        expected_decision_id: str,
        catalogue: Any,
        expected_catalogue_hash: str,
        current_stage: DesignStage | str | None,
        budget_remaining: Mapping[Any, int],
        mask: Any | None = None,
        infrastructure_events_per_step: Mapping[Any, int] | None = None,
        capability_contract: str = "v1",
    ) -> GateDecision:
        """Return dispatchable steps or a structured, no-fallback rejection."""

        parsed_level = _coerce_enum(level, DoFLevel)
        if parsed_level is None:
            return _rejection(
                EndpointCode.CAPABILITY_ACTION_REJECTION,
                f"unknown_level:{level}",
            )
        nested = capability_contract == "e1v2"
        if capability_contract not in {"v1", "e1v2"}:
            return _rejection(
                EndpointCode.CAPABILITY_ACTION_REJECTION,
                "unknown_capability_contract",
            )
        if parsed_level is DoFLevel.L4 and not nested:
            return _rejection(
                EndpointCode.L4_REFERENCE_PENDING,
                "l4_reference_pending",
            )
        if parsed_level is DoFLevel.L0:
            return _rejection(
                EndpointCode.CAPABILITY_ACTION_REJECTION,
                "l0_has_no_policy_action_surface",
            )

        try:
            proposal = parse_action_proposal(
                _untrusted_proposal_envelope(payload),
                max_plan_steps=self.max_plan_steps,
            )
        except ProposalParseError as exc:
            return _rejection(EndpointCode.INVALID_POLICY_OUTPUT, *exc.reasons)

        expected_id_reasons: list[str] = []
        canonical_expected_decision_id = _parse_id(
            expected_decision_id,
            "expected_decision_id",
            expected_id_reasons,
        )
        if expected_id_reasons:
            return _rejection(
                EndpointCode.CAPABILITY_ACTION_REJECTION,
                *expected_id_reasons,
                proposal=proposal,
            )
        if proposal.decision_id != canonical_expected_decision_id:
            return _rejection(
                EndpointCode.CAPABILITY_ACTION_REJECTION,
                "proposal_decision_id_mismatch",
                proposal=proposal,
            )

        # Import lazily because manifests content-addresses the proposal schema
        # exported by this module.  The manifest is the canonical declaration;
        # the gate must not maintain a second, hand-written kind matrix.
        from .manifests import capability_manifest, capability_manifest_v2

        allowed_kinds = (
            capability_manifest_v2(parsed_level).policy_kinds
            if nested
            else capability_manifest(parsed_level).policy_kinds
        )
        if proposal.kind.value not in allowed_kinds:
            return _rejection(
                EndpointCode.CAPABILITY_ACTION_REJECTION,
                f"proposal_kind_not_allowed:{parsed_level.value}:{proposal.kind.value}",
                proposal=proposal,
            )

        try:
            validated_mask = validate_mask_rules(mask)
        except MaskRulesValidationError as exc:
            return _rejection(
                EndpointCode.CAPABILITY_ACTION_REJECTION,
                *exc.reasons,
                proposal=proposal,
            )
        # A content-valid digest cannot make a misspelled or case-shifted
        # family meaningful.  Bind every mask to the canonical clean-macro
        # family vocabulary before using it at any L1--L3 gate boundary.
        from .catalogues import clean_macro_families

        unknown_hidden_families = sorted(
            set(validated_mask.hidden_macro_families) - clean_macro_families()
        )
        if unknown_hidden_families:
            return _rejection(
                EndpointCode.CAPABILITY_ACTION_REJECTION,
                "mask.hidden_macro_families_unknown:"
                + ",".join(unknown_hidden_families),
                proposal=proposal,
            )
        actual_hash = _catalogue_hash(catalogue)
        actual_version = str(getattr(catalogue, "version", "") or "")
        binding_errors: list[str] = []
        if not _SHA256_PATTERN.fullmatch(str(expected_catalogue_hash or "")):
            binding_errors.append("expected_catalogue_hash_not_sha256")
        if actual_hash != str(expected_catalogue_hash):
            binding_errors.append("catalogue_hash_does_not_match_condition")
        if proposal.catalogue_hash != actual_hash:
            binding_errors.append("proposal_catalogue_hash_mismatch")
        if proposal.catalogue_version != actual_version:
            binding_errors.append("proposal_catalogue_version_mismatch")
        if binding_errors:
            return _rejection(
                EndpointCode.CAPABILITY_ACTION_REJECTION,
                *binding_errors,
                proposal=proposal,
            )

        if proposal.kind is ProposalKind.STOP:
            return GateDecision(
                accepted=True,
                endpoint=EndpointCode.VALID_COMPLETION,
                reasons=("policy_stop",),
                proposal=proposal,
                required_units=MappingProxyType({}),
                final_stage=_coerce_stage(current_stage),
            )

        actions = _catalogue_actions(catalogue)
        if parsed_level is DoFLevel.L3 and not nested:
            catalogue_errors = _strict_l3_catalogue_errors(actions)
            if catalogue_errors:
                return _rejection(
                    EndpointCode.CAPABILITY_ACTION_REJECTION,
                    *catalogue_errors,
                    proposal=proposal,
                )

        stage = _coerce_stage(current_stage)
        if parsed_level in {DoFLevel.L3, DoFLevel.L4} and stage is None:
            return _rejection(
                EndpointCode.CAPABILITY_ACTION_REJECTION,
                f"unknown_stage:{current_stage}",
                proposal=proposal,
            )

        hidden_families = frozenset(validated_mask.hidden_macro_families)
        infrastructure_events, infrastructure_errors = _infrastructure_events(
            infrastructure_events_per_step
        )
        if infrastructure_errors:
            return _rejection(
                EndpointCode.CAPABILITY_ACTION_REJECTION,
                *infrastructure_errors,
                proposal=proposal,
            )
        gated_steps: list[GatedStep] = []
        required: dict[str, int] = {}
        projected_stage = stage
        for index, step in enumerate(proposal.steps):
            forbidden_reason = _forbidden_action_reason(
                step.action_key, parsed_level, nested=nested
            )
            if forbidden_reason:
                return _rejection(
                    EndpointCode.CAPABILITY_ACTION_REJECTION,
                    f"step_{index}:{forbidden_reason}",
                    proposal=proposal,
                )
            action = actions.get(step.action_key)
            if action is None:
                return _rejection(
                    EndpointCode.CAPABILITY_ACTION_REJECTION,
                    f"step_{index}:action_not_in_level:{step.action_key}",
                    proposal=proposal,
                )
            family = str(getattr(action, "family", "") or "")
            if family in hidden_families:
                return _rejection(
                    EndpointCode.MASK_VIOLATION,
                    f"step_{index}:masked_action_family:{family}",
                    proposal=proposal,
                )

            forbidden_parameters = _forbidden_parameter_reasons(step.parameters)
            if forbidden_parameters:
                return _rejection(
                    EndpointCode.CAPABILITY_ACTION_REJECTION,
                    *(f"step_{index}:{reason}" for reason in forbidden_parameters),
                    proposal=proposal,
                )
            parameter_errors = _parameter_schema_errors(
                step.parameters,
                getattr(action, "parameters_schema", {}),
            )
            if parameter_errors:
                return _rejection(
                    EndpointCode.CAPABILITY_ACTION_REJECTION,
                    *(f"step_{index}:{reason}" for reason in parameter_errors),
                    proposal=proposal,
                )
            if nested and step.action_key == "generated.pblock_region":
                from .generated import GeneratedProgramError, validate_generated_program

                try:
                    validate_generated_program(step.parameters)
                except GeneratedProgramError as exc:
                    return _rejection(
                        EndpointCode.CAPABILITY_ACTION_REJECTION,
                        f"step_{index}:{exc}",
                        proposal=proposal,
                    )

            if parsed_level is DoFLevel.L3 and not nested:
                assert projected_stage is not None
                stage_errors = _strict_l3_stage_errors(
                    action,
                    step.action_key,
                    projected_stage,
                )
                if stage_errors:
                    return _rejection(
                        EndpointCode.CAPABILITY_ACTION_REJECTION,
                        *(f"step_{index}:{reason}" for reason in stage_errors),
                        proposal=proposal,
                    )
                next_stage = _advance_l3_stage(projected_stage, step.action_key)
            else:
                allowed = _action_allowed_stages(action)
                if allowed and projected_stage is None:
                    return _rejection(
                        EndpointCode.CAPABILITY_ACTION_REJECTION,
                        f"step_{index}:unknown_stage:{current_stage}",
                        proposal=proposal,
                    )
                if allowed and projected_stage not in allowed:
                    return _rejection(
                        EndpointCode.CAPABILITY_ACTION_REJECTION,
                        f"step_{index}:stage_precondition_failed:{step.action_key}:{projected_stage.value}",
                        proposal=proposal,
                    )
                next_stage = (
                    _advance_nested_stage(projected_stage, step.action_key)
                    if nested and projected_stage is not None
                    else projected_stage
                )

            events, event_errors = _declared_events(action)
            if event_errors:
                return _rejection(
                    EndpointCode.CAPABILITY_ACTION_REJECTION,
                    *(f"step_{index}:{reason}" for reason in event_errors),
                    proposal=proposal,
                )
            step_events = (
                tuple(
                    event
                    for event in infrastructure_events
                    if event.event_class is EventClass.OUTER_ATTEMPT
                )
                + events
                + tuple(
                    event
                    for event in infrastructure_events
                    if event.event_class is EventClass.VALIDATION
                )
            )
            for event in step_events:
                required[event.event_class.value] = (
                    required.get(event.event_class.value, 0) + event.units
                )
            gated_steps.append(
                GatedStep(
                    proposal_id=proposal.proposal_id,
                    proposal_hash=proposal.proposal_hash,
                    catalogue_hash=actual_hash,
                    step_index=index,
                    step_id=step.step_id,
                    action_key=step.action_key,
                    family=family,
                    parameters=step.parameters,
                    stage_before=projected_stage,
                    stage_after=next_stage,
                    events=step_events,
                )
            )
            projected_stage = next_stage

        budget_errors = _budget_errors(required, budget_remaining)
        if budget_errors:
            return _rejection(
                EndpointCode.BUDGET_REJECTION,
                *budget_errors,
                proposal=proposal,
            )
        return GateDecision(
            accepted=True,
            endpoint=None,
            reasons=("accepted",),
            proposal=proposal,
            gated_steps=tuple(gated_steps),
            required_units=MappingProxyType(dict(sorted(required.items()))),
            final_stage=projected_stage,
        )


# Backwards-friendly concise name for callers that prefer ``ActionGate``.
ActionGate = ExperimentActionGate


def _untrusted_proposal_envelope(
    payload: Mapping[str, Any] | ActionProposal,
) -> Any:
    """Return a parser input without trusting dataclass construction.

    Python type annotations and frozen dataclasses do not enforce the wire
    schema.  In particular, callers can directly construct objects with bad
    IDs, cardinality, kinds, parameter keys, or non-JSON values.  Preserve
    those raw values here so :func:`parse_action_proposal` rejects exactly the
    same defect instead of normalizing it away.
    """

    if type(payload) is not ActionProposal:
        return payload

    raw_steps = payload.steps
    if isinstance(raw_steps, (list, tuple)):
        steps: Any = []
        for raw_step in raw_steps:
            if type(raw_step) is ActionStepProposal:
                steps.append(
                    {
                        "step_id": raw_step.step_id,
                        "action_key": raw_step.action_key,
                        # Do not use ``to_dict`` here: its presentation thaw
                        # stringifies mapping keys, which could hide malformed
                        # directly-constructed parameter objects from parsing.
                        "parameters": raw_step.parameters,
                    }
                )
            else:
                steps.append(raw_step)
    else:
        steps = raw_steps

    kind = (
        payload.kind.value
        if isinstance(payload.kind, ProposalKind)
        else payload.kind
    )
    return {
        "schema_version": payload.schema_version,
        "proposal_id": payload.proposal_id,
        "decision_id": payload.decision_id,
        "catalogue_version": payload.catalogue_version,
        "catalogue_hash": payload.catalogue_hash,
        "kind": kind,
        "steps": steps,
    }


def _parse_id(value: Any, name: str, reasons: list[str]) -> str:
    if not isinstance(value, str) or not _ID_PATTERN.fullmatch(value):
        reasons.append(f"{name}_not_canonical_id")
        return "invalid"
    return value


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def _validate_json(value: Any, *, path: str = "$") -> None:
    if value is None or isinstance(value, (str, bool, int)):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"{path} contains non-finite number")
        return
    if isinstance(value, Mapping):
        for key, item in value.items():
            if not isinstance(key, str):
                raise TypeError(f"{path} contains non-string object key")
            _validate_json(item, path=f"{path}.{key}")
        return
    if isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            _validate_json(item, path=f"{path}[{index}]")
        return
    raise TypeError(f"{path} contains non-JSON value {type(value).__name__}")


def _freeze_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType(
            {str(key): _freeze_json(item) for key, item in sorted(value.items())}
        )
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_json(item) for item in value)
    return value


def _thaw_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _thaw_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw_json(item) for item in value]
    return value


def _coerce_enum(value: Any, enum_type: type[Enum]) -> Any | None:
    if isinstance(value, enum_type):
        return value
    text = str(getattr(value, "value", value) or "")
    for member in enum_type:
        if text == member.value or text.upper() == member.name.upper():
            return member
    return None


def _coerce_stage(value: Any) -> DesignStage | None:
    return _coerce_enum(value, DesignStage)


def _catalogue_hash(catalogue: Any) -> str:
    value = getattr(catalogue, "sha256", None)
    value = value() if callable(value) else value
    if not isinstance(value, str) or not _SHA256_PATTERN.fullmatch(value):
        return ""
    return value


def _catalogue_actions(catalogue: Any) -> dict[str, Any]:
    raw_actions = getattr(catalogue, "actions", ())
    actions: dict[str, Any] = {}
    for action in raw_actions:
        name = str(getattr(action, "name", "") or "")
        if name in actions:
            # Keep an impossible sentinel so catalogue verification fails.
            actions[f"__duplicate__:{name}"] = action
        actions[name] = action
    return actions


def _forbidden_action_reason(
    action_key: str, level: DoFLevel, *, nested: bool = False
) -> str | None:
    lowered = action_key.lower()
    if lowered in _FORBIDDEN_RAW_ACTIONS or lowered.startswith(("shell.", "tcl.")):
        return f"forbidden_raw_action:{action_key}"
    if lowered == "auto_top3":
        return f"forbidden_alias_or_meta_action:{action_key}"
    if level is DoFLevel.L3 and not nested:
        if lowered.startswith("flow."):
            return f"forbidden_flow_primitive:{action_key}"
        if lowered in _FORBIDDEN_META_OR_ALIASES:
            return f"forbidden_alias_or_meta_action:{action_key}"
        if lowered.startswith(("macro.", "vivado.")):
            return f"forbidden_alias_surface:{action_key}"
    return None


def _advance_nested_stage(stage: DesignStage, action_key: str) -> DesignStage:
    if action_key == "manual.unroute_all":
        return DesignStage.PLACED
    if action_key == "manual.unplace_all":
        return DesignStage.UNPLACED
    if action_key == "manual.place_design":
        return DesignStage.PLACED
    if action_key == "manual.route_design":
        return DesignStage.ROUTED
    return stage


def _forbidden_parameter_reasons(parameters: Mapping[str, Any]) -> list[str]:
    reasons: list[str] = []

    def walk(value: Any, path: str) -> None:
        if isinstance(value, Mapping):
            for key, item in value.items():
                lowered = str(key).lower()
                if any(part in lowered for part in _FORBIDDEN_PARAMETER_PARTS):
                    reasons.append(f"forbidden_parameter_key:{path}.{key}")
                walk(item, f"{path}.{key}")
        elif isinstance(value, (list, tuple)):
            for index, item in enumerate(value):
                walk(item, f"{path}[{index}]")
        elif isinstance(value, str) and _PATH_VALUE_PATTERN.search(value):
            reasons.append(f"forbidden_path_or_file_value:{path}")

    walk(parameters, "parameters")
    return reasons


def _as_mapping(value: Any) -> Mapping[str, Any]:
    if isinstance(value, Mapping):
        return value
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        converted = to_dict()
        if isinstance(converted, Mapping):
            return converted
    return {}


def _parameter_schema_errors(
    parameters: Mapping[str, Any], schema_value: Any
) -> list[str]:
    schema = _as_mapping(schema_value)
    if not schema:
        return [] if not parameters else ["parameters_not_allowed"]
    if schema.get("type") == "object" or "properties" in schema:
        properties = _as_mapping(schema.get("properties"))
        required = schema.get("required", ())
        additional = bool(schema.get("additionalProperties", False))
    else:
        # Also accept the compact {parameter_name: schema} representation.
        properties = schema
        required = tuple(
            name
            for name, item in properties.items()
            if bool(_as_mapping(item).get("required", False))
        )
        additional = False
    if not isinstance(required, (list, tuple, set, frozenset)):
        return ["parameter_schema_required_not_array"]
    errors: list[str] = []
    missing = sorted(str(name) for name in required if str(name) not in parameters)
    if missing:
        errors.append("missing_parameters:" + ",".join(missing))
    extras = sorted(set(parameters) - set(properties))
    if extras and not additional:
        errors.append("unknown_parameters:" + ",".join(extras))
    for name, value in parameters.items():
        definition = _as_mapping(properties.get(name))
        if not definition:
            continue
        expected = str(definition.get("type") or "").lower()
        type_ok = {
            "": True,
            "string": isinstance(value, str),
            "integer": isinstance(value, int) and not isinstance(value, bool),
            "number": isinstance(value, (int, float)) and not isinstance(value, bool),
            "boolean": isinstance(value, bool),
            "array": isinstance(value, (list, tuple)),
            "object": isinstance(value, Mapping),
        }.get(expected, False)
        if not type_ok:
            errors.append(f"parameter_type:{name}:{expected or 'unknown'}")
            continue
        allowed = definition.get("enum", definition.get("allowed_values", ()))
        if allowed and value not in tuple(allowed):
            errors.append(f"parameter_not_allowed:{name}:{value}")
    return errors


def _action_allowed_stages(action: Any) -> frozenset[DesignStage]:
    raw = getattr(action, "allowed_stages", ())
    stages: set[DesignStage] = set()
    for value in raw:
        parsed = _coerce_stage(value)
        if parsed is not None:
            stages.add(parsed)
    return frozenset(stages)


def _strict_l3_catalogue_errors(actions: Mapping[str, Any]) -> list[str]:
    expected = set(_STRICT_L3_DIRECTIVES)
    actual = set(actions)
    if actual != expected:
        return [
            "strict_l3_action_set_mismatch:"
            f"missing={','.join(sorted(expected - actual))};"
            f"extra={','.join(sorted(actual - expected))}"
        ]
    errors: list[str] = []
    for name in sorted(expected):
        action = actions[name]
        schema_errors = _strict_l3_schema_errors(
            getattr(action, "parameters_schema", {}),
            _STRICT_L3_DIRECTIVES[name],
        )
        errors.extend(f"strict_l3_catalogue:{name}:{error}" for error in schema_errors)
        stages = _action_allowed_stages(action)
        if stages != _STRICT_L3_STAGES[name]:
            errors.append(f"strict_l3_catalogue:{name}:allowed_stages_mismatch")
        events, event_errors = _declared_events(action)
        if event_errors:
            errors.extend(
                f"strict_l3_catalogue:{name}:{error}" for error in event_errors
            )
        elif len(events) != 1 or events[0].event_class is not _STRICT_L3_EVENT[name] or events[0].units != 1:
            errors.append(f"strict_l3_catalogue:{name}:event_expansion_mismatch")
    return errors


def _strict_l3_schema_errors(
    schema_value: Any, directives: Sequence[str]
) -> list[str]:
    schema = _as_mapping(schema_value)
    if not directives:
        if not schema:
            return []
        properties = _as_mapping(schema.get("properties"))
        if schema.get("type") == "object" and not properties and not schema.get("required"):
            return []
        return ["parameters_schema_must_be_empty"]
    properties = _as_mapping(schema.get("properties")) if "properties" in schema else schema
    if set(properties) != {"directive"}:
        return ["directive_must_be_only_parameter"]
    definition = _as_mapping(properties.get("directive"))
    if str(definition.get("type") or "") != "string":
        return ["directive_type_must_be_string"]
    allowed = tuple(definition.get("enum", definition.get("allowed_values", ())))
    if allowed != tuple(directives):
        return ["directive_enum_mismatch"]
    required = schema.get("required", ()) if "properties" in schema else ()
    compact_required = bool(definition.get("required", False))
    if "directive" not in tuple(required) and not compact_required:
        return ["directive_must_be_required"]
    if "properties" in schema and bool(schema.get("additionalProperties", False)):
        return ["additional_parameters_must_be_forbidden"]
    return []


def _strict_l3_stage_errors(
    action: Any,
    action_key: str,
    stage: DesignStage,
) -> list[str]:
    expected = _STRICT_L3_STAGES[action_key]
    declared = _action_allowed_stages(action)
    errors: list[str] = []
    if declared != expected:
        errors.append("catalogue_stage_contract_mismatch")
    if stage not in expected:
        errors.append(f"stage_precondition_failed:{action_key}:{stage.value}")
    return errors


def _advance_l3_stage(stage: DesignStage, action_key: str) -> DesignStage:
    if action_key == "manual.unroute_all":
        return DesignStage.PLACED
    if action_key == "manual.unplace_all":
        return DesignStage.UNPLACED
    if action_key == "manual.place_design":
        return DesignStage.PLACED
    if action_key == "manual.route_design":
        return DesignStage.ROUTED
    return stage


def _declared_events(action: Any) -> tuple[tuple[GatedEvent, ...], list[str]]:
    raw_events = getattr(action, "internal_events", ())
    if not raw_events:
        return (), ["action_has_no_declared_event_expansion"]
    events: list[GatedEvent] = []
    errors: list[str] = []
    for index, raw in enumerate(raw_events):
        event_class = _coerce_enum(getattr(raw, "event_class", None), EventClass)
        operation = str(getattr(raw, "operation", "") or "")
        units = getattr(raw, "units", None)
        if event_class is None:
            errors.append(f"event_{index}_unknown_class")
        if not operation.strip():
            errors.append(f"event_{index}_missing_operation")
        if not isinstance(units, int) or isinstance(units, bool) or units <= 0:
            errors.append(f"event_{index}_units_not_positive_integer")
        if event_class is not None and operation.strip() and isinstance(units, int) and not isinstance(units, bool) and units > 0:
            events.append(GatedEvent(event_class, operation, units))
    return tuple(events), errors


def _infrastructure_events(
    configured: Mapping[Any, int] | None,
) -> tuple[tuple[GatedEvent, ...], list[str]]:
    """Normalize platform-owned per-step accounting into the gate trace.

    Action-internal read/mutation/heavy events remain catalogue-owned.  The
    common runner may additionally declare one or more outer-attempt and
    validation units around every gated action step; keeping those here avoids
    an unreviewed budget increment after gate acceptance.
    """

    if configured is None:
        return (), []
    if not isinstance(configured, Mapping):
        return (), ["infrastructure_events_per_step_not_object"]
    allowed = {EventClass.OUTER_ATTEMPT, EventClass.VALIDATION}
    normalized: dict[EventClass, int] = {}
    errors: list[str] = []
    for raw_class, raw_units in configured.items():
        event_class = _coerce_enum(raw_class, EventClass)
        label = str(getattr(raw_class, "value", raw_class))
        if event_class is None:
            errors.append(f"infrastructure_event_unknown_class:{label}")
            continue
        if event_class not in allowed:
            errors.append(f"infrastructure_event_class_not_allowed:{event_class.value}")
            continue
        if not isinstance(raw_units, int) or isinstance(raw_units, bool) or raw_units < 0:
            errors.append(f"infrastructure_event_units_invalid:{event_class.value}")
            continue
        if event_class in normalized:
            errors.append(f"infrastructure_event_duplicate_class:{event_class.value}")
            continue
        normalized[event_class] = raw_units
    events = tuple(
        GatedEvent(
            event_class=event_class,
            operation=f"infrastructure.{event_class.value.lower()}",
            units=units,
        )
        for event_class in (EventClass.OUTER_ATTEMPT, EventClass.VALIDATION)
        if (units := normalized.get(event_class, 0)) > 0
    )
    return events, errors


def _budget_errors(
    required: Mapping[str, int], budget_remaining: Mapping[Any, int]
) -> list[str]:
    normalized: dict[str, int] = {}
    if not isinstance(budget_remaining, Mapping):
        return ["budget_remaining_not_object"]
    for raw_key, raw_value in budget_remaining.items():
        key = str(getattr(raw_key, "value", raw_key))
        if not isinstance(raw_value, int) or isinstance(raw_value, bool) or raw_value < 0:
            return [f"budget_remaining_invalid:{key}"]
        normalized[key] = raw_value
    errors: list[str] = []
    for event_class, units in sorted(required.items()):
        if event_class not in normalized:
            errors.append(f"budget_missing:{event_class}")
        elif units > normalized[event_class]:
            errors.append(
                f"budget_would_exceed:{event_class}:{units}>{normalized[event_class]}"
            )
    return errors


def _rejection(
    endpoint: EndpointCode,
    *reasons: str,
    proposal: ActionProposal | None = None,
) -> GateDecision:
    return GateDecision(
        accepted=False,
        endpoint=endpoint,
        reasons=tuple(reasons) or ("rejected",),
        proposal=proposal,
        gated_steps=(),
        required_units=MappingProxyType({}),
        final_stage=None,
    )


__all__ = [
    "ACTION_PROPOSAL_SCHEMA_VERSION",
    "DEFAULT_MAX_PLAN_STEPS",
    "ActionGate",
    "ActionProposal",
    "ActionStepProposal",
    "ExperimentActionGate",
    "GateDecision",
    "GatedEvent",
    "GatedStep",
    "ProposalKind",
    "ProposalParseError",
    "parse_action_proposal",
]
