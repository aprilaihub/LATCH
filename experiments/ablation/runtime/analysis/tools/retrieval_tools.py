"""Retrieval-memory tools for online deep analysis."""

from __future__ import annotations

from typing import Any, Dict, List

from .base import ToolContext


class QueryRetrievalMemoryTool:
    name = "query_retrieval_memory"
    tool_class = "inspect"
    mutates_design = False

    def run(self, args: Dict[str, Any], context: ToolContext) -> Dict[str, Any]:
        if context.retrieval_index is None:
            return {"available": False, "reason": "no retrieval index configured"}
        node = context.node
        node_id = getattr(node, "node_id", None)
        analysis_context = context.analysis_contexts.get(str(node_id), {}) if node_id else {}
        flows: List[str] = list(args.get("flows") or [])
        if not flows:
            flows = _candidate_flows_from_context(analysis_context)[:5]
        evidence = []
        for flow in flows:
            try:
                ev = context.retrieval_index.candidate_evidence(
                    node={
                        "node_id": node_id,
                        "flow_sequence": list(getattr(node, "flow_sequence", []) or []),
                        "pure_qor_score": getattr(node, "score", None),
                        "trajectory_objective_score": getattr(node, "score", None),
                    },
                    analysis_context=analysis_context,
                    candidate_flow=flow,
                    k=int(args.get("k") or 5),
                )
                row = {"flow": flow, **ev}
                row["retrieved_motifs"] = _retrieved_motifs(ev)
                row["outcome_delta_summary"] = _outcome_delta_summary(ev)
                evidence.append(row)
            except Exception as exc:
                evidence.append({"flow": flow, "available": False, "error": f"{type(exc).__name__}: {exc}"})
        evidence.sort(key=lambda item: (item.get("success_prior") or 0.0, -(item.get("invalid_risk") or 0.0)), reverse=True)
        return {"available": True, "evidence": evidence, "flows": {str(item.get("flow")): item for item in evidence}}


def _candidate_flows_from_context(ctx: Dict[str, Any]) -> List[str]:
    """Flows worth querying retrieval memory for, restricted to the action space.

    This reads the raw analysis context, so it still sees the auto_top3 ranking
    that LLM-facing projections strip. Fall back to the allowed flows and filter
    against them, so a ranking entry for a non-selectable flow (``pblock``,
    ``vexriscv``, ``fanout``) can never surface as retrieval evidence.
    """
    cf = ctx.get("candidate_flows") if isinstance(ctx.get("candidate_flows"), dict) else {}
    allowed = [str(flow) for flow in (cf.get("allowed_flows") or []) if flow]
    if not allowed:
        return []
    ranked: List[str] = []
    scores = cf.get("deterministic_scores") or ctx.get("flow_scores") or []
    if isinstance(scores, list):
        for item in scores:
            if isinstance(item, dict) and item.get("flow"):
                ranked.append(str(item["flow"]))
    ordered = [flow for flow in ranked if flow in allowed]
    ordered += [flow for flow in allowed if flow not in ordered]
    return ordered


def _retrieved_motifs(ev: Dict[str, Any]) -> List[Dict[str, Any]]:
    for key in ("retrieved_motifs", "similar_motifs", "motifs", "motif_evidence"):
        value = ev.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)][:5]
    examples = ev.get("examples")
    if isinstance(examples, list):
        motifs: List[Dict[str, Any]] = []
        for item in examples[:5]:
            if isinstance(item, dict):
                motifs.append({
                    "source": item.get("source") or item.get("node_id") or item.get("trace_id"),
                    "flow_sequence_before": item.get("flow_sequence_before"),
                    "flow_sequence_after": item.get("flow_sequence_after") or item.get("flow_sequence"),
                    "delta_pure_qor": item.get("delta_pure_qor_from_parent") or item.get("delta_pure_qor"),
                    "validation_status": item.get("validation_status"),
                })
        return motifs
    return []


def _outcome_delta_summary(ev: Dict[str, Any]) -> Dict[str, Any]:
    for key in ("outcome_delta_summary", "outcome_deltas", "delta_summary"):
        value = ev.get(key)
        if isinstance(value, dict):
            return value
    return {
        "best_delta_pure_qor": ev.get("best_delta_pure_qor"),
        "mean_delta_pure_qor": ev.get("mean_delta_pure_qor"),
        "success_prior": ev.get("success_prior"),
        "invalid_risk": ev.get("invalid_risk"),
    }
