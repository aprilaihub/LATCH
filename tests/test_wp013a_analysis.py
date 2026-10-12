from __future__ import annotations

from scripts.analyze_wp013a_e0 import action_name, brief, quantile, summary


def test_quantiles_and_summary_are_deterministic() -> None:
    assert quantile([4, 1, 3, 2], 0.25) == 1.75
    assert summary([1, 2, 3])["median"] == 2
    assert summary([])["n"] == 0


def test_action_name_uses_only_canonical_attempt_prefix() -> None:
    assert action_name({"execution": {"attempt_id": "attempt:node_001_phys_opt"}}) == "phys_opt"
    assert action_name({"execution": {"attempt_id": "attempt:other"}}) is None
    assert action_name({}) is None


def test_brief_preserves_development_evidence_boundary() -> None:
    text = brief({
        "execution": {"heavy_calls": 44, "heavy_cap": 64, "queue_wall_seconds": 602.0},
        "planning": {"total_requests": 21},
        "provider": {"known_cost_usd": 0.2, "cost_cap_usd": 1.0},
    })
    assert "does not authorize or launch a primary matrix" in text
    assert "GPT_PRO" not in text
    assert "All 12 model sessions committed language M" in text
