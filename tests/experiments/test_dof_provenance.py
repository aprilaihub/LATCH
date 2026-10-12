from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess

import pytest

from optimizer_pipeline.core.store import SQLiteRunStore
from optimizer_pipeline.dof import runner as dof_runner
from optimizer_pipeline.dof.evidence import verify_registered_evidence
from optimizer_pipeline.dof.provenance import (
    RepositoryConstraintMismatch,
    RepositoryObservation,
    RepositoryProvenanceError,
    enforce_repository_constraint,
    observe_repository,
    repository_constraint_mismatches,
    validate_repository_observation_payload,
)
from scripts.summarize_wp003_gate0_mock_runs import summarize


CONFIG_ROOT = (
    Path(__file__).resolve().parents[2]
    / "configs"
    / "experiments"
    / "dof_contract"
)
EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()
CLEAN_BINARY_DIFF_SHA256 = hashlib.sha256(b"cached\0\0working\0").hexdigest()


def _git(repo: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        text=True,
        capture_output=True,
    )
    return completed.stdout.strip()


def _repository(tmp_path: Path) -> Path:
    repo = tmp_path / "repository"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.name", "Gate Zero Test")
    _git(repo, "config", "user.email", "gate-zero@example.invalid")
    (repo / "tracked.txt").write_text("one\n", encoding="utf-8")
    _git(repo, "add", "tracked.txt")
    _git(repo, "commit", "-m", "initial")
    return repo


def fixed_clean_observation(*, head: str = "1" * 40) -> RepositoryObservation:
    return RepositoryObservation(
        observed_at="2026-08-06T12:00:00.000000+00:00",
        repository_root_identity_sha256="2" * 64,
        head_commit=head,
        head_tree="3" * 40,
        ref_state="branch",
        branch_name="test-branch",
        tracked_tree_dirty=False,
        staged_entry_count=0,
        unstaged_entry_count=0,
        conflicted_entry_count=0,
        assume_unchanged_entry_count=0,
        skip_worktree_entry_count=0,
        tracked_status_sha256=EMPTY_SHA256,
        index_flag_status_sha256="5" * 64,
        binary_diff_sha256=CLEAN_BINARY_DIFF_SHA256,
        submodule_status_sha256=EMPTY_SHA256,
        submodule_count=0,
        nonclean_submodule_count=0,
        untracked_entry_count=0,
        untracked_status_sha256=EMPTY_SHA256,
        git_version="git version 2.43.0",
    )


def test_clean_repository_observation_records_actual_head_tree_branch(tmp_path: Path) -> None:
    repo = _repository(tmp_path)
    observed = observe_repository(repo)

    assert observed.head_commit == _git(repo, "rev-parse", "HEAD")
    assert observed.head_tree == _git(repo, "rev-parse", "HEAD^{tree}")
    assert observed.ref_state == "branch"
    assert observed.branch_name == _git(repo, "branch", "--show-current")
    assert observed.tracked_tree_dirty is False
    assert observed.tracked_status_sha256 == EMPTY_SHA256
    assert observed.untracked_entry_count == 0
    assert str(repo) not in json.dumps(observed.to_dict(), sort_keys=True)
    assert validate_repository_observation_payload(observed.to_dict()) == ()


