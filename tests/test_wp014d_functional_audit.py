from __future__ import annotations

import importlib.util
from pathlib import Path


MODULE_PATH = Path(__file__).parents[1] / "scripts" / "audit_wp014d_functional_admission.py"
SPEC = importlib.util.spec_from_file_location("audit_wp014d_functional_admission", MODULE_PATH)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def passing_result() -> dict:
    return {
        "strict_classification": "FUNCTIONAL_PASS",
        "returncode": 0,
        "timed_out": False,
        "validator_report": {
            "phase1_passed": True,
            "phase2_passed": True,
            "phase2_skipped": False,
            "infrastructure_failure": False,
            "num_vectors": 200,
            "simulation_report": {
                "cycles_simulated": 200,
                "mismatch_count": 0,
                "protocol_mismatch_count": 0,
                "full_report": {
                    "vectors_requested": 200,
                    "cycles_simulated": 200,
                    "mismatch_count": 0,
                    "protocol_mismatch_count": 0,
                    "passed": True,
                },
            },
        },
    }


def test_strict_report_requires_executed_phase2() -> None:
    assert MODULE.strict_report_pass(passing_result())
    skipped = passing_result()
    skipped["validator_report"]["phase2_skipped"] = True
    assert not MODULE.strict_report_pass(skipped)


def test_strict_report_rejects_incomplete_or_mismatch() -> None:
    incomplete = passing_result()
    incomplete["validator_report"]["simulation_report"]["cycles_simulated"] = 199
    assert not MODULE.strict_report_pass(incomplete)
    mismatch = passing_result()
    mismatch["validator_report"]["simulation_report"]["full_report"]["mismatch_count"] = 1
    assert not MODULE.strict_report_pass(mismatch)


def test_embedded_hash_round_trip_and_mutation() -> None:
    body = {"accepted": True, "rows": [1, 2]}
    value = body | {"audit_sha256": MODULE.digest(body)}
    assert MODULE.verify_embedded(value, "audit_sha256")
    value["rows"].append(3)
    assert not MODULE.verify_embedded(value, "audit_sha256")
