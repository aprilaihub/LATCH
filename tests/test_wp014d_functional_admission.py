from __future__ import annotations

import importlib.util
from pathlib import Path


MODULE_PATH = Path(__file__).parents[1] / "scripts" / "run_wp014d_functional_admission.py"
SPEC = importlib.util.spec_from_file_location("run_wp014d_functional_admission", MODULE_PATH)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def report(**updates):
    value = {
        "phase1_passed": True,
        "phase2_passed": True,
        "phase2_skipped": False,
        "infrastructure_failure": False,
        "num_vectors": 200,
        "simulation_report": {
            "cycles_simulated": 200,
            "mismatch_count": 0,
            "protocol_mismatch_count": 0,
            "result_fail_seen": False,
        },
    }
    value.update(updates)
    return value


def test_strict_functional_pass_requires_full_phase2() -> None:
    assert MODULE.classify(report(), 0, False) == "FUNCTIONAL_PASS"
    assert MODULE.classify(report(phase2_skipped=True), 0, False) == "VALIDATOR_UNSUPPORTED_SKIPPED"
    assert MODULE.classify(report(phase2_passed=False), 0, False) == "FUNCTIONAL_INCOMPLETE"


def test_definitive_mismatch_is_not_infrastructure() -> None:
    value = report(phase2_passed=False)
    value["simulation_report"]["mismatch_count"] = 1
    value["simulation_report"]["result_fail_seen"] = True
    assert MODULE.classify(value, 1, False) == "FUNCTIONAL_MISMATCH"


def test_structural_and_infrastructure_classes_are_separate() -> None:
    assert MODULE.classify(report(phase1_passed=False, phase2_passed=False), 1, False) == "STRUCTURAL_GATE_REJECTION"
    assert MODULE.classify(report(infrastructure_failure=True, phase2_passed=False), 2, False) == "INFRASTRUCTURE_FAILURE"
    assert MODULE.classify(None, None, True) == "TIMEOUT"


def test_digest_excludes_no_hidden_state() -> None:
    assert MODULE.digest({"a": 1}) == MODULE.digest({"a": 1})
    assert MODULE.digest({"a": 1}) != MODULE.digest({"a": 2})
