from pathlib import Path

import pytest

from scripts.run_wp014h_synthetic_timing_recon import (
    choose_period_policy,
    choose_source_binding,
    evaluate_topology,
    parse_kv,
    root_specific_period,
    source_target_options,
    topologies_stable,
    xdc_text,
)


POLICY = {
    "minimum_clock_sinks": 32,
    "minimum_dominant_fraction": 0.8,
    "maximum_second_domain_fraction": 0.05,
    "reject_unresolved_mmcm_or_pll": True,
}


def probe(*candidates):
    return {"part": "xcvu3p", "sequential_cells": "100", "clock_pins": "100", "candidates": list(candidates)}


def candidate(sinks=96, ports=None, drivers=None, clocking=None, net="clk", domain=None):
    row = {
        "net": net, "sink_clock_pins": sinks,
        "source_ports": ["clk_in"] if ports is None else ports,
        "drivers": [] if drivers is None else drivers,
        "clocking_cells": [] if clocking is None else clocking,
    }
    if domain is not None:
        row["sink_domain_id"] = domain
    return row


def test_single_dominant_port_clock_is_eligible() -> None:
    result = evaluate_topology(probe(candidate()), POLICY)
    assert result["terminal"] == "TOPOLOGY_ELIGIBLE"
    assert result["target"] == {"kind": "port", "name": "clk_in"}


def test_multiclock_and_generated_clock_are_distinct_rejections() -> None:
    multi = evaluate_topology(probe(candidate(80), candidate(20, net="other")), POLICY)
    assert multi["terminal"] == "AMBIGUOUS_MULTICLOCK"
    generated = evaluate_topology(probe(candidate(clocking=["u_mmcm@MMCME4_ADV"])), POLICY)
    assert generated["terminal"] == "GENERATED_CLOCK_UNRESOLVED"


def test_exact_same_sink_domain_collapses_multiple_source_ports() -> None:
    result = evaluate_topology(probe(
        candidate(100, ports=["clk_n"], net="port:clk_n", domain="domain:0000"),
        candidate(100, ports=["clk_p"], net="port:clk_p", domain="domain:0000"),
    ), POLICY)
    assert result["terminal"] == "TOPOLOGY_ELIGIBLE"
    assert result["second_domain_fraction"] == 0.0
    assert result["target"] == {"kind": "port", "name": "clk_n"}
    assert result["dominant_candidate"]["domain_member_count"] == 2
    assert result["dominant_candidate"]["equivalent_source_candidates"] == ["port:clk_n", "port:clk_p"]
    assert source_target_options(result) == [
        {"kind": "port", "name": "clk_n"},
        {"kind": "port", "name": "clk_p"},
    ]


def test_source_binding_uses_target_clock_reach_not_qor() -> None:
    probes = [
        {"terminal": "TIMING_PROBE_PASS", "target_timed_fraction": 0.0, "dcrit_ns": 1.0,
         "target": {"kind": "port", "name": "clk_n"}},
        {"terminal": "TIMING_PROBE_PASS", "target_timed_fraction": 0.99, "dcrit_ns": 100.0,
         "target": {"kind": "port", "name": "clk_p"}},
    ]
    assert choose_source_binding(probes)["target"]["name"] == "clk_p"


def test_equal_sink_counts_do_not_collapse_without_exact_domain_evidence() -> None:
    result = evaluate_topology(probe(
        candidate(100, ports=["clk_a"], net="port:clk_a", domain="domain:0000"),
        candidate(100, ports=["clk_b"], net="port:clk_b", domain="domain:0001"),
    ), POLICY)
    assert result["terminal"] == "AMBIGUOUS_MULTICLOCK"


