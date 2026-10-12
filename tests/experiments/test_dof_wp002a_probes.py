from __future__ import annotations

from scripts.run_wp002a_probes import run_probes


def test_wp002a_independent_style_probe_summary_is_closed() -> None:
    result = run_probes()

    assert result["all_corrected"] is True
    assert result["all_rejections_have_zero_gated_steps"] is True
    assert set(result["checks"].values()) == {False}
    assert result["execution_boundary"] == {
        "real_dcp_attempts": 0,
        "vivado_starts": 0,
        "rapidwright_starts": 0,
        "mcp_starts": 0,
        "external_model_requests": 0,
        "l4_execution": False,
        "schema_migration": False,
    }
