from __future__ import annotations

import importlib.util
from pathlib import Path
import sys


REPO = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "analyze_wp013_e0", REPO / "scripts" / "analyze_wp013_e0.py"
)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


def test_projection_is_eight_jobs_and_twenty_eight_heavy_calls_per_root() -> None:
    aggregate = {
        "jobs": [], "terminal_counts": {}, "provider_requests": 0,
        "known_openrouter_cost_usd": 0.0, "actual_heavy_calls_e0": 0,
        "wp013_observed_heavy_total": 242, "queue_wall_seconds": 0.0,
        "accepted_for_evidence_review": True,
    }
    actions = [
        {"median_runtime_seconds": value, "runtime_iqr_seconds": [value - 1, value + 1]}
        for value in range(1, 9)
    ]
    result = MODULE.analyze(aggregate, actions)
    one = result["projections"][0]
    assert one["roots"] == 1
    assert one["total_jobs"] == 8
    assert one["implementation_heavy_upper"] == 28
    assert result["interpretation"]["primary_matrix_ready"] is False
