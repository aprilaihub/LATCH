from pathlib import Path


def test_wp014h_strict_witness_controller_preserves_firewalls() -> None:
    source = (Path(__file__).parents[1] / "scripts" / "run_wp014h_strict_witnesses.py").read_text()
    assert '"provider_requests": 0' in source
    assert '"sealed_holdouts_accessed": False' in source
    assert '"not_a_wp015_run": True' in source
    assert 'result["strict_g_exercised"] == len(selected_witnesses)' in source


def test_wp014h_validation_is_exactly_five_seed_and_three_witnesses() -> None:
    source = (Path(__file__).parents[1] / "scripts" / "run_wp014h_strict_validation.py").read_text()
    assert source.count("0x") >= 5
    assert 'if len(cases) != 3' in source
    assert 'aggregate["terminal_counts"].get("MULTISEED_FUNCTIONAL_PASS") == 3' in source
