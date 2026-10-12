from __future__ import annotations

import json
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time

import pytest

from optimizer_pipeline.dof.e1v2 import content_hash
from optimizer_pipeline.dof.job_queue import (
    CompleteJobQueue,
    QueueJob,
    aggregate_trajectory_results,
)
from optimizer_pipeline.dof.process_registry import (
    RECORD_SCHEMA,
    atomic_write,
    cleanup_registry,
    content_hash as registry_hash,
    create_registry,
    read_process_identity,
    seal_registry,
    signal_verified_identity,
)


REPOSITORY = Path(__file__).resolve().parents[2]
SUPERVISOR = REPOSITORY / "optimizer_pipeline/dof/process_supervisor.py"


def _sleep_job(job_id: str, seconds: float = 0.15) -> QueueJob:
    return QueueJob(job_id, (sys.executable, "-c", f"import time;time.sleep({seconds})"))


def _accounted_job(
    job_id: str, *, heavy: int, light: int, mutated: bool
) -> QueueJob:
    core = {
        "schema_version": "dof.e1v2-trajectory-result.v1",
        "job": {"job_id": job_id},
        "model_requests": 0,
        "heavy_calls": heavy,
        "light_mutation_calls": light,
        "design_mutated": mutated,
    }
    payload = core | {"result_sha256": content_hash(core)}
    code = (
        "import json,os,pathlib;"
        "p=pathlib.Path(os.environ['DOF_JOB_OUTPUT'])/'trajectory-result.json';"
        f"p.write_text(json.dumps({payload!r}))"
    )
    return QueueJob(job_id, (sys.executable, "-c", code))


def _provider_terminal_job(
    job_id: str,
    model: str = "model-a",
    *,
    category: str = "UPSTREAM_OR_SERVER",
    global_breaker: bool = False,
) -> QueueJob:
    core = {
        "schema_version": "dof.e1v2-trajectory-result.v2",
        "job": {"job_id": job_id, "model": model},
        "endpoint": "PROVIDER_TRANSPORT_FAILURE",
        "provider_route": "test/upstream-route",
        "provider_global_breaker": global_breaker,
        "provider_error_category": category,
        "model_requests": 1,
        "known_cost_usd": 0.0,
        "unknown_cost_attempt_count": 1,
        "reserved_unknown_cost_usd": 0.10,
        "cost_lower_bound_usd": 0.0,
        "cost_upper_bound_usd": 0.10,
        "known_token_total": 0,
        "unknown_usage_attempt_count": 1,
        "heavy_calls": 0,
        "light_mutation_calls": 0,
        "design_mutated": False,
    }
    payload = core | {"result_sha256": content_hash(core)}
    code = (
        "import json,os,pathlib;"
        "p=pathlib.Path(os.environ['DOF_JOB_OUTPUT'])/'trajectory-result.json';"
        f"p.write_text(json.dumps({payload!r}))"
    )
    return QueueJob(job_id, (sys.executable, "-c", code))


def _canonical_result_job(job_id: str, endpoint: str) -> QueueJob:
    core = {
        "schema_version": "dof.e1v2-trajectory-result.v2",
        "job": {"job_id": job_id, "model": "fake-model"},
        "endpoint": endpoint,
        "model_requests": int(endpoint == "INVALID_POLICY_OUTPUT"),
        "known_cost_usd": 0.01 if endpoint == "INVALID_POLICY_OUTPUT" else 0.0,
        "unknown_cost_attempt_count": 0,
        "reserved_unknown_cost_usd": 0.0,
        "cost_lower_bound_usd": 0.01 if endpoint == "INVALID_POLICY_OUTPUT" else 0.0,
        "cost_upper_bound_usd": 0.01 if endpoint == "INVALID_POLICY_OUTPUT" else 0.0,
        "known_token_total": 30 if endpoint == "INVALID_POLICY_OUTPUT" else 0,
        "unknown_usage_attempt_count": 0,
        "heavy_calls": 0,
        "light_mutation_calls": 0,
        "design_mutated": False,
    }
    payload = core | {"result_sha256": content_hash(core)}
    code = (
        "import json,os,pathlib;"
        "p=pathlib.Path(os.environ['DOF_JOB_OUTPUT'])/'trajectory-result.json';"
        f"p.write_text(json.dumps({payload!r}))"
    )
    return QueueJob(job_id, (sys.executable, "-c", code))


