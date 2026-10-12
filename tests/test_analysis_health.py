import json
from pathlib import Path

from optimizer_pipeline.analysis_health import audit_analysis_contexts


def write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")


def test_analysis_context_health_counts_nonempty_evidence(tmp_path: Path):
    write_json(
        tmp_path / "run" / "a" / "analysis_context.json",
        {
            "timing": {"final_fmax_mhz": 410.0, "final_wns_ns": -0.1},
            "critical_path_features": {"route_delay_ratio": 0.72},
            "congestion": {"report_available": False},
            "fanout": {"top_nets": []},
            "candidate_flows": {
                "deterministic_scores": [{"flow": "phys_opt", "flow_heuristic_score": 9.0}],
                "selected_top3": ["phys_opt"],
            },
            "analysis_provider_error": None,
        },
    )
    write_json(
        tmp_path / "run" / "b" / "analysis_context.json",
        {
            "timing": {"final_fmax_mhz": None},
            "critical_path_features": {"dominant_path_type": "unknown"},
            "congestion": {"report_available": False},
            "fanout": {"top_nets": []},
            "candidate_flows": {"deterministic_scores": [], "selected_top3": []},
            "analysis_provider_error": "RuntimeError: broken",
        },
    )

    health = audit_analysis_contexts(tmp_path)

    assert health["total_analysis_contexts"] == 2
    assert health["timing_nonempty"] == 1
    assert health["critical_path_nonempty"] == 1
    assert health["congestion_nonempty"] == 0
    assert health["fanout_nonempty"] == 0
    assert health["deterministic_flow_scores_nonempty"] == 1
    assert health["selected_top3_nonempty"] == 1
    assert health["provider_error_histogram"] == {"RuntimeError: broken": 1}
    assert health["evidence_starved"] is False


def test_analysis_context_health_marks_all_empty_contexts_evidence_starved(tmp_path: Path):
    write_json(
        tmp_path / "run" / "a" / "analysis_context.json",
        {
            "timing": {"final_fmax_mhz": None},
            "critical_path_features": {"dominant_path_type": "unknown"},
            "congestion": {"report_available": False},
            "fanout": {"top_nets": []},
            "candidate_flows": {"deterministic_scores": [], "selected_top3": []},
            "analysis_provider_error": None,
        },
    )

    health = audit_analysis_contexts(tmp_path)

    assert health["total_analysis_contexts"] == 1
    assert health["evidence_starved"] is True


def test_analysis_context_health_counts_artifact_backed_diagnostics(tmp_path: Path):
    write_json(
        tmp_path / "run" / "a" / "analysis_context.json",
        {
            "timing": {"final_fmax_mhz": 410.0},
            "critical_path_features": {
                "dominant_path_type": "unknown",
                "num_paths_analyzed": 2,
                "primitive_mix": {"bram": 2},
                "source_artifacts": ["executor_run/critical_paths.json"],
            },
            "congestion": {
                "report_available": False,
                "evidence_checked": True,
                "hint": "not_observed",
                "source": "executor_log_or_route_report",
            },
            "fanout": {
                "max_fanout": None,
                "num_high_fanout_nets": None,
                "proxy_available": True,
                "proxy_source": "critical_path_cell_recurrence",
                "top_recurrent_cells": [{"name": "top/a_reg", "count": 2}],
            },
            "candidate_flows": {"deterministic_scores": [], "selected_top3": []},
        },
    )

    health = audit_analysis_contexts(tmp_path)

    assert health["critical_path_nonempty"] == 1
    assert health["critical_path_artifact_nonempty"] == 1
    assert health["congestion_nonempty"] == 1
    assert health["congestion_checked_nonempty"] == 1
    assert health["fanout_nonempty"] == 1
    assert health["fanout_proxy_nonempty"] == 1
