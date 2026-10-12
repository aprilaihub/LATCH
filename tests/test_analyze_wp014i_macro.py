import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "analyze_wp014i_macro", ROOT / "scripts/analyze_wp014i_macro.py"
)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)


def test_scenario_uses_functional_valid_root_fallback_and_frozen_threshold():
    rows = [
        {"root_id":"r1","action_key":"a","gain_mhz":10,"functional_valid":True,"functional_terminal":"MULTISEED_FUNCTIONAL_PASS","runtime_seconds":1},
        {"root_id":"r1","action_key":"b","gain_mhz":9.5,"functional_valid":True,"functional_terminal":"MULTISEED_FUNCTIONAL_PASS","runtime_seconds":2},
        {"root_id":"r2","action_key":"a","gain_mhz":100,"functional_valid":False,"functional_terminal":"FUNCTIONAL_MISMATCH","runtime_seconds":1},
        {"root_id":"r2","action_key":"b","gain_mhz":2,"functional_valid":True,"functional_terminal":"MULTISEED_FUNCTIONAL_PASS","runtime_seconds":2},
    ]
    result = MODULE.scenario("test", rows, ["a", "b"])
    assert result["functional_valid_cells"] == 3
    assert result["root_cards"][1]["functional_valid_gain_mhz_by_action"]["a"] == 0
    assert result["root_cards"][0]["near_oracle_removed_action_keys"] == ["a", "b"]
    assert result["global_action"] == "b"
