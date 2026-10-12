#!/usr/bin/env python3
"""Build the WP-014B reconnaissance registry without changing production schemas."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
from typing import Any


REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from optimizer_pipeline.dof.single_submission.contract_v3 import (  # noqa: E402
    G_CARDS, M_CARDS, P_ARGUMENT_SCHEMAS, P_CARDS, QUERY_NAMES,
)
from optimizer_pipeline.dof.vivado_mutation_worker import _DIRECTIVES  # noqa: E402


SCHEMA = "dof.wp014b-capability-registry.v1"
AUTHORITY_IDENTITY = "wp014b-action-surface-continuation-v1"
CLASSIFICATIONS = {
    "INCLUDE_M", "INCLUDE_P_CORE", "INCLUDE_P_ADVANCED_CONDITIONAL",
    "INCLUDE_G_AST_EXTENSION", "INCLUDE_Q_ONLY", "DEVICE_OR_STAGE_CONDITIONAL",
    "DEVICE_OR_FLOW_CONDITIONAL", "EXCLUDE_UNSAFE", "EXCLUDE_REDUNDANT",
    "EXCLUDE_UNSUPPORTED", "EXCLUDE_NOT_RELEVANT_TO_CLAIM", "NEEDS_FURTHER_EVIDENCE",
}

DIRECTIVE_VALUES = {
    "opt_design": ["Explore", "ExploreArea", "ExploreWithRemap", "ExploreSequentialArea", "RuntimeOptimized", "RQS", "Default"],
    "place_design": ["Explore", "AggressiveExplore", "RuntimeOptimized", "Quick", "Default"],
    "phys_opt_design": [
        "Explore", "ExploreWithHoldFix", "ExploreWithAggressiveHoldFix",
        "AggressiveExplore", "AlternateReplication", "AggressiveFanoutOpt",
        "AlternateFlowWithRetiming", "AddRetime", "RuntimeOptimized", "RQS", "Default",
    ],
    "route_design": [
        "Explore", "AggressiveExplore", "NoTimingRelaxation", "MoreGlobalIterations",
        "HigherDelayCost", "AdvancedSkewModeling", "AlternateCLBRouting",
        "RuntimeOptimized", "Quick", "Default",
    ],
}
OTHER_ENUMS = {
    ("place_design", "net_delay_weight"): ["low", "medium", "high"],
    ("place_design", "clock_vtree_type"): ["balanced", "intraSLR", "interSLR"],
    ("place_design", "psip_options"): [
        "critical_cell_opt", "fanout_opt", "very_high_fanout_opt", "equ_drivers_opt",
        "dsp_register_opt", "bram_register_opt", "uram_register_opt", "shift_register_opt",
        "retime", "per_slr_replication_opt", "shift_register_to_pipeline",
        "auto_pipeline_insertion", "control_set_opt",
    ],
    ("get_timing_paths", "delay_type"): ["min", "max", "min_max"],
    ("get_timing_paths", "sort_by"): ["group", "slack"],
}
OFFICIAL_ONLY_PLACE = [
    "AltSpreadLogic_high", "AltSpreadLogic_low", "AltSpreadLogic_medium", "Auto_1", "Auto_2",
    "Auto_3", "EarlyBlockPlacement", "ExtraNetDelay_high", "ExtraNetDelay_low",
    "ExtraPostPlacementOpt", "ExtraTimingOpt", "RQS", "SSI_BalanceSLLs",
    "SSI_BalanceSLRs", "SSI_HighUtilSLRs", "SSI_SpreadLogic_high",
    "SSI_SpreadLogic_low", "SSI_SpreadSLLs", "WLDrivenBlockPlacement",
]
READ_ONLY = {
    "list_property", "list_property_value", "report_property", "get_timing_paths",
    "report_timing", "report_timing_summary", "report_high_fanout_nets",
    "report_design_analysis", "report_qor_assessment", "report_qor_suggestions",
    "get_cells", "get_nets", "get_sites", "get_pblocks", "get_slrs", "get_clock_regions",
}
HEAVY = {"opt_design", "place_design", "phys_opt_design", "route_design", "power_opt_design"}
PBLOCK = {"create_pblock", "resize_pblock", "move_pblock", "add_cells_to_pblock", "remove_cells_from_pblock", "delete_pblocks"}
PLACEMENT_MUTATION = {"place_cell", "unplace_cell", "swap_locs"}
PROPERTY_MUTATION = {"set_property", "reset_property"}
CONTROL_ARGUMENTS = {"quiet", "verbose", "help", "file", "append", "return_string", "name", "debug_log", "timing_summary"}
STAGE = {
    "opt_design": "SYNTHESIZED", "place_design": "SYNTHESIZED_OR_UNPLACED",
    "phys_opt_design": "PLACED_OR_ROUTED", "route_design": "PLACED_OR_ROUTED_FOR_TARGETED",
    "power_opt_design": "SYNTHESIZED_OR_PLACED", "place_cell": "OPEN_DESIGN",
    "unplace_cell": "PLACED_OR_ROUTED", "swap_locs": "PLACED_OR_ROUTED",
}


def digest(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    return hashlib.sha256(encoded).hexdigest()


def help_rows(installed: dict[str, Any]) -> dict[tuple[str, str], dict[str, Any]]:
    return {(row["command"], row["mode"]): row for row in installed["rows"]}


def takes_value(syntax: str, argument: str) -> bool:
    pattern = rf"-{re.escape(argument)}(?:\s+|\]\s*)<(?:arg|args|name|value|object|objects|pattern)>"
    return bool(re.search(pattern, syntax.replace("\n", " "), re.IGNORECASE))


def command_meta(command: str) -> dict[str, Any]:
    if command in READ_ONLY:
        return dict(read_mutation_heavy="READ_ONLY", source_stage="ANY_OPEN_DESIGN",
                    state_transition="UNCHANGED", scoreability="NOT_MUTATING", follow_up=[],
                    cost_class="READ_ONLY_LIGHT", safety="ALLOWLISTED_QUERY", candidate="INCLUDE_Q_ONLY")
    if command in HEAVY:
        transitions = {
            "opt_design": "SYNTHESIZED_TO_OPTIMIZED", "place_design": "UNPLACED_TO_PLACED",
            "phys_opt_design": "PLACED_OR_ROUTED_TO_SAME_STAGE_MUTATED",
            "route_design": "PLACED_TO_ROUTED_OR_TARGETED_ROUTE_MUTATION",
            "power_opt_design": "DESIGN_MUTATED_STAGE_DEPENDENT",
        }
        return dict(read_mutation_heavy="HEAVY_IMPLEMENTATION", source_stage=STAGE[command],
                    state_transition=transitions[command],
                    scoreability="SCOREABLE_IF_ROUTED_ELSE_FOLLOW_UP_REQUIRED",
                    follow_up=[] if command == "route_design" else ["route_design"],
                    cost_class="IMPLEMENTATION_HEAVY", safety="BOUNDED_TYPED_COMMAND",
                    candidate="INCLUDE_P_ADVANCED_CONDITIONAL")
    if command in PBLOCK:
        return dict(read_mutation_heavy="LIGHT_MUTATION", source_stage="OPEN_DESIGN",
                    state_transition="CONSTRAINT_OR_PBLOCK_MUTATION",
                    scoreability="FOLLOW_UP_IMPLEMENTATION_REQUIRED", follow_up=["route_design"],
                    cost_class="LIGHT_MUTATION", safety="BOUNDED_TYPED_TARGET_REQUIRED",
                    candidate="INCLUDE_P_ADVANCED_CONDITIONAL")
    if command in PLACEMENT_MUTATION:
        return dict(read_mutation_heavy="LIGHT_MUTATION", source_stage=STAGE[command],
                    state_transition="PLACEMENT_MUTATION", scoreability="FOLLOW_UP_ROUTE_REQUIRED",
                    follow_up=["route_design"], cost_class="LIGHT_MUTATION",
                    safety="BOUNDED_TYPED_TARGET_REQUIRED", candidate="INCLUDE_P_ADVANCED_CONDITIONAL")
    if command in PROPERTY_MUTATION:
        return dict(read_mutation_heavy="LIGHT_MUTATION", source_stage="OBJECT_DEPENDENT",
                    state_transition="OBJECT_PROPERTY_MUTATION", scoreability="PROPERTY_AND_STAGE_DEPENDENT",
                    follow_up=["PROPERTY_SPECIFIC"], cost_class="LIGHT_MUTATION",
                    safety="GENERIC_FORM_NOT_ALLOWLISTED", candidate="EXCLUDE_UNSAFE")
    raise KeyError(command)


def argument_value_source(command: str, argument: str, has_value: bool) -> tuple[list[Any], str]:
    if argument == "directive" and command in DIRECTIVE_VALUES:
        return DIRECTIVE_VALUES[command], "INSTALLED_HELP_ENUM"
    if (command, argument) in OTHER_ENUMS:
        return OTHER_ENUMS[(command, argument)], "INSTALLED_HELP_ENUM_OR_DOCUMENTED_PATTERN"
    if not has_value:
        return [True], "BOOLEAN_SWITCH"
    object_map = {
        "cells": "CELL_OBJECT_SET", "cell": "CELL_OBJECT_SET", "nets": "NET_OBJECT_SET",
        "pins": "PIN_OBJECT_SET", "of_objects": "TYPED_OBJECT_SET", "objects": "TYPED_OBJECT_SET",
        "of": "TYPED_OBJECT_SET", "from": "TIMING_OR_SPATIAL_OBJECT_SET",
        "to": "TIMING_OR_SPATIAL_OBJECT_SET", "through": "TIMING_OBJECT_SET",
        "locs": "DEVICE_RESOURCE_RANGE", "filter": "BOUNDED_FILTER_EXPRESSION",
        "class": "INSTALLED_OBJECT_CLASS", "path_groups": "PATH_GROUP_SET",
        "force_replication_on_nets": "NET_OBJECT_SET",
    }
    return [], object_map.get(argument, "TYPED_OR_BOUNDED_RUNTIME_VALUE")


def compatibility(command: str, argument: str) -> list[str]:
    if command == "phys_opt_design" and argument == "directive": return ["DIRECTIVE_MODE_EXCLUDES_SPECIFIC_OPTIMISATION_OPTIONS"]
    if command == "phys_opt_design" and argument not in {"directive", "quiet", "verbose", "help"}: return ["SPECIFIC_OPTIONS_MODE_EXCLUDES_DIRECTIVE_MODE"]
    if command == "route_design" and argument == "directive": return ["ONLY_PRESERVE_TNS_CLEANUP_QUIET_VERBOSE_MAY_COMBINE"]
    if command == "place_design" and argument == "directive": return ["ONLY_NO_PSIP_NET_DELAY_WEIGHT_QUIET_VERBOSE_MAY_COMBINE"]
    if command == "resize_pblock" and argument in {"from", "to"}: return ["FROM_AND_TO_MUST_BE_PAIRED"]
    return []


def device_applicability(command: str, argument: str, value: Any) -> str:
    if command == "phys_opt_design" and argument in {"memory_rewire_opt", "interconnect_retime", "lut_opt", "casc_opt"}: return "VERSAL_ONLY_NOT_XCVU3P"
    if command == "phys_opt_design" and argument in {"fanout_opt", "placement_opt", "critical_cell_opt"}: return "NON_VERSAL_APPLICABLE_XCVU3P"
    if command == "phys_opt_design" and argument == "slr_crossing_opt": return "ULTRASCALE_PLUS_APPLICABLE_XCVU3P"
    if command == "route_design" and argument == "directive" and value == "AlternateCLBRouting": return "ULTRASCALE_ONLY_NOT_XCVU3P_PLUS"
    if command == "place_design" and argument in {"clock_vtree_type", "eco", "psip_options", "subdirective"}: return "FLOW_CONDITIONAL"
    return "CURRENT_XCVU3P_CANDIDATE"


def target_type(command: str, argument: str) -> str:
    if argument in {"nets", "force_replication_on_nets"}: return "NET"
    if argument == "pins": return "PIN"
    if argument in {"cells", "cell"}: return "CELL"
    if command in PBLOCK: return "PBLOCK_OR_CELL"
    if command in PLACEMENT_MUTATION: return "CELL_OR_SITE"
    if command in PROPERTY_MUTATION: return "PROPERTY_ALLOWLIST_REQUIRED"
    if command in READ_ONLY: return "QUERY_DEPENDENT"
    return "WHOLE_DESIGN"


def argument_candidate(command: str, argument: str, base: str) -> str:
    if argument in CONTROL_ARGUMENTS: return "EXCLUDE_NOT_RELEVANT_TO_CLAIM"
    if command in READ_ONLY: return "INCLUDE_Q_ONLY"
    if command in PROPERTY_MUTATION: return "EXCLUDE_UNSAFE"
    if argument in {"ultrathreads", "release_memory"}: return "EXCLUDE_NOT_RELEVANT_TO_CLAIM"
    if argument in {"eco", "incremental", "subdirective", "psip_options", "clock_vtree_type"}: return "DEVICE_OR_STAGE_CONDITIONAL"
    if argument in {"directive", "net_delay_weight", "tns_cleanup", "preserve"}: return "INCLUDE_P_CORE"
    if command in PBLOCK | PLACEMENT_MUTATION: return "INCLUDE_P_ADVANCED_CONDITIONAL"
    return base


def exclusion_reason(candidate: str, command: str, argument: str) -> str | None:
    if candidate == "EXCLUDE_UNSAFE": return "generic_property_mutation_requires_object_property_value_allowlist"
    if candidate == "EXCLUDE_NOT_RELEVANT_TO_CLAIM": return f"execution_or_output_control_not_action_semantics:{command}.{argument}"
    return None


def build_registry(installed_path: Path, conflict_path: Path) -> dict[str, Any]:
    installed = json.loads(installed_path.read_text())
    conflict = json.loads(conflict_path.read_text())
    rows_by_key = help_rows(installed)
    capture_root = installed_path.parent.parent
    capabilities: list[dict[str, Any]] = []
    commands = []
    for command in installed["commands"]:
        help_row, syntax_row = rows_by_key[(command, "help")], rows_by_key[(command, "syntax")]
        meta = command_meta(command)
        official_url = f"https://docs.amd.com/r/2025.1-English/ug835-vivado-tcl-commands/{command}"
        commands.append({"command": command, **meta,
            "installed_help": {mode: {"path": rows_by_key[(command, mode)]["relative_path"], "sha256": rows_by_key[(command, mode)]["sha256"], "status": rows_by_key[(command, mode)]["status"]} for mode in installed["modes"]},
            "official_doc": {"url": official_url,
                "local_snapshot_sha256": conflict["official_snapshot_sha256"] if command == "place_design" else None,
                "local_snapshot_scope": "PLACE_DESIGN_DUPLICATE_TOPICS" if command == "place_design" else "NOT_LOCALLY_CAPTURED"}})
        syntax = (capture_root / syntax_row["relative_path"]).read_text(errors="replace")
        for argument in help_row["arguments"]:
            values, value_source = argument_value_source(command, argument, takes_value(syntax, argument))
            candidate = argument_candidate(command, argument, meta["candidate"])
            for value in values or [None]:
                capabilities.append({
                    "capability_id": f"{command}.{argument}" + (f".{value}" if value is not None else ""),
                    "command": command, "argument": argument, "value": value,
                    "allowed_values": values, "value_source": value_source,
                    "authority_status": "EXECUTABLE_CURRENT",
                    "mode_incompatibilities": compatibility(command, argument),
                    "required_design_stage": meta["source_stage"],
                    "device_applicability": device_applicability(command, argument, value),
                    "target_object_type": target_type(command, argument),
                    "read_mutation_heavy": meta["read_mutation_heavy"],
                    "expected_state_transition": meta["state_transition"],
                    "immediately_scoreable": meta["scoreability"], "follow_up_required": meta["follow_up"],
                    "estimated_cost_class": meta["cost_class"], "observed_cost_seconds": None,
                    "safety_containment_class": meta["safety"], "candidate_classification": candidate,
                    "installed_help_sha256": help_row["sha256"], "official_doc_url": official_url,
                    "official_doc_sha256": conflict["official_snapshot_sha256"] if command == "place_design" else None,
                    "real_admission_status": "NOT_YET_ADMITTED", "exclusion_reason": exclusion_reason(candidate, command, argument),
                })
    installed_place = set(DIRECTIVE_VALUES["place_design"])
    for value in OFFICIAL_ONLY_PLACE:
        if value in installed_place: continue
        capabilities.append({
            "capability_id": f"place_design.directive.{value}.official_conditional", "command": "place_design",
            "argument": "directive", "value": value, "allowed_values": OFFICIAL_ONLY_PLACE,
            "value_source": "OFFICIAL_2025_1_CONFLICTING_TOPIC",
            "authority_status": "DOCUMENTED_CONDITIONAL_NOT_EXECUTABLE_CURRENT",
            "mode_incompatibilities": ["NOT_ADMITTED_TO_CURRENT_EXECUTABLE_SURFACE"],
            "required_design_stage": "UNKNOWN_FLOW_SPECIFIC", "device_applicability": "DEVICE_OR_FLOW_CONDITIONAL",
            "target_object_type": "WHOLE_DESIGN", "read_mutation_heavy": "HEAVY_IMPLEMENTATION",
            "expected_state_transition": "DOCUMENTED_ONLY", "immediately_scoreable": "NOT_EXECUTABLE_CURRENT",
            "follow_up_required": [], "estimated_cost_class": "UNKNOWN", "observed_cost_seconds": None,
            "safety_containment_class": "NOT_ADMITTED", "candidate_classification": "DEVICE_OR_FLOW_CONDITIONAL",
            "installed_help_sha256": rows_by_key[("place_design", "help")]["sha256"],
            "official_doc_url": conflict["official_metadata"]["source_url"],
            "official_doc_sha256": conflict["official_snapshot_sha256"],
            "real_admission_status": "NOT_RUN_BY_AUTHORITY_RULE",
            "exclusion_reason": "absent_from_current_installed_help_preserved_as_documented_conditional",
        })
    if not all(row["candidate_classification"] in CLASSIFICATIONS for row in capabilities):
        raise RuntimeError("classification_outside_contract")
    core = {
        "schema_version": SCHEMA, "continuation_identity": AUTHORITY_IDENTITY,
        "environment": {"part": "xcvu3p-ffvc1517-2-e", "vivado": installed["vivado_version_short"], "executable_authority": "installed_vivado_help"},
        "authority_rule": {"executable_current": "installed help argument/value surface",
            "official_only": ["DEVICE_OR_FLOW_CONDITIONAL", "NEEDS_FURTHER_EVIDENCE"],
            "negative_claim_boundary": "not executable-current is not globally unsupported"},
        "commands": commands, "capabilities": capabilities,
        "counts": {"commands": len(commands), "capability_rows": len(capabilities),
            "executable_current_rows": sum(row["authority_status"] == "EXECUTABLE_CURRENT" for row in capabilities),
            "documented_conditional_rows": sum(row["authority_status"].startswith("DOCUMENTED_CONDITIONAL") for row in capabilities)},
    }
    return core | {"registry_sha256": digest(core)}


def current_schema_diff(registry: dict[str, Any]) -> dict[str, Any]:
    installed = {command: sorted({row["value"] for row in registry["capabilities"]
        if row["authority_status"] == "EXECUTABLE_CURRENT" and row["command"] == command
        and row["argument"] == "directive" and row["value"] is not None}) for command in DIRECTIVE_VALUES}
    current = {
        "PLACE": list(P_ARGUMENT_SCHEMAS["PLACE"]["properties"]["directive"]["enum"]),
        "ROUTE": list(P_ARGUMENT_SCHEMAS["ROUTE"]["properties"]["directive"]["enum"]),
        "PHYS_OPT": list(P_ARGUMENT_SCHEMAS["PHYS_OPT"]["properties"]["directive"]["enum"]),
    }
    map_command = {"PLACE": "place_design", "ROUTE": "route_design", "PHYS_OPT": "phys_opt_design"}
    diffs = [{"operation": op, "command": map_command[op], "current_directives": values,
              "executable_current_directives": installed[map_command[op]],
              "current_not_executable": sorted(set(values) - set(installed[map_command[op]])),
              "executable_missing_from_current": sorted(set(installed[map_command[op]]) - set(values))}
             for op, values in current.items()]
    core = {
        "schema_version": "dof.wp014b-current-schema-diff.v1", "continuation_identity": AUTHORITY_IDENTITY,
        "directive_diffs": diffs,
        "current_phys_opt_options": list(P_ARGUMENT_SCHEMAS["PHYS_OPT"]["properties"]["options"]["items"]["enum"]),
        "phys_opt_mode_defect": "current schema permits non-default directive plus options; installed help defines mutually exclusive modes",
        "worker_directive_allowlists": {key: sorted(values) for key, values in _DIRECTIVES.items()},
        "current_M_keys": [row["key"] for row in M_CARDS], "current_P_keys": [row["key"] for row in P_CARDS],
        "current_G_keys": [row["key"] for row in G_CARDS], "current_Q_names": list(QUERY_NAMES),
        "formal_protocol_modified": False,
    }
    return core | {"audit_sha256": digest(core)}


def write_outputs(registry: dict[str, Any], output: Path) -> None:
    output.mkdir(parents=True, exist_ok=True)
    (output / "vivado-2025.1-command-registry.json").write_text(json.dumps(registry, sort_keys=True, indent=2) + "\n")
    with (output / "directive-option-compatibility.csv").open("w", newline="") as stream:
        fields = ["capability_id", "command", "argument", "value", "authority_status", "mode_incompatibilities", "candidate_classification"]
        writer = csv.DictWriter(stream, fieldnames=fields); writer.writeheader()
        for row in registry["capabilities"]:
            if row["mode_incompatibilities"] or row["argument"] in {"directive", "net_delay_weight"}:
                writer.writerow({key: ";".join(row[key]) if isinstance(row[key], list) else row[key] for key in fields})
    with (output / "stage-device-applicability.csv").open("w", newline="") as stream:
        fields = ["capability_id", "required_design_stage", "device_applicability", "target_object_type", "read_mutation_heavy", "follow_up_required"]
        writer = csv.DictWriter(stream, fieldnames=fields); writer.writeheader()
        for row in registry["capabilities"]:
            writer.writerow({key: ";".join(row[key]) if isinstance(row[key], list) else row[key] for key in fields})
    (output / "current-schema-vs-authoritative.json").write_text(json.dumps(current_schema_diff(registry), sort_keys=True, indent=2) + "\n")
    exclusions = [row for row in registry["capabilities"] if row["exclusion_reason"]]
    lines = ["# WP-014B Initial Exclusion Register", "", "This pre-admission register is source-driven. Runtime-dependent classifications remain open.", "", "| Capability | Classification | Reason |", "|---|---|---|"]
    lines.extend(f"| `{row['capability_id']}` | `{row['candidate_classification']}` | {row['exclusion_reason']} |" for row in exclusions)
    (output / "exclusion-register.md").write_text("\n".join(lines) + "\n")
    manifest = {"schema_version": "dof.wp014b-registry-artifacts.v1",
                "source_commit": subprocess.run(("git", "rev-parse", "HEAD"), cwd=REPO, check=True, capture_output=True, text=True).stdout.strip(),
                "files": {}}
    for path in sorted(output.iterdir()):
        if path.name != "manifest.json": manifest["files"][path.name] = {"sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "size_bytes": path.stat().st_size}
    manifest["manifest_sha256"] = digest(manifest)
    (output / "manifest.json").write_text(json.dumps(manifest, sort_keys=True, indent=2) + "\n")


def main() -> int:
    parser = argparse.ArgumentParser(allow_abbrev=False)
    parser.add_argument("--installed-help", type=Path, required=True)
    parser.add_argument("--official-conflict", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    registry = build_registry(args.installed_help, args.official_conflict)
    write_outputs(registry, args.output)
    print(json.dumps(registry["counts"], sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