def _nested_supervisor_job(
    job_id: str, *, child_mode: str = "term-retaining", top_mode: str = "sleep"
) -> QueueJob:
    if child_mode == "setsid-descendant":
        child_code = (
            "import signal,subprocess,sys,time;"
            "signal.signal(signal.SIGTERM,signal.SIG_IGN);"
            "subprocess.Popen([sys.executable,'-c',"
            "'import signal,time;signal.signal(signal.SIGTERM,signal.SIG_IGN);time.sleep(60)'],"
            "start_new_session=True);time.sleep(60)"
        )
    elif child_mode == "success":
        child_code = "import time;time.sleep(0.05)"
    else:
        child_code = (
            "import signal,time;"
            "signal.signal(signal.SIGTERM,signal.SIG_IGN);time.sleep(60)"
        )
    success_payload = {
        "schema_version": "dof.e1v2-trajectory-result.v1",
        "job": {"job_id": job_id},
        "model_requests": 0,
        "heavy_calls": 0,
        "light_mutation_calls": 0,
        "design_mutated": False,
    }
    success_payload["result_sha256"] = content_hash(success_payload)
    top_code = (
        "import json,os,pathlib,subprocess,sys,time;"
        "registry=pathlib.Path(os.environ['DOF_PROCESS_REGISTRY_DIR']);"
        f"p=subprocess.Popen([sys.executable,'-I','-S',{str(SUPERVISOR)!r},'--',sys.executable,'-c',{child_code!r}],start_new_session=True);"
        "deadline=time.monotonic()+5;"
        "records=registry/'records';"
        "\nwhile not list(records.glob('*.json')) and time.monotonic()<deadline: time.sleep(0.01)\n"
        "assert list(records.glob('*.json'))"
    )
    if top_mode == "crash":
        top_code += "\nraise SystemExit(7)"
    elif top_mode == "success":
        top_code += (
            "\nassert p.wait(timeout=5)==0\n"
            "out=pathlib.Path(os.environ['DOF_JOB_OUTPUT']);"
            f"(out/'trajectory-result.json').write_text(json.dumps({success_payload!r}))"
        )
    elif top_mode == "corrupt":
        top_code += "\n(records/'corrupt.json').write_text('{')\ntime.sleep(60)"
    else:
        top_code += "\ntime.sleep(60)"
    return QueueJob(job_id, (sys.executable, "-c", top_code))


def _unregistered_path_bound_supervisor_job(job_id: str) -> QueueJob:
    child_code = "import time;time.sleep(60)"
    top_code = (
        "import os,subprocess,sys,time;"
        "attempt=os.environ['DOF_JOB_OUTPUT'];"
        "env=dict(os.environ);"
        "[env.pop(k,None) for k in "
        "('DOF_PROCESS_REGISTRY_DIR','DOF_JOB_ID','DOF_ATTEMPT_ID')];"
        f"subprocess.Popen([sys.executable,'-I','-S',{str(SUPERVISOR)!r},'--',"
        f"sys.executable,'-c',{child_code!r},attempt],"
        "start_new_session=True,env=env);"
        "time.sleep(0.15);raise SystemExit(7)"
    )
    return QueueJob(job_id, (sys.executable, "-c", top_code))


def _run_stopped_nested_queue(tmp_path: Path, job: QueueJob) -> tuple[dict, dict]:
    output = tmp_path / job.job_id
    queue = CompleteJobQueue(
        repository_anchor=REPOSITORY,
        output_root=output,
        max_concurrency=1,
        poll_seconds=0.01,
        terminate_grace_seconds=0.2,
        kill_grace_seconds=0.5,
    )
    returned: list[dict] = []
    thread = threading.Thread(target=lambda: returned.append(queue.run([job])))
    thread.start()
    deadline = time.monotonic() + 10
    records = output / "jobs" / job.job_id / f"{job.job_id}--attempt-01/process-ownership/records"
    while time.monotonic() < deadline:
        if records.is_dir() and list(records.glob("*.json")):
            break
        time.sleep(0.01)
    assert records.is_dir() and list(records.glob("*.json"))
    if job.job_id.endswith("corrupt"):
        deadline = time.monotonic() + 2
        while not (records / "corrupt.json").is_file() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert (records / "corrupt.json").is_file()
    queue.request_stop("test_nested_stop")
    thread.join(timeout=10)
    assert not thread.is_alive() and returned
    return returned[0], json.loads(queue.ledger_file.read_text())


