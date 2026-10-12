import importlib.util
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "run_wp014f_validation_recon",
    REPO / "scripts" / "run_wp014f_validation_recon.py",
)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)


def _report(*, passed: bool = True, mismatch: int = 0, infrastructure: bool = False) -> dict:
    seeds = [0xDEADBEEF, 0xE604362F]
    return {
        "stimulus_seeds": seeds,
        "phase2_passed": passed,
        "overall_passed": passed,
        "infrastructure_failure": infrastructure,
        "phase2_skipped": False,
        "simulation_report": {
            "mismatch_count": mismatch,
            "protocol_mismatch_count": 0,
            "result_fail_seen": mismatch > 0,
            "seed_reports": [
                {"passed": passed, "stimulus_seed": seed} for seed in seeds
            ],
        },
    }


def test_multiseed_classification_requires_every_seed() -> None:
    assert MODULE.classify(_report(), 0, False, "multiseed") == "MULTISEED_FUNCTIONAL_PASS"
    incomplete = _report()
    incomplete["simulation_report"]["seed_reports"].pop()
    assert MODULE.classify(incomplete, 0, False, "multiseed") == "FUNCTIONAL_INCOMPLETE"


def test_mismatch_is_not_relabelled_as_infrastructure() -> None:
    report = _report(passed=False, mismatch=1)
    assert MODULE.classify(report, 1, False, "multiseed") == "FUNCTIONAL_MISMATCH"


def test_validator_incompatibility_is_preserved() -> None:
    report = {
        "validator_incompatibility": "VALIDATOR_INCOMPATIBLE_TOP_MODULE",
        "overall_passed": False,
    }
    assert MODULE.classify(report, 1, False, "multiseed") == "VALIDATOR_INCOMPATIBLE_TOP_MODULE"


def test_timing_taxonomy_is_separate() -> None:
    assert MODULE.classify(_report(), 0, False, "timing") == "TIMING_SIM_PASS"
    assert MODULE.classify(None, 2, False, "timing") == "TIMING_VALIDATOR_INFRASTRUCTURE_FAILURE"
    assert MODULE.classify(
        _report(passed=False),
        1,
        False,
        "timing",
        "ERROR: [XSIM 43-3462] Unable to annotate SDF delays in the design.",
    ) == "TIMING_SIM_UNSUPPORTED"


def test_resolved_frozen_panel_is_complete() -> None:
    plan = MODULE.read_json(REPO / "configs" / "wp014f" / "validation-recon-v1.json")
    cases = MODULE.resolve_cases(plan)
    assert len(cases) == 16
    assert len({case["case_id"] for case in cases}) == 16
    assert len({case["root_id"] for case in cases}) == 6
    assert {case["language"] for case in cases} >= {"M", "P", "G"}


def test_formal_discovery_has_non_conflating_terminal() -> None:
    plan = MODULE.read_json(REPO / "configs" / "wp014f" / "validation-recon-v1.json")
    discovery = MODULE.formal_discovery(plan)
    assert discovery["terminal"] in {
        "FORMAL_NOT_RUN_TOOL_UNAVAILABLE",
        "FORMAL_TOOL_DISCOVERED_ADAPTER_REQUIRED",
    }
    assert discovery["terminal"] != "FORMAL_FAIL"
