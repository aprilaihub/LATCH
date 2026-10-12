from scripts.run_wp013_macro_panel import build_jobs


def test_frozen_panel_jobs_are_complete_unique_and_deterministic() -> None:
    roots = {
        "roots": [
            {"root_id": f"r{i}", "filename": f"r{i}.dcp", "sha256": str(i)}
            for i in range(8)
        ]
    }
    macros = {
        "frozen_actions": [
            {
                "action_key": f"macro.m{i}",
                "flow_name": f"m{i}",
                "heavy_call_range": [1, 2],
            }
            for i in range(8)
        ]
    }
    first = build_jobs(roots, macros)
    second = build_jobs(roots, macros)
    assert first == second
    assert len(first) == 64
    assert len({row["job_id"] for row in first}) == 64
    assert len({(row["root_id"], row["action_key"]) for row in first}) == 64
    assert sum(row["heavy_call_upper_bound"] for row in first) == 128
    assert [row["queue_key"] for row in first] == sorted(row["queue_key"] for row in first)
