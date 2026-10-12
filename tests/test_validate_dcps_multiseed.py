import asyncio
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from validate_dcps import (
    DCPValidator,
    DEFAULT_STIMULUS_SEED,
    ValidatorIncompatibility,
    parse_stimulus_seed,
)


def _design_info() -> dict:
    return {
        "module_name": "demo",
        "ports": {
            "inputs": [
                {"name": "clk", "width": None},
                {"name": "reset", "width": None},
                {"name": "in_data", "width": "[31:0]"},
            ],
            "outputs": [{"name": "out_data", "width": "[31:0]"}],
            "inouts": [],
        },
    }


def test_parse_stimulus_seed_accepts_decimal_and_hex() -> None:
    assert parse_stimulus_seed("3735928559") == DEFAULT_STIMULUS_SEED
    assert parse_stimulus_seed("0xDEADBEEF") == DEFAULT_STIMULUS_SEED


@pytest.mark.parametrize("value", ["-1", "0x100000000", "not-a-seed"])
def test_parse_stimulus_seed_rejects_invalid_values(value: str) -> None:
    with pytest.raises(Exception):
        parse_stimulus_seed(value)


def test_generated_testbench_has_runtime_seed_and_frozen_clock(tmp_path: Path) -> None:
    validator = DCPValidator(
        tmp_path / "golden.dcp",
        tmp_path / "revised.dcp",
        stimulus_seeds=[0xDEADBEEF, 0xE604362F],
    )
    tb = tmp_path / "testbench.v"
    validator.generate_testbench(
        _design_info(),
        _design_info(),
        tb,
        clock_names=["clk"],
        clock_period_ns=4.0,
    )
    text = tb.read_text()
    assert '$value$plusargs("TEST_SEED=%h", lfsr)' in text
    assert "lfsr = 32'hDEADBEEF" in text
    assert "forever #2.000000 clk" in text
    assert validator.interface_audit["clock_ports"] == ["clk"]
    assert validator.interface_audit["reset_ports"] == ["reset"]


def test_primary_clock_sources_precede_fanin_fallback() -> None:
    script = DCPValidator._CLOCK_PORT_DETECT_TCL
    direct = script.index("get_ports -quiet -of_objects $clocks")
    fanin = script.index("get_pins -quiet -of_objects $clocks")
    assert direct < fanin
    assert "if {[dict size $seen] == 0}" in script


def test_clock_port_is_not_redeclared_as_reset(tmp_path: Path) -> None:
    validator = DCPValidator(tmp_path / "golden.dcp", tmp_path / "revised.dcp")
    tb = tmp_path / "overlap.v"
    validator.generate_testbench(
        _design_info(), _design_info(), tb, clock_names=["reset"]
    )
    assert validator.interface_audit["clock_ports"] == ["reset"]
    assert validator.interface_audit["reset_ports"] == []
    assert tb.read_text().count("reg reset;") == 1


def test_directed_profile_is_explicit_and_seeded(tmp_path: Path) -> None:
    validator = DCPValidator(
        tmp_path / "golden.dcp",
        tmp_path / "revised.dcp",
        stimulus_profile="directed",
        stimulus_seeds=[0x23B7FC5D],
    )
    tb = tmp_path / "directed.v"
    validator.generate_testbench(
        _design_info(), _design_info(), tb, clock_names=["clk"]
    )
    text = tb.read_text()
    assert "function [31:0] directed_word" in text
    assert "in_data = directed_word(cycle_count, lfsr);" in text
    assert validator.interface_audit["stimulus_profile"] == "directed"


def test_default_seed_and_mode_remain_backward_compatible(tmp_path: Path) -> None:
    validator = DCPValidator(tmp_path / "golden.dcp", tmp_path / "revised.dcp")
    assert validator.stimulus_seeds == [DEFAULT_STIMULUS_SEED]
    assert validator.stimulus_profile == "random"
    assert validator.simulation_mode == "functional"


def test_exported_top_falls_back_from_null_design_name_to_netlist_top(tmp_path: Path) -> None:
    validator = DCPValidator(tmp_path / "golden.dcp", tmp_path / "revised.dcp")
    validator.structural_report = {
        "golden_design": {
            "design_name": None,
            "netlist_top_cell": "actual_top",
            "top_module": "actual_top",
        }
    }
    exported = tmp_path / "golden_sim.v"
    exported.write_text(
        "module actual_top(clk, out_data);\n"
        "input clk;\noutput out_data;\nwire n;\nendmodule\n"
    )
    info = validator.get_design_info_from_verilog(exported)
    assert info["module_name"] == "actual_top"
    assert info["ports"]["inputs"] == [{"name": "clk", "width": None}]


def test_exported_top_uses_unique_hierarchy_candidate(tmp_path: Path) -> None:
    validator = DCPValidator(tmp_path / "golden.dcp", tmp_path / "revised.dcp")
    validator.structural_report = {
        "golden_design": {"design_name": "null", "netlist_top_cell": "missing"}
    }
    exported = tmp_path / "golden_sim.v"
    exported.write_text(
        "module leaf(a, y);\ninput a;\noutput y;\nwire n;\nendmodule\n"
        "module wrapper(clk, out_data);\ninput clk;\noutput out_data;\n"
        "leaf u_leaf (.a(clk), .y(out_data));\nwire n;\nendmodule\n"
    )
    assert validator.get_design_info_from_verilog(exported)["module_name"] == "wrapper"


def test_zero_observable_io_is_typed_incompatibility(tmp_path: Path) -> None:
    validator = DCPValidator(tmp_path / "golden.dcp", tmp_path / "revised.dcp")
    empty = {
        "module_name": "design_1",
        "ports": {"inputs": [], "outputs": [], "inouts": []},
    }
    with pytest.raises(ValidatorIncompatibility) as error:
        validator.generate_testbench(empty, empty, tmp_path / "tb.v")
    assert error.value.terminal == "VALIDATOR_INCOMPATIBLE_ZERO_OBSERVABLE_IO"


def test_validate_preserves_pre_simulation_incompatibility(tmp_path: Path) -> None:
    validator = DCPValidator(tmp_path / "golden.dcp", tmp_path / "revised.dcp")
    validator.phase1_structural_checks = AsyncMock(return_value=True)
    validator.phase2_functional_simulation = AsyncMock(side_effect=ValidatorIncompatibility(
        "VALIDATOR_INCOMPATIBLE_ZERO_OBSERVABLE_IO",
        "exported functional top has zero inputs and zero outputs",
    ))
    validator.print_final_report = lambda elapsed: None

    assert asyncio.run(validator.validate()) is False
    assert validator.validator_incompatibility == "VALIDATOR_INCOMPATIBLE_ZERO_OBSERVABLE_IO"
    assert validator.infrastructure_failure is False
    assert validator.simulation_report["stage"] == "phase2_pre_simulation"
