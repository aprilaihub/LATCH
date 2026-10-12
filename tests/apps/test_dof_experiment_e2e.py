from __future__ import annotations

import json
import hashlib
from pathlib import Path

import pytest

from optimizer_pipeline.core.store import SQLiteRunStore
from optimizer_pipeline.dof import runner as dof_runner
from optimizer_pipeline.dof.evidence import verify_registered_evidence
from optimizer_pipeline.dof.provenance import RepositoryObservation
from optimizer_pipeline.dof.runner import run_mock_experiment
from optimizer_pipeline.dof.spec import load_experiment_spec


CONFIG_ROOT = (
    Path(__file__).resolve().parents[2]
    / "configs"
    / "experiments"
    / "dof_contract"
)


EXAMPLES = {
    "01_l0_known.json": "VALID_COMPLETION",
    "02_l1_known.json": "VALID_COMPLETION",
    "03_l2_legal_known.json": "VALID_COMPLETION",
    "04_l2_invalid_raw.json": "INVALID_POLICY_OUTPUT",
    "05_l3_legal_plan.json": "VALID_COMPLETION",
    "06_l3_forbidden_flow.json": "CAPABILITY_ACTION_REJECTION",
    "07_l2_forced_unknown_masked.json": "MASK_VIOLATION",
    "08_l4_reference_pending.json": "L4_REFERENCE_PENDING",
}


@pytest.fixture(autouse=True)
def _stable_clean_repository_observation(monkeypatch: pytest.MonkeyPatch) -> None:
    empty_hash = hashlib.sha256(b"").hexdigest()
    observation = RepositoryObservation(
        observed_at="2026-08-06T12:00:00.000000+00:00",
        repository_root_identity_sha256="1" * 64,
        head_commit="2" * 40,
        head_tree="3" * 40,
        ref_state="branch",
        branch_name="test-branch",
        tracked_tree_dirty=False,
        staged_entry_count=0,
        unstaged_entry_count=0,
        conflicted_entry_count=0,
        assume_unchanged_entry_count=0,
        skip_worktree_entry_count=0,
        tracked_status_sha256=empty_hash,
        index_flag_status_sha256="5" * 64,
        binary_diff_sha256=hashlib.sha256(b"cached\0\0working\0").hexdigest(),
        submodule_status_sha256=empty_hash,
        submodule_count=0,
        nonclean_submodule_count=0,
        untracked_entry_count=0,
        untracked_status_sha256=empty_hash,
        git_version="git version 2.43.0",
    )
    monkeypatch.setattr(dof_runner, "observe_repository", lambda _anchor: observation)


def _run(name: str, tmp_path: Path, suffix: str = "run") -> tuple[dict, Path]:
    run_dir = tmp_path / f"{Path(name).stem}-{suffix}"
    summary = run_mock_experiment(CONFIG_ROOT / name, run_dir)
    return summary, run_dir


@pytest.mark.parametrize(("name", "expected_endpoint"), EXAMPLES.items())
def test_all_checked_in_conditions_finalize_verifiable_canonical_evidence(
    name: str,
    expected_endpoint: str,
    tmp_path: Path,
) -> None:
    summary, run_dir = _run(name, tmp_path)
    spec = load_experiment_spec(CONFIG_ROOT / name)

    assert summary["endpoint"] == expected_endpoint
    assert summary["evidence_verification"] == "PASS"
    assert summary["canonical_schema_version"] == 2
    assert summary["canonical_integrity_errors"] == []
    assert summary["real_tools_started"] is False
    assert summary["external_model_requests"] == 0
    assert summary["fallback_used"] is False
    assert (run_dir / "run.db").is_file()

    with SQLiteRunStore(run_dir / "run.db", migrate=False) as store:
        assert store.integrity_check() == ()
        assert store.run_events()[-1].status.value == "FINALIZED"
        verification = verify_registered_evidence(
            store,
            run_dir=run_dir,
            expected_run_id=spec.run_id,
            expected_spec_hash=spec.spec_hash,
        )
        assert verification.ok, verification.errors

    expected_sidecars = {
        "experiment_manifest.json",
        "repository_provenance.json",
        "capability_manifest.json",
        "catalogue_manifest.json",
        "mask_manifest.json",
        "prompt_model_manifest.json",
        "tool_environment_manifest.json",
        "decision_events.jsonl",
        "result_summary.json",
    }
    assert {path.name for path in (run_dir / "evidence").iterdir()} == expected_sidecars
    assert all(
        path.stat().st_mode & 0o222 == 0 for path in (run_dir / "evidence").iterdir()
    )


def test_level_separation_call_counts_and_rejections_have_no_fake_attempts(
    tmp_path: Path,
) -> None:
    summaries = {
        name: _run(name, tmp_path)[0]
        for name in EXAMPLES
    }

    l0_used = summaries["01_l0_known.json"]["budget"]["used"]
    assert l0_used["MUTATION_LIGHT"] == 0
    assert l0_used["IMPLEMENTATION_HEAVY"] == 0
    assert l0_used["OUTER_ATTEMPT"] == 0
    assert summaries["02_l1_known.json"]["budget"]["used"]["LLM_REQUEST"] == 0
    assert summaries["03_l2_legal_known.json"]["budget"]["used"]["LLM_REQUEST"] == 1
    assert summaries["05_l3_legal_plan.json"]["budget"]["used"] == {
        "READ_ONLY": 1,
        "MUTATION_LIGHT": 2,
        "IMPLEMENTATION_HEAVY": 3,
        "VALIDATION": 6,
        "LLM_REQUEST": 1,
        "OUTER_ATTEMPT": 5,
    }
    assert summaries["05_l3_legal_plan.json"]["accepted_steps"] == 5

    for name in (
        "04_l2_invalid_raw.json",
        "06_l3_forbidden_flow.json",
        "07_l2_forced_unknown_masked.json",
        "08_l4_reference_pending.json",
    ):
        run_dir = tmp_path / f"{Path(name).stem}-run"
        with SQLiteRunStore(run_dir / "run.db", migrate=False) as store:
            assert store.attempts() == ()
            assert store.outcomes() == ()
            assert store.actions() == ()
            assert store.proposals() == ()

    l4_used = summaries["08_l4_reference_pending.json"]["budget"]["used"]
    assert set(l4_used.values()) == {0}


