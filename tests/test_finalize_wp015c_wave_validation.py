from __future__ import annotations

from pathlib import Path

import pytest

from scripts import finalize_wp015c_wave_validation as final


def test_verify_wrapper_rejects_payload_drift(tmp_path: Path) -> None:
    case = {"case_id": "case"}
    core = {"case": case, "validation": {"terminal": "MULTISEED_FUNCTIONAL_PASS"}}
    wrapper = core | {"case_aggregate_sha256": final.canonical_hash(core)}
    path = tmp_path / "aggregate.json"
    final.write(path, wrapper)
    assert final.verify_wrapper(path, case)[0] == "case"
    with pytest.raises(RuntimeError, match="case_payload_mismatch"):
        final.verify_wrapper(path, {"case_id": "case", "drift": True})


def test_verify_wrapper_rejects_hash_drift(tmp_path: Path) -> None:
    case = {"case_id": "case"}
    wrapper = {
        "case": case, "validation": {"terminal": "MULTISEED_FUNCTIONAL_PASS"},
        "case_aggregate_sha256": "0" * 64,
    }
    path = tmp_path / "aggregate.json"
    final.write(path, wrapper)
    with pytest.raises(RuntimeError, match="case_aggregate_hash_mismatch"):
        final.verify_wrapper(path, case)
