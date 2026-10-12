import importlib.util
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "run_wp014i_m8_validation", ROOT / "scripts/run_wp014i_m8_validation.py"
)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)


def test_m8_plan_is_validation_only_and_memory_aware():
    plan = json.loads((ROOT / "configs/wp014i/m8-validation-v1.json").read_text())
    assert plan["limits"]["provider_or_llm_calls"] == 0
    assert plan["limits"]["optimization_calls"] == 0
    assert plan["limits"]["target_concurrency"] == 16
    assert plan["limits"]["reserve_fraction"] >= 0.20


def test_m8_panel_resolves_all_80_immutable_children():
    plan = json.loads((ROOT / "configs/wp014i/m8-validation-v1.json").read_text())
    cases = MODULE.resolve(plan)
    assert len(cases) == 80
    assert len({row["case_id"] for row in cases}) == 80
    assert {row["root_id"] for row in cases} == {
        "amd_mini-isp", "corescore_500_mod", "finn_radioml", "logicnets_jscl",
        "rosetta_3d-rendering", "rosetta_digit-recognition", "rosetta_optical-flow",
        "rosetta_spam-filter", "vexriscv_re-place", "vexriscv_re-place_v2",
    }
