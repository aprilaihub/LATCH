"""Deterministic flow executor wrapper."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict

from .models import ExecutionResult


RunTestMode = Callable[..., Awaitable[Any]]


class DeterministicFlowExecutor:
    """Executes allowlisted deterministic flows through the existing test path.

    RapidWright sometimes shells out to ``vivado`` even when the top-level flow
    uses ``VIVADO_EXEC``.  Before each run, make the Vivado binary directory
    visible on PATH so subprocesses can auto-generate readable EDIFs from DCPs.
    """

    def __init__(self, run_test_mode: RunTestMode, debug: bool = False, max_nets: int = 5, preserve_summary: bool = True):
        self.run_test_mode = run_test_mode
        self.debug = debug
        self.max_nets = max_nets
        self.preserve_summary = preserve_summary

    async def execute(
        self,
        input_dcp: Path,
        output_dcp: Path,
        run_dir: Path,
        flow: str,
        optimizer_params: Dict[str, Any] | None = None,
    ) -> ExecutionResult:
        run_dir.mkdir(parents=True, exist_ok=True)
        output_dcp.parent.mkdir(parents=True, exist_ok=True)
        summary_path = run_dir / "run_summary.json"

        vivado_path_info = self._ensure_vivado_on_path()
        start_time = time.time()
        started_at = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(start_time))
        try:
            raw_result = await self.run_test_mode(
                input_dcp,
                output_dcp,
                debug=self.debug,
                max_nets=self.max_nets,
                run_dir=run_dir,
                flow=flow,
                optimizer_params=dict(optimizer_params or {}),
            )
            end_time = time.time()
            exit_code = self._normalize_exit_code(raw_result)
            metrics = self._load_summary(summary_path)
            metrics.setdefault("total_runtime_seconds", end_time - start_time)
            metrics["executor_runtime"] = {
                "start_time": started_at,
                "end_time": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(end_time)),
                "duration_seconds": end_time - start_time,
                "duration_minutes": (end_time - start_time) / 60.0,
            }
            metrics["environment"] = {
                "vivado_exec": os.environ.get("VIVADO_EXEC"),
                "vivado_path_info": vivado_path_info,
                "path_has_vivado_dir": bool(vivado_path_info.get("vivado_dir_in_path")),
            }
            metrics["optimizer_params"] = dict(optimizer_params or {})
            return ExecutionResult(
                success=exit_code == 0,
                flow=flow,
                input_dcp=str(input_dcp),
                output_dcp=str(output_dcp),
                run_dir=str(run_dir),
                # The transition validator still needs the backend summary.
                # Managed runs remove it only after validation has consumed it.
                summary_path=str(summary_path),
                metrics=metrics,
                exit_code=exit_code,
            )
        except Exception as exc:
            end_time = time.time()
            metrics = {
                "total_runtime_seconds": end_time - start_time,
                "executor_runtime": {
                    "start_time": started_at,
                    "end_time": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(end_time)),
                    "duration_seconds": end_time - start_time,
                    "duration_minutes": (end_time - start_time) / 60.0,
                },
                "environment": {
                    "vivado_exec": os.environ.get("VIVADO_EXEC"),
                    "vivado_path_info": vivado_path_info,
                    "path_has_vivado_dir": bool(vivado_path_info.get("vivado_dir_in_path")),
                },
            }
            return ExecutionResult(
                success=False,
                flow=flow,
                input_dcp=str(input_dcp),
                output_dcp=str(output_dcp),
                run_dir=str(run_dir),
                summary_path=str(summary_path),
                metrics=metrics,
                exit_code=1,
                error=f"{type(exc).__name__}: {exc}",
            )

    @staticmethod
    def _ensure_vivado_on_path() -> Dict[str, Any]:
        vivado_exec = os.environ.get("VIVADO_EXEC")
        info = {
            "vivado_exec": vivado_exec,
            "vivado_exec_exists": False,
            "vivado_dir": None,
            "vivado_dir_in_path": False,
            "path_modified": False,
        }
        if not vivado_exec:
            return info
        vivado_path = Path(vivado_exec).expanduser()
        info["vivado_exec_exists"] = vivado_path.exists()
        if not vivado_path.exists():
            return info
        vivado_dir = str(vivado_path.parent.resolve())
        info["vivado_dir"] = vivado_dir
        path_entries = os.environ.get("PATH", "").split(os.pathsep)
        if vivado_dir in path_entries:
            info["vivado_dir_in_path"] = True
            return info
        os.environ["PATH"] = vivado_dir + os.pathsep + os.environ.get("PATH", "")
        info["vivado_dir_in_path"] = True
        info["path_modified"] = True
        return info

    @staticmethod
    def _normalize_exit_code(raw_result: Any) -> int:
        if isinstance(raw_result, bool):
            return 0 if raw_result else 1
        try:
            return int(raw_result)
        except (TypeError, ValueError):
            return 0 if raw_result is None else 1

    @staticmethod
    def _load_summary(summary_path: Path) -> Dict[str, Any]:
        if not summary_path.exists():
            return {}
        try:
            with open(summary_path, "r") as handle:
                return json.load(handle)
        except Exception:
            return {}
