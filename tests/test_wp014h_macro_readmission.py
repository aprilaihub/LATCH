from scripts.run_wp013_macro_panel import attempted_result
from scripts.run_wp014h_macro_readmission import FLOWS, ROOTS, parser

import json
import sqlite3


def test_readmission_campaign_is_exact_and_bounded() -> None:
    assert ROOTS == ("vexriscv_re-place", "logicnets_jscl", "corescore_500_mod")
    assert FLOWS == ("fanout", "lut_cone")
    args = parser().parse_args([
        "--bundle", "bundle.json", "--out", "out", "--python", "python",
    ])
    assert args.max_parallel == 6
    assert args.timeout_seconds == 3600.0


def test_warning_attempt_is_a_valid_completion_with_retained_warning(tmp_path) -> None:
    (tmp_path / "flow_matrix_summary.json").write_text(json.dumps({
        "flows": [{"status": "passed", "exit_code": 0, "duration_seconds": 1.0}],
    }))
    job = tmp_path / "runs" / "01_fanout"
    job.mkdir(parents=True)
    db = sqlite3.connect(job / "run.db")
    db.executescript("""
        CREATE TABLE attempts (attempt_id TEXT, scheduled_at TEXT);
        CREATE TABLE validations (attempt_id TEXT, checked_at TEXT, status TEXT);
        CREATE TABLE outcomes (attempt_id TEXT, finished_at TEXT, status TEXT, error_type TEXT, error_message TEXT, output_artifact_id TEXT, metrics_json TEXT);
    """)
    db.execute("INSERT INTO attempts VALUES (?, ?)", ("a", "1"))
    db.execute("INSERT INTO validations VALUES (?, ?, ?)", ("a", "1", "WARNING"))
    db.execute("INSERT INTO outcomes VALUES (?, ?, ?, ?, ?, ?, ?)", (
        "a", "1", "SUCCEEDED", None, None, "child", json.dumps({"validation_valid": True}),
    ))
    db.commit()
    db.close()
    result = attempted_result(tmp_path, "fanout")
    assert result["canonical_endpoint"] == "VALID_COMPLETION"
    assert result["validation_status"] == "WARNING"
