"""Typed read/probe tool abstraction for online deep analysis."""

from __future__ import annotations

import concurrent.futures
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional, Protocol

from ..schemas import AnalysisToolCall


@dataclass
class ToolContext:
    run_dir: Path
    node: Any
    nodes: Dict[str, Any]
    analysis_contexts: Dict[str, Dict[str, Any]]
    retrieval_index: Any = None
    config: Any = None
    allowed_flows: Optional[list[str]] = None
    report_probe_backend: Any = None


class AnalysisTool(Protocol):
    name: str
    tool_class: str
    mutates_design: bool

    def run(self, args: Dict[str, Any], context: ToolContext) -> Dict[str, Any]:
        ...


def execute_tool(tool: AnalysisTool, args: Dict[str, Any], context: ToolContext, *, timeout_sec: Optional[float] = None) -> AnalysisToolCall:
    started = time.strftime("%Y-%m-%d %H:%M:%S")
    t0 = time.time()
    timeout = _timeout_for(tool, timeout_sec)
    try:
        executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        fut = executor.submit(tool.run, args, context)
        try:
            output = fut.result(timeout=timeout)
        except concurrent.futures.TimeoutError:
            fut.cancel()
            executor.shutdown(wait=False, cancel_futures=True)
            return AnalysisToolCall(
                tool_name=tool.name,
                args=args,
                started_at=started,
                elapsed_sec=round(time.time() - t0, 6),
                success=False,
                output={},
                error=f"Tool timed out after {timeout} seconds",
                tool_class=getattr(tool, "tool_class", "inspect"),
                timed_out=True,
                timeout_sec=timeout,
                mutates_design=bool(getattr(tool, "mutates_design", False)),
            )
        finally:
            if fut.done():
                executor.shutdown(wait=True)
        return AnalysisToolCall(
            tool_name=tool.name,
            args=args,
            started_at=started,
            elapsed_sec=round(time.time() - t0, 6),
            success=True,
            output=output if isinstance(output, dict) else {"value": output},
            tool_class=getattr(tool, "tool_class", "inspect"),
            timeout_sec=timeout,
            mutates_design=bool(getattr(tool, "mutates_design", False)),
        )
    except Exception as exc:  # pragma: no cover - defensive path
        return AnalysisToolCall(
            tool_name=getattr(tool, "name", "unknown_tool"),
            args=args,
            started_at=started,
            elapsed_sec=round(time.time() - t0, 6),
            success=False,
            output={},
            error=f"{type(exc).__name__}: {exc}",
            tool_class=getattr(tool, "tool_class", "inspect"),
            timeout_sec=timeout,
            mutates_design=bool(getattr(tool, "mutates_design", False)),
        )


def _timeout_for(tool: AnalysisTool, timeout_sec: Optional[float]) -> Optional[float]:
    if timeout_sec is not None:
        return max(0.001, float(timeout_sec))
    value = getattr(tool, "default_timeout_sec", None)
    if value is None:
        return None
    return max(0.001, float(value))
