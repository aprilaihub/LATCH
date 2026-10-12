from pathlib import Path

from scripts.run_wp014h_benchmark_admission import admission_v1_terminal, parse_probe


def test_parse_probe_requires_explicit_routing_and_preserves_clock(tmp_path: Path) -> None:
    path = tmp_path / "probe.txt"
    path.write_text(
        "WP014H_ROOT_ADMISSION_V1\n"
        "status=OK\n"
        "part=xcvu3p-ffvc1517-2-e\n"
        "clock_count=1\n"
        "clock=clk_fpl26contest|1.500\n"
        "route_status=# of routable nets : 42;# of fully routed nets : 42;# of nets with routing errors : 0;\n"
    )
    parsed = parse_probe(path)
    assert parsed["fully_routed"] is True
    assert parsed["clocks"] == [{"name": "clk_fpl26contest", "period_ns": 1.5}]


def test_parse_probe_does_not_infer_route_pass_from_zero_alone(tmp_path: Path) -> None:
    path = tmp_path / "probe.txt"
    path.write_text("status=OK\npart=xcvu3p\nclock=clk|2.0\nroute_status=Number of unrouted nets = 0\n")
    assert parse_probe(path)["fully_routed"] is False


def test_no_clock_validator_terminal_is_candidate_incompatibility() -> None:
    result = {"terminal": "VALIDATOR_INFRASTRUCTURE_FAILURE"}
    assert admission_v1_terminal(result, "Fatal error: No clock signal found in design") == "VALIDATOR_INCOMPATIBLE_NO_CLOCK_PORT"
