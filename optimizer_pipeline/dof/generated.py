"""Bounded generated-intervention microprograms for Protocol E1-v2.

The policy never supplies Tcl.  It supplies a small typed region program which
is validated twice and compiled by the isolated Vivado worker to one fixed Tcl
template.  The generated coordinates and selector make the surface strictly
more expressive than the frozen macro/primitive catalogues without creating a
shell, file, network, checkpoint, or hidden implementation-call escape hatch.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Mapping


GENERATED_PROGRAM_SCHEMA_VERSION = "dof.generated-pblock-program.v1"
GENERATED_ACTION_KEY = "generated.pblock_region"
GENERATED_OPERATION_ID = "vivado.generated_pblock_region"
GENERATED_MAX_BYTES = 1024
GENERATED_MAX_SELECTED_CELLS = 64
GENERATED_TIMEOUT_SECONDS = 120.0
_PBLOCK_ID_RE = re.compile(r"^g_[a-z][a-z0-9_]{0,22}$")
_SELECTORS = frozenset({"sequential_cells", "high_fanout_cells"})
_FIELDS = frozenset(
    {
        "pblock_id",
        "selector_kind",
        "cell_limit",
        "slice_x0",
        "slice_y0",
        "slice_x1",
        "slice_y1",
    }
)


class GeneratedProgramError(ValueError):
    """A generated program was rejected before any design mutation."""


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def validate_generated_program(parameters: Mapping[str, Any]) -> dict[str, Any]:
    """Return a canonical worker payload or fail closed.

    Coordinates are deliberately bounded by syntax and broad device-safe
    integer ceilings.  Vivado remains the authority on whether the requested
    region exists on a particular device; an out-of-device region is a normal
    typed tool failure rather than an opportunity to inject Tcl.
    """

    if not isinstance(parameters, Mapping):
        raise GeneratedProgramError("generated_program_not_object")
    value = {str(key): item for key, item in parameters.items()}
    if set(value) != _FIELDS:
        missing = sorted(_FIELDS - set(value))
        extra = sorted(set(value) - _FIELDS)
        raise GeneratedProgramError(
            "generated_program_fields_not_exact:"
            + f"missing={','.join(missing)};extra={','.join(extra)}"
        )
    pblock_id = value["pblock_id"]
    if not isinstance(pblock_id, str) or _PBLOCK_ID_RE.fullmatch(pblock_id) is None:
        raise GeneratedProgramError("generated_pblock_id_invalid")
    selector = value["selector_kind"]
    if selector not in _SELECTORS:
        raise GeneratedProgramError("generated_selector_not_allowed")
    integers: dict[str, int] = {}
    for name in (
        "cell_limit",
        "slice_x0",
        "slice_y0",
        "slice_x1",
        "slice_y1",
    ):
        item = value[name]
        if not isinstance(item, int) or isinstance(item, bool):
            raise GeneratedProgramError(f"generated_{name}_not_integer")
        integers[name] = item
    if not 1 <= integers["cell_limit"] <= GENERATED_MAX_SELECTED_CELLS:
        raise GeneratedProgramError("generated_cell_limit_out_of_range")
    if not 0 <= integers["slice_x0"] <= integers["slice_x1"] <= 999:
        raise GeneratedProgramError("generated_slice_x_range_invalid")
    if not 0 <= integers["slice_y0"] <= integers["slice_y1"] <= 9999:
        raise GeneratedProgramError("generated_slice_y_range_invalid")
    canonical = {
        "pblock_id": pblock_id,
        "selector_kind": selector,
        **integers,
    }
    raw = canonical_bytes(canonical)
    if len(raw) > GENERATED_MAX_BYTES:
        raise GeneratedProgramError("generated_program_too_large")
    return canonical


def generated_program_evidence(parameters: Mapping[str, Any]) -> dict[str, Any]:
    canonical = validate_generated_program(parameters)
    raw = canonical_bytes(canonical)
    payload = {
        "schema_version": GENERATED_PROGRAM_SCHEMA_VERSION,
        "action_key": GENERATED_ACTION_KEY,
        "canonical_program": canonical,
        "canonical_program_utf8": raw.decode("utf-8"),
        "program_sha256": hashlib.sha256(raw).hexdigest(),
        "size_bytes": len(raw),
        "compiled_operation_id": GENERATED_OPERATION_ID,
        "event_class": "MUTATION_LIGHT",
        "heavy_calls": 0,
        "timeout_seconds": GENERATED_TIMEOUT_SECONDS,
        "changed_object_intent": {
            "object_type": "pblock",
            "pblock_id": canonical["pblock_id"],
            "selector_kind": canonical["selector_kind"],
            "cell_limit": canonical["cell_limit"],
            "region": (
                f"SLICE_X{canonical['slice_x0']}Y{canonical['slice_y0']}:"
                f"SLICE_X{canonical['slice_x1']}Y{canonical['slice_y1']}"
            ),
        },
        "containment": {
            "raw_tcl": False,
            "shell": False,
            "network": False,
            "filesystem": False,
            "checkpoint_write": False,
            "hidden_heavy_call": False,
        },
    }
    return payload | {
        "evidence_sha256": hashlib.sha256(canonical_bytes(payload)).hexdigest()
    }


def generated_parameters_schema() -> dict[str, Any]:
    properties: dict[str, Any] = {
        "pblock_id": {"type": "string"},
        "selector_kind": {"type": "string", "enum": sorted(_SELECTORS)},
        "cell_limit": {"type": "integer"},
        "slice_x0": {"type": "integer"},
        "slice_y0": {"type": "integer"},
        "slice_x1": {"type": "integer"},
        "slice_y1": {"type": "integer"},
    }
    return {
        "type": "object",
        "properties": properties,
        "required": sorted(properties),
        "additionalProperties": False,
    }


__all__ = [
    "GENERATED_ACTION_KEY",
    "GENERATED_MAX_BYTES",
    "GENERATED_MAX_SELECTED_CELLS",
    "GENERATED_OPERATION_ID",
    "GENERATED_PROGRAM_SCHEMA_VERSION",
    "GENERATED_TIMEOUT_SECONDS",
    "GeneratedProgramError",
    "generated_parameters_schema",
    "generated_program_evidence",
    "validate_generated_program",
]
