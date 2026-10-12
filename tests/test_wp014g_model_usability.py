from scripts import run_wp014g_model_usability as wp014g


def test_cost_control_uses_only_direct_openai_and_deepseek() -> None:
    assert wp014g.MODELS == (
        "openai/gpt-5.6-terra", "deepseek/deepseek-v4-pro",
    )
    assert len(wp014g.ARCHITECTURES) == 3
    assert len(wp014g.TASKS) == 4
    assert len(wp014g.MODELS) * len(wp014g.ARCHITECTURES) * len(wp014g.TASKS) == 24


def test_architecture_cards_require_lowered_causal_binding() -> None:
    text = " ".join(wp014g.ARCHITECTURES.values())
    assert "producer" in text.lower()
    assert "consumer" in text.lower()
    assert "unused" in text.lower()