def test_queue_requires_external_output(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="external"):
        CompleteJobQueue(
            repository_anchor=REPOSITORY,
            output_root=REPOSITORY / "forbidden-runtime",
        )


def test_fake_sixteen_slot_queue_isolated_and_quiescent(tmp_path: Path) -> None:
    output = tmp_path / "queue"
    jobs = [_sleep_job(f"fake-{index:02d}") for index in range(16)]
    result = CompleteJobQueue(
        repository_anchor=REPOSITORY,
        output_root=output,
        max_concurrency=16,
        poll_seconds=0.01,
    ).run(jobs)
    ledger = json.loads((output / "controller-ledger.json").read_text())
    assert result["peak_concurrency"] == 16
    assert result["cleanup_quiescent"] is True
    assert result["repository_unchanged"] is True
    assert len({row["output_directory"] for row in ledger["attempts"]}) == 16
    assert len({row["pgid"] for row in ledger["attempts"]}) == 16


def test_stop_barrier_prevents_delayed_refill_and_reaps_owned_groups(tmp_path: Path) -> None:
    output = tmp_path / "stop-queue"
    queue = CompleteJobQueue(
        repository_anchor=REPOSITORY,
        output_root=output,
        max_concurrency=4,
        poll_seconds=0.01,
        terminate_grace_seconds=0.4,
        kill_grace_seconds=0.4,
    )
    jobs = [_sleep_job(f"slow-{index:02d}", 30.0) for index in range(12)]
    returned: list[dict] = []
    thread = threading.Thread(target=lambda: returned.append(queue.run(jobs)))
    thread.start()
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        if queue.ledger_file.is_file():
            ledger = json.loads(queue.ledger_file.read_text())
            if len([row for row in ledger["events"] if row["event"] == "STARTED"]) == 4:
                break
        time.sleep(0.01)
    queue.request_stop("test_stop")
    thread.join(timeout=5.0)
    assert not thread.is_alive()
    assert returned[0]["stopped"] is True
    ledger = json.loads(queue.ledger_file.read_text())
    starts = [row for row in ledger["events"] if row["event"] == "STARTED"]
    assert len(starts) == 4
    assert ledger["cleanup_audit"]["quiescent"] is True


def test_only_proven_zero_effect_predispatch_failure_is_resubmitted(tmp_path: Path) -> None:
    output = tmp_path / "retry-queue"
    code = """
import json, os, pathlib, sys
out=pathlib.Path(os.environ['DOF_JOB_OUTPUT'])
if os.environ['DOF_ATTEMPT_ID'].endswith('01'):
    (out/'predispatch-status.json').write_text(json.dumps({'provider_requests':0,'heavy_calls':0,'design_mutated':False,'failure_class':'PREDISPATCH_INFRASTRUCTURE'}))
    raise SystemExit(7)
raise SystemExit(0)
"""
    result = CompleteJobQueue(
        repository_anchor=REPOSITORY,
        output_root=output,
        max_concurrency=1,
        poll_seconds=0.01,
    ).run([QueueJob("retry-one", (sys.executable, "-c", code))])
    ledger = json.loads((output / "controller-ledger.json").read_text())
    assert result["terminal_job_count"] == 1
    assert [row["attempt_index"] for row in ledger["attempts"]] == [1, 2]
    assert sum(row["event"] == "PREDISPATCH_RESUBMITTED" for row in ledger["events"]) == 1


@pytest.mark.parametrize(
    "heavy,light,mutated",
    [(0, 0, False), (0, 1, True), (1, 0, True)],
)
def test_queue_uses_authoritative_heavy_and_light_mutation_accounting(
    tmp_path: Path, heavy: int, light: int, mutated: bool
) -> None:
    output = tmp_path / f"accounting-{heavy}-{light}"
    CompleteJobQueue(
        repository_anchor=REPOSITORY,
        output_root=output,
        max_concurrency=1,
        poll_seconds=0.01,
    ).run([_accounted_job("accounted", heavy=heavy, light=light, mutated=mutated)])
    attempt = json.loads((output / "controller-ledger.json").read_text())["attempts"][0]
    assert attempt["heavy_calls"] == heavy
    assert attempt["light_mutation_calls"] == light
    assert attempt["design_mutated"] is mutated
    assert attempt["mutation_evidence_status"] == "AUTHORITATIVE"


