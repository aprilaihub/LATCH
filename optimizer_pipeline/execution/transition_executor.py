"""Reusable transition executor for one DCP optimisation step."""

from __future__ import annotations

import json
import inspect
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from optimizer_pipeline.analysis_schema import build_analysis_context_v2
from optimizer_pipeline.models import ExecutionResult, OutputValidationResult, to_jsonable
from optimizer_pipeline.qor import (
    QoRWeights,
    compute_qor_score,
    fill_missing_qor_baseline,
    has_qor_baseline,
    merge_run_and_analysis_metrics,
)

from .contracts import DcpTransitionRequest, DcpTransitionResult
from .diagnostics import apply_executor_diagnostic_backfill


class DcpTransitionExecutor:
    """Run exactly one DCP transformation and return factual step results.

    This class deliberately does not own frontier search, best-node updates,
    beam width, or portfolio policy. Those remain outer-run concerns.
    """

    def __init__(
        self,
        *,
        executor: Any,
        output_validator: Any,
        analyzer: Any,
        pipeline_config: Any,
        artifact_store: Optional[Any] = None,
        qos_weights: Optional[QoRWeights] = None,
        persist_diagnostics: bool = True,
    ) -> None:
        self.executor = executor
        self.output_validator = output_validator
        self.analyzer = analyzer
        self.pipeline_config = pipeline_config
        self.artifact_store = artifact_store
        self.qos_weights = qos_weights or QoRWeights()
        self.persist_diagnostics = bool(persist_diagnostics)

    async def run(self, request: DcpTransitionRequest, *, history: Optional[List[Any]] = None) -> DcpTransitionResult:
        output_dir = Path(request.output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        output_dcp = request.resolved_output_dcp()
        executor_run_dir = output_dir / "executor_run"
        started = time.time()
        events: List[Dict[str, Any]] = [
            {
                "event_type": "DCP_TRANSITION_STARTED",
                "optimizer": request.optimizer_name,
                "input_dcp": str(request.input_dcp),
                "output_dir": str(output_dir),
                "child_node_id": request.child_node_id,
            },
        ]
        try:
            if _executor_accepts_optimizer_params(self.executor):
                execution = await self.executor.execute(
                    Path(request.input_dcp),
                    output_dcp,
                    executor_run_dir,
                    request.optimizer_name,
                    optimizer_params=dict(request.optimizer_params or {}),
                )
            else:
                execution = await self.executor.execute(
                    Path(request.input_dcp),
                    output_dcp,
                    executor_run_dir,
                    request.optimizer_name,
                )
        except Exception as exc:
            execution = ExecutionResult(
                success=False,
                flow=request.optimizer_name,
                input_dcp=str(request.input_dcp),
                output_dcp=str(output_dcp),
                run_dir=str(executor_run_dir),
                metrics={"total_runtime_seconds": time.time() - started},
                exit_code=1,
                error=f"{type(exc).__name__}: {exc}",
            )

        artifacts: Dict[str, str] = {}
        self._write_json(output_dir / "executor_run_summary.json", execution.to_dict(), artifacts, "executor_run_summary")
        output_validation: OutputValidationResult = self.output_validator.validate(execution)
        self._write_json(output_dir / "validation_report.json", output_validation.to_dict(), artifacts, "validation_report")

        analysis_context: Dict[str, Any] = {}
        if output_validation.valid and execution.output_dcp and Path(execution.output_dcp).exists():
            try:
                raw = await self.analyzer.analyze(Path(execution.output_dcp), history or [], self.pipeline_config)
                if isinstance(raw, dict) and raw.get("schema_version") == 2:
                    analysis_context = dict(raw)
                    analysis_context.setdefault("node_id", request.child_node_id)
                    analysis_context.setdefault("parent_id", request.parent_node_id)
                else:
                    analysis_context = build_analysis_context_v2(
                        input_dcp=Path(execution.output_dcp),
                        provider_context=raw if isinstance(raw, dict) else {},
                        history=history or [],
                        config=self.pipeline_config,
                        node_id=request.child_node_id,
                        parent_id=request.parent_node_id,
                    )
            except Exception as exc:
                analysis_context = build_analysis_context_v2(
                    input_dcp=Path(execution.output_dcp),
                    provider_context={},
                    history=history or [],
                    config=self.pipeline_config,
                    provider_error=f"post-run analysis failed: {type(exc).__name__}: {exc}",
                    node_id=request.child_node_id,
                    parent_id=request.parent_node_id,
                )
        self._apply_execution_metrics_to_analysis_context(analysis_context, execution.metrics)
        apply_executor_diagnostic_backfill(analysis_context, executor_run_dir)
        self._write_json(output_dir / "analysis_context.json", analysis_context, artifacts, "analysis_context")

        metrics = merge_run_and_analysis_metrics(execution.metrics, analysis_context)
        metrics["benchmark_name"] = analysis_context.get("benchmark_name")
        node_runtime_seconds = _safe_float(metrics.get("total_runtime_seconds") or metrics.get("duration_seconds"), 0.0)
        path_runtime_seconds = _safe_float(request.parent_metrics.get("path_runtime_seconds"), 0.0) + node_runtime_seconds
        metrics.update(
            {
                "node_runtime_seconds": node_runtime_seconds,
                "path_runtime_seconds": path_runtime_seconds,
                "parent_id": request.parent_node_id,
                "candidate": {
                    "flow": request.optimizer_name,
                    "optimizer_name": request.optimizer_name,
                    "optimizer_params": dict(request.optimizer_params or {}),
                    "candidate_id": request.candidate_id,
                },
                "validation_valid": output_validation.valid,
                "validation_status": output_validation.status,
                "validation_errors": output_validation.errors,
                "validation_warnings": output_validation.warnings,
                "validation_fallback_used": output_validation.fallback_used,
                "validation_evidence": output_validation.evidence,
                "validation_log_paths": output_validation.log_paths,
            }
        )
        original_root_has_baseline = has_qor_baseline(request.root_metrics)
        effective_root_metrics = fill_missing_qor_baseline(request.root_metrics, metrics)
        inferred_root_baseline = (not original_root_has_baseline) and has_qor_baseline(effective_root_metrics)
        metrics["qor_root_metrics"] = effective_root_metrics
        metrics["qor_root_metrics_inferred"] = bool(inferred_root_baseline)
        qor = compute_qor_score(
            effective_root_metrics,
            metrics,
            weights=self.qos_weights,
            valid_output=output_validation.valid,
            path_runtime_seconds=path_runtime_seconds,
        )
        metrics["qor_score"] = qor.to_dict()
        metrics["root_fmax_mhz"] = qor.components.get("root_fmax_mhz")
        metrics["root_wns_ns"] = qor.components.get("root_wns_ns")
        metrics["root_tns_ns"] = qor.components.get("root_tns_ns")
        self._write_json(output_dir / "qor_score.json", qor.to_dict(), artifacts, "qor_score")

        accepted = bool(output_validation.valid)
        rejection_reason = None if accepted else "validation_failed"
        transition = self._transition_record(request, execution, output_validation, metrics, qor, accepted, rejection_reason)
        self._write_json(
            output_dir / "dcp_transition_result.json",
            {"request": request.to_dict(), "transition_record": transition},
            artifacts,
            "dcp_transition_result",
        )
        events.append(
            {
                "event_type": "DCP_TRANSITION_FINISHED",
                "optimizer": request.optimizer_name,
                "validation_status": output_validation.status,
                "accepted": accepted,
                "pure_qor_score": qor.total_score,
                "child_node_id": request.child_node_id,
            }
        )
        if not self.persist_diagnostics:
            # Backend JSON is a transient ingestion format.  Once validation,
            # diagnostic backfill, and metric capture are complete, canonical
            # managed runs retain structured facts only in run.db.
            for diagnostic_json in executor_run_dir.glob("*.json"):
                diagnostic_json.unlink(missing_ok=True)
        return DcpTransitionResult(
            success=bool(execution.success),
            accepted=accepted,
            input_dcp=str(request.input_dcp),
            output_dcp=str(execution.output_dcp) if execution.output_dcp else None,
            optimizer_name=request.optimizer_name,
            optimizer_params=dict(request.optimizer_params or {}),
            validation_status=output_validation.status,
            rejection_reason=rejection_reason,
            execution=execution,
            output_validation=output_validation,
            analysis_context=analysis_context,
            metrics=metrics,
            qor_score=qor,
            runtime_seconds=node_runtime_seconds,
            artifacts=artifacts,
            events=events,
            transition_record=transition,
        )

    def _write_json(self, path: Path, data: Any, artifacts: Dict[str, str], key: str) -> None:
        if not self.persist_diagnostics:
            return
        if self.artifact_store is not None:
            self.artifact_store.write_json(path, data)
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(to_jsonable(data), indent=2, sort_keys=True) + "\n")
        artifacts[key] = str(path)

    @staticmethod
    def _apply_execution_metrics_to_analysis_context(analysis_context: Dict[str, Any], run_metrics: Dict[str, Any]) -> None:
        """Prefer post-transition factual metrics when execution provides them."""
        if not isinstance(run_metrics, dict):
            return
        DcpTransitionExecutor._apply_execution_timing_to_analysis_context(analysis_context, run_metrics)
        DcpTransitionExecutor._apply_execution_runtime_to_analysis_context(analysis_context, run_metrics)
        DcpTransitionExecutor._apply_execution_utilization_to_analysis_context(analysis_context, run_metrics)

    @staticmethod
    def _apply_execution_timing_to_analysis_context(analysis_context: Dict[str, Any], run_metrics: Dict[str, Any]) -> None:
        timing_keys = (
            "clock_period_ns",
            "target_clock",
            "initial_wns_ns",
            "final_wns_ns",
            "initial_tns_ns",
            "final_tns_ns",
            "initial_fmax_mhz",
            "final_fmax_mhz",
        )
        values = {key: run_metrics.get(key) for key in timing_keys if run_metrics.get(key) is not None}
        if not values:
            return
        timing = analysis_context.get("timing")
        if not isinstance(timing, dict):
            timing = {}
        timing.update(values)
        analysis_context["timing"] = timing
        for key, value in values.items():
            analysis_context[key] = value

    @staticmethod
    def _apply_execution_runtime_to_analysis_context(analysis_context: Dict[str, Any], run_metrics: Dict[str, Any]) -> None:
        seconds = _first_present(
            run_metrics,
            "duration_seconds",
            "total_runtime_seconds",
            ("runtime", "duration_seconds"),
            ("executor_runtime", "duration_seconds"),
        )
        minutes = _first_present(
            run_metrics,
            "duration_minutes",
            ("runtime", "duration_minutes"),
            ("executor_runtime", "duration_minutes"),
        )
        seconds_float = _safe_optional_float(seconds)
        minutes_float = _safe_optional_float(minutes)
        if minutes_float is None and seconds_float is not None:
            minutes_float = seconds_float / 60.0
        if seconds_float is None and minutes_float is None:
            return
        runtime = analysis_context.get("runtime")
        if not isinstance(runtime, dict):
            runtime = {}
        if seconds_float is not None:
            runtime["duration_seconds"] = seconds_float
            analysis_context["duration_seconds"] = seconds_float
        if minutes_float is not None:
            runtime["duration_minutes"] = minutes_float
            analysis_context["duration_minutes"] = minutes_float
        analysis_context["runtime"] = runtime

    @staticmethod
    def _apply_execution_utilization_to_analysis_context(analysis_context: Dict[str, Any], run_metrics: Dict[str, Any]) -> None:
        """Prefer post-transition utilization when execution metrics provide it."""
        final_utilization = run_metrics.get("final_utilization") if isinstance(run_metrics, dict) else None
        if not isinstance(final_utilization, dict) or not final_utilization:
            return
        compact = {
            key: final_utilization.get(key)
            for key in ("luts", "ffs", "dsps", "brams", "urams")
            if final_utilization.get(key) is not None
        }
        if not compact:
            return
        analysis_context["utilization_summary"] = compact
        analysis_context["execution_final_utilization"] = dict(final_utilization)
        delta = run_metrics.get("utilization_delta")
        if isinstance(delta, dict):
            analysis_context["execution_utilization_delta"] = dict(delta)

    @staticmethod
    def _transition_record(
        request: DcpTransitionRequest,
        execution: ExecutionResult,
        validation: OutputValidationResult,
        metrics: Dict[str, Any],
        qor: Any,
        accepted: bool,
        rejection_reason: Optional[str],
    ) -> Dict[str, Any]:
        comps = (qor.components if qor else {}) or {}
        return {
            "transition_id": f"{request.child_node_id or request.candidate_id or request.optimizer_name}",
            "run_id": request.run_id,
            "parent_node_id": request.parent_node_id,
            "child_node_id": request.child_node_id,
            "input_dcp": str(request.input_dcp),
            "output_dcp": execution.output_dcp,
            "optimizer_name": request.optimizer_name,
            "optimizer_params": dict(request.optimizer_params or {}),
            "flow_sequence_before": list(request.flow_sequence_before or []),
            "flow_sequence_after": list(request.flow_sequence_before or []) + [request.optimizer_name],
            "validation_status": validation.status,
            "accepted": accepted,
            "rejection_reason": rejection_reason,
            "pure_qor_score": qor.total_score if qor else None,
            "qor_status": qor.status if qor else None,
            "delta_fmax_mhz": comps.get("fmax_delta_mhz"),
            "delta_wns_ns": comps.get("wns_improvement_ns"),
            "runtime_seconds": metrics.get("node_runtime_seconds"),
        }


def _safe_float(value: Any, default: float) -> float:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except Exception:
        return default


def _safe_optional_float(value: Any) -> Optional[float]:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except Exception:
        return None


def _first_present(source: Dict[str, Any], *keys: Any) -> Any:
    for key in keys:
        if isinstance(key, tuple):
            value: Any = source
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


def _executor_accepts_optimizer_params(executor: Any) -> bool:
    try:
        signature = inspect.signature(executor.execute)
    except (TypeError, ValueError, AttributeError):
        return False
    return "optimizer_params" in signature.parameters
