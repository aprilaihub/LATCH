from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts.resume_wp015c_post_agent_references import (
    completed_artifact_paths,
    recover_completed_result,
    validate_existing_result,
)


def _write(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(value, (dict, list)):
        path.write_text(json.dumps(value), encoding="utf-8")
    else:
        path.write_text(str(value), encoding="utf-8")


def _job() -> dict[str, object]:
    return {
        "job_id": "wp015-ref__root__phys_opt",
        "root_id": "root",
        "resource_class": "NORMAL",
        "action_key": "macro.phys_opt",
        "flow_name": "phys_opt",
        "reserved_heavy_calls": 3,
    }


def test_completed_artifacts_require_all_frozen_outputs(tmp_path: Path) -> None:
    job_dir = tmp_path / "job"
    expected = [
        job_dir / "flow_matrix_summary.json",
        job_dir / "flow_matrix_manifest.json",
        job_dir / "status/01_phys_opt.status.json",
        job_dir / "runs/01_phys_opt/result_manifest.json",
    ]
    for path in expected[:-1]:
        _write(path, {})
    assert completed_artifact_paths(job_dir, "phys_opt") == []
    _write(expected[-1], {})
    assert completed_artifact_paths(job_dir, "phys_opt") == expected


def test_recover_complete_backend_without_rerun(tmp_path: Path) -> None:
    job = _job()
    job_dir = tmp_path / str(job["job_id"])
    child = job_dir / "outputs/child.dcp"
    _write(child, "checkpoint")
    _write(job_dir / "flow_matrix_summary.json", {
        "flows": [{
            "flow": "phys_opt", "exit_code": 0, "duration_seconds": 12.5,
            "command": ["python", "run_flow_matrix.py"],
        }],
    })
    _write(job_dir / "flow_matrix_manifest.json", {})
    _write(job_dir / "status/01_phys_opt.status.json", {})
    _write(job_dir / "runs/01_phys_opt/result_manifest.json", {})
    module = SimpleNamespace(
        attempted_result=lambda *_: {
            "canonical_endpoint": "VALID_COMPLETION", "output_dcp": str(child),
        },
        _actual_heavy_calls=lambda *_: (2, {
            "place_design": 0, "route_design": 1, "phys_opt_design": 1,
        }),
    )

    result, receipt = recover_completed_result(job, job_dir, module, survivor_audit=[])

    assert result["canonical_terminal"] == "VALID_COMPLETION"
    assert result["functional_terminal"] == "PENDING_V1"
    assert result["process"]["endpoint"] == "RECOVERED_COMPLETED_AFTER_CONTROLLER_LOSS"
    assert result["process"]["cleanup_ok"] is True
    assert result["provider_requests"] == 0
    assert result["heavy_reservation_respected"] is True
    assert receipt["classification"] == "BACKEND_COMPLETE_CELL_RECORD_INTERRUPTED"
    assert receipt["receipt_sha256"] == result["controller_recovery"]["receipt_sha256"]


def test_existing_result_rejects_changed_child(tmp_path: Path) -> None:
    job = _job()
    child = tmp_path / "child.dcp"
    _write(child, "changed")
    result_path = tmp_path / "cell-result.json"
    _write(result_path, dict(job) | {
        "provider_requests": 0,
        "cost_usd": 0.0,
        "resolved_child_path": str(child),
        "resolved_child_sha256": "0" * 64,
        "actual_heavy_calls": 0,
        "actual_heavy_call_counts": {
            "place_design": 0, "route_design": 0, "phys_opt_design": 0,
        },
    })
    module = SimpleNamespace(_actual_heavy_calls=lambda *_: (0, {
        "place_design": 0, "route_design": 0, "phys_opt_design": 0,
    }))

    with pytest.raises(ValueError, match="existing_reference_child_hash_mismatch"):
        validate_existing_result(job, result_path, module)