@pytest.mark.parametrize("payload,status", [(None, "MISSING"), ("{", "CORRUPT")])
def test_missing_or_corrupt_terminal_result_is_not_proven_no_mutation(
    tmp_path: Path, payload: str | None, status: str
) -> None:
    code = "pass"
    if payload is not None:
        code = (
            "import os,pathlib;"
            "p=pathlib.Path(os.environ['DOF_JOB_OUTPUT'])/'trajectory-result.json';"
            f"p.write_text({payload!r})"
        )
    output = tmp_path / f"ambiguous-{status.lower()}"
    CompleteJobQueue(
        repository_anchor=REPOSITORY,
        output_root=output,
        max_concurrency=1,
        poll_seconds=0.01,
    ).run([QueueJob("ambiguous", (sys.executable, "-c", code))])
    attempt = json.loads((output / "controller-ledger.json").read_text())["attempts"][0]
    assert attempt["design_mutated"] is None
    assert attempt["light_mutation_calls"] is None
    assert attempt["mutation_evidence_status"] == status


def test_deterministic_aggregator_checks_hashes_and_job_identity(tmp_path: Path) -> None:
    output = tmp_path / "aggregate-queue"
    output.mkdir()
    attempt = output / "jobs" / "job-a" / "job-a--attempt-01"
    attempt.mkdir(parents=True)
    core = {
        "schema_version": "dof.e1v2-trajectory-result.v1",
        "job": {"job_id": "job-a"},
        "model_requests": 0,
        "heavy_calls": 0,
        "light_mutation_calls": 0,
        "design_mutated": False,
    }
    (attempt / "trajectory-result.json").write_text(
        json.dumps(core | {"result_sha256": content_hash(core)})
    )
    ledger_core = {
        "schema_version": "dof.e1v2-complete-job-queue.v1",
        "attempts": [
            {
                "job_id": "job-a",
                "attempt_index": 1,
                "output_directory": str(attempt),
                "provider_requests": 0,
                "heavy_calls": 0,
                "light_mutation_calls": 0,
                "design_mutated": False,
                "mutation_evidence_status": "AUTHORITATIVE",
                "result_sha256": content_hash(core),
            }
        ],
    }
    (output / "controller-ledger.json").write_text(
        json.dumps(ledger_core | {"ledger_sha256": content_hash(ledger_core)})
    )
    first = aggregate_trajectory_results(output)
    second = aggregate_trajectory_results(output)
    assert first == second
    assert first["job_count"] == 1
    assert first["attempt_accounting"][0]["light_mutation_calls"] == 0
    assert first["attempt_accounting"][0]["design_mutated"] is False


def test_provider_transport_terminal_aggregates_cost_interval_and_trips_model_breaker(
    tmp_path: Path,
) -> None:
    output = tmp_path / "provider-breaker"
    result = CompleteJobQueue(
        repository_anchor=REPOSITORY,
        output_root=output,
        max_concurrency=1,
        poll_seconds=0.01,
        provider_transport_failure_limit_per_model=1,
        cost_upper_bound_usd=1.0,
    ).run([_provider_terminal_job("terminal-a"), _provider_terminal_job("terminal-b")])
    assert result["stopped"] is True
    assert result["stop_reason"] == (
        "provider_route_failure_circuit_breaker:test/upstream-route"
    )
    aggregate = aggregate_trajectory_results(output)
    assert aggregate["job_count"] == 1
    assert aggregate["endpoint_counts"] == {"PROVIDER_TRANSPORT_FAILURE": 1}
    assert aggregate["known_cost_usd"] == 0
    assert aggregate["reserved_unknown_cost_usd"] == pytest.approx(0.10)
    assert aggregate["cost_lower_bound_usd"] == 0
    assert aggregate["cost_upper_bound_usd"] == pytest.approx(0.10)
    assert aggregate["unknown_usage_attempt_count"] == 1


def test_structural_provider_category_trips_immediate_global_breaker(
    tmp_path: Path,
) -> None:
    output = tmp_path / "provider-global-breaker"
    result = CompleteJobQueue(
        repository_anchor=REPOSITORY,
        output_root=output,
        max_concurrency=1,
        poll_seconds=0.01,
        provider_transport_failure_limit_per_model=3,
    ).run(
        [
            _provider_terminal_job(
                "payment-blocked",
                category="PAYMENT_OR_BUDGET",
                global_breaker=True,
            ),
            _provider_terminal_job("must-not-launch"),
        ]
    )
    assert result["stopped"] is True
    assert result["stop_reason"] == "provider_global_breaker:PAYMENT_OR_BUDGET"
    assert result["terminal_job_count"] == 1


