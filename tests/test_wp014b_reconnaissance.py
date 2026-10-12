from __future__ import annotations

import json
from pathlib import Path
import os

import pytest

from scripts.capture_wp014b_installed_help import COMMANDS, MODES, sections, tcl
from scripts.audit_wp014b_place_semantics_conflict import audit as audit_place_conflict
from scripts.build_wp014b_registry import build_registry, current_schema_diff
from scripts.analyze_wp014b_properties import analyse as analyse_properties, property_shape
from scripts.freeze_wp014b_admission import matrix as admission_matrix
from scripts.run_wp014b_admission import endpoint as admission_endpoint
from scripts.freeze_wp014b_supplement import main as freeze_supplement_main
from scripts.analyze_wp014b_admission import refined_endpoint


def retained_evidence(path: str) -> Path:
    workspace = os.environ.get("LATCH_HISTORICAL_WORKSPACE")
    if not workspace:
        pytest.skip("explicit historical workspace is not configured")
    evidence = Path(workspace) / path
    if not evidence.exists():
        pytest.skip(f"requires retained WP-014B evidence: {evidence}")
    return evidence


def test_help_capture_covers_required_surface_and_modes():
    required = {
        "opt_design", "place_design", "phys_opt_design", "route_design",
        "power_opt_design", "place_cell", "unplace_cell", "swap_locs",
        "create_pblock", "resize_pblock", "move_pblock", "add_cells_to_pblock",
        "remove_cells_from_pblock", "delete_pblocks", "set_property",
        "reset_property", "list_property", "list_property_value", "report_property",
        "get_timing_paths", "report_timing_summary", "report_high_fanout_nets",
        "report_design_analysis", "report_qor_assessment", "report_qor_suggestions",
        "get_cells", "get_nets", "get_sites", "get_pblocks", "get_slrs",
        "get_clock_regions",
    }
    assert required <= set(COMMANDS)
    assert MODES == ("help", "syntax", "args")
    source = tcl().decode()
    assert "help -syntax place_design" in source
    assert "help -args phys_opt_design" in source
    assert "help -args set_property" in source


def test_help_capture_parser_requires_complete_exact_markers():
    raw = ["DOF_VERSION_BEGIN", "2025.1", "DOF_VERSION_END"]
    for command in COMMANDS:
        for mode in MODES:
            raw.extend((
                f"DOF_HELP_BEGIN:{command}:{mode}",
                f"{command}-{mode}-body",
                f"DOF_HELP_STATUS:{command}:{mode}:0",
                f"DOF_HELP_END:{command}:{mode}",
            ))
    version, parsed = sections(("\n".join(raw) + "\n").encode())
    assert version == "2025.1"
    assert len(parsed) == len(COMMANDS) * len(MODES)
    assert parsed[("place_design", "args")] == (0, b"place_design-args-body")


def test_official_duplicate_place_surfaces_stop_before_real_admission(tmp_path):
    installed = tmp_path / "help.txt"
    installed.write_text(
        "Supported values include:\n *  Explore\n *  AggressiveExplore\n"
        " *  Default\nImportant:\n"
    )
    snapshot = tmp_path / "snapshot.txt"
    snapshot.write_text(
        "- Page URL: https://docs.amd.com/r/2025.1-English/ug835-vivado-tcl-commands/place_design?tocId=new\n"
        "Document ID: UG835\nRelease Date: 2025-06-16\nVersion: 2025.1 English\n"
        "- article [ref=e1]:\n heading \"place_design\"\n"
        "tocId%3Dold\nSupported values include:\n"
        "- listitem [ref=e2]:\n - code [ref=e3]: Explore\n"
        "- listitem [ref=e4]:\n - code [ref=e5]: ExtraNetDelay_high\n"
        "- generic [ref=e6]: Important:\n"
        "- article [ref=e7]:\n heading \"place_design\"\n"
        "tocId%3Dnew\nSupported values include:\n"
        "- listitem [ref=e8]:\n - code [ref=e9]: Explore\n"
        "- listitem [ref=e10]:\n - code [ref=e11]: AggressiveExplore\n"
        "- listitem [ref=e12]:\n - code [ref=e13]: Default\n"
        "- generic [ref=e14]: Important:\n"
    )
    result = audit_place_conflict(installed, snapshot)
    assert result["official_internal_conflict"] is True
    assert result["installed_matching_official_toc_ids"] == ["new"]
    assert result["requires_protocol_owner_alignment"] is True
    assert result["real_dcp_admission_started"] is False


