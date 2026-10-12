from pathlib import Path


def test_wp014h_gate0_audit_is_public_safe() -> None:
    source = (Path(__file__).parents[1] / "scripts" / "audit_wp014h_gate0.py").read_text()
    assert '"credential_presence": credentials' in source
    assert '"secret_values_recorded": False' in source
    assert '"path_disclosed": False' in source
    assert '"sealed_holdout_optimisation_accesses": 0' in source
    assert "os.environ.get(name)" in source
    assert "os.environ[name]" not in source