def test_queue_cost_circuit_breaker_uses_upper_bound_not_known_cost(
    tmp_path: Path,
) -> None:
    output = tmp_path / "cost-breaker"
    result = CompleteJobQueue(
        repository_anchor=REPOSITORY,
        output_root=output,
        max_concurrency=1,
        poll_seconds=0.01,
        cost_upper_bound_usd=0.05,
    ).run([_provider_terminal_job("terminal-cost"), _sleep_job("must-not-launch")])
    assert result["stopped"] is True
    assert result["stop_reason"] == "provider_cost_upper_bound_exceeded"
    ledger = json.loads((output / "controller-ledger.json").read_text())
    assert [row["job_id"] for row in ledger["attempts"]] == ["terminal-cost"]


def test_policy_binding_terminal_does_not_stop_queue_refill(tmp_path: Path) -> None:
    output = tmp_path / "policy-binding-continues"
    result = CompleteJobQueue(
        repository_anchor=REPOSITORY,
        output_root=output,
        max_concurrency=1,
        poll_seconds=0.01,
    ).run(
        [
            _canonical_result_job("binding-terminal", "INVALID_POLICY_OUTPUT"),
            _canonical_result_job("subsequent-job", "VALID_COMPLETION"),
        ]
    )
    assert result["stopped"] is False
    assert result["terminal_job_count"] == 2
    assert aggregate_trajectory_results(output)["endpoint_counts"] == {
        "INVALID_POLICY_OUTPUT": 1,
        "VALID_COMPLETION": 1,
    }


def test_registered_sigterm_retaining_nested_session_is_reaped_automatically(
    tmp_path: Path,
) -> None:
    result, ledger = _run_stopped_nested_queue(
        tmp_path, _nested_supervisor_job("nested-retaining")
    )
    assert result["cleanup_quiescent"] is True
    nested = ledger["attempts"][0]["nested_process_registry"]
    assert nested["cleanup"]["events"][0]["sigkill_count"] >= 1
    assert nested["audits"][0]["quiescent"] is True
    assert nested["audits"][1]["quiescent"] is True


def test_registered_setsid_descendant_is_reaped_via_supervisor_ownership(
    tmp_path: Path,
) -> None:
    result, ledger = _run_stopped_nested_queue(
        tmp_path,
        _nested_supervisor_job(
            "nested-setsid", child_mode="setsid-descendant"
        ),
    )
    assert result["cleanup_quiescent"] is True
    cleanup = ledger["attempts"][0]["nested_process_registry"]["cleanup"]
    assert cleanup["complete"] is True
    assert cleanup["events"][0]["result"] == "REAPED"


def test_top_job_crash_after_registration_still_reaps_nested_session(
    tmp_path: Path,
) -> None:
    output = tmp_path / "nested-crash"
    result = CompleteJobQueue(
        repository_anchor=REPOSITORY,
        output_root=output,
        max_concurrency=1,
        poll_seconds=0.01,
        terminate_grace_seconds=0.2,
        kill_grace_seconds=0.5,
    ).run([_nested_supervisor_job("nested-crash", top_mode="crash")])
    ledger = json.loads((output / "controller-ledger.json").read_text())
    assert result["stopped"] is True
    assert result["cleanup_quiescent"] is True
    assert ledger["attempts"][0]["returncode"] == 7
    assert ledger["attempts"][0]["nested_process_registry"]["audits"][1][
        "quiescent"
    ] is True


def test_corrupt_nested_registry_stops_and_never_claims_quiescence(
    tmp_path: Path,
) -> None:
    result, ledger = _run_stopped_nested_queue(
        tmp_path,
        _nested_supervisor_job("nested-corrupt", top_mode="corrupt"),
    )
    assert result["stopped"] is True
    assert result["cleanup_quiescent"] is False
    errors = ledger["cleanup_audit"]["nested_registry_errors"]
    assert any("corrupt_json:corrupt.json" in error for error in errors)


