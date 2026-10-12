"""Runtime-observed repository provenance for common DoF experiments.

Checked-in experiment specifications may constrain repository state, but they
must never supply the repository facts recorded for a run.  This module keeps
the observation boundary small and independent of providers and FPGA tools: it
executes bounded, read-only Git commands and returns a versioned value object.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import subprocess
from typing import Any


REPOSITORY_PROVENANCE_SCHEMA_VERSION = "dof.repository-provenance.v1"
REPOSITORY_PROVENANCE_COLLECTOR_VERSION = "dof.git-observer.v1"
TRACKED_CLEANLINESS_POLICY = "git-tracked-only.v1"
DEFAULT_GIT_TIMEOUT_SECONDS = 10.0

_GIT_OBJECT_RE = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_UNTRACKED_POLICIES = frozenset({"record_only", "require_none"})
_EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()
_CLEAN_BINARY_DIFF_SHA256 = hashlib.sha256(
    b"cached\0\0working\0"
).hexdigest()


class RepositoryProvenanceError(RuntimeError):
    """A typed failure to obtain truthful repository provenance."""

    code = "REPOSITORY_OBSERVATION_UNAVAILABLE"

    def __init__(self, reasons: tuple[str, ...] | list[str] | str) -> None:
        if isinstance(reasons, str):
            values = (reasons,)
        else:
            values = tuple(str(reason) for reason in reasons)
        self.reasons = values
        super().__init__(f"{self.code}: " + "; ".join(values))


class RepositoryConstraintMismatch(RepositoryProvenanceError):
    """The observed checkout does not satisfy a declared input constraint."""

    code = "DECLARED_CONSTRAINT_MISMATCH"

    def __init__(
        self,
        reasons: tuple[str, ...] | list[str],
        observation: "RepositoryObservation",
    ) -> None:
        self.observation = observation
        super().__init__(reasons)


@dataclass(frozen=True, slots=True)
class RepositoryObservation:
    observed_at: str
    repository_root_identity_sha256: str
    head_commit: str
    head_tree: str
    ref_state: str
    branch_name: str | None
    tracked_tree_dirty: bool
    staged_entry_count: int
    unstaged_entry_count: int
    conflicted_entry_count: int
    assume_unchanged_entry_count: int
    skip_worktree_entry_count: int
    tracked_status_sha256: str
    index_flag_status_sha256: str
    binary_diff_sha256: str
    submodule_status_sha256: str
    submodule_count: int
    nonclean_submodule_count: int
    untracked_entry_count: int
    untracked_status_sha256: str
    git_version: str
    schema_version: str = REPOSITORY_PROVENANCE_SCHEMA_VERSION
    collector_version: str = REPOSITORY_PROVENANCE_COLLECTOR_VERSION
    repository_mode: str = "git"
    tracked_cleanliness_policy: str = TRACKED_CLEANLINESS_POLICY

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "collector_version": self.collector_version,
            "observed_at": self.observed_at,
            "repository_mode": self.repository_mode,
            "repository_root_identity_sha256": self.repository_root_identity_sha256,
            "head_commit": self.head_commit,
            "head_tree": self.head_tree,
            "ref_state": self.ref_state,
            "branch_name": self.branch_name,
            "tracked_cleanliness_policy": self.tracked_cleanliness_policy,
            "tracked_tree_dirty": self.tracked_tree_dirty,
            "tracked_status_summary": {
                "staged_entry_count": self.staged_entry_count,
                "unstaged_entry_count": self.unstaged_entry_count,
                "conflicted_entry_count": self.conflicted_entry_count,
                "assume_unchanged_entry_count": self.assume_unchanged_entry_count,
                "skip_worktree_entry_count": self.skip_worktree_entry_count,
            },
            "tracked_status_sha256": self.tracked_status_sha256,
            "index_flag_status_sha256": self.index_flag_status_sha256,
            "binary_diff_sha256": self.binary_diff_sha256,
            "submodule_status_sha256": self.submodule_status_sha256,
            "submodule_summary": {
                "submodule_count": self.submodule_count,
                "nonclean_submodule_count": self.nonclean_submodule_count,
            },
            "untracked_summary": {
                "entry_count": self.untracked_entry_count,
                "status_sha256": self.untracked_status_sha256,
                "affects_tracked_cleanliness": False,
            },
            "git_version": self.git_version,
        }

    @property
    def sha256(self) -> str:
        return hashlib.sha256(_canonical_json_bytes(self.to_dict())).hexdigest()


def observe_repository(
    anchor: str | Path,
    *,
    timeout_seconds: float = DEFAULT_GIT_TIMEOUT_SECONDS,
) -> RepositoryObservation:
    """Observe the Git checkout containing ``anchor`` without modifying it."""

    start = Path(anchor).resolve()
    if start.is_file():
        start = start.parent
    root_raw = _run_git(start, ("rev-parse", "--show-toplevel"), timeout_seconds)
    try:
        root = Path(os.fsdecode(root_raw).strip()).resolve(strict=True)
    except (OSError, ValueError) as exc:
        raise RepositoryProvenanceError("git_repository_root_invalid") from exc

    head_commit = _ascii_git(root, ("rev-parse", "--verify", "HEAD^{commit}"), timeout_seconds)
    head_tree = _ascii_git(root, ("rev-parse", "--verify", "HEAD^{tree}"), timeout_seconds)
    if not _GIT_OBJECT_RE.fullmatch(head_commit):
        raise RepositoryProvenanceError("git_head_commit_not_full_object_id")
    if not _GIT_OBJECT_RE.fullmatch(head_tree):
        raise RepositoryProvenanceError("git_head_tree_not_full_object_id")

    branch_process = _run_git_process(
        root,
        ("symbolic-ref", "--quiet", "--short", "HEAD"),
        timeout_seconds,
        accepted_returncodes=(0, 1),
    )
    if branch_process.returncode == 0:
        branch_name = _decode_ascii(branch_process.stdout, "git_branch_not_ascii").strip()
        if not branch_name:
            raise RepositoryProvenanceError("git_branch_empty")
        ref_state = "branch"
    else:
        branch_name = None
        ref_state = "detached"

    tracked_status = _run_git(
        root,
        (
            "status",
            "--porcelain=v2",
            "-z",
            "--untracked-files=no",
            "--ignore-submodules=none",
        ),
        timeout_seconds,
    )
    staged, unstaged, conflicted = _tracked_status_counts(tracked_status)
    index_flag_status = _run_git(
        root,
        ("ls-files", "-v", "-z"),
        timeout_seconds,
    )
    assume_unchanged, skip_worktree = _index_flag_counts(index_flag_status)
    cached_diff = _run_git(root, _binary_diff_args(cached=True), timeout_seconds)
    working_diff = _run_git(root, _binary_diff_args(cached=False), timeout_seconds)
    combined_diff = b"cached\0" + cached_diff + b"\0working\0" + working_diff

    submodule_status = _run_git(
        root,
        ("submodule", "status", "--recursive"),
        timeout_seconds,
    )
    submodule_lines = [line for line in submodule_status.splitlines() if line]
    nonclean_submodules = sum(
        1 for line in submodule_lines if line[:1] in {b"+", b"-", b"U"}
    )
    untracked_status = _run_git(
        root,
        ("ls-files", "--others", "--exclude-standard", "-z"),
        timeout_seconds,
    )
    untracked_entries = [item for item in untracked_status.split(b"\0") if item]
    git_version = _ascii_git(root, ("--version",), timeout_seconds)

    # Git has no snapshot transaction spanning these commands.  Re-read every
    # tracked-provenance input and fail if it moved while collection was in
    # progress, rather than emitting a hybrid of two repository states.
    head_commit_after = _ascii_git(
        root, ("rev-parse", "--verify", "HEAD^{commit}"), timeout_seconds
    )
    head_tree_after = _ascii_git(
        root, ("rev-parse", "--verify", "HEAD^{tree}"), timeout_seconds
    )
    branch_after = _run_git_process(
        root,
        ("symbolic-ref", "--quiet", "--short", "HEAD"),
        timeout_seconds,
        accepted_returncodes=(0, 1),
    )
    stable_reads = (
        ("head_commit", head_commit.encode("ascii"), head_commit_after.encode("ascii")),
        ("head_tree", head_tree.encode("ascii"), head_tree_after.encode("ascii")),
        (
            "branch_returncode",
            str(branch_process.returncode).encode("ascii"),
            str(branch_after.returncode).encode("ascii"),
        ),
        ("branch", branch_process.stdout, branch_after.stdout),
        (
            "tracked_status",
            tracked_status,
            _run_git(
                root,
                (
                    "status",
                    "--porcelain=v2",
                    "-z",
                    "--untracked-files=no",
                    "--ignore-submodules=none",
                ),
                timeout_seconds,
            ),
        ),
        (
            "index_flags",
            index_flag_status,
            _run_git(root, ("ls-files", "-v", "-z"), timeout_seconds),
        ),
        (
            "cached_diff",
            cached_diff,
            _run_git(root, _binary_diff_args(cached=True), timeout_seconds),
        ),
        (
            "working_diff",
            working_diff,
            _run_git(root, _binary_diff_args(cached=False), timeout_seconds),
        ),
        (
            "submodules",
            submodule_status,
            _run_git(
                root, ("submodule", "status", "--recursive"), timeout_seconds
            ),
        ),
        (
            "untracked",
            untracked_status,
            _run_git(
                root,
                ("ls-files", "--others", "--exclude-standard", "-z"),
                timeout_seconds,
            ),
        ),
    )
    changed = [name for name, before, after in stable_reads if before != after]
    if changed:
        raise RepositoryProvenanceError(
            "repository_changed_during_observation:" + ",".join(changed)
        )

    return RepositoryObservation(
        observed_at=datetime.now(timezone.utc).isoformat(timespec="microseconds"),
        repository_root_identity_sha256=hashlib.sha256(
            os.fsencode(str(root))
        ).hexdigest(),
        head_commit=head_commit,
        head_tree=head_tree,
        ref_state=ref_state,
        branch_name=branch_name,
        # An index suppression flag makes ordinary status/diff output unable
        # to prove that the executing worktree matches HEAD.  Treat the
        # checkout as non-clean even if the underlying file happens not to be
        # modified; selected evidence must first remove the ambiguity.
        tracked_tree_dirty=bool(
            tracked_status or assume_unchanged or skip_worktree
        ),
        staged_entry_count=staged,
        unstaged_entry_count=unstaged,
        conflicted_entry_count=conflicted,
        assume_unchanged_entry_count=assume_unchanged,
        skip_worktree_entry_count=skip_worktree,
        tracked_status_sha256=hashlib.sha256(tracked_status).hexdigest(),
        index_flag_status_sha256=hashlib.sha256(index_flag_status).hexdigest(),
        binary_diff_sha256=hashlib.sha256(combined_diff).hexdigest(),
        submodule_status_sha256=hashlib.sha256(submodule_status).hexdigest(),
        submodule_count=len(submodule_lines),
        nonclean_submodule_count=nonclean_submodules,
        untracked_entry_count=len(untracked_entries),
        untracked_status_sha256=hashlib.sha256(untracked_status).hexdigest(),
        git_version=git_version,
    )


def repository_constraint_mismatches(
    constraint: Mapping[str, Any],
    observation: RepositoryObservation | Mapping[str, Any],
) -> tuple[str, ...]:
    """Return stable mismatch codes for a strict repository constraint."""

    observed = (
        observation.to_dict()
        if isinstance(observation, RepositoryObservation)
        else observation
    )
    reasons: list[str] = []
    if constraint.get("repository_mode") != observed.get("repository_mode"):
        reasons.append("repository_mode_mismatch")
    required_head = constraint.get("required_head")
    if required_head is not None and required_head != observed.get("head_commit"):
        reasons.append("required_head_mismatch")
    if (
        constraint.get("require_tracked_clean") is True
        and observed.get("tracked_tree_dirty") is not False
    ):
        reasons.append("tracked_tree_not_clean")
    untracked_policy = constraint.get("untracked_policy")
    untracked = observed.get("untracked_summary")
    untracked_count = untracked.get("entry_count") if isinstance(untracked, Mapping) else None
    if untracked_policy == "require_none" and untracked_count != 0:
        reasons.append("untracked_entries_not_permitted")
    return tuple(reasons)


def enforce_repository_constraint(
    constraint: Mapping[str, Any],
    observation: RepositoryObservation,
) -> None:
    reasons = repository_constraint_mismatches(constraint, observation)
    if reasons:
        raise RepositoryConstraintMismatch(reasons, observation)


def tracked_repository_observations_match(
    before: RepositoryObservation,
    after: RepositoryObservation,
) -> bool:
    """Compare tracked execution identity while ignoring output-only untracked drift."""

    fields = (
        "repository_root_identity_sha256",
        "head_commit",
        "head_tree",
        "ref_state",
        "branch_name",
        "tracked_tree_dirty",
        "staged_entry_count",
        "unstaged_entry_count",
        "conflicted_entry_count",
        "assume_unchanged_entry_count",
        "skip_worktree_entry_count",
        "tracked_status_sha256",
        "index_flag_status_sha256",
        "binary_diff_sha256",
        "submodule_status_sha256",
        "submodule_count",
        "nonclean_submodule_count",
    )
    return all(getattr(before, field) == getattr(after, field) for field in fields)


def validate_repository_observation_payload(value: Any) -> tuple[str, ...]:
    """Validate a serialized observation without consulting the live checkout."""

    if not isinstance(value, Mapping) or not all(isinstance(key, str) for key in value):
        return ("repository_provenance_not_object",)
    expected = {
        "schema_version", "collector_version", "observed_at", "repository_mode",
        "repository_root_identity_sha256", "head_commit", "head_tree", "ref_state",
        "branch_name", "tracked_cleanliness_policy", "tracked_tree_dirty",
        "tracked_status_summary", "tracked_status_sha256", "index_flag_status_sha256",
        "binary_diff_sha256",
        "submodule_status_sha256", "submodule_summary", "untracked_summary",
        "git_version",
    }
    reasons: list[str] = []
    if set(value) != expected:
        reasons.append("repository_provenance_fields_not_exact")
    if value.get("schema_version") != REPOSITORY_PROVENANCE_SCHEMA_VERSION:
        reasons.append("repository_provenance_schema_mismatch")
    if value.get("collector_version") != REPOSITORY_PROVENANCE_COLLECTOR_VERSION:
        reasons.append("repository_provenance_collector_mismatch")
    if value.get("repository_mode") != "git":
        reasons.append("repository_provenance_mode_not_git")
    if value.get("tracked_cleanliness_policy") != TRACKED_CLEANLINESS_POLICY:
        reasons.append("repository_provenance_cleanliness_policy_mismatch")
    for key in (
        "repository_root_identity_sha256", "tracked_status_sha256",
        "index_flag_status_sha256",
        "binary_diff_sha256", "submodule_status_sha256",
    ):
        if not isinstance(value.get(key), str) or not _SHA256_RE.fullmatch(value[key]):
            reasons.append(f"repository_provenance_{key}_invalid")
    for key in ("head_commit", "head_tree"):
        if not isinstance(value.get(key), str) or not _GIT_OBJECT_RE.fullmatch(value[key]):
            reasons.append(f"repository_provenance_{key}_invalid")
    if value.get("ref_state") not in {"branch", "detached"}:
        reasons.append("repository_provenance_ref_state_invalid")
    branch_name = value.get("branch_name")
    if value.get("ref_state") == "branch" and (
        not isinstance(branch_name, str) or not branch_name.strip()
    ):
        reasons.append("repository_provenance_branch_missing")
    if value.get("ref_state") == "detached" and branch_name is not None:
        reasons.append("repository_provenance_detached_branch_not_null")
    if not isinstance(value.get("tracked_tree_dirty"), bool):
        reasons.append("repository_provenance_dirty_not_boolean")
    try:
        observed_at = datetime.fromisoformat(str(value.get("observed_at")))
    except ValueError:
        reasons.append("repository_provenance_timestamp_invalid")
    else:
        if observed_at.tzinfo is None or observed_at.utcoffset() is None:
            reasons.append("repository_provenance_timestamp_not_timezone_aware")
    if not isinstance(value.get("git_version"), str) or not value[
        "git_version"
    ].startswith("git version "):
        reasons.append("repository_provenance_git_version_invalid")

    tracked = value.get("tracked_status_summary")
    tracked_keys = {
        "staged_entry_count",
        "unstaged_entry_count",
        "conflicted_entry_count",
        "assume_unchanged_entry_count",
        "skip_worktree_entry_count",
    }
    if not _nonnegative_int_mapping(tracked, tracked_keys):
        reasons.append("repository_provenance_tracked_summary_invalid")
    elif value.get("tracked_tree_dirty") is False:
        if any(tracked[key] != 0 for key in tracked_keys):
            reasons.append("repository_provenance_clean_summary_nonzero")
        if value.get("tracked_status_sha256") != _EMPTY_SHA256:
            reasons.append("repository_provenance_clean_status_hash_mismatch")
        if value.get("binary_diff_sha256") != _CLEAN_BINARY_DIFF_SHA256:
            reasons.append("repository_provenance_clean_diff_hash_mismatch")
    elif value.get("tracked_tree_dirty") is True and all(
        tracked[key] == 0 for key in tracked_keys
    ) and value.get("tracked_status_sha256") == _EMPTY_SHA256:
        reasons.append("repository_provenance_dirty_without_tracked_evidence")

    submodules = value.get("submodule_summary")
    if not _nonnegative_int_mapping(
        submodules, {"submodule_count", "nonclean_submodule_count"}
    ):
        reasons.append("repository_provenance_submodule_summary_invalid")
    elif submodules["nonclean_submodule_count"] > submodules["submodule_count"]:
        reasons.append("repository_provenance_nonclean_submodule_count_invalid")
    elif (
        submodules["submodule_count"] == 0
        and value.get("submodule_status_sha256") != _EMPTY_SHA256
    ):
        reasons.append("repository_provenance_empty_submodule_hash_mismatch")

    untracked = value.get("untracked_summary")
    if not isinstance(untracked, Mapping) or set(untracked) != {
        "entry_count", "status_sha256", "affects_tracked_cleanliness"
    }:
        reasons.append("repository_provenance_untracked_summary_invalid")
    else:
        if not isinstance(untracked.get("entry_count"), int) or isinstance(
            untracked.get("entry_count"), bool
        ) or untracked["entry_count"] < 0:
            reasons.append("repository_provenance_untracked_count_invalid")
        if not isinstance(untracked.get("status_sha256"), str) or not _SHA256_RE.fullmatch(
            untracked["status_sha256"]
        ):
            reasons.append("repository_provenance_untracked_hash_invalid")
        if untracked.get("affects_tracked_cleanliness") is not False:
            reasons.append("repository_provenance_untracked_policy_invalid")
        if untracked.get("entry_count") == 0 and untracked.get("status_sha256") != _EMPTY_SHA256:
            reasons.append("repository_provenance_empty_untracked_hash_mismatch")
    return tuple(reasons)


def _binary_diff_args(*, cached: bool) -> tuple[str, ...]:
    args = [
        "diff", "--binary", "--full-index", "--no-ext-diff", "--no-textconv",
        "--no-color", "--src-prefix=a/", "--dst-prefix=b/",
    ]
    if cached:
        args.append("--cached")
    args.append("--")
    return tuple(args)


def _tracked_status_counts(raw: bytes) -> tuple[int, int, int]:
    staged = 0
    unstaged = 0
    conflicted = 0
    for record in raw.split(b"\0"):
        if not record or record[:1] not in {b"1", b"2", b"u"}:
            continue
        fields = record.split(b" ", 2)
        if len(fields) < 2 or len(fields[1]) != 2:
            raise RepositoryProvenanceError("git_status_porcelain_v2_unparseable")
        xy = fields[1]
        if record[:1] == b"u" or b"U" in xy:
            conflicted += 1
        if xy[:1] != b".":
            staged += 1
        if xy[1:] != b".":
            unstaged += 1
    return staged, unstaged, conflicted


def _index_flag_counts(raw: bytes) -> tuple[int, int]:
    """Count index flags that can suppress ordinary status/diff evidence."""

    assume_unchanged = 0
    skip_worktree = 0
    for record in raw.split(b"\0"):
        if not record:
            continue
        if len(record) < 3 or record[1:2] != b" ":
            raise RepositoryProvenanceError("git_ls_files_v_unparseable")
        tag = record[:1]
        if b"a" <= tag <= b"z":
            assume_unchanged += 1
        if tag == b"S":
            skip_worktree += 1
    return assume_unchanged, skip_worktree


def _ascii_git(root: Path, args: tuple[str, ...], timeout_seconds: float) -> str:
    return _decode_ascii(
        _run_git(root, args, timeout_seconds),
        "git_output_not_ascii",
    ).strip()


def _decode_ascii(raw: bytes, reason: str) -> str:
    try:
        return raw.decode("ascii")
    except UnicodeDecodeError as exc:
        raise RepositoryProvenanceError(reason) from exc


def _run_git(root: Path, args: tuple[str, ...], timeout_seconds: float) -> bytes:
    return _run_git_process(root, args, timeout_seconds).stdout


def _run_git_process(
    root: Path,
    args: tuple[str, ...],
    timeout_seconds: float,
    *,
    accepted_returncodes: tuple[int, ...] = (0,),
) -> subprocess.CompletedProcess[bytes]:
    if (
        not isinstance(timeout_seconds, (int, float))
        or isinstance(timeout_seconds, bool)
        or timeout_seconds <= 0
    ):
        raise ValueError("timeout_seconds must be positive")
    # Repository selection must come from ``-C root`` above, never from a
    # caller-controlled Git override.  In particular, inherited GIT_DIR and
    # GIT_WORK_TREE can otherwise make an observation anchored in checkout A
    # report checkout B's HEAD.  Drop the complete Git namespace instead of
    # trying to maintain an incomplete deny-list, then add back only the
    # read-only lock policy owned by this collector.
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.upper().startswith("GIT_")
    }
    environment.update(
        {
            "GIT_OPTIONAL_LOCKS": "0",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_ATTR_NOSYSTEM": "1",
            "LC_ALL": "C",
            "LANG": "C",
        }
    )
    command = [
        "git",
        "-c",
        "core.quotepath=false",
        "-c",
        "core.fsmonitor=false",
        "-C",
        str(root),
        *args,
    ]
    try:
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=environment,
            start_new_session=True,
        )
    except FileNotFoundError as exc:
        raise RepositoryProvenanceError("git_executable_not_found") from exc
    try:
        stdout, stderr = process.communicate(timeout=float(timeout_seconds))
    except subprocess.TimeoutExpired as exc:
        # The Git collector owns a dedicated session.  A timeout therefore
        # cleans that exact PGID rather than only the direct child or a broad
        # process-name match.
        try:
            os.killpg(process.pid, signal.SIGTERM)
            process.communicate(timeout=min(0.5, float(timeout_seconds)))
        except (ProcessLookupError, subprocess.TimeoutExpired):
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            try:
                process.communicate(timeout=0.5)
            except subprocess.TimeoutExpired:
                pass
        raise RepositoryProvenanceError("git_observation_timeout") from exc
    completed = subprocess.CompletedProcess(
        command,
        process.returncode,
        stdout,
        stderr,
    )
    if completed.returncode not in accepted_returncodes:
        stderr_hash = hashlib.sha256(completed.stderr).hexdigest()
        raise RepositoryProvenanceError(
            f"git_command_failed:{args[0]}:exit={completed.returncode}:stderr_sha256={stderr_hash}"
        )
    return completed


def _nonnegative_int_mapping(value: Any, keys: set[str]) -> bool:
    return (
        isinstance(value, Mapping)
        and set(value) == keys
        and all(
            isinstance(value[key], int)
            and not isinstance(value[key], bool)
            and value[key] >= 0
            for key in keys
        )
    )


def _canonical_json_bytes(value: Any) -> bytes:
    return (
        json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
        + b"\n"
    )


__all__ = [
    "DEFAULT_GIT_TIMEOUT_SECONDS",
    "REPOSITORY_PROVENANCE_COLLECTOR_VERSION",
    "REPOSITORY_PROVENANCE_SCHEMA_VERSION",
    "RepositoryConstraintMismatch",
    "RepositoryObservation",
    "RepositoryProvenanceError",
    "enforce_repository_constraint",
    "observe_repository",
    "repository_constraint_mismatches",
    "tracked_repository_observations_match",
    "validate_repository_observation_payload",
]