def test_topology_parser_accepts_legacy_and_exact_domain_rows(tmp_path: Path) -> None:
    path = tmp_path / "probe.txt"
    path.write_text(
        "clock_pins=100\n"
        "candidate=port:legacy|80|clk_legacy||\n"
        "candidate=port:clk_n|100|clk_n|||domain:0000\n"
    )
    parsed = parse_kv(path)
    assert "sink_domain_id" not in parsed["candidates"][0]
    assert parsed["candidates"][1]["sink_domain_id"] == "domain:0000"


def test_unique_bufg_driver_can_supply_pin_target() -> None:
    row = candidate(ports=[], drivers=["u_bufg/O@BUFGCE"])
    assert evaluate_topology(probe(row), POLICY)["target"] == {"kind": "pin", "name": "u_bufg/O"}


def test_no_clock_and_unstable_probe_are_rejected() -> None:
    assert evaluate_topology(probe(), POLICY)["terminal"] == "NO_CLOCK_CANDIDATE"
    assert not topologies_stable(probe(candidate()), probe(candidate(sinks=95)))


def test_root_specific_period_uses_margin_and_ceiling_quantum() -> None:
    assert root_specific_period(2.001, 1.05, 0.01) == 2.11
    assert root_specific_period(3.711, 0.95, 0.01) == 3.53
    assert root_specific_period(0.0101, 0.95, 0.01) == 0.01
    with pytest.raises(ValueError):
        root_specific_period(0, 1.05, 0.01)


def test_fixed_period_requires_every_root_to_pass_ratio_and_coverage() -> None:
    policy = {"fixed_period_ns": 2.5, "fixed_ratio_min": 0.8, "fixed_ratio_max": 1.2}
    good = [{"dcrit_ns": 2.4, "target_timed_fraction": 1.0, "no_clock_pins": 0}, {"dcrit_ns": 2.8, "target_timed_fraction": 0.98, "no_clock_pins": 0}]
    assert choose_period_policy(good, policy, 0.95) == "fixed"
    assert choose_period_policy(good + [{"dcrit_ns": 3.2, "timed_fraction": 1.0, "no_clock_pins": 0}], policy, 0.95) == "root_specific"
    assert choose_period_policy([{**good[0], "no_clock_pins": 1}], policy, 0.95) == "root_specific"
    frozen = {**policy, "selection": "root_specific_frozen_5pct_tightening"}
    assert choose_period_policy(good, frozen, 0.95) == "root_specific"


def test_xdc_is_minimal_and_rejects_tcl_unsafe_identity() -> None:
    text = xdc_text({"kind": "port", "name": "clk"}, 2.5)
    assert text.count("create_clock") == 1
    assert all(word not in text for word in ("false_path", "multicycle", "place_design", "route_design", "set_property"))
    with pytest.raises(ValueError):
        xdc_text({"kind": "port", "name": "bad}name"}, 2.5)


def test_tcl_surfaces_do_not_contain_optimization_or_timing_exceptions() -> None:
    repo = Path(__file__).parents[1]
    combined = (repo / "scripts/wp014h_synthetic_clock_topology.tcl").read_text() + (repo / "scripts/wp014h_synthetic_timing_probe.tcl").read_text()
    for forbidden in ("place_design", "route_design", "phys_opt_design", "set_false_path", "set_multicycle_path"):
        assert forbidden not in combined
    topology = (repo / "scripts/wp014h_synthetic_clock_topology.tcl").read_text()
    assert topology.count("all_fanin") == 2  # one command and one explanatory comment
    assert topology.count("all_fanout") == 1
    timing = (repo / "scripts/wp014h_synthetic_timing_probe.tcl").read_text()
    assert "There are ([0-9]+) register/latch pins with no clock" in timing
    assert "unassociated_clock_pins" in timing
    assert "[llength $pin_clocks] > 0 &&" in timing


def test_checker_vivado_wrapper_delegates_to_pinned_environment() -> None:
    wrapper = Path(__file__).parents[1] / "tools/wp014h/bin/vivado"
    text = wrapper.read_text()
    assert "VIVADO_EXEC" in text
    assert "/home/" not in text