def test_unregistered_exact_path_bound_supervisor_is_cleaned_but_fails_closed(
    tmp_path: Path,
) -> None:
    output = tmp_path / "unregistered-path"
    result = CompleteJobQueue(
        repository_anchor=REPOSITORY,
        output_root=output,
        max_concurrency=1,
        poll_seconds=0.01,
        terminate_grace_seconds=0.05,
        kill_grace_seconds=0.5,
    ).run([_unregistered_path_bound_supervisor_job("unregistered-path")])
    ledger = json.loads((output / "controller-ledger.json").read_text())
    nested = ledger["attempts"][0]["nested_process_registry"]
    pre, first, second = nested["attempt_path_audits"]
    assert result["stopped"] is True
    assert result["cleanup_quiescent"] is False
    assert pre["registry_complete"] is False
    assert pre["unregistered_supervisor_count"] == 1
    assert nested["attempt_path_cleanup"]["complete"] is True
    assert first["live_supervisor_count"] == 0
    assert second["live_supervisor_count"] == 0
    assert any(
        error.startswith("unregistered_path_bound_supervisor:")
        for error in ledger["cleanup_audit"]["nested_registry_errors"]
    )


def test_pid_starttime_mismatch_rejects_signal(tmp_path: Path) -> None:
    unrelated = subprocess.Popen(
        [sys.executable, "-c", "import time;time.sleep(60)"],
        start_new_session=True,
    )
    try:
        identity = read_process_identity(unrelated.pid)
        assert identity is not None
        registry = tmp_path / "mismatch-registry"
        create_registry(registry, job_id="job", attempt_id="attempt")
        wrong_start = identity.starttime + 1
        core = {
            "schema_version": RECORD_SCHEMA,
            "job_id": "job",
            "attempt_id": "attempt",
            "supervisor_pid": identity.pid,
            "procfs_start_time": wrong_start,
            "pgid": identity.pid,
            "sid": identity.pid,
            "command_worker_identity_hash": "a" * 64,
            "registered_at": "2026-08-09T00:00:00+00:00",
            "state": "REGISTERED",
        }
        path = registry / "records" / f"supervisor-{identity.pid}-{wrong_start}.json"
        atomic_write(path, core | {"record_sha256": registry_hash(core)})
        assert seal_registry(registry, job_id="job", attempt_id="attempt") == ()
        cleanup = cleanup_registry(
            registry,
            job_id="job",
            attempt_id="attempt",
            terminate_grace_seconds=0.01,
            kill_grace_seconds=0.01,
        )
        assert any("pid_starttime_mismatch" in row for row in cleanup["errors"])
        assert unrelated.poll() is None
    finally:
        identity = read_process_identity(unrelated.pid)
        if identity is not None:
            signal_verified_identity(identity, signal.SIGKILL)
        unrelated.wait(timeout=2)


def test_unrelated_same_executable_is_not_signalled(tmp_path: Path) -> None:
    unrelated = subprocess.Popen(
        [sys.executable, "-c", "import time;time.sleep(60)"],
        start_new_session=True,
    )
    try:
        result, _ledger = _run_stopped_nested_queue(
            tmp_path, _nested_supervisor_job("nested-with-unrelated")
        )
        assert result["cleanup_quiescent"] is True
        assert unrelated.poll() is None
    finally:
        identity = read_process_identity(unrelated.pid)
        if identity is not None:
            signal_verified_identity(identity, signal.SIGKILL)
        unrelated.wait(timeout=2)


def test_successful_nested_session_marks_record_reaped(tmp_path: Path) -> None:
    output = tmp_path / "nested-success"
    result = CompleteJobQueue(
        repository_anchor=REPOSITORY,
        output_root=output,
        max_concurrency=1,
        poll_seconds=0.01,
    ).run(
        [
            _nested_supervisor_job(
                "nested-success", child_mode="success", top_mode="success"
            )
        ]
    )
    ledger = json.loads((output / "controller-ledger.json").read_text())
    record_path = next(
        (
            output
            / "jobs/nested-success/nested-success--attempt-01/process-ownership/records"
        ).glob("*.json")
    )
    assert result["cleanup_quiescent"] is True
    assert json.loads(record_path.read_text())["state"] == "REAPED"
    assert ledger["attempts"][0]["nested_process_registry"]["cleanup"][
        "record_count"
    ] == 1


def test_cleanup_quiescent_requires_two_zero_nested_audits(tmp_path: Path) -> None:
    result, ledger = _run_stopped_nested_queue(
        tmp_path, _nested_supervisor_job("nested-two-audits")
    )
    audits = ledger["attempts"][0]["nested_process_registry"]["audits"]
    assert len(audits) == 2
    assert all(audit["quiescent"] is True for audit in audits)
    assert ledger["cleanup_audit"]["two_audit_requirement_met"] is True
    assert result["cleanup_quiescent"] is True
