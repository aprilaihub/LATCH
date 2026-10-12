"""Shared real-DCP analysis provider factory.

The provider intentionally imports ``dcp_optimizer`` lazily so unit tests and
mock-only workflows do not need Vivado/RapidWright dependencies at import time.
"""

from __future__ import annotations

import inspect
import asyncio
import json
import logging
import os
import re
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from optimizer_pipeline.backends.xilinx.tcl import tcl_word

logger = logging.getLogger(__name__)

# Wall-clock spent inside the analysis provider: the auto_top3 diagnostic pass
# plus the read-only path-feature probe, both real Vivado sessions. This is
# per-node work that lands in neither node_runtime_seconds (it is not a flow
# execution) nor analysis_wall_time_seconds (that only exists when an online
# analysis controller is built, which the no-LLM conditions do not have). Left
# unmeasured it was 63% of an S0 run's wall clock.
#
# One process per run, so a module-level accumulator is sufficient; the lock
# keeps it correct when nodes are executed concurrently.
_DIAGNOSTIC_LOCK = threading.Lock()
_DIAGNOSTIC_SECONDS = 0.0
_DIAGNOSTIC_CALLS = 0


def reset_diagnostic_timing() -> None:
    """Zero the accumulator. Called once at run start."""
    global _DIAGNOSTIC_SECONDS, _DIAGNOSTIC_CALLS
    with _DIAGNOSTIC_LOCK:
        _DIAGNOSTIC_SECONDS = 0.0
        _DIAGNOSTIC_CALLS = 0


def diagnostic_timing() -> Dict[str, Any]:
    with _DIAGNOSTIC_LOCK:
        return {"seconds": round(_DIAGNOSTIC_SECONDS, 3), "calls": _DIAGNOSTIC_CALLS}


def _record_diagnostic(elapsed: float) -> None:
    global _DIAGNOSTIC_SECONDS, _DIAGNOSTIC_CALLS
    with _DIAGNOSTIC_LOCK:
        _DIAGNOSTIC_SECONDS += float(elapsed)
        _DIAGNOSTIC_CALLS += 1


def build_real_analysis_provider(
    *,
    run_dir: Path,
    config: Any,
    optimizer_test_cls: Optional[Callable[..., Any]] = None,
) -> Callable[[Path, Any], Any]:
    """Return an async provider backed by ``FPGAOptimizerTest``.

    Each call gets an isolated ``analysis_runs/analysis_XXX_<dcp>`` directory.
    Exceptions are deliberately allowed to propagate so ``DeterministicAnalyzer``
    can record them in ``analysis_provider_error``.
    """

    root = Path(run_dir)
    counter = {"i": 0}

    async def analysis_provider(input_dcp: Path, _pipeline_config: Any) -> Dict[str, Any]:
        counter["i"] += 1
        cls = optimizer_test_cls
        if cls is None:
            from optimizer_pipeline.backends.xilinx import FPGAOptimizerTest

            cls = FPGAOptimizerTest
        analysis_run_dir = root / "analysis_runs" / f"analysis_{counter['i']:03d}_{_safe_label(Path(input_dcp).stem)}"
        tester = cls(debug=bool(getattr(config, "debug", False)), run_dir=analysis_run_dir)
        started = time.time()
        try:
            await _maybe_await(tester.start_servers())
            result = await _maybe_await(tester.analyze_design_for_flow_selection(Path(input_dcp)))
            result = result if isinstance(result, dict) else {}
            # analyze_design_for_flow_selection reports timing and utilisation
            # but no path character, so a policy keying on route/logic balance
            # would see nulls at every node. Fill that in from a read-only
            # report pass rather than extending the auto_top3 analysis itself.
            # The owned runner must execute outside this event loop for every
            # physical probe, independently of acceptance accounting.
            result.update(await asyncio.to_thread(_probe_path_features, Path(input_dcp), analysis_run_dir))
            return result
        finally:
            cleanup = getattr(tester, "cleanup", None)
            if callable(cleanup):
                await _maybe_await(cleanup())
            # Recorded in `finally` so a failed diagnostic still costs what it
            # cost, rather than vanishing from the accounting.
            _record_diagnostic(time.time() - started)

    return analysis_provider


