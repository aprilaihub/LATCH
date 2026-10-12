#!/usr/bin/env python3
"""Run resumable APRIL Vivado/RapidWright semantic golden cases."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import time
from typing import Any, Sequence


REPO = Path(__file__).resolve().parents[1]
DEFAULT_MANIFEST = REPO / "tests" / "real" / "golden_matrix.json"
LOCKED_RAPIDWRIGHT = "2fc8b6aec859c20c04218573f9dda09db0a72e3f"


def read_json(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text())
    if not isinstance(data, dict):
        raise ValueError(f"expected JSON object: {path}")
    return data


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def command_env() -> dict[str, str]:
    configured_python = os.environ.get("PYTHON")
    if not configured_python or Path(configured_python).resolve() != Path(sys.executable).resolve():
        raise RuntimeError("configure an explicit isolated interpreter and invoke this runner with \"$PYTHON\"")
    vivado = os.environ.get("VIVADO_EXEC")
    if not vivado or not os.access(vivado, os.X_OK):
        raise RuntimeError("VIVADO_EXEC is unset or not executable")
    rapidwright = REPO / "RapidWright"
    commit = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=rapidwright, check=True, text=True, stdout=subprocess.PIPE
    ).stdout.strip()
    if commit != LOCKED_RAPIDWRIGHT:
        raise RuntimeError("current worktree RapidWright is not at the locked commit")
    jars = rapidwright / "jars"
    if not jars.is_dir() or not any(jars.glob("*.jar")) or not (rapidwright / "bin").is_dir():
        raise RuntimeError('current RapidWright runtime is unbuilt; run: make build-rapidwright RAPIDWRIGHT_PATH="$PWD/RapidWright"')
    env = os.environ.copy()
    env["RAPIDWRIGHT_PATH"] = str(rapidwright)
    env["RAPIDWRIGHT_JAR"] = str(jars / "rapidwright-2025.2.1-standalone-lin64.jar")
    env["CLASSPATH"] = f"{rapidwright / 'bin'}:{jars}/*"
    return env


def environment_for_case(base: dict[str, str], case: dict[str, Any]) -> dict[str, str]:
    env = base.copy()
    if not case.get("allow_external_llm"):
        for name in ("OPENAI_API_KEY", "OPENROUTER_API_KEY"):
            env.pop(name, None)
    return env


def run_logged(command: Sequence[str], *, log_path: Path, timeout: int, env: dict[str, str]) -> tuple[int | None, bool, float]:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    with log_path.open("w") as log:
        log.write("$ " + " ".join(command) + "\n\n")
        log.flush()
        process = subprocess.Popen(
            command,
            cwd=REPO,
            env=env,
            text=True,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        timed_out = False
        try:
            returncode = process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            os.killpg(process.pid, signal.SIGTERM)
            try:
                returncode = process.wait(timeout=20)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                returncode = process.wait()
    return returncode, timed_out, time.monotonic() - started


def capture_trace(run_dir: Path, case_id: str, target: Path) -> dict[str, Any] | None:
    if not (run_dir / "run.db").is_file():
        return None
    command = [sys.executable, str(REPO / "tools" / "capture_semantic_trace.py"), str(run_dir), "--case-id", case_id, "--output", str(target)]
    subprocess.run(command, cwd=REPO, check=True)
    return read_json(target)


def verify_expected(case: dict[str, Any], trace: dict[str, Any] | None, *, timed_out: bool, output: Path, input_dcp: Path) -> list[str]:
    errors: list[str] = []
    if bool(case.get("expect_timeout")) != timed_out:
        errors.append(f"timeout expectation mismatch: expected={bool(case.get('expect_timeout'))}, actual={timed_out}")
    if timed_out:
        if not output.is_file():
            errors.append("contest baseline output missing after forced timeout")
        elif sha256(output) != sha256(input_dcp):
            errors.append("forced-timeout output is not the baseline input")
        return errors
    if trace is None:
        return ["semantic trace unavailable"]
    expected_actions = case.get("expected_actions")
    if expected_actions is not None and trace.get("action_sequence") != expected_actions:
        errors.append(f"action sequence mismatch: {trace.get('action_sequence')!r}")
    expected_count = case.get("expected_attempt_count")
    if expected_count is not None and trace.get("attempt_count") != expected_count:
        errors.append(f"attempt count mismatch: {trace.get('attempt_count')!r}")
    expected_validation = case.get("expected_validation_classes")
    if expected_validation is not None and trace.get("validation_classes") != expected_validation:
        errors.append(f"validation classes mismatch: {trace.get('validation_classes')!r}")
    expected_lineage = case.get("expected_parent_lineage")
    if expected_lineage is not None and trace.get("parent_lineage") != expected_lineage:
        errors.append(f"parent lineage mismatch: {trace.get('parent_lineage')!r}")
    expected_parallelism = case.get("expected_max_parallel_jobs")
    actual_parallelism = (trace.get("resolved_config") or {}).get("max_parallel_jobs")
    if expected_parallelism is not None and actual_parallelism != expected_parallelism:
        errors.append(f"max parallel jobs mismatch: {actual_parallelism!r}")
    expected_budget = case.get("expected_budget_summary")
    actual_budget = trace.get("budget_summary") or {}
    if expected_budget is not None:
        mismatched_budget = {
            key: actual_budget.get(key)
            for key, expected in expected_budget.items()
            if actual_budget.get(key) != expected
        }
        if mismatched_budget:
            errors.append(f"budget summary mismatch: {mismatched_budget!r}")
    expected_eligibility = case.get("expected_selection_eligible")
    actual_eligibility = [attempt.get("selection_eligible") for attempt in trace.get("attempts", [])]
    if expected_eligibility is not None and actual_eligibility != expected_eligibility:
        errors.append(f"selection eligibility mismatch: {actual_eligibility!r}")
    if case.get("require_invalid_parent_continuation"):
        invalid_outputs: set[str] = set()
        continued_from_invalid = False
        for attempt in trace.get("attempts", []):
            if attempt.get("parent_state") in invalid_outputs:
                continued_from_invalid = True
            if str(attempt.get("validation_class") or "").upper() == "INVALID" and attempt.get("output_state"):
                invalid_outputs.add(str(attempt["output_state"]))
        if not continued_from_invalid:
            errors.append("no attempt continued from a canonical INVALID parent state")
    expected_fallback = case.get("expected_baseline_fallback")
    actual_fallback = (trace.get("selection") or {}).get("baseline_fallback")
    if expected_fallback is not None and actual_fallback is not expected_fallback:
        errors.append(f"baseline fallback mismatch: {actual_fallback!r}")
    if not trace.get("output_exists"):
        errors.append("selected output does not exist")
    if not (trace.get("selection") or {}).get("valid_for_publication"):
        errors.append("selected output is not valid for publication")
    return errors


def newest_validation_report(before: set[Path]) -> Path | None:
    created = [path for path in REPO.glob("dcp_validation_*") if path not in before]
    candidates = created or list(REPO.glob("dcp_validation_*"))
    if not candidates:
        return None
    report = max(candidates, key=lambda path: path.stat().st_mtime) / "validation_report.json"
    return report if report.is_file() else None


def certify(case: dict[str, Any], *, input_dcp: Path, output: Path, run_dir: Path, case_dir: Path, env: dict[str, str]) -> dict[str, Any]:
    config = case["certification"]
    before = set(REPO.glob("dcp_validation_*"))
    command = [
        sys.executable,
        str(REPO / "validate_dcps.py"),
        str(input_dcp),
        str(output),
        "--vectors",
        str(config["vectors"]),
        "--precheck-vectors",
        str(config["precheck_vectors"]),
    ]
    returncode, timed_out, duration = run_logged(
        command,
        log_path=case_dir / "certification.log",
        timeout=int(config["timeout_seconds"]),
        env=env,
    )
    report_path = newest_validation_report(before)
    report = read_json(report_path) if report_path else {}
    if report:
        write_json(case_dir / "certification-report.json", report)
        subprocess.run(
            [sys.executable, str(REPO / "tools" / "record_certification.py"), str(run_dir / "run.db"), str(case_dir / "certification-report.json")],
            cwd=REPO,
            env=env,
            check=True,
        )
    return {
        "returncode": returncode,
        "timed_out": timed_out,
        "duration_seconds": duration,
        "overall_passed": report.get("overall_passed"),
        "infrastructure_failure": report.get("infrastructure_failure"),
    }


def execute_case(case: dict[str, Any], *, benchmark_root: Path, out_root: Path, env: dict[str, str], resume: bool) -> dict[str, Any]:
    case_id = str(case["case_id"])
    case_dir = out_root / case_id
    result_path = case_dir / "result.json"
    source_input = benchmark_root / str(case["benchmark"])
    if not source_input.is_file():
        raise FileNotFoundError(source_input)
    case_dir.mkdir(parents=True, exist_ok=True)
    input_dcp = source_input
    if case.get("renamed_input"):
        input_dcp = case_dir / str(case["renamed_input"])
        if not input_dcp.exists():
            shutil.copy2(source_input, input_dcp)
    run_dir = case_dir / "run"
    output = case_dir / "output.dcp"
    case_env = environment_for_case(env, case)

    if resume and result_path.is_file():
        existing = read_json(result_path)
        if existing.get("status") == "passed":
            trace = capture_trace(run_dir, case_id, case_dir / "semantic-trace.json")
            if trace and float(existing.get("duration_seconds") or 0.0) == 0.0:
                existing["duration_seconds"] = float(
                    (trace.get("runtime") or {}).get("recorded_attempt_seconds") or 0.0
                )
            existing["errors"] = verify_expected(
                case,
                trace,
                timed_out=bool(existing.get("timed_out")),
                output=output,
                input_dcp=input_dcp,
            )
            existing["status"] = "passed" if not existing["errors"] else "failed"
            write_json(result_path, existing)
            return existing

    if resume and (run_dir / "run.db").is_file() and output.is_file():
        returncode, timed_out, duration = 0, False, 0.0
    else:
        command = [
            sys.executable,
            str(REPO / "dcp_optimizer.py"),
            str(input_dcp),
            "--no-dotenv",
            "--run-mode",
            "contest-strict",
            "--analysis",
            "none",
            "--llm-provider",
            "none",
            "--run-dir",
            str(run_dir),
            "--output",
            str(output),
            *[str(value) for value in case.get("args", [])],
        ]
        returncode, timed_out, duration = run_logged(
            command,
            log_path=case_dir / "optimizer.log",
            timeout=int(case["timeout_seconds"]),
            env=case_env,
        )

    trace = capture_trace(run_dir, case_id, case_dir / "semantic-trace.json")
    if duration == 0.0 and trace:
        duration = float((trace.get("runtime") or {}).get("recorded_attempt_seconds") or 0.0)
    errors = verify_expected(case, trace, timed_out=timed_out, output=output, input_dcp=input_dcp)
    if not timed_out and returncode != 0:
        errors.append(f"optimizer return code: {returncode}")
    certification = None
    if case.get("certification") and not errors:
        certification = certify(case, input_dcp=input_dcp, output=output, run_dir=run_dir, case_dir=case_dir, env=case_env)
        if certification.get("overall_passed") is not True or certification.get("returncode") != 0:
            errors.append("certification did not pass")
    result = {
        "schema_version": "real_golden_result.v1",
        "case_id": case_id,
        "category": case["category"],
        "status": "passed" if not errors else "failed",
        "returncode": returncode,
        "timed_out": timed_out,
        "duration_seconds": duration,
        "input_sha256": sha256(input_dcp),
        "errors": errors,
        "certification": certification,
    }
    write_json(result_path, result)
    return result


def aggregate_existing_results(out_root: Path) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for path in sorted(out_root.glob("*/result.json")):
        try:
            results.append(read_json(path))
        except (OSError, ValueError, json.JSONDecodeError):
            continue
    return results


def observed_qor_envelope(out_root: Path) -> dict[str, Any] | None:
    traces: list[dict[str, Any]] = []
    for path in sorted(out_root.glob("logicnets_phys_opt_repeat_*/semantic-trace.json")):
        try:
            trace = read_json(path)
        except (OSError, ValueError, json.JSONDecodeError):
            continue
        if (trace.get("run") or {}).get("success") and trace.get("final_fmax_mhz") is not None:
            traces.append(trace)
    if len(traces) < 2:
        return None
    final_fmax = [float(trace["final_fmax_mhz"]) for trace in traces]
    final_wns = [float(trace["final_wns_ns"]) for trace in traces if trace.get("final_wns_ns") is not None]
    runtimes = [float((trace.get("runtime") or {}).get("recorded_attempt_seconds") or 0.0) for trace in traces]
    output_hashes = {str(trace.get("selected_output_sha256")) for trace in traces}
    return {
        "case_family": "logicnets_phys_opt_repeat",
        "sample_count": len(traces),
        "final_fmax_mhz": {"min": min(final_fmax), "max": max(final_fmax), "observed_spread": max(final_fmax) - min(final_fmax)},
        "final_wns_ns": (
            {"min": min(final_wns), "max": max(final_wns), "observed_spread": max(final_wns) - min(final_wns)}
            if final_wns
            else None
        ),
        "runtime_seconds": {"min": min(runtimes), "max": max(runtimes), "gate": False},
        "distinct_selected_output_hashes": len(output_hashes),
        "bit_exact_required": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--benchmark-root", type=Path, default=os.environ.get("FPL26_BENCHMARK_ROOT"))
    parser.add_argument("--out", type=Path)
    parser.add_argument("--case", action="append", default=[])
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--list", action="store_true")
    args = parser.parse_args()
    manifest = read_json(args.manifest)
    cases = [case for case in manifest.get("cases", []) if not args.case or case.get("case_id") in args.case]
    if args.list:
        for case in cases:
            print(f"{case['case_id']}\t{case['category']}\t{case['benchmark']}")
        return 0
    if not args.benchmark_root or not args.out:
        parser.error("--benchmark-root and --out are required unless --list is used")
    env = command_env()
    results = [
        execute_case(case, benchmark_root=args.benchmark_root.resolve(), out_root=args.out.resolve(), env=env, resume=args.resume)
        for case in cases
    ]
    write_json(
        args.out / "matrix-summary.json",
        {
            "schema_version": "real_golden_summary.v1",
            "results": aggregate_existing_results(args.out),
            "observed_qor_envelope": observed_qor_envelope(args.out),
        },
    )
    failed = [result for result in results if result.get("status") != "passed"]
    print(f"real golden matrix: {len(results) - len(failed)} passed, {len(failed)} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
