"""Small complete-job queue with explicit process-group ownership.

This is intentionally not a generic scheduler.  It owns complete scientific
job subprocesses, gives each attempt an external directory, records process
identity, and implements a stop-before-refill barrier.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import time
from typing import Any, Mapping, Sequence

from .process_registry import (
    REGISTRY_ENV,
    ProcessIdentity,
    audit_attempt_path_supervisors,
    audit_registry,
    cleanup_attempt_path_supervisors,
    cleanup_registry,
    create_registry,
    read_process_identity,
    scan_attempt_path_supervisors,
    scan_verified_group,
    seal_registry,
    signal_verified_identity,
)


QUEUE_SCHEMA = "dof.e1v2-complete-job-queue.v1"
QUEUE_RESULT_SCHEMA = "dof.e1v2-complete-job-queue-result.v1"
AGGREGATE_SCHEMA = "dof.e1v2-deterministic-aggregate.v1"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _bytes(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode("utf-8")


def _hash(value: Any) -> str:
    return hashlib.sha256(value if isinstance(value, bytes) else _bytes(value)).hexdigest()


def _write(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_bytes(json.dumps(value, sort_keys=True, indent=2).encode() + b"\n")
    os.replace(temporary, path)


def _git_identity(repository: Path) -> dict[str, str]:
    def run(*args: str) -> bytes:
        return subprocess.run(
            ("git", "-C", os.fspath(repository), *args),
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        ).stdout

    return {
        "head": run("rev-parse", "HEAD").decode().strip(),
        "tree": run("rev-parse", "HEAD^{tree}").decode().strip(),
        "status_sha256": hashlib.sha256(run("status", "--porcelain=v1", "-z")).hexdigest(),
    }


@dataclass(frozen=True, slots=True)
class QueueJob:
    job_id: str
    command: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.job_id or any(character not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.:-" for character in self.job_id):
            raise ValueError("queue_job_id_invalid")
        if not self.command or any(not isinstance(item, str) or not item for item in self.command):
            raise ValueError("queue_job_command_invalid")


@dataclass(slots=True)
class _OwnedAttempt:
    job: QueueJob
    attempt_index: int
    attempt_id: str
    directory: Path
    process: subprocess.Popen[bytes]
    stdout: Any
    stderr: Any
    pid: int
    pgid: int
    starttime: int
    started_at: str
    registry_directory: Path
    registry_cleanup: dict[str, Any] | None = None
    registry_audits: tuple[dict[str, Any], ...] = ()
    path_cleanup: dict[str, Any] | None = None
    path_audits: tuple[dict[str, Any], ...] = ()


class CompleteJobQueue:
    def __init__(
        self,
        *,
        repository_anchor: str | Path,
        output_root: str | Path,
        max_concurrency: int = 16,
        provider_transport_failure_limit_per_model: int | None = None,
        cost_upper_bound_usd: float | None = None,
        poll_seconds: float = 0.1,
        terminate_grace_seconds: float = 3.0,
        kill_grace_seconds: float = 3.0,
    ) -> None:
        self.repository = Path(repository_anchor).resolve()
        self.output = Path(output_root).resolve()
        if self.output == self.repository or self.output.is_relative_to(self.repository):
            raise ValueError("queue_output_must_be_external_to_repository")
        if not 1 <= max_concurrency <= 16:
            raise ValueError("queue_concurrency_out_of_range")
        self.max_concurrency = max_concurrency
        if provider_transport_failure_limit_per_model is not None and (
            not isinstance(provider_transport_failure_limit_per_model, int)
            or isinstance(provider_transport_failure_limit_per_model, bool)
            or provider_transport_failure_limit_per_model <= 0
        ):
            raise ValueError("provider_failure_limit_invalid")
        if cost_upper_bound_usd is not None and cost_upper_bound_usd <= 0:
            raise ValueError("queue_cost_upper_bound_invalid")
        self.provider_transport_failure_limit_per_model = (
            provider_transport_failure_limit_per_model
        )
        self.cost_upper_bound_usd = cost_upper_bound_usd
        self.poll_seconds = poll_seconds
        self.terminate_grace_seconds = terminate_grace_seconds
        self.kill_grace_seconds = kill_grace_seconds
        self.stop_file = self.output / "STOP"
        self.ledger_file = self.output / "controller-ledger.json"
        self._active: dict[str, _OwnedAttempt] = {}
        self._stop_refill = False
        self._ledger: dict[str, Any] = {}

    def _persist(self) -> None:
        core = dict(self._ledger)
        core.pop("ledger_sha256", None)
        self._ledger = core | {"ledger_sha256": _hash(core)}
        _write(self.ledger_file, self._ledger)

    @staticmethod
    def _result_path(directory: Path) -> Path:
        direct = directory / "trajectory-result.json"
        nested = directory / "trajectory" / "trajectory-result.json"
        return direct if direct.is_file() else nested

    @staticmethod
    def _mutation_accounting(result_path: Path) -> dict[str, Any]:
        unknown = {
            "provider_requests": None,
            "provider_endpoint": None,
            "provider_model": None,
            "known_cost_usd": None,
            "unknown_cost_attempt_count": None,
            "reserved_unknown_cost_usd": None,
            "cost_lower_bound_usd": None,
            "cost_upper_bound_usd": None,
            "known_token_total": None,
            "unknown_usage_attempt_count": None,
            "provider_accounting_status": "MISSING",
            "heavy_calls": None,
            "light_mutation_calls": None,
            "design_mutated": None,
            "mutation_evidence_status": "MISSING",
            "result_sha256": None,
        }
        if not result_path.is_file():
            return unknown
        try:
            loaded = json.loads(result_path.read_text())
        except (OSError, UnicodeError, json.JSONDecodeError):
            return unknown | {"mutation_evidence_status": "CORRUPT"}
        if not isinstance(loaded, Mapping):
            return unknown | {"mutation_evidence_status": "CORRUPT"}
        result = dict(loaded)
        claimed = result.pop("result_sha256", None)
        if not isinstance(claimed, str) or claimed != _hash(result):
            return unknown | {"mutation_evidence_status": "HASH_MISMATCH"}
        provider = result.get("model_requests")
        heavy = result.get("heavy_calls")
        light = result.get("light_mutation_calls")
        mutated = result.get("design_mutated")
        counts_valid = all(
            isinstance(value, int) and not isinstance(value, bool) and value >= 0
            for value in (provider, heavy, light)
        )
        mutation_valid = (
            isinstance(mutated, bool)
            and counts_valid
            and (mutated is bool(heavy or light))
        )
        if not mutation_valid:
            return unknown | {
                "mutation_evidence_status": "AMBIGUOUS",
                "result_sha256": claimed,
            }
        known_cost = result.get("known_cost_usd")
        unknown_cost_count = result.get("unknown_cost_attempt_count")
        reserved_cost = result.get("reserved_unknown_cost_usd")
        lower_cost = result.get("cost_lower_bound_usd")
        upper_cost = result.get("cost_upper_bound_usd")
        known_tokens = result.get("known_token_total")
        unknown_usage_count = result.get("unknown_usage_attempt_count")
        cost_values_valid = all(
            isinstance(value, (int, float))
            and not isinstance(value, bool)
            and value >= 0
            for value in (known_cost, reserved_cost, lower_cost, upper_cost)
        )
        count_values_valid = all(
            isinstance(value, int) and not isinstance(value, bool) and value >= 0
            for value in (
                unknown_cost_count,
                known_tokens,
                unknown_usage_count,
            )
        )
        provider_accounting_valid = (
            cost_values_valid
            and count_values_valid
            and abs(float(lower_cost) - float(known_cost)) <= 1e-12
            and abs(
                float(upper_cost) - float(known_cost) - float(reserved_cost)
            )
            <= 1e-12
        )
        return {
            "provider_requests": provider,
            "provider_endpoint": result.get("endpoint"),
            "provider_model": result.get("job", {}).get("model"),
            "provider_route": result.get("provider_route"),
            "provider_error_category": result.get("provider_error_category"),
            "provider_global_breaker": result.get("provider_global_breaker") is True,
            "known_cost_usd": known_cost,
            "unknown_cost_attempt_count": unknown_cost_count,
            "reserved_unknown_cost_usd": reserved_cost,
            "cost_lower_bound_usd": lower_cost,
            "cost_upper_bound_usd": upper_cost,
            "known_token_total": known_tokens,
            "unknown_usage_attempt_count": unknown_usage_count,
            "provider_accounting_status": (
                "AUTHORITATIVE" if provider_accounting_valid else "MISSING"
            ),
            "heavy_calls": heavy,
            "light_mutation_calls": light,
            "design_mutated": mutated,
            "mutation_evidence_status": "AUTHORITATIVE",
            "result_sha256": claimed,
        }

    def _attempt_status(self, owned: _OwnedAttempt, returncode: int, terminal: str) -> dict[str, Any]:
        result_path = self._result_path(owned.directory)
        accounting = self._mutation_accounting(result_path)
        return {
            "attempt_id": owned.attempt_id,
            "job_id": owned.job.job_id,
            "attempt_index": owned.attempt_index,
            "pid": owned.pid,
            "pgid": owned.pgid,
            "procfs_start_time": owned.starttime,
            "output_directory": os.fspath(owned.directory),
            "started_at": owned.started_at,
            "terminal_at": _now(),
            "terminal": terminal,
            "returncode": returncode,
            **accounting,
            "cleanup_status": "REAPED",
            "nested_process_registry": {
                "directory": os.fspath(owned.registry_directory),
                "cleanup": owned.registry_cleanup,
                "audits": list(owned.registry_audits),
                "attempt_path_cleanup": owned.path_cleanup,
                "attempt_path_audits": list(owned.path_audits),
            },
        }

    def _launch(self, job: QueueJob, attempt_index: int) -> _OwnedAttempt:
        attempt_id = f"{job.job_id}--attempt-{attempt_index:02d}"
        directory = self.output / "jobs" / job.job_id / attempt_id
        directory.mkdir(parents=True, exist_ok=False)
        registry_directory = directory / "process-ownership"
        create_registry(
            registry_directory, job_id=job.job_id, attempt_id=attempt_id
        )
        stdout = (directory / "stdout.log").open("xb")
        stderr = (directory / "stderr.log").open("xb")
        environment = dict(os.environ)
        environment["DOF_JOB_ID"] = job.job_id
        environment["DOF_ATTEMPT_ID"] = attempt_id
        environment["DOF_JOB_OUTPUT"] = os.fspath(directory)
        environment[REGISTRY_ENV] = os.fspath(registry_directory)
        process = subprocess.Popen(
            job.command,
            cwd=self.repository,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=stdout,
            stderr=stderr,
            start_new_session=True,
        )
        identity = read_process_identity(process.pid)
        if identity is None or identity.pid != identity.pgid or identity.pid != identity.sid:
            process.kill()
            process.wait(timeout=1.0)
            stdout.close()
            stderr.close()
            raise RuntimeError("queue_top_process_identity_invalid")
        owned = _OwnedAttempt(
            job,
            attempt_index,
            attempt_id,
            directory,
            process,
            stdout,
            stderr,
            identity.pid,
            identity.pgid,
            identity.starttime,
            _now(),
            registry_directory,
        )
        self._active[attempt_id] = owned
        self._ledger["events"].append(
            {
                "event": "STARTED",
                "at": owned.started_at,
                "job_id": job.job_id,
                "attempt_id": attempt_id,
                "pid": owned.pid,
                "pgid": owned.pgid,
                "procfs_start_time": owned.starttime,
                "output_directory": os.fspath(directory),
            }
        )
        self._ledger["peak_concurrency"] = max(
            self._ledger["peak_concurrency"], len(self._active)
        )
        self._persist()
        return owned

    @staticmethod
    def _predispatch_retry_eligible(directory: Path) -> bool:
        path = directory / "predispatch-status.json"
        if not path.is_file():
            return False
        value = json.loads(path.read_text())
        return (
            value.get("provider_requests") == 0
            and value.get("heavy_calls") == 0
            and value.get("design_mutated") is False
            and value.get("failure_class") == "PREDISPATCH_INFRASTRUCTURE"
        )

    def request_stop(self, reason: str = "operator_stop") -> None:
        self._stop_refill = True
        self.output.mkdir(parents=True, exist_ok=True)
        _write(self.stop_file, {"reason": reason, "requested_at": _now()})

    def _circuit_breaker_reason(self) -> str | None:
        attempts = self._ledger["attempts"]
        for row in attempts:
            if row.get("provider_global_breaker") is True:
                return "provider_global_breaker:" + str(
                    row.get("provider_error_category") or "UNKNOWN"
                )
        if self.cost_upper_bound_usd is not None:
            observed_upper = sum(
                float(row["cost_upper_bound_usd"])
                for row in attempts
                if row.get("provider_accounting_status") == "AUTHORITATIVE"
            )
            if observed_upper > self.cost_upper_bound_usd + 1e-12:
                return "provider_cost_upper_bound_exceeded"
        limit = self.provider_transport_failure_limit_per_model
        if limit is not None:
            routes = {
                str(row["provider_route"])
                for row in attempts
                if row.get("provider_endpoint") == "PROVIDER_TRANSPORT_FAILURE"
                and isinstance(row.get("provider_route"), str)
            }
            for route in sorted(routes):
                failures = sum(
                    row.get("provider_endpoint") == "PROVIDER_TRANSPORT_FAILURE"
                    and row.get("provider_route") == route
                    for row in attempts
                )
                if failures >= limit:
                    return "provider_route_failure_circuit_breaker:" + route
            global_failures = sum(
                row.get("provider_endpoint") == "PROVIDER_TRANSPORT_FAILURE"
                for row in attempts
            )
            if global_failures >= 5:
                return "provider_global_transport_failure_circuit_breaker"
        return None

    def _terminate_owned(self) -> None:
        observed_groups: dict[str, tuple[ProcessIdentity, ...]] = {}
        for owned in tuple(self._active.values()):
            if owned.process.poll() is None:
                root = ProcessIdentity(
                    owned.pid, 0, owned.pgid, owned.pgid, owned.starttime
                )
                members = scan_verified_group(root)
                observed_groups[owned.attempt_id] = members
                for identity in reversed(members):
                    try:
                        signal_verified_identity(identity, signal.SIGTERM)
                    except (ProcessLookupError, RuntimeError, OSError):
                        pass
        deadline = time.monotonic() + self.terminate_grace_seconds
        while self._active and time.monotonic() < deadline:
            if all(row.process.poll() is not None for row in self._active.values()):
                break
            time.sleep(min(self.poll_seconds, 0.05))
        for owned in tuple(self._active.values()):
            if owned.process.poll() is None:
                root = ProcessIdentity(
                    owned.pid, 0, owned.pgid, owned.pgid, owned.starttime
                )
                current_members = scan_verified_group(root)
                members = {
                    (identity.pid, identity.starttime): identity
                    for identity in (
                        *observed_groups.get(owned.attempt_id, ()),
                        *current_members,
                    )
                }
                for identity in reversed(tuple(members.values())):
                    try:
                        signal_verified_identity(identity, signal.SIGKILL)
                    except (ProcessLookupError, RuntimeError, OSError):
                        pass
        deadline = time.monotonic() + self.kill_grace_seconds
        for owned in tuple(self._active.values()):
            remaining = max(0.0, deadline - time.monotonic())
            try:
                owned.process.wait(timeout=remaining)
            except subprocess.TimeoutExpired:
                pass

    def _finalize_registry(self, owned: _OwnedAttempt) -> bool:
        seal_errors = seal_registry(
            owned.registry_directory,
            job_id=owned.job.job_id,
            attempt_id=owned.attempt_id,
        )
        cleanup = cleanup_registry(
            owned.registry_directory,
            job_id=owned.job.job_id,
            attempt_id=owned.attempt_id,
            terminate_grace_seconds=self.terminate_grace_seconds,
            kill_grace_seconds=self.kill_grace_seconds,
        )
        if seal_errors:
            cleanup = dict(cleanup)
            cleanup["errors"] = sorted(
                set(cleanup.get("errors", ())) | set(seal_errors)
            )
            cleanup["complete"] = False
        path_pre_audit = audit_attempt_path_supervisors(
            owned.directory,
            owned.registry_directory,
            job_id=owned.job.job_id,
            attempt_id=owned.attempt_id,
        )
        path_identities = scan_attempt_path_supervisors(owned.directory)
        path_cleanup = cleanup_attempt_path_supervisors(
            owned.directory,
            path_identities,
            terminate_grace_seconds=self.terminate_grace_seconds,
            kill_grace_seconds=self.kill_grace_seconds,
        )
        first = audit_registry(
            owned.registry_directory,
            job_id=owned.job.job_id,
            attempt_id=owned.attempt_id,
        )
        time.sleep(min(0.05, max(0.01, self.poll_seconds)))
        second = audit_registry(
            owned.registry_directory,
            job_id=owned.job.job_id,
            attempt_id=owned.attempt_id,
        )
        path_first = audit_attempt_path_supervisors(
            owned.directory,
            owned.registry_directory,
            job_id=owned.job.job_id,
            attempt_id=owned.attempt_id,
        )
        time.sleep(min(0.05, max(0.01, self.poll_seconds)))
        path_second = audit_attempt_path_supervisors(
            owned.directory,
            owned.registry_directory,
            job_id=owned.job.job_id,
            attempt_id=owned.attempt_id,
        )
        owned.registry_cleanup = cleanup
        owned.registry_audits = (first, second)
        owned.path_cleanup = path_cleanup
        owned.path_audits = (path_pre_audit, path_first, path_second)
        return bool(
            cleanup["complete"]
            and path_cleanup["complete"]
            and path_pre_audit["registry_complete"]
            and first["quiescent"]
            and second["quiescent"]
            and path_first["quiescent"]
            and path_second["quiescent"]
        )

    def _host_audit(self, owned_attempts: Sequence[_OwnedAttempt]) -> dict[str, Any]:
        live_top = []
        registry_errors = []
        second_audits = []
        path_second_audits = []
        all_registry_audits_quiescent = True
        for owned in owned_attempts:
            current = read_process_identity(owned.pid)
            if current is not None and current.starttime == owned.starttime:
                live_top.append(owned.pgid)
            if len(owned.registry_audits) == 2:
                second_audits.append(owned.registry_audits[1])
                for registry_audit in owned.registry_audits:
                    registry_errors.extend(registry_audit.get("errors", ()))
                    all_registry_audits_quiescent = (
                        all_registry_audits_quiescent
                        and registry_audit.get("quiescent") is True
                    )
            else:
                all_registry_audits_quiescent = False
                registry_errors.append(
                    "nested_registry_audits_missing:" + owned.attempt_id
                )
            if len(owned.path_audits) == 3:
                path_second_audits.append(owned.path_audits[2])
                for path_audit in owned.path_audits:
                    registry_errors.extend(path_audit.get("errors", ()))
                for path_audit in owned.path_audits[1:]:
                    all_registry_audits_quiescent = (
                        all_registry_audits_quiescent
                        and path_audit.get("quiescent") is True
                    )
            else:
                all_registry_audits_quiescent = False
                registry_errors.append("attempt_path_audits_missing:" + owned.attempt_id)
        quiescent = (
            not live_top
            and not registry_errors
            and all_registry_audits_quiescent
            and len(second_audits) == len(owned_attempts)
            and len(path_second_audits) == len(owned_attempts)
        )
        return {
            "owned_pgids": sorted({row.pgid for row in owned_attempts}),
            "live_owned_pgids": sorted(set(live_top)),
            "nested_registry_second_audits": second_audits,
            "attempt_path_second_audits": path_second_audits,
            "nested_registry_errors": sorted(set(registry_errors)),
            "two_audit_requirement_met": (
                len(second_audits) == len(owned_attempts)
                and len(path_second_audits) == len(owned_attempts)
            ),
            "quiescent": quiescent,
        }

    def run(self, jobs: Sequence[QueueJob]) -> dict[str, Any]:
        if len({job.job_id for job in jobs}) != len(jobs):
            raise ValueError("queue_job_ids_not_unique")
        self.output.mkdir(parents=True, exist_ok=False)
        repository_before = _git_identity(self.repository)
        self._ledger = {
            "schema_version": QUEUE_SCHEMA,
            "controller_pid": os.getpid(),
            "controller_session_id": os.getsid(0),
            "started_at": _now(),
            "max_concurrency": self.max_concurrency,
            "queue_order": [job.job_id for job in jobs],
            "peak_concurrency": 0,
            "events": [],
            "attempts": [],
            "repository_before": repository_before,
        }
        self._persist()
        pending = list(jobs)
        attempts_by_job = {job.job_id: 0 for job in jobs}
        owned_attempts: list[_OwnedAttempt] = []
        terminal_jobs: set[str] = set()
        try:
            while (pending or self._active) and not self._stop_refill:
                if self.stop_file.exists():
                    self._stop_refill = True
                    break
                while pending and len(self._active) < self.max_concurrency:
                    job = pending.pop(0)
                    attempts_by_job[job.job_id] += 1
                    owned = self._launch(job, attempts_by_job[job.job_id])
                    owned_attempts.append(owned)
                for attempt_id, owned in tuple(self._active.items()):
                    returncode = owned.process.poll()
                    if returncode is None:
                        continue
                    owned.process.wait()
                    registry_ok = self._finalize_registry(owned)
                    owned.stdout.close()
                    owned.stderr.close()
                    status = self._attempt_status(owned, returncode, "COMPLETED")
                    self._ledger["attempts"].append(status)
                    self._ledger["events"].append(
                        {"event": "TERMINAL", "at": status["terminal_at"], "job_id": owned.job.job_id, "attempt_id": attempt_id}
                    )
                    del self._active[attempt_id]
                    if not registry_ok:
                        self.request_stop(
                            "nested_process_registry_blocker:" + owned.job.job_id
                        )
                    retry_eligible = (
                        returncode != 0
                        and attempts_by_job[owned.job.job_id] == 1
                        and self._predispatch_retry_eligible(owned.directory)
                    )
                    if (
                        returncode != 0
                        and not retry_eligible
                        and status.get("mutation_evidence_status") != "AUTHORITATIVE"
                    ):
                        self.request_stop(
                            "job_terminal_evidence_missing:" + owned.job.job_id
                        )
                    circuit_reason = self._circuit_breaker_reason()
                    if circuit_reason is not None:
                        self.request_stop(circuit_reason)
                    if retry_eligible:
                        pending.insert(0, owned.job)
                        self._ledger["events"].append(
                            {"event": "PREDISPATCH_RESUBMITTED", "at": _now(), "job_id": owned.job.job_id, "prior_attempt_id": attempt_id}
                        )
                    else:
                        terminal_jobs.add(owned.job.job_id)
                    self._persist()
                if pending or self._active:
                    time.sleep(self.poll_seconds)
        finally:
            if self._stop_refill or self._active:
                self._stop_refill = True
                self._terminate_owned()
                for attempt_id, owned in tuple(self._active.items()):
                    returncode = owned.process.poll()
                    if returncode is None:
                        returncode = -signal.SIGKILL
                    registry_ok = self._finalize_registry(owned)
                    owned.stdout.close()
                    owned.stderr.close()
                    self._ledger["attempts"].append(
                        self._attempt_status(owned, returncode, "STOPPED")
                    )
                    self._active.pop(attempt_id, None)
                    if not registry_ok and not self.stop_file.is_file():
                        self.request_stop(
                            "nested_process_registry_blocker:" + owned.job.job_id
                        )
            repository_after = _git_identity(self.repository)
            audit = self._host_audit(owned_attempts)
            self._ledger.update(
                {
                    "terminal_at": _now(),
                    "stop_refill": self._stop_refill,
                    "launched_job_count": len({row["job_id"] for row in self._ledger["attempts"]}),
                    "terminal_job_count": len(terminal_jobs),
                    "repository_after": repository_after,
                    "repository_unchanged": repository_before == repository_after,
                    "cleanup_audit": audit,
                }
            )
            self._persist()
        result_core = {
            "schema_version": QUEUE_RESULT_SCHEMA,
            "controller_ledger_sha256": self._ledger["ledger_sha256"],
            "stopped": self._stop_refill,
            "repository_unchanged": self._ledger["repository_unchanged"],
            "cleanup_quiescent": self._ledger["cleanup_audit"]["quiescent"],
            "peak_concurrency": self._ledger["peak_concurrency"],
            "terminal_job_count": self._ledger["terminal_job_count"],
            "stop_reason": (
                json.loads(self.stop_file.read_text()).get("reason")
                if self.stop_file.is_file()
                else None
            ),
        }
        result = result_core | {"queue_result_sha256": _hash(result_core)}
        _write(self.output / "queue-result.json", result)
        return result


def aggregate_trajectory_results(queue_root: str | Path) -> dict[str, Any]:
    root = Path(queue_root).resolve()
    ledger = json.loads((root / "controller-ledger.json").read_text())
    core = dict(ledger)
    claimed = core.pop("ledger_sha256", None)
    if claimed != _hash(core):
        raise ValueError("queue_ledger_hash_mismatch")
    rows = []
    seen: set[str] = set()
    for attempt in sorted(ledger["attempts"], key=lambda row: (row["job_id"], row["attempt_index"])):
        directory = Path(attempt["output_directory"])
        direct = directory / "trajectory-result.json"
        path = direct if direct.is_file() else directory / "trajectory" / "trajectory-result.json"
        if not path.is_file():
            continue
        result = json.loads(path.read_text())
        if result.get("schema_version") == "dof.g-diagnostic-trajectory-result.v1":
            raise ValueError("g_diagnostic_result_rejected_from_primary_aggregate")
        result_core = {key: item for key, item in result.items() if key != "result_sha256"}
        if result.get("result_sha256") != _hash(result_core):
            raise ValueError("trajectory_result_hash_mismatch")
        job_id = result.get("job", {}).get("job_id")
        if job_id != attempt["job_id"] or job_id in seen:
            raise ValueError("aggregate_job_identity_invalid")
        seen.add(job_id)
        rows.append(result)
    payload = {
        "schema_version": AGGREGATE_SCHEMA,
        "queue_ledger_sha256": claimed,
        "job_count": len(rows),
        "jobs": rows,
        "attempt_accounting": [
            {
                key: attempt.get(key)
                for key in (
                    "attempt_id",
                    "job_id",
                    "attempt_index",
                    "provider_requests",
                    "provider_endpoint",
                    "provider_model",
                    "known_cost_usd",
                    "unknown_cost_attempt_count",
                    "reserved_unknown_cost_usd",
                    "cost_lower_bound_usd",
                    "cost_upper_bound_usd",
                    "known_token_total",
                    "unknown_usage_attempt_count",
                    "provider_accounting_status",
                    "heavy_calls",
                    "light_mutation_calls",
                    "design_mutated",
                    "mutation_evidence_status",
                    "result_sha256",
                )
            }
            for attempt in sorted(
                ledger["attempts"],
                key=lambda row: (row["job_id"], row["attempt_index"]),
            )
        ],
    }
    known_cost_usd = sum(float(row.get("known_cost_usd", 0.0)) for row in rows)
    reserved_unknown_cost_usd = sum(
        float(row.get("reserved_unknown_cost_usd", 0.0)) for row in rows
    )
    payload.update(
        {
            "known_cost_usd": known_cost_usd,
            "unknown_cost_attempt_count": sum(
                int(row.get("unknown_cost_attempt_count", 0)) for row in rows
            ),
            "reserved_unknown_cost_usd": reserved_unknown_cost_usd,
            "cost_lower_bound_usd": known_cost_usd,
            "cost_upper_bound_usd": known_cost_usd
            + reserved_unknown_cost_usd,
            "known_token_total": sum(
                int(row.get("known_token_total", 0)) for row in rows
            ),
            "unknown_usage_attempt_count": sum(
                int(row.get("unknown_usage_attempt_count", 0)) for row in rows
            ),
            "endpoint_counts": {
                endpoint: sum(row.get("endpoint") == endpoint for row in rows)
                for endpoint in sorted(
                    {
                        str(row.get("endpoint"))
                        for row in rows
                        if isinstance(row.get("endpoint"), str)
                    }
                )
            },
        }
    )
    aggregate = payload | {"aggregate_sha256": _hash(payload)}
    _write(root / "aggregate.json", aggregate)
    return aggregate


__all__ = [
    "AGGREGATE_SCHEMA",
    "CompleteJobQueue",
    "QueueJob",
    "aggregate_trajectory_results",
]