def test_repository_observation_ignores_git_environment_redirection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    anchored_parent = tmp_path / "anchored-parent"
    redirected_parent = tmp_path / "redirected-parent"
    anchored_parent.mkdir()
    redirected_parent.mkdir()
    anchored = _repository(anchored_parent)
    redirected = _repository(redirected_parent)
    (redirected / "tracked.txt").write_text("redirected\n", encoding="utf-8")
    _git(redirected, "add", "tracked.txt")
    _git(redirected, "commit", "-m", "redirected")
    anchored_head = _git(anchored, "rev-parse", "HEAD")
    redirected_head = _git(redirected, "rev-parse", "HEAD")
    assert anchored_head != redirected_head

    monkeypatch.setenv("GIT_DIR", str(redirected / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(redirected))
    monkeypatch.setenv("GIT_INDEX_FILE", str(redirected / ".git" / "index"))

    observed = observe_repository(anchored)

    assert observed.head_commit == anchored_head
    assert observed.head_commit != redirected_head


def test_tracked_modification_changes_deterministic_status_and_diff_hashes(
    tmp_path: Path,
) -> None:
    repo = _repository(tmp_path)
    (repo / "tracked.txt").write_text("two\n", encoding="utf-8")

    first = observe_repository(repo)
    second = observe_repository(repo)
    assert first.tracked_tree_dirty is True
    assert first.unstaged_entry_count == 1
    assert first.tracked_status_sha256 == second.tracked_status_sha256
    assert first.binary_diff_sha256 == second.binary_diff_sha256

    (repo / "tracked.txt").write_text("three\n", encoding="utf-8")
    third = observe_repository(repo)
    assert third.tracked_status_sha256 == first.tracked_status_sha256
    assert third.binary_diff_sha256 != first.binary_diff_sha256

    _git(repo, "add", "tracked.txt")
    staged = observe_repository(repo)
    assert staged.staged_entry_count == 1
    assert staged.unstaged_entry_count == 0
    assert staged.tracked_status_sha256 != third.tracked_status_sha256


def test_untracked_files_are_recorded_but_do_not_make_tracked_tree_dirty(
    tmp_path: Path,
) -> None:
    repo = _repository(tmp_path)
    (repo / "untracked.txt").write_text("evidence\n", encoding="utf-8")

    observed = observe_repository(repo)
    assert observed.tracked_tree_dirty is False
    assert observed.untracked_entry_count == 1
    assert observed.untracked_status_sha256 != EMPTY_SHA256


@pytest.mark.parametrize(
    ("index_flag", "count_field"),
    (
        ("--assume-unchanged", "assume_unchanged_entry_count"),
        ("--skip-worktree", "skip_worktree_entry_count"),
    ),
)
def test_index_flags_cannot_hide_a_modified_tracked_file(
    tmp_path: Path,
    index_flag: str,
    count_field: str,
) -> None:
    repo = _repository(tmp_path)
    _git(repo, "update-index", index_flag, "tracked.txt")
    (repo / "tracked.txt").write_text("hidden modification\n", encoding="utf-8")

    observed = observe_repository(repo)

    assert observed.tracked_tree_dirty is True
    assert getattr(observed, count_field) == 1
    assert repository_constraint_mismatches(
        {
            "repository_mode": "git",
            "required_head": None,
            "require_tracked_clean": True,
            "untracked_policy": "record_only",
        },
        observed,
    ) == ("tracked_tree_not_clean",)
    assert observed.to_dict()["untracked_summary"]["affects_tracked_cleanliness"] is False


def test_detached_head_is_explicit(tmp_path: Path) -> None:
    repo = _repository(tmp_path)
    _git(repo, "checkout", "--detach", "HEAD")

    observed = observe_repository(repo)
    assert observed.ref_state == "detached"
    assert observed.branch_name is None
    assert validate_repository_observation_payload(observed.to_dict()) == ()


def test_serialized_clean_provenance_requires_canonical_empty_evidence() -> None:
    payload = fixed_clean_observation().to_dict()
    payload["binary_diff_sha256"] = "9" * 64
    payload["submodule_status_sha256"] = "8" * 64

    reasons = validate_repository_observation_payload(payload)

    assert "repository_provenance_clean_diff_hash_mismatch" in reasons
    assert "repository_provenance_empty_submodule_hash_mismatch" in reasons


def test_non_git_directory_cannot_impersonate_git_provenance(tmp_path: Path) -> None:
    with pytest.raises(RepositoryProvenanceError, match="REPOSITORY_OBSERVATION_UNAVAILABLE"):
        observe_repository(tmp_path)


def test_declared_constraints_are_evaluated_against_observation() -> None:
    observed = fixed_clean_observation()
    accepted = {
        "repository_mode": "git",
        "required_head": observed.head_commit,
        "require_tracked_clean": True,
        "untracked_policy": "record_only",
    }
    enforce_repository_constraint(accepted, observed)

    mismatch = dict(accepted, required_head="f" * 40)
    with pytest.raises(RepositoryConstraintMismatch) as captured:
        enforce_repository_constraint(mismatch, observed)
    assert captured.value.code == "DECLARED_CONSTRAINT_MISMATCH"
    assert captured.value.reasons == ("required_head_mismatch",)


def test_runner_constraint_mismatch_precedes_output_policy_and_dispatch(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    payload = json.loads((CONFIG_ROOT / "03_l2_legal_known.json").read_text())
    payload["provenance"]["repository_constraint"].update(
        {"required_head": "f" * 40, "require_tracked_clean": False}
    )
    spec_path = tmp_path / "mismatch.json"
    spec_path.write_text(json.dumps(payload), encoding="utf-8")
    run_dir = tmp_path / "must-not-exist"
    monkeypatch.setattr(
        dof_runner,
        "observe_repository",
        lambda _anchor: fixed_clean_observation(head="e" * 40),
    )

    def forbidden(*_args, **_kwargs):
        raise AssertionError("policy/backend path was reached")

    monkeypatch.setattr(dof_runner, "_policy_envelope", forbidden)
    monkeypatch.setattr(dof_runner.MockGatedDispatcher, "dispatch", forbidden)

    with pytest.raises(RepositoryConstraintMismatch) as captured:
        dof_runner.run_mock_experiment(spec_path, run_dir)
    assert captured.value.code == "DECLARED_CONSTRAINT_MISMATCH"
    assert run_dir.exists() is False


def test_repository_observation_never_enters_model_context(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    observed = fixed_clean_observation()
    monkeypatch.setattr(dof_runner, "observe_repository", lambda _anchor: observed)
    run_dir = tmp_path / "forced-unknown"
    dof_runner.run_mock_experiment(
        CONFIG_ROOT / "07_l2_forced_unknown_masked.json",
        run_dir,
    )

    trace = (run_dir / "evidence" / "decision_events.jsonl").read_text()
    assert observed.head_commit not in trace
    assert observed.branch_name not in trace
    assert str(Path(__file__).resolve().parents[2]) not in trace
    repository_payload = json.loads(
        (run_dir / "evidence" / "repository_provenance.json").read_text()
    )
    assert repository_payload["head_commit"] == observed.head_commit


def test_registered_verifier_detects_repository_provenance_tampering(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        dof_runner,
        "observe_repository",
        lambda _anchor: fixed_clean_observation(),
    )
    run_dir = tmp_path / "tamper"
    dof_runner.run_mock_experiment(
        CONFIG_ROOT / "01_l0_known.json",
        run_dir,
    )
    provenance_path = run_dir / "evidence" / "repository_provenance.json"
    payload = json.loads(provenance_path.read_text(encoding="utf-8"))
    payload["head_commit"] = "f" * 40
    provenance_path.chmod(0o644)
    provenance_path.write_text(
        json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )

    with SQLiteRunStore(run_dir / "run.db", migrate=False) as store:
        spec_hash = store.get_run().metadata.to_dict()["spec_hash"]
        verified = verify_registered_evidence(
            store,
            run_dir=run_dir,
            expected_run_id=store.get_run().run_id,
            expected_spec_hash=spec_hash,
        )
    assert not verified.ok
    assert any("repository_provenance" in error for error in verified.errors)
    assert any("sha256 mismatch" in error for error in verified.errors)


def test_gate0_selected_mock_summarizer_requires_clean_expected_head(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    observed = fixed_clean_observation()
    monkeypatch.setattr(dof_runner, "observe_repository", lambda _anchor: observed)
    run_root = tmp_path / "selected"
    for spec_path in sorted(CONFIG_ROOT.glob("[0-9][0-9]_*.json")):
        dof_runner.run_mock_experiment(spec_path, run_root / spec_path.stem)

    summary = summarize(run_root, expected_head=observed.head_commit)
    assert summary["run_count"] == 8
    assert summary["all_observed_heads_match"] is True
    assert summary["all_tracked_trees_clean"] is True
    assert summary["canonical_schema_version"] == 2
