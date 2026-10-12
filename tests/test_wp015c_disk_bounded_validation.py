from __future__ import annotations

import json
from pathlib import Path
import os

import pytest

from scripts import run_wp015c_disk_bounded_validation as wave


def test_owned_validator_path_is_exact_child(tmp_path: Path) -> None:
    owned = tmp_path / "scientific"
    owned.mkdir()
    valid = owned / "dcp_validation_exact"
    valid.mkdir()
    assert wave.verify_owned_validator_path(valid, owned) == valid.resolve()
    nested = valid / "nested"
    nested.mkdir()
    with pytest.raises(RuntimeError, match="refuse_unowned_validator_path"):
        wave.verify_owned_validator_path(nested, owned)
    sibling = tmp_path / "dcp_validation_other"
    sibling.mkdir()
    with pytest.raises(RuntimeError, match="refuse_unowned_validator_path"):
        wave.verify_owned_validator_path(sibling, owned)


def test_canary_selection_covers_memory_roots_and_mechanisms(tmp_path: Path) -> None:
    owned = tmp_path / "scientific"
    owned.mkdir()
    definitions = [
        ("high", "root-a", "P", 30),
        ("normal-b", "root-b", "M", 20),
        ("normal-c", "root-c", "G", 10),
    ]
    reusable, cases, dirs = [], {}, []
    for case_id, root_id, mechanism, size in definitions:
        validator = owned / f"dcp_validation_{case_id}"
        validator.mkdir()
        memory_class = "high" if case_id == "high" else "normal"
        reusable.append({"case_id": case_id, "validator_directory": str(validator)})
        cases[case_id] = {
            "case_id": case_id, "root_id": root_id, "mechanism": mechanism,
            "memory_class": memory_class,
        }
        dirs.append({"path": str(validator), "logical_bytes": size})
    selected = wave.select_canary(reusable, cases, {"owned_directories": dirs})
    assert len(selected) == 3
    selected_cases = [cases[case_id] for case_id in selected]
    assert {row["memory_class"] for row in selected_cases} == {"normal", "high"}
    assert len({row["root_id"] for row in selected_cases}) == 3
    assert len({row["mechanism"] for row in selected_cases}) == 3


def test_interrupted_cleanup_is_exact_and_resume_safe(tmp_path: Path) -> None:
    scientific = tmp_path / "scientific"
    output = tmp_path / "validation"
    scientific.mkdir()
    rows, by_case = [], {}
    for index in range(36):
        case_id = f"case-{index:02d}"
        validator = scientific / f"dcp_validation_{index:02d}"
        validator.mkdir()
        (validator / "partial.log").write_text("partial")
        metadata = output / "prepared" / case_id
        metadata.mkdir(parents=True)
        (metadata / "validator.log").write_text("partial")
        rows.append({
            "case_id": case_id, "status": "PREPARATION_INTERRUPTED",
            "validator_directory": str(validator),
        })
        by_case[case_id] = {"case_id": case_id}
    unrelated = scientific / "keep-me"
    unrelated.mkdir()
    entry = {"entry_audit_sha256": "entry", "cases": rows}
    first = wave.cleanup_interrupted_entry(
        plan_by_case=by_case, entry=entry, scientific_repo=scientific, output=output,
    )
    second = wave.cleanup_interrupted_entry(
        plan_by_case=by_case, entry=entry, scientific_repo=scientific, output=output,
    )
    assert first == second
    assert first["event_count"] == 36
    assert unrelated.is_dir()
    assert not list(scientific.glob("dcp_validation_*"))
    assert len(list((output / "wp015c" / "interrupted-entry").iterdir())) == 36


def test_validation_scientific_core_matches_frozen_source() -> None:
    workspace = os.environ.get("LATCH_HISTORICAL_WORKSPACE")
    if not workspace:
        pytest.skip("explicit historical workspace is not configured")
    scientific = Path(workspace) / "wp015-scientific-source-32d852a"
    if not scientific.is_dir():
        pytest.skip("frozen scientific source worktree is not present")
    observed, rows = wave.validation_core(scientific)
    assert observed == wave.EXPECTED_VALIDATION_CORE
    assert len(rows) == len(wave.VALIDATION_CORE_FILES)


