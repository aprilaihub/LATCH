import json
from pathlib import Path

import pytest

from scripts import run_wp014h_macro_validation as subject


def _case(tmp_path: Path) -> dict:
    golden = tmp_path / "golden.dcp"
    revised = tmp_path / "revised.dcp"
    golden.write_bytes(b"golden")
    revised.write_bytes(b"revised")
    hashes = {
        "golden": subject.validator.file_sha256(golden),
        "revised": subject.validator.file_sha256(revised),
    }
    return {
        "case_id": "case-a",
        "golden_dcp": str(golden),
        "revised_dcp": str(revised),
        "resolved_input_sha256": hashes,
    }


def _write_result(root: Path, case: dict) -> None:
    core = {
        "case": case,
        "terminal": "MULTISEED_FUNCTIONAL_PASS",
        "wall_seconds": 1.0,
        "cleanup": {"audit_2": []},
        "input_sha256_before": case["resolved_input_sha256"],
        "input_sha256_after": case["resolved_input_sha256"],
    }
    result = core | {"result_sha256": subject.validator.object_sha256(core)}
    path = root / case["case_id"] / "result.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(result))


def test_resume_reuses_only_hash_verified_immutable_result(tmp_path: Path) -> None:
    case = _case(tmp_path)
    resume = tmp_path / "resume"
    _write_result(resume, case)

    reused, pending = subject._verified_reusable_results([case], resume)

    assert [row["case"]["case_id"] for row in reused] == ["case-a"]
    assert pending == []


def test_resume_rejects_result_hash_drift(tmp_path: Path) -> None:
    case = _case(tmp_path)
    resume = tmp_path / "resume"
    _write_result(resume, case)
    path = resume / "case-a" / "result.json"
    result = json.loads(path.read_text())
    result["terminal"] = "FUNCTIONAL_MISMATCH"
    path.write_text(json.dumps(result))

    with pytest.raises(ValueError, match="resume_result_hash_mismatch"):
        subject._verified_reusable_results([case], resume)


def test_resume_rejects_input_mutation(tmp_path: Path) -> None:
    case = _case(tmp_path)
    resume = tmp_path / "resume"
    _write_result(resume, case)
    Path(case["revised_dcp"]).write_bytes(b"changed")

    with pytest.raises(ValueError, match="resume_input_hash_mismatch"):
        subject._verified_reusable_results([case], resume)
