import importlib.util
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "run_wp014i_synthetic_revalidation",
    ROOT / "scripts/run_wp014i_synthetic_revalidation.py",
)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)


def test_revalidation_plan_is_validation_only_and_new_identity():
    config = json.loads(
        (ROOT / "configs/wp014i/synthetic-external-v1-revalidation.json").read_text()
    )
    assert config["identity"].endswith("CONTINUATION-03")
    assert config["reason"].startswith("SHARED_VALIDATOR_PRIMARY_CLOCK_SOURCE_DEFECT")
    assert config["limits"]["provider_or_llm_calls"] == 0
    assert config["limits"]["optimization_calls"] == 0
    assert config["limits"]["holdout_optimization_calls"] == 0
    assert config["limits"]["maximum_complete_cases"] == 16
    assert config["memory_estimates_gib"]["unknown"] > 0
