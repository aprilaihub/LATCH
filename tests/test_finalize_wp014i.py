import importlib.util
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "finalize_wp014i", ROOT / "scripts/finalize_wp014i.py"
)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)


def test_synthetic_admission_uses_latest_numbered_continuation(tmp_path):
    run = tmp_path
    (run / "final/candidate").mkdir(parents=True)
    (run / "synthetic-timing-recon-summary.json").write_text(json.dumps({
        "cohort": ["candidate"], "summary_sha256": "summary",
        "classification": "test", "routing_attempts": 0,
        "route_terminals": {}, "provider_or_llm_calls": 0,
        "optimization_calls": 0, "holdout_optimization_calls": 0,
        "owned_process_survivors": [],
    }))
    (run / "final/candidate/final-result.json").write_text(json.dumps({
        "terminal": "SYNTHETIC_V0_SCORE_READY", "final_dcp": "/tmp/test.dcp",
        "final_dcp_sha256": "dcp", "synthetic_timing_certificate": {"v": 1},
        "period_ns": 1.0,
    }))
    for name, terminal, digest in (
        ("v1", "VALIDATOR_INFRASTRUCTURE_FAILURE", "old"),
        ("v1-continuation-03", "MULTISEED_FUNCTIONAL_PASS", "new"),
    ):
        path = run / name
        path.mkdir()
        (path / "aggregate.json").write_text(json.dumps({
            "results": [{"case": {"root_id": "candidate"}, "terminal": terminal}],
            "terminal_counts": {terminal: 1}, "aggregate_sha256": digest,
        }))
    value = MODULE._synthetic_admissions(run)
    assert value["admitted_ids"] == ["candidate"]
    assert value["v1_evidence_sha256"] == "new"
    assert value["superseded_v1_sha256"] == "old"
