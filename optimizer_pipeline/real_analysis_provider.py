"""Shared real-DCP analysis provider factory owned by the Xilinx backend."""

from __future__ import annotations

import inspect
import re
from pathlib import Path
from typing import Any, Callable, Dict, Optional


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
        try:
            await _maybe_await(tester.start_servers())
            result = await _maybe_await(tester.analyze_design_for_flow_selection(Path(input_dcp)))
            return result if isinstance(result, dict) else {}
        finally:
            cleanup = getattr(tester, "cleanup", None)
            if callable(cleanup):
                await _maybe_await(cleanup())

    return analysis_provider


async def _maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


def _safe_label(value: str) -> str:
    label = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(value)).strip("._")
    return label or "dcp"
