from pathlib import Path

from scripts.run_wp014i_macro_panel import read, resolve


REPO = Path(__file__).resolve().parents[1]


def test_wp014i_panel_is_exact_20_plus_10() -> None:
    config = read(REPO / "configs" / "wp014i" / "m10-reference-panel-v1.json")
    roots, jobs = resolve(config)
    assert len(roots) == 11
    assert len(jobs) == 30
    fir = [row for row in jobs if row["root_id"] == "fir_systolic_transposed_routed"]
    others = [row for row in jobs if row["root_id"] != "fir_systolic_transposed_routed"]
    assert len(fir) == 10
    assert len(others) == 20
    assert {row["flow_name"] for row in fir} == {
        "phys_opt", "place_route", "auto_pblock", "retime", "net_swap",
        "memory_path_opt", "hardblock_relocate", "congestion_spread",
        "fanout", "lut_cone",
    }
    assert {row["flow_name"] for row in others} == {"fanout", "lut_cone"}
