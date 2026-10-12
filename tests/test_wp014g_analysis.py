from pathlib import Path


def test_wp014g_analysis_declares_no_provider_or_backend_execution() -> None:
    source = (Path(__file__).parents[1] / "scripts" / "analyze_wp014g.py").read_text()
    assert "OPENAI_API_KEY" not in source
    assert "DEEPSEEK_API_KEY" not in source
    assert "subprocess" not in source
    assert 'path.name != "analysis-manifest.json"' in source


def test_exploration_freeze_keeps_wp015_and_holdouts_closed() -> None:
    import json

    path = Path(__file__).parents[1] / "configs" / "wp014g" / "exploration-freeze-v1.json"
    value = json.loads(path.read_text())
    assert value["wp015_status"] == "PAUSED"
    assert value["sealed_holdout_access"] == "FORBIDDEN"
    assert value["provider_exclusions_for_this_probe"] == ["anthropic-direct", "openrouter"]
