from optimizer_pipeline.dof.seed_validation import aggregate_seed_reports


SEEDS = [1, 2, 3]


def prepared():
    return {
        "simulation_report": {
            "precheck_report": {"passed": True}, "precheck_passed": True,
            "full_vectors": 200, "full_run_skipped": True,
        }
    }


def row(seed, passed=True):
    return {
        "vectors_requested": 200, "stimulus_seed": seed,
        "stimulus_seed_hex": f"0x{seed:08X}", "cycles_simulated": 200,
        "mismatch_count": 0 if passed else 1, "protocol_mismatch_count": 0,
        "result_pass_seen": passed, "result_fail_seen": not passed,
        "simulator_failed": False, "infrastructure_failure": False,
        "infrastructure_reason": None, "returncode": 0, "passed": passed,
    }


def test_aggregate_requires_every_seed_exactly_once() -> None:
    result = aggregate_seed_reports(
        prepared_report=prepared(), seed_reports=[row(3), row(1), row(2)],
        expected_seeds=SEEDS,
    )
    assert result["terminal"] == "MULTISEED_FUNCTIONAL_PASS"
    assert result["all_seeds_present_exactly_once"] is True
    duplicate = aggregate_seed_reports(
        prepared_report=prepared(), seed_reports=[row(1), row(1), row(2)],
        expected_seeds=SEEDS,
    )
    assert duplicate["terminal"] == "FUNCTIONAL_INCOMPLETE"


def test_aggregate_preserves_mismatch_terminal() -> None:
    result = aggregate_seed_reports(
        prepared_report=prepared(), seed_reports=[row(1), row(2, False), row(3)],
        expected_seeds=SEEDS,
    )
    assert result["terminal"] == "FUNCTIONAL_MISMATCH"
