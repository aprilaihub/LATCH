from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import run_wp015c_reference_validation as reference


def plan_value() -> dict:
    core = {
        "source_aggregate_sha256": "a" * 64,
        "shared_scientific_core_hash": reference.EXPECTED_SHARED_CORE,
        "repository": {
            "head": reference.EXPECTED_SOURCE_COMMIT,
            "tree": reference.EXPECTED_SOURCE_TREE,
        },
        "bundle_sha256": "bundle",
        "case_count": 1,
        "cases": [{"case_id": "wp015__wp015-ref__root__phys_opt", "root_id": "root"}],
        "provider_or_llm_calls": 0,
        "optimization_calls": 0,
        "transfer_or_holdout_accesses": 0,
    }
    return core | {"plan_sha256": reference.canonical_hash(core)}


def test_reference_plan_verification_is_nondevelopment_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(reference, "git", lambda _repo, *args: {
        ("rev-parse", "HEAD"): reference.EXPECTED_SOURCE_COMMIT,
        ("rev-parse", "HEAD^{tree}"): reference.EXPECTED_SOURCE_TREE,
        ("status", "--porcelain"): "",
    }[args])
    monkeypatch.setattr(reference, "validation_core", lambda _repo: (
        reference.EXPECTED_VALIDATION_CORE, [],
    ))
    bundle = {
        "bundle_sha256": "bundle",
        "roots": {"root": {"split": "FAMILY_HELDOUT"}},
    }
    result = reference.verify_reference_plan(
        plan=plan_value(), bundle=bundle, scientific_repo=tmp_path,
    )
    assert all(result["checks"].values())


def test_reference_plan_rejects_development_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(reference, "git", lambda _repo, *args: {
        ("rev-parse", "HEAD"): reference.EXPECTED_SOURCE_COMMIT,
        ("rev-parse", "HEAD^{tree}"): reference.EXPECTED_SOURCE_TREE,
        ("status", "--porcelain"): "",
    }[args])
    monkeypatch.setattr(reference, "validation_core", lambda _repo: (
        reference.EXPECTED_VALIDATION_CORE, [],
    ))
    bundle = {
        "bundle_sha256": "bundle",
        "roots": {"root": {"split": "DEVELOPMENT_KNOWN"}},
    }
    with pytest.raises(RuntimeError, match="reference_plan_verification_failed"):
        reference.verify_reference_plan(
            plan=plan_value(), bundle=bundle, scientific_repo=tmp_path,
        )


def test_reference_preparations_admit_twenty_with_resource_headroom(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    cases = {
        f"case-{index}": {
            "case_id": f"case-{index}", "root_id": f"root-{index % 4}",
            "memory_class": "normal",
        }
        for index in range(20)
    }
    monkeypatch.setattr(reference, "linux_memory_bytes", lambda: (
        1500 * reference.GIB, 1400 * reference.GIB,
    ))
    monkeypatch.setattr(reference, "owned_transient_bytes", lambda *_args: 0)
    monkeypatch.setattr(reference.shutil, "disk_usage", lambda _path: SimpleNamespace(
        free=1024 * reference.GIB,
    ))
    monkeypatch.setattr(reference, "prepare_case_frozen", lambda case, *_args: {
        "case": case, "prepared_workspace": "/tmp/prepared",
    })
    results, events = reference.run_wave_preparations(
        case_ids=list(cases), cases=cases, plan={}, output=tmp_path,
        scientific_repo=tmp_path, maximum=20, wave_id="wave",
    )
    assert len(results) == 20
    assert max(row["running"] for row in events) == 20
