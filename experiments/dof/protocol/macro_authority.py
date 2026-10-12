"""Canonical execution and accounting authority for the frozen M10 macros."""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping


MACRO_AUTHORITY_VERSION = "dof.single-submission.macro-authority.v2"


@dataclass(frozen=True, slots=True)
class MacroDefinition:
    action_key: str
    flow_name: str
    heavy_call_cost: int
    light_mutation_cost: int
    heavy_call_sequence: tuple[str, ...]
    light_mutation_sequence: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.action_key != f"macro.{self.flow_name}":
            raise ValueError("macro_action_flow_mismatch")
        if self.heavy_call_cost != len(self.heavy_call_sequence):
            raise ValueError("macro_heavy_sequence_cost_mismatch")
        if self.light_mutation_cost != len(self.light_mutation_sequence):
            raise ValueError("macro_light_sequence_cost_mismatch")

    def public(self) -> dict[str, object]:
        return {
            "authority_version": MACRO_AUTHORITY_VERSION,
            "action_key": self.action_key,
            "flow_name": self.flow_name,
            "heavy_call_cost": self.heavy_call_cost,
            "light_mutation_cost": self.light_mutation_cost,
            "heavy_call_sequence": list(self.heavy_call_sequence),
            "light_mutation_sequence": list(self.light_mutation_sequence),
        }


_ROWS = (
    MacroDefinition("macro.phys_opt", "phys_opt", 3, 0, ("phys_opt_design", "route_design", "phys_opt_design")),
    MacroDefinition("macro.place_route", "place_route", 2, 2, ("place_design", "route_design"), ("unroute", "unplace")),
    MacroDefinition("macro.auto_pblock", "auto_pblock", 2, 1, ("place_design", "route_design"), ("apply_report_pblock",)),
    MacroDefinition("macro.retime", "retime", 3, 0, ("place_design", "phys_opt_design", "route_design")),
    MacroDefinition("macro.net_swap", "net_swap", 3, 0, ("phys_opt_design", "route_design", "phys_opt_design")),
    MacroDefinition("macro.memory_path_opt", "memory_path_opt", 4, 0, ("place_design", "phys_opt_design", "route_design", "phys_opt_design")),
    MacroDefinition("macro.hardblock_relocate", "hardblock_relocate", 4, 0, ("place_design", "phys_opt_design", "route_design", "phys_opt_design")),
    MacroDefinition("macro.congestion_spread", "congestion_spread", 2, 0, ("phys_opt_design", "route_design")),
    MacroDefinition("macro.fanout", "fanout", 1, 0, ("route_design",)),
    MacroDefinition("macro.lut_cone", "lut_cone", 1, 0, ("route_design",)),
)

MACRO_DEFINITIONS: Mapping[str, MacroDefinition] = MappingProxyType(
    {row.action_key: row for row in _ROWS}
)
M8_ACTION_KEYS = tuple(row.action_key for row in _ROWS[:8])
M10_ACTION_KEYS = tuple(MACRO_DEFINITIONS)
# Backward-compatible names are the historical WP-013 M8 contract.  New
# protocol epochs must opt in to the explicitly versioned M10 names below.
M_ACTION_KEYS = M8_ACTION_KEYS
M_HEAVY_CALLS: Mapping[str, int] = MappingProxyType(
    {key: MACRO_DEFINITIONS[key].heavy_call_cost for key in M8_ACTION_KEYS}
)
M10_HEAVY_CALLS: Mapping[str, int] = MappingProxyType(
    {key: row.heavy_call_cost for key, row in MACRO_DEFINITIONS.items()}
)


def macro_definition(value: str) -> MacroDefinition:
    action_key = value if value.startswith("macro.") else f"macro.{value}"
    try:
        return MACRO_DEFINITIONS[action_key]
    except KeyError as exc:
        raise ValueError("macro_not_admitted") from exc


def public_macro_registry() -> dict[str, object]:
    return {
        "schema_version": MACRO_AUTHORITY_VERSION,
        "actions": [row.public() for row in _ROWS],
    }


__all__ = [
    "MACRO_AUTHORITY_VERSION", "MACRO_DEFINITIONS", "M8_ACTION_KEYS",
    "M10_ACTION_KEYS", "M_ACTION_KEYS", "M_HEAVY_CALLS", "M10_HEAVY_CALLS",
    "MacroDefinition", "macro_definition",
    "public_macro_registry",
]
