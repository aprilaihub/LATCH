import importlib.util
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("analyze_wp014f", REPO / "scripts" / "analyze_wp014f.py")
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)


def test_compact_aggregate_preserves_provenance_and_omits_heavy_report() -> None:
    aggregate = {
        "planned_cases": 1,
        "executed_cases": 1,
        "workers": 16,
        "terminal_counts": {"MULTISEED_FUNCTIONAL_PASS": 1},
        "queue_wall_seconds": 2.0,
        "summed_case_wall_seconds": 2.0,
        "aggregate_sha256": "a" * 64,
        "all_inputs_unchanged": True,
        "owned_process_survivors": [],
        "results": [{
            "case": {"case_id": "x", "root_id": "r", "language": "M", "mechanism": "retime"},
            "terminal": "MULTISEED_FUNCTIONAL_PASS", "wall_seconds": 2.0,
            "resource": {"max_rss_bytes": 100}, "validator_artifact_bytes": 200,
            "input_sha256_before": {"golden": "g", "revised": "r"},
            "input_sha256_after": {"golden": "g", "revised": "r"},
            "result_sha256": "b" * 64, "log_sha256": "c" * 64,
            "validator_report": {"large": "omitted"},
        }],
    }
    compact = MODULE.compact_aggregate("multiseed", aggregate)
    assert compact["results"][0]["result_sha256"] == "b" * 64
    assert "validator_report" not in compact["results"][0]
    assert compact["results"][0]["input_unchanged"] is True
