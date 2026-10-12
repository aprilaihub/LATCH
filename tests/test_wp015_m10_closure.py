import json
from pathlib import Path

from scripts.run_wp015_m10_fresh import resolve


REPO = Path(__file__).resolve().parents[1]


def test_fresh_m10_is_exact_three_by_ten_development_cross_product() -> None:
    config = json.loads((REPO / "configs/wp015/m10-development-closure-v1.json").read_text())
    roots, jobs = resolve(config)
    assert set(roots) == {"boom_soc", "ispd16_example2", "vtr_mcml"}
    assert len(jobs) == 30
    assert len({(row["root_id"], row["action_key"]) for row in jobs}) == 30
    assert {row["action_key"] for row in jobs} == {
        "macro.phys_opt", "macro.place_route", "macro.auto_pblock", "macro.retime",
        "macro.net_swap", "macro.memory_path_opt", "macro.hardblock_relocate",
        "macro.congestion_spread", "macro.fanout", "macro.lut_cone",
    }
