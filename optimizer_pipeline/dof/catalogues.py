"""Immutable, hash-addressed action catalogues for WP-002.

L1 and L2 deliberately bind to the same byte-identical clean macro catalogue.
The strict L3 surface is compiled from ``ManualPrimitiveCatalog.default()``;
only the five primitive names and directive allowlists cross that boundary.
Fixed Tcl implementation details and legacy flow aliases are never serialized.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
import hashlib
import json
from types import MappingProxyType
from typing import Any

from optimizer_pipeline.capabilities.intervention.manual import ManualPrimitiveCatalog

from .contracts import DesignStage, DoFLevel, EventClass
from .generated import (
    GENERATED_ACTION_KEY,
    GENERATED_OPERATION_ID,
    generated_parameters_schema,
)


CATALOGUE_SCHEMA_VERSION = "dof.action-catalogue.v1"
CLEAN_MACRO_CATALOGUE_VERSION = "dof.clean-macro.v1"
STRICT_L3_CATALOGUE_VERSION = "dof.strict-manual-primitives.v1"
NO_ACTION_CATALOGUE_VERSION = "dof.no-actions.v1"
L4_REFERENCE_CATALOGUE_VERSION = "dof.l4-reference-pending.v1"
NESTED_L2_CATALOGUE_VERSION = "dof.e1v2.m.v1"
NESTED_L3_CATALOGUE_VERSION = "dof.e1v2.m-plus-p.v1"
NESTED_L4_CATALOGUE_VERSION = "dof.e1v2.m-plus-p-plus-g.v1"
FILTERED_NESTED_CATALOGUE_VERSION = "dof.e1v2.filtered-nested.v1"


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def _empty_catalogue_hash(version: str, level_contract: str) -> str:
    payload = {
        "schema_version": CATALOGUE_SCHEMA_VERSION,
        "version": version,
        "level_contract": level_contract,
        "actions": [],
    }
    return hashlib.sha256(_canonical_bytes(payload)).hexdigest()


NO_ACTION_CATALOGUE_HASH = _empty_catalogue_hash(
    NO_ACTION_CATALOGUE_VERSION, DoFLevel.L0.value
)
L4_REFERENCE_CATALOGUE_HASH = _empty_catalogue_hash(
    L4_REFERENCE_CATALOGUE_VERSION, "REFERENCE_PENDING"
)


def _freeze_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType(
            {str(key): _freeze_json(item) for key, item in value.items()}
        )
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_json(item) for item in value)
    if value is None or isinstance(value, (str, int, float, bool)):
        # Validate finite floats and other JSON constraints once at creation.
        _canonical_bytes(value)
        return value
    raise TypeError(f"value is not JSON-compatible: {type(value).__name__}")


def _thaw_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _thaw_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw_json(item) for item in value]
    return value


def _empty_object_schema() -> Mapping[str, Any]:
    return {
        "type": "object",
        "properties": {},
        "required": [],
        "additionalProperties": False,
    }


def _directive_schema(values: tuple[str, ...]) -> Mapping[str, Any]:
    return {
        "type": "object",
        "properties": {
            "directive": {
                "type": "string",
                "enum": list(values),
            }
        },
        "required": ["directive"],
        "additionalProperties": False,
    }


@dataclass(frozen=True, slots=True)
class InternalEventSpec:
    event_class: EventClass
    operation: str
    units: int = 1

    def __post_init__(self) -> None:
        if not isinstance(self.event_class, EventClass):
            raise TypeError("event_class must be EventClass")
        if not isinstance(self.operation, str) or not self.operation.strip():
            raise ValueError("operation must be non-empty")
        if not isinstance(self.units, int) or isinstance(self.units, bool) or self.units <= 0:
            raise ValueError("units must be a positive integer")

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_class": self.event_class.value,
            "operation": self.operation,
            "units": self.units,
        }


@dataclass(frozen=True, slots=True)
class CatalogueAction:
    name: str
    family: str
    parameters_schema: Mapping[str, Any] = field(default_factory=_empty_object_schema)
    internal_events: tuple[InternalEventSpec, ...] = ()
    allowed_stages: tuple[DesignStage, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name.strip():
            raise ValueError("action name must be non-empty")
        if not isinstance(self.family, str) or not self.family.strip():
            raise ValueError("action family must be non-empty")
        if not self.internal_events:
            raise ValueError("an action must declare at least one internal event")
        if any(not isinstance(item, InternalEventSpec) for item in self.internal_events):
            raise TypeError("internal_events must contain InternalEventSpec")
        if any(not isinstance(item, DesignStage) for item in self.allowed_stages):
            raise TypeError("allowed_stages must contain DesignStage")
        frozen_schema = _freeze_json(self.parameters_schema)
        if not isinstance(frozen_schema, Mapping):
            raise TypeError("parameters_schema must be an object")
        object.__setattr__(self, "parameters_schema", frozen_schema)
        object.__setattr__(self, "internal_events", tuple(self.internal_events))
        object.__setattr__(self, "allowed_stages", tuple(self.allowed_stages))

    @property
    def action_key(self) -> str:
        return self.name

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "family": self.family,
            "parameters_schema": _thaw_json(self.parameters_schema),
            "internal_events": [item.to_dict() for item in self.internal_events],
            "allowed_stages": [item.value for item in self.allowed_stages],
        }


@dataclass(frozen=True, slots=True)
class ActionCatalogueManifest:
    version: str
    level_contract: str
    actions: tuple[CatalogueAction, ...]
    schema_version: str = CATALOGUE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if not isinstance(self.version, str) or not self.version.strip():
            raise ValueError("catalogue version must be non-empty")
        if not isinstance(self.level_contract, str) or not self.level_contract.strip():
            raise ValueError("level_contract must be non-empty")
        actions = tuple(self.actions)
        if not actions or any(not isinstance(item, CatalogueAction) for item in actions):
            raise ValueError("catalogue must contain typed actions")
        names = [item.name for item in actions]
        if len(names) != len(set(names)):
            raise ValueError("catalogue action names must be unique")
        object.__setattr__(self, "actions", tuple(sorted(actions, key=lambda item: item.name)))

    @property
    def level(self) -> str:
        return self.level_contract

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "version": self.version,
            "level_contract": self.level_contract,
            "actions": [item.to_dict() for item in self.actions],
        }

    def canonical_bytes(self) -> bytes:
        return _canonical_bytes(self.to_dict())

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.canonical_bytes()).hexdigest()

    def action(self, name: str) -> CatalogueAction | None:
        return next((item for item in self.actions if item.name == name), None)


def clean_macro_catalogue(level: DoFLevel | str = DoFLevel.L1) -> ActionCatalogueManifest:
    """Return the shared clean mock macro surface for L1 and L2.

    ``level`` is validation only and is intentionally omitted from the payload,
    so requesting L1 versus L2 produces exactly the same bytes and digest.
    """

    try:
        parsed = level if isinstance(level, DoFLevel) else DoFLevel(str(level))
    except ValueError as exc:
        raise ValueError(f"unknown DoF level: {level}") from exc
    if parsed not in {DoFLevel.L1, DoFLevel.L2}:
        raise ValueError("clean macro catalogue is only valid for L1/L2")
    empty = _empty_object_schema()
    return ActionCatalogueManifest(
        version=CLEAN_MACRO_CATALOGUE_VERSION,
        level_contract="L1_L2_SHARED",
        actions=(
            CatalogueAction(
                name="macro.phys_opt",
                family="physical_optimization",
                parameters_schema=empty,
                internal_events=(
                    InternalEventSpec(EventClass.IMPLEMENTATION_HEAVY, "mock.phys_opt"),
                ),
            ),
            CatalogueAction(
                name="macro.retime",
                family="retiming",
                parameters_schema=empty,
                internal_events=(
                    InternalEventSpec(EventClass.IMPLEMENTATION_HEAVY, "mock.retime"),
                ),
            ),
            CatalogueAction(
                name="macro.place_route",
                family="placement_routing",
                parameters_schema=empty,
                internal_events=(
                    InternalEventSpec(EventClass.IMPLEMENTATION_HEAVY, "mock.place"),
                    InternalEventSpec(EventClass.IMPLEMENTATION_HEAVY, "mock.route"),
                ),
            ),
        ),
    )


def clean_macro_families() -> frozenset[str]:
    """Return the canonical mask vocabulary derived from the clean catalogue."""

    return frozenset(
        action.family for action in clean_macro_catalogue(DoFLevel.L1).actions
    )


_L3_STAGES: dict[str, tuple[DesignStage, ...]] = {
    "manual.unroute_all": (DesignStage.ROUTED,),
    "manual.unplace_all": (DesignStage.PLACED,),
    "manual.place_design": (DesignStage.UNPLACED,),
    "manual.route_design": (DesignStage.PLACED,),
    "manual.phys_opt_design": (DesignStage.PLACED, DesignStage.ROUTED),
}
_L3_EVENT_CLASS: dict[str, EventClass] = {
    "manual.unroute_all": EventClass.MUTATION_LIGHT,
    "manual.unplace_all": EventClass.MUTATION_LIGHT,
    "manual.place_design": EventClass.IMPLEMENTATION_HEAVY,
    "manual.route_design": EventClass.IMPLEMENTATION_HEAVY,
    "manual.phys_opt_design": EventClass.IMPLEMENTATION_HEAVY,
}


def strict_l3_catalogue() -> ActionCatalogueManifest:
    """Compile the exact five audited manual primitives into the L3 surface."""

    source = ManualPrimitiveCatalog.default()
    expected = set(_L3_STAGES)
    actual = set(source.names())
    if actual != expected:
        raise RuntimeError(
            "ManualPrimitiveCatalog.default changed; audit L3 before use: "
            f"missing={sorted(expected - actual)}, extra={sorted(actual - expected)}"
        )
    actions: list[CatalogueAction] = []
    for name in sorted(expected):
        primitive = source.get(name)
        if primitive is None or not primitive.mutates_design:
            raise RuntimeError(f"L3 primitive is missing/non-mutating: {name}")
        directives = tuple(primitive.allowed_directives)
        schema = _directive_schema(directives) if directives else _empty_object_schema()
        actions.append(
            CatalogueAction(
                name=name,
                family=name.removeprefix("manual.").removesuffix("_design"),
                parameters_schema=schema,
                internal_events=(
                    InternalEventSpec(
                        _L3_EVENT_CLASS[name],
                        name,
                    ),
                ),
                allowed_stages=_L3_STAGES[name],
            )
        )
    return ActionCatalogueManifest(
        version=STRICT_L3_CATALOGUE_VERSION,
        level_contract=DoFLevel.L3.value,
        actions=tuple(actions),
    )


def generated_intervention_action() -> CatalogueAction:
    """Return the sole frozen G action for the strict E1-v2 matrix."""

    return CatalogueAction(
        name=GENERATED_ACTION_KEY,
        family="generated_intervention",
        parameters_schema=generated_parameters_schema(),
        internal_events=(
            InternalEventSpec(EventClass.MUTATION_LIGHT, GENERATED_OPERATION_ID),
        ),
        allowed_stages=(DesignStage.PLACED, DesignStage.ROUTED),
    )


def nested_action_catalogue(level: DoFLevel | str) -> ActionCatalogueManifest:
    """Build the strict nested M, M+P, M+P+G Protocol E1-v2 surface.

    Inherited :class:`CatalogueAction` objects come directly from the frozen
    lower-level catalogues, so their canonical action bytes are identical at
    every level even though the containing catalogue hash necessarily differs.
    """

    parsed = level if isinstance(level, DoFLevel) else DoFLevel(str(level))
    if parsed not in {DoFLevel.L2, DoFLevel.L3, DoFLevel.L4}:
        raise ValueError("nested action catalogue is valid only for L2-L4")
    macros = clean_macro_catalogue(DoFLevel.L2).actions
    primitives = strict_l3_catalogue().actions
    if parsed is DoFLevel.L2:
        version = NESTED_L2_CATALOGUE_VERSION
        actions = macros
        contract = "M"
    elif parsed is DoFLevel.L3:
        version = NESTED_L3_CATALOGUE_VERSION
        actions = macros + primitives
        contract = "M_PLUS_P"
    else:
        version = NESTED_L4_CATALOGUE_VERSION
        actions = macros + primitives + (generated_intervention_action(),)
        contract = "M_PLUS_P_PLUS_G"
    return ActionCatalogueManifest(
        version=version,
        level_contract=contract,
        actions=actions,
    )


def filtered_nested_action_catalogue(
    level: DoFLevel | str,
    *,
    excluded_action_keys: tuple[str, ...] | list[str],
) -> ActionCatalogueManifest:
    """Return a deterministic M' / M'+P / M'+P+G catalogue.

    Filtering is an audit-time condition operation.  It never rebuilds or
    changes an inherited action definition: retained ``CatalogueAction``
    instances come directly from the frozen known catalogue.
    """

    base = nested_action_catalogue(level)
    excluded = tuple(sorted(excluded_action_keys))
    if not excluded or len(excluded) != len(set(excluded)):
        raise ValueError("excluded_action_keys_must_be_unique_nonempty")
    available = {action.name for action in base.actions}
    unknown = sorted(set(excluded) - available)
    if unknown:
        raise ValueError("excluded_action_not_in_level:" + ",".join(unknown))
    if any(not key.startswith("macro.") for key in excluded):
        raise ValueError("only_macro_actions_may_be_excluded")
    actions = tuple(action for action in base.actions if action.name not in excluded)
    suffix = hashlib.sha256(_canonical_bytes(list(excluded))).hexdigest()[:16]
    return ActionCatalogueManifest(
        version=f"{FILTERED_NESTED_CATALOGUE_VERSION}.{base.level_contract.lower()}.{suffix}",
        level_contract=base.level_contract.replace("M", "M'", 1),
        actions=actions,
    )


def filtered_nested_capability_audit(
    *, excluded_action_keys: tuple[str, ...] | list[str]
) -> dict[str, Any]:
    """Prove filtered nestedness and byte identity of every retained action."""

    levels = (DoFLevel.L2, DoFLevel.L3, DoFLevel.L4)
    known = {level.value: nested_action_catalogue(level) for level in levels}
    filtered = {
        level.value: filtered_nested_action_catalogue(
            level, excluded_action_keys=excluded_action_keys
        )
        for level in levels
    }
    rows = {
        name: {
            action.name: hashlib.sha256(_canonical_bytes(action.to_dict())).hexdigest()
            for action in catalogue.actions
        }
        for name, catalogue in filtered.items()
    }
    known_rows = {
        name: {
            action.name: hashlib.sha256(_canonical_bytes(action.to_dict())).hexdigest()
            for action in catalogue.actions
        }
        for name, catalogue in known.items()
    }
    sets = {name: set(values) for name, values in rows.items()}
    retained_identical = all(
        digest == known_rows[level][key]
        for level, values in rows.items()
        for key, digest in values.items()
    )
    excluded = tuple(sorted(excluded_action_keys))
    payload = {
        "schema_version": "dof.e1v2-filtered-nested-capability-audit.v1",
        "excluded_action_keys": list(excluded),
        "catalogues": {
            level: {
                "version": filtered[level].version,
                "sha256": filtered[level].sha256,
                "actions": sorted(sets[level]),
                "action_definition_sha256": rows[level],
            }
            for level in ("L2", "L3", "L4")
        },
        "l2_strict_subset_l3": sets["L2"] < sets["L3"],
        "l3_strict_subset_l4": sets["L3"] < sets["L4"],
        "retained_action_definitions_identical_to_known": retained_identical,
        "excluded_only_from_macro_surface": all(
            key not in sets["L2"] and key not in sets["L3"] and key not in sets["L4"]
            for key in excluded
        ),
        "primitive_place_route_sequence_retained": {
            "manual.unroute_all",
            "manual.unplace_all",
            "manual.place_design",
            "manual.route_design",
        }
        <= sets["L3"],
        "generated_intervention_retained": GENERATED_ACTION_KEY in sets["L4"],
    }
    payload["accepted"] = all(
        payload[key]
        for key in (
            "l2_strict_subset_l3",
            "l3_strict_subset_l4",
            "retained_action_definitions_identical_to_known",
            "excluded_only_from_macro_surface",
            "primitive_place_route_sequence_retained",
            "generated_intervention_retained",
        )
    )
    return payload | {
        "audit_sha256": hashlib.sha256(_canonical_bytes(payload)).hexdigest()
    }


def nested_capability_audit() -> dict[str, Any]:
    catalogues = {
        level.value: nested_action_catalogue(level)
        for level in (DoFLevel.L2, DoFLevel.L3, DoFLevel.L4)
    }
    action_rows: dict[str, dict[str, str]] = {}
    for level, catalogue in catalogues.items():
        action_rows[level] = {
            action.name: hashlib.sha256(_canonical_bytes(action.to_dict())).hexdigest()
            for action in catalogue.actions
        }
    sets = {level: set(rows) for level, rows in action_rows.items()}
    inherited_equal = all(
        action_rows["L2"][name]
        == action_rows["L3"][name]
        == action_rows["L4"][name]
        for name in sets["L2"]
    ) and all(
        action_rows["L3"][name] == action_rows["L4"][name]
        for name in sets["L3"]
    )
    payload = {
        "schema_version": "dof.e1v2-nested-capability-audit.v1",
        "catalogues": {
            level: {
                "version": catalogue.version,
                "sha256": catalogue.sha256,
                "actions": sorted(sets[level]),
                "action_definition_sha256": action_rows[level],
            }
            for level, catalogue in catalogues.items()
        },
        "l2_strict_subset_l3": sets["L2"] < sets["L3"],
        "l3_strict_subset_l4": sets["L3"] < sets["L4"],
        "inherited_action_definitions_identical": inherited_equal,
    }
    payload["accepted"] = all(
        payload[name]
        for name in (
            "l2_strict_subset_l3",
            "l3_strict_subset_l4",
            "inherited_action_definitions_identical",
        )
    )
    return payload | {
        "audit_sha256": hashlib.sha256(_canonical_bytes(payload)).hexdigest()
    }


build_clean_macro_catalogue = clean_macro_catalogue
build_strict_l3_catalogue = strict_l3_catalogue
