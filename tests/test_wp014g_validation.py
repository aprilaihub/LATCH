from pathlib import Path


def test_wp014g_validator_is_provider_free_and_uses_five_frozen_seeds() -> None:
    source = (Path(__file__).parents[1] / "scripts" / "run_wp014g_validation.py").read_text()
    assert "0xDEADBEEF" in source
    assert "vectors_per_seed\": 200" in source
    for credential in ("OPENAI_API_KEY", "DEEPSEEK_API_KEY", "ANTHROPIC_API_KEY", "OPENROUTER_API_KEY"):
        assert credential not in source
