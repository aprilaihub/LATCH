"""Read-only rescue evidence tools for online analysis."""

from __future__ import annotations

from typing import Any, Dict

from optimizer_pipeline.capabilities.intervention.evidence import RescueEvidenceBuilder, RescueKnownPublicPriorStore, diagnose_attempt

from .base import ToolContext


class LoadRescueAttemptsTool:
    name = "load_rescue_attempts"
    tool_class = "inspect"
    mutates_design = False

    def run(self, args: Dict[str, Any], context: ToolContext) -> Dict[str, Any]:
        attempts = _attempts_from_context(context)
        return {
            "schema_version": "rescue_attempts_tool.v1",
            "attempts": [_compact_attempt(attempt) for attempt in attempts],
        }


class CompareParentChildQorTool:
    name = "compare_parent_child_qor"
    tool_class = "inspect"
    mutates_design = False

    def run(self, args: Dict[str, Any], context: ToolContext) -> Dict[str, Any]:
        parent_id = str(args.get("parent_node_id") or getattr(context.node, "parent_id", "") or "")
        child_id = str(args.get("child_node_id") or args.get("node_id") or getattr(context.node, "node_id", "") or "")
        parent = context.nodes.get(parent_id)
        child = context.nodes.get(child_id)
        builder = _builder(context)
        return {
            "schema_version": "parent_child_qor_compare.v1",
            "parent": builder.node_summary(parent),
            "child": builder.node_summary(child),
            "delta_fmax_mhz": _delta(
                builder.node_summary(child).get("fmax_mhz"),
                builder.node_summary(parent).get("fmax_mhz"),
            ),
            "child_validation_errors": list(getattr(child, "validation_errors", []) or [])[:20] if child is not None else [],
            "child_validation_warnings": list(getattr(child, "validation_warnings", []) or [])[:20] if child is not None else [],
        }


class ClassifyAttemptFailureTool:
    name = "classify_attempt_failure"
    tool_class = "inspect"
    mutates_design = False

    def run(self, args: Dict[str, Any], context: ToolContext) -> Dict[str, Any]:
        attempts = _attempts_from_context(context)
        index = args.get("attempt_index")
        attempt = None
        if index is not None:
            for row in attempts:
                if int(row.get("attempt_index") or 0) == int(index):
                    attempt = row
                    break
        if attempt is None and attempts:
            attempt = attempts[-1]
        if attempt is None:
            return {"schema_version": "attempt_failure_classification.v1", "diagnosis": None, "reason": "no_attempts"}
        return {
            "schema_version": "attempt_failure_classification.v1",
            "diagnosis": diagnose_attempt(attempt).to_dict(),
        }


class LoadBestValidBranchesTool:
    name = "load_best_valid_branches"
    tool_class = "inspect"
    mutates_design = False

    def run(self, args: Dict[str, Any], context: ToolContext) -> Dict[str, Any]:
        attempts = _attempts_from_context(context)
        candidates = _builder(context).safe_parent_candidates(attempts=attempts)
        return {
            "schema_version": "best_valid_branches.v1",
            "safe_parent_candidates": candidates,
        }


class LoadPrimitiveEffectHistoryTool:
    name = "load_primitive_effect_history"
    tool_class = "inspect"
    mutates_design = False

    def run(self, args: Dict[str, Any], context: ToolContext) -> Dict[str, Any]:
        attempts = _attempts_from_context(context)
        builder = _builder(context)
        return {
            "schema_version": "primitive_effect_history.v1",
            "primitive_effect_history": builder.primitive_effect_history(attempts=attempts),
            "flow_effect_history": builder.flow_effect_history(),
            "validation_risk_summary": builder.validation_risk_summary(attempts=attempts),
        }


class LoadKnownPublicRecipePriorTool:
    name = "load_known_public_recipe_prior"
    tool_class = "inspect"
    mutates_design = False

    def run(self, args: Dict[str, Any], context: ToolContext) -> Dict[str, Any]:
        scope = str(args.get("history_scope") or getattr(context.config, "rescue_history_scope", "in_run") or "in_run")
        benchmark = str(args.get("benchmark_name") or _benchmark_from_context(context) or "")
        prior = RescueKnownPublicPriorStore().prior_for(benchmark_name=benchmark, history_scope=scope)
        return {
            "schema_version": "known_public_recipe_prior.v1",
            "history_scope": scope,
            "benchmark_name": benchmark,
            "prior": prior,
        }


def _builder(context: ToolContext) -> RescueEvidenceBuilder:
    ctx = _fake_ctx(context)
    return RescueEvidenceBuilder(ctx)


def _fake_ctx(context: ToolContext) -> Any:
    class Ctx:
        pass

    ctx = Ctx()
    ctx.nodes = context.nodes
    ctx.current_node = context.node
    ctx.config = context.config
    ctx.analysis_contexts = context.analysis_contexts
    ctx.root_dcp = getattr(context.config, "root_dcp", "")
    ctx.root_metrics = _root_metrics(context)
    ctx.transition_records = list(getattr(context.config, "transition_records", []) or [])
    state = Ctx()
    best = _best_node(context)
    state.best_node_id = getattr(best, "node_id", None)
    ctx.state = state
    return ctx


def _attempts_from_context(context: ToolContext) -> list[dict]:
    config_attempts = getattr(context.config, "rescue_attempts", None)
    if isinstance(config_attempts, list):
        return [dict(item) for item in config_attempts if isinstance(item, dict)]
    ctx = context.analysis_contexts.get("__rescue__", {}) if isinstance(context.analysis_contexts, dict) else {}
    attempts = ctx.get("attempts") if isinstance(ctx, dict) else None
    if isinstance(attempts, list):
        return [dict(item) for item in attempts if isinstance(item, dict)]
    return []


def _compact_attempt(attempt: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "attempt_index": attempt.get("attempt_index"),
        "parent_node_id": attempt.get("parent_node_id"),
        "child_node_id": attempt.get("child_node_id"),
        "delta_fmax_mhz": attempt.get("delta_fmax_mhz"),
        "runtime_seconds": attempt.get("runtime_seconds"),
        "validation_status": attempt.get("validation_status"),
        "updated_incumbent": attempt.get("updated_incumbent"),
        "rejection_reason": attempt.get("rejection_reason"),
    }


def _best_node(context: ToolContext) -> Any:
    best = None
    for node in context.nodes.values():
        if best is None:
            best = node
            continue
        score = getattr(node, "score", None)
        best_score = getattr(best, "score", None)
        if score is not None and (best_score is None or score > best_score):
            best = node
    return best or context.node


def _root_metrics(context: ToolContext) -> Dict[str, Any]:
    root = context.nodes.get("node_000_root") if isinstance(context.nodes, dict) else None
    return dict(getattr(root, "metrics", {}) or {})


def _benchmark_from_context(context: ToolContext) -> str:
    root = context.nodes.get("node_000_root") if isinstance(context.nodes, dict) else None
    path = str(getattr(root, "input_dcp", "") or getattr(root, "output_dcp", "") or "")
    return path.rsplit("/", 1)[-1].replace("_2025.1.dcp", "").replace(".dcp", "").replace("-", "_")


def _delta(value: Any, base: Any) -> Any:
    try:
        if value is None or base is None:
            return None
        return float(value) - float(base)
    except (TypeError, ValueError):
        return None
