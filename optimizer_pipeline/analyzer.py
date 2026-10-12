"""Deterministic DCP analysis context builder."""

from __future__ import annotations

import inspect
import time
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, Iterable, Optional, Union

from .analysis_schema import build_analysis_context_v2
from .models import DcpStateNode, to_jsonable

AnalysisProvider = Callable[[Path, Any], Union[Awaitable[Dict[str, Any]], Dict[str, Any]]]


class DeterministicAnalyzer:
    """Builds structured selector context from deterministic artifacts.

    The pipeline package stays decoupled from Vivado/RapidWright by accepting an
    optional provider callback. The CLI wires that provider to the existing
    auto_top3-style analysis code in dcp_optimizer.py.
    """

    def __init__(self, analysis_provider: Optional[AnalysisProvider] = None):
        self.analysis_provider = analysis_provider

    async def analyze(
        self,
        input_dcp: Path,
        history: Iterable[DcpStateNode],
        config: Any,
    ) -> Dict[str, Any]:
        path = Path(input_dcp)
        history_items = [node.to_dict() for node in history]
        completed = [node for node in history if node.metrics]
        previous_metrics = completed[-1].metrics if completed else {}
        provider_context: Dict[str, Any] = {}
        provider_error = None

        if self.analysis_provider is not None:
            try:
                maybe_context = self.analysis_provider(path, config)
                if inspect.isawaitable(maybe_context):
                    maybe_context = await maybe_context
                if isinstance(maybe_context, dict):
                    provider_context = maybe_context
                else:
                    provider_error = f"analysis provider returned {type(maybe_context).__name__}"
            except Exception as exc:
                provider_error = f"{type(exc).__name__}: {exc}"

        file_info: Dict[str, Any] = {
            "path": str(path.resolve()) if path.exists() else str(path),
            "name": path.name,
            "exists": path.exists(),
            "size_bytes": path.stat().st_size if path.exists() else None,
            "modified_time": time.strftime(
                "%Y-%m-%d %H:%M:%S",
                time.localtime(path.stat().st_mtime),
            )
            if path.exists()
            else None,
        }

        initial_wns = self._first_present(
            provider_context,
            "initial_wns_ns",
            ("timing", "wns_ns"),
            "wns_ns",
        )
        clock_period = self._first_present(
            provider_context,
            "clock_period_ns",
            ("timing", "clock_period_ns"),
        )
        initial_fmax = self._first_present(
            provider_context,
            "initial_fmax_mhz",
            ("timing", "achievable_fmax_mhz"),
        )
        if initial_fmax is None:
            initial_fmax = self._calculate_fmax(initial_wns, clock_period)

        utilization = provider_context.get("utilization_summary")
        if utilization is None:
            utilization = provider_context.get("utilization")
        if not isinstance(utilization, dict):
            utilization = {}

        flow_scores = provider_context.get("flow_scores")
        if not isinstance(flow_scores, list):
            flow_scores = []
        flow_scores = [
            item
            for item in flow_scores
            if isinstance(item, dict)
        ]

        return build_analysis_context_v2(
            input_dcp=path,
            provider_context=provider_context,
            history=history,
            config=config,
            provider_error=provider_error,
        )

    @staticmethod
    def _first_present(source: Dict[str, Any], *keys: Any) -> Any:
        for key in keys:
            if isinstance(key, tuple):
                value = source
                for part in key:
                    if not isinstance(value, dict) or part not in value:
                        value = None
                        break
                    value = value[part]
                if value is not None:
                    return value
            elif key in source and source[key] is not None:
                return source[key]
        return None

    @staticmethod
    def _calculate_fmax(wns: Any, clock_period: Any) -> Optional[float]:
        try:
            wns_value = float(wns)
            period_value = float(clock_period)
        except (TypeError, ValueError):
            return None
        if period_value <= 0:
            return None
        achievable_period = period_value - wns_value
        if achievable_period <= 0:
            return None
        return 1000.0 / achievable_period