_PROBE_TCL = """open_checkpoint {dcp}
report_timing_summary -delay_type max -file {report}
report_timing -delay_type max -max_paths 5 -nworst 1 -append -file {report}
report_utilization -append -file {report}
report_high_fanout_nets -max_nets 20 -append -file {report}
close_design
exit
"""


def _probe_path_features(input_dcp: Path, run_dir: Path, *, owned: bool = False) -> Dict[str, Any]:
    """Read-only Vivado pass for critical-path character and fanout.

    Returns {} on any failure: a missing probe must degrade the analysis
    context, never abort the run. Callers see the absence via the usual
    null-feature handling. ``owned`` is retained for caller compatibility;
    every physical probe now uses the owned runner.
    """
    from optimizer_pipeline.backends.xilinx.owned_process import run_owned

    from experiments.ablation.runtime.analysis.report_probe_backends import (
        _parse_timing_report,
        _parse_utilization_report,
    )

    # Same resolution order as report_probe_backends.build_report_probe_backend.
    vivado = os.environ.get("VIVADO_BIN") or os.environ.get("VIVADO_EXEC") or "vivado"
    # Absolute: the script and log paths below are handed to Vivado while
    # cwd is set to this same directory, so a relative path would be
    # resolved a second time against it (path_probe/experiments/...) and
    # Vivado would fail to read its own script. --out is usually relative,
    # so run_dir is too.
    out = (Path(run_dir) / "path_probe").resolve()
    try:
        out.mkdir(parents=True, exist_ok=True)
        attempt_dir = Path(tempfile.mkdtemp(prefix="attempt-", dir=out))
        script = attempt_dir / "probe.tcl"
        report = attempt_dir / "probe.rpt"
        script.write_text(_PROBE_TCL.format(dcp=tcl_word(Path(input_dcp).resolve()), report=tcl_word(report)))
        argv = [vivado, "-mode", "batch", "-source", str(script), "-nojournal", "-log", str(attempt_dir / "probe.log")]
        proc = run_owned(argv, cwd=str(attempt_dir), text=True, timeout=1800)
        text = report.read_text(errors="ignore") if report.exists() else ""
        if proc.returncode != 0 or not text:
            (out / "probe-status.json").write_text(json.dumps({
                "status": "INFRASTRUCTURE_FAILURE", "exit_code": proc.returncode,
                "report_available": bool(text),
                "report_path": str(report) if report.exists() else None,
            }) + "\n")
            # Vivado reports startup failures on stdout, not stderr, so both
            # are captured here -- logging only stderr showed an empty string
            # and hid the actual cause.
            logger.warning(
                "path-feature probe did not complete for %s (vivado rc=%s, "
                "report=%s exists=%s) stdout=%r stderr=%r",
                input_dcp, getattr(proc, "returncode", "?"), report, report.exists(),
                (getattr(proc, "stdout", "") or "")[-1500:] or "(empty)",
                (getattr(proc, "stderr", "") or "")[-800:] or "(empty)",
            )
            return {}
        timing = _parse_timing_report(text)
        util = _parse_utilization_report(text)
        merged: Dict[str, Any] = {}
        if timing.get("critical_path_features"):
            merged["critical_path_features"] = timing["critical_path_features"]
        if util.get("fanout"):
            merged["fanout"] = util["fanout"]
        (out / "probe-status.json").write_text(json.dumps({
            "status": "MEASURED", "exit_code": 0, "report_path": str(report),
        }) + "\n")
        return merged
    except Exception as exc:  # noqa: BLE001 - best-effort, but never silent
        if out.is_dir():
            (out / "probe-status.json").write_text(json.dumps({
                "status": "INFRASTRUCTURE_FAILURE", "error_type": type(exc).__name__,
            }) + "\n")
        # A swallowed failure here is indistinguishable from "this design has
        # no path features", which is how three of four scoring features went
        # missing across an entire sweep unnoticed. Always leave a trace.
        logger.warning(
            "path-feature probe FAILED for %s: %s: %s (vivado=%r out=%s)",
            input_dcp, type(exc).__name__, exc, vivado, out, exc_info=True,
        )
        return {}


async def _maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


def _safe_label(value: str) -> str:
    label = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(value)).strip("._")
    return label or "dcp"