def test_registry_keeps_official_only_values_conditional():
    registry = build_registry(
        retained_evidence("wp014b-runs/installed-help-v1/normalised/installed-help.json"),
        retained_evidence("wp014b-runs/official-installed-conflict-v2.json"),
    )
    assert registry["counts"]["commands"] == 32
    installed_place = {
        row["value"] for row in registry["capabilities"]
        if row["command"] == "place_design" and row["argument"] == "directive"
        and row["authority_status"] == "EXECUTABLE_CURRENT"
    }
    assert installed_place == {"AggressiveExplore", "Default", "Explore", "Quick", "RuntimeOptimized"}
    official_extra = next(
        row for row in registry["capabilities"]
        if row["capability_id"] == "place_design.directive.ExtraNetDelay_high.official_conditional"
    )
    assert official_extra["candidate_classification"] == "DEVICE_OR_FLOW_CONDITIONAL"
    assert official_extra["real_admission_status"] == "NOT_RUN_BY_AUTHORITY_RULE"


def test_schema_diff_exposes_current_place_and_phys_opt_defects():
    registry = build_registry(
        retained_evidence("wp014b-runs/installed-help-v1/normalised/installed-help.json"),
        retained_evidence("wp014b-runs/official-installed-conflict-v2.json"),
    )
    audit = current_schema_diff(registry)
    place = next(row for row in audit["directive_diffs"] if row["operation"] == "PLACE")
    assert place["current_not_executable"] == ["ExtraNetDelay_high"]
    assert "AggressiveExplore" in place["executable_missing_from_current"]
    assert "mutually exclusive" in audit["phys_opt_mode_defect"]
    assert audit["formal_protocol_modified"] is False


def test_property_shape_and_allowlist_are_object_specific():
    assert property_shape("Type: bool\nRead-only: false\n") == ("bool", False)
    aggregate = json.loads(retained_evidence(
        "wp014b-runs/property-capture-v1/aggregate.json"
    ).read_text())
    result = analyse_properties(aggregate)
    loc = next(row for row in result["candidates"] if row["candidate_id"] == "property.cell.LOC")
    assert loc["writable"] is True
    assert loc["present_roots"] == 3
    fixed_route = next(row for row in result["candidates"] if row["candidate_id"] == "property.net.FIXED_ROUTE")
    assert fixed_route["recommended_classification"] == "EXCLUDE_UNSAFE"
    assert result["generic_set_property_disposition"] == "EXCLUDE_UNSAFE"


def test_admission_matrix_is_bounded_and_predeclared():
    rows = admission_matrix()
    assert len(rows) == len({row["job_id"] for row in rows})
    assert sum(row["planned_heavy_calls"] for row in rows) <= 96
    assert {row["root_id"] for row in rows} == {
        "vexriscv_re-place", "logicnets_jscl", "corescore_500_mod",
    }
    assert all(row["timeout_seconds"] <= 1200 for row in rows)


def test_admission_endpoint_keeps_failure_and_qor_classes_distinct():
    assert admission_endpoint(None, True, None) == "TIMEOUT"
    assert admission_endpoint({"open": {"status": 0}, "action": {"status": 1, "detail": "WP014B_NO_APPLICABLE_TARGET"}}, False, 0) == "NO_APPLICABLE_TARGET"
    assert admission_endpoint({"open": {"status": 0}, "action": {"status": 1, "detail": "invalid directive"}}, False, 0) == "UNSUPPORTED_SYNTAX"
    valid = {"open": {"status": 0}, "action": {"status": 0}, "child": {"status": 0}, "qor": {"before": {"wns_ns": -1.0}, "after": {"wns_ns": -0.5}}}
    assert admission_endpoint(valid, False, 0) == "VALID_QOR_GAIN"
    valid["qor"]["after"]["wns_ns"] = -1.2
    assert admission_endpoint(valid, False, 0) == "VALID_NO_QOR_GAIN"


def test_supplement_module_is_importable_without_execution():
    assert callable(freeze_supplement_main)


def test_refined_endpoint_distinguishes_stage_probe_and_tool_failure():
    base = {"endpoint": "TOOL_EXECUTION_FAILURE", "parsed": {"action": {"detail": ""}}}
    assert refined_endpoint(base, "not supported yet for post-route physical synthesis")[0] == "DEVICE_OR_STAGE_INCOMPATIBLE"
    probe = {"endpoint": "TOOL_EXECUTION_FAILURE", "parsed": {"action": {"detail": "pblock resize has invalid range X0Y0"}}}
    assert refined_endpoint(probe, "")[0] == "PROBE_DEFINITION_FAILURE"
    placer = {"endpoint": "TOOL_EXECUTION_FAILURE", "parsed": {"action": {"detail": "Placer could not place all instances"}}}
    assert refined_endpoint(placer, "")[0] == "TOOL_EXECUTION_FAILURE"