def test_l1_is_deterministic_and_l1_l2_catalogue_bytes_are_identical(
    tmp_path: Path,
) -> None:
    first, first_dir = _run("02_l1_known.json", tmp_path, "first")
    second, second_dir = _run("02_l1_known.json", tmp_path, "second")
    l2, l2_dir = _run("03_l2_legal_known.json", tmp_path, "l2")

    ignored = {"run_dir"}
    assert {k: v for k, v in first.items() if k not in ignored} == {
        k: v for k, v in second.items() if k not in ignored
    }
    assert (
        first_dir / "evidence" / "decision_events.jsonl"
    ).read_bytes() == (
        second_dir / "evidence" / "decision_events.jsonl"
    ).read_bytes()
    assert (
        first_dir / "evidence" / "catalogue_manifest.json"
    ).read_bytes() == (
        l2_dir / "evidence" / "catalogue_manifest.json"
    ).read_bytes()
    assert first["proposal_hash"] != l2["proposal_hash"]


def test_forced_unknown_serialized_context_is_clean_but_keeps_live_measurement(
    tmp_path: Path,
) -> None:
    _, run_dir = _run("07_l2_forced_unknown_masked.json", tmp_path)
    rows = [
        json.loads(line)
        for line in (run_dir / "evidence" / "decision_events.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    model_context = next(row for row in rows if row["record_type"] == "MODEL_CONTEXT")
    serialized = model_context["serialized_context"]

    assert "secret-benchmark" not in serialized
    assert "/archive/" not in serialized
    assert "0123456789abcdef" not in serialized
    assert "macro.retime" not in serialized
    assert '"estimated_fmax_mhz":380.0' in serialized


def test_active_manifest_hash_bindings_are_persisted(tmp_path: Path) -> None:
    _, run_dir = _run("03_l2_legal_known.json", tmp_path)
    experiment = json.loads(
        (run_dir / "evidence" / "experiment_manifest.json").read_text()
    )
    capability = json.loads(
        (run_dir / "evidence" / "capability_manifest.json").read_text()
    )
    catalogue = json.loads(
        (run_dir / "evidence" / "catalogue_manifest.json").read_text()
    )
    mask = json.loads((run_dir / "evidence" / "mask_manifest.json").read_text())
    prompt_model = json.loads(
        (run_dir / "evidence" / "prompt_model_manifest.json").read_text()
    )
    declared = experiment["spec"]

    assert declared["catalogue"]["capability_hash"] == capability["sha256"]
    assert declared["catalogue"]["catalogue_hash"] == catalogue["sha256"]
    assert declared["mask"]["hash"] == mask["hash"]
    assert declared["interface"] == prompt_model["interface_binding"]


@pytest.mark.parametrize(
    ("base_name", "budget_updates", "expected_used"),
    [
        (
            "01_l0_known.json",
            {"max_read_calls": 0},
            {
                "READ_ONLY": 0,
                "MUTATION_LIGHT": 0,
                "IMPLEMENTATION_HEAVY": 0,
                "VALIDATION": 0,
                "LLM_REQUEST": 0,
                "OUTER_ATTEMPT": 0,
            },
        ),
        (
            "02_l1_known.json",
            {"whole_run_wall_time_seconds": 0.05},
            {
                "READ_ONLY": 1,
                "MUTATION_LIGHT": 0,
                "IMPLEMENTATION_HEAVY": 0,
                "VALIDATION": 1,
                "LLM_REQUEST": 0,
                "OUTER_ATTEMPT": 0,
            },
        ),
    ],
)
def test_platform_and_dispatch_budget_rejections_finalize_before_attempt(
    base_name: str,
    budget_updates: dict,
    expected_used: dict,
    tmp_path: Path,
) -> None:
    payload = json.loads((CONFIG_ROOT / base_name).read_text())
    payload["condition_id"] = f"{payload['condition_id']}-budget-reject"
    payload["budget"].update(budget_updates)
    spec_path = tmp_path / f"budget-{base_name}"
    spec_path.write_text(json.dumps(payload), encoding="utf-8")
    (tmp_path / "mock_root.mockdcp").write_bytes(
        (CONFIG_ROOT / "mock_root.mockdcp").read_bytes()
    )

    summary = run_mock_experiment(spec_path, tmp_path / f"run-{base_name}")

    assert summary["endpoint"] == "BUDGET_REJECTION"
    assert summary["budget"]["used"] == expected_used
    assert summary["evidence_verification"] == "PASS"
    with SQLiteRunStore(tmp_path / f"run-{base_name}" / "run.db", migrate=False) as store:
        assert store.attempts() == ()
        assert store.outcomes() == ()
        statuses = [item.status.value for item in store.validations(subject_id="state-root")]
        if base_name.startswith("01_"):
            assert statuses == ["NOT_RUN"]
        else:
            assert statuses == ["NOT_RUN", "PASSED"]
