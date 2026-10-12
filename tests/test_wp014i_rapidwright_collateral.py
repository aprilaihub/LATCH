import importlib.util
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "run_wp014i_rapidwright_collateral", ROOT / "scripts/run_wp014i_rapidwright_collateral.py"
)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)


def test_collateral_plan_is_priority_bounded_and_source_timed():
    plan = json.loads((ROOT / "configs/wp014i/rapidwright-collateral-v1.json").read_text())
    assert plan["limits"]["admitted_roots"] == 1
    assert plan["limits"]["v0_parallel"] == 1
    assert plan["limits"]["v1_parallel"] == 1
    assert plan["limits"]["optimization_calls"] == 0
    assert plan["limits"]["provider_or_llm_calls"] == 0
    assert all(row["source_period_ns"] > 0 for row in plan["candidates"])
    assert all(row["part"].startswith("xcvu3p-") for row in plan["candidates"])


def test_source_xdc_members_are_hash_bound():
    plan = json.loads((ROOT / "configs/wp014i/rapidwright-collateral-v1.json").read_text())
    for row in plan["candidates"]:
        assert MODULE.member_hash(Path(row["dcp"]), row["source_xdc_member"]) == row["source_xdc_sha256"]