def test_case_aggregate_cleanup_is_persist_before_delete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    scientific = tmp_path / "scientific"
    output = tmp_path / "validation"
    validator = scientific / "dcp_validation_case"
    workspace = validator / "xsim_work"
    workspace.mkdir(parents=True)
    (workspace / "asset").write_text("compiled")
    golden = tmp_path / "golden.dcp"
    revised = tmp_path / "revised.dcp"
    golden.write_bytes(b"g")
    revised.write_bytes(b"r")
    case = {
        "case_id": "case", "root_id": "root", "memory_class": "normal",
        "golden_dcp": str(golden), "revised_dcp": str(revised),
        "resolved_input_sha256": {
            "golden": wave.file_sha256(golden), "revised": wave.file_sha256(revised),
        },
    }
    manifest = wave.tree_manifest(workspace)
    prepared_report = {
        "simulation_report": {
            "precheck_report": {"passed": True}, "precheck_passed": True,
            "full_vectors": 200, "full_run_skipped": True,
        }
    }
    prep = {
        "case": case, "validator_directory": str(validator),
        "prepared_workspace": str(workspace),
        "prepared_workspace_manifest_sha256": wave.canonical_hash(manifest),
        "validator_report": prepared_report, "wp015c_reused": True,
    }
    seeds = [1, 2, 3, 4, 5]
    rows = []
    for index, seed in enumerate(seeds):
        row = {
            "vectors_requested": 200, "stimulus_seed": seed,
            "stimulus_seed_hex": f"0x{seed:08X}", "cycles_simulated": 200,
            "mismatch_count": 0, "protocol_mismatch_count": 0,
            "result_pass_seen": True, "result_fail_seen": False,
            "simulator_failed": False, "infrastructure_failure": False,
            "infrastructure_reason": None, "returncode": 0, "passed": True,
            "seed_id": f"seed-{index:02d}",
        }
        row["semantic_result_sha256"] = wave.canonical_hash(wave.semantic_seed_result(row))
        rows.append(row)
    aggregate = wave.persist_case_and_cleanup(
        prep=prep, seed_rows=rows,
        plan={"plan_sha256": "plan", "functional": {"seeds": seeds}},
        output=output, scientific_repo=scientific, wave_id="test",
    )
    assert aggregate["validation"]["terminal"] == "MULTISEED_FUNCTIONAL_PASS"
    assert not validator.exists()
    assert (output / "wp015c" / "cases" / "case" / "aggregate.json").is_file()
    receipt = json.loads((output / "wp015c" / "cleanup-receipts" / "case.json").read_text())
    assert receipt["prepared_workspace_deleted"] is True
    assert receipt["case_aggregate_sha256"] == aggregate["case_aggregate_sha256"]


def test_resume_pending_cleanup_does_not_rerun_seed(tmp_path: Path) -> None:
    scientific = tmp_path / "scientific"
    output = tmp_path / "validation"
    validator = scientific / "dcp_validation_resume"
    validator.mkdir(parents=True)
    (validator / "transient").write_text("owned")
    golden = tmp_path / "golden.dcp"
    revised = tmp_path / "revised.dcp"
    golden.write_bytes(b"g")
    revised.write_bytes(b"r")
    case = {
        "case_id": "resume", "golden_dcp": str(golden), "revised_dcp": str(revised),
        "resolved_input_sha256": {
            "golden": wave.file_sha256(golden), "revised": wave.file_sha256(revised),
        },
    }
    core = {
        "schema_version": "dof.wp015c-case-aggregate.v1", "case": case,
        "wave_id": "wave", "validation": {"terminal": "MULTISEED_FUNCTIONAL_PASS"},
    }
    aggregate = core | {"case_aggregate_sha256": wave.canonical_hash(core)}
    wave.write(output / "wp015c" / "cases" / "resume" / "aggregate.json", aggregate)
    intent = {
        "case_id": "resume", "validator_directory": str(validator),
        "validator_logical_bytes_before": 5,
    }
    intent["intent_sha256"] = wave.canonical_hash(intent)
    wave.write(output / "wp015c" / "cleanup-intents" / "resume.json", intent)
    events = wave.resume_pending_cleanups(
        output=output, scientific_repo=scientific, plan_by_case={"resume": case},
    )
    assert len(events) == 1
    assert not validator.exists()
    assert events[0]["resumed_cleanup_only"] is True
    assert wave.resume_pending_cleanups(
        output=output, scientific_repo=scientific, plan_by_case={"resume": case},
    ) == []
