"""auto_top8 - deterministic flow-selection policy for the S0 condition.

Self-contained. The only public entry points are:

    load_policy(path=None) -> AutoTop8Policy
    AutoTop8Policy.select(features, allowed_flows) -> str

Nothing else in the pipeline should import from this module. It is registered
at exactly one place (the `policy_type` dispatch in
scripts/run_frontier_expansion.py), so a replacement heuristic can be added as
a new policy_type value without touching any surrounding code.

Scoring is `w . z + intercept` per flow, where `z` is the feature vector
standardised with the means/stds recorded at fit time. The weights live in
configs/auto_top8_weights_default.json and are never computed here - fitting
is scripts/fit_auto_top8.py. Keeping the scorer and the weights separate is
what lets the policy be re-fitted against new data (a fanout signal, a real
congestion signal) without any code change.

Features consumed, in fixed order (see FEATURE_ORDER):
    route_delay_ratio, max_logic_levels, cp_spread_avg_distance,
    total_hardblocks

A feature that is missing or non-numeric falls back to the fit-time mean,
i.e. contributes zero after standardisation. That makes the policy usable at
any node in a chain, including states where only some features have been
extracted, rather than only at the pristine root.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence

__all__ = ["AutoTop8Policy", "load_policy", "FEATURE_ORDER", "DEFAULT_WEIGHTS_PATH"]

FEATURE_ORDER: Sequence[str] = (
    "route_delay_ratio",
    "max_logic_levels",
    "cp_spread_avg_distance",
    "total_hardblocks",
)

DEFAULT_WEIGHTS_PATH = str(Path(__file__).resolve().parents[1] / "configs" / "auto_top8_weights_default.json")

# Ties are broken alphabetically by flow name. Fixed and documented so that a
# tie can never make the policy non-deterministic across runs or machines.
_TIE_BREAK = sorted


def _as_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, bool):
        return float(value)
    try:
        text = str(value).strip()
        if text == "":
            return None
        return float(text)
    except (TypeError, ValueError):
        return None


class AutoTop8Policy:
    """Scores the 9-flow catalogue and returns a single chosen flow."""

    def __init__(self, payload: Mapping[str, Any]) -> None:
        self.version: str = str(payload.get("auto_top8_version", "unknown"))
        self.feature_order: List[str] = list(
            payload.get("auto_top8_feature_order") or FEATURE_ORDER
        )
        self.standardisation: Dict[str, Dict[str, float]] = dict(
            payload.get("auto_top8_standardisation") or {}
        )
        self.flows: Dict[str, Dict[str, Any]] = dict(
            payload.get("auto_top8_flows") or {}
        )
        if not self.flows:
            raise ValueError("auto_top8 weights contain no flows")

    # -- internals -----------------------------------------------------

    def _standardise(self, features: Mapping[str, Any]) -> List[float]:
        out: List[float] = []
        for name in self.feature_order:
            stats = self.standardisation.get(name) or {}
            mean = float(stats.get("mean", 0.0))
            std = float(stats.get("std", 1.0)) or 1.0
            raw = _as_float(features.get(name))
            # Missing -> fit-time mean -> zero contribution after centring.
            value = mean if raw is None else raw
            out.append((value - mean) / std)
        return out

    def score(self, features: Mapping[str, Any]) -> Dict[str, float]:
        """Score every flow this policy knows about. Deterministic."""
        z = self._standardise(features)
        scores: Dict[str, float] = {}
        for flow, spec in self.flows.items():
            weights = [float(w) for w in (spec.get("weights") or [])]
            intercept = float(spec.get("intercept", 0.0))
            total = intercept
            for w, zi in zip(weights, z):
                total += w * zi
            scores[flow] = total
        return scores

    # -- public interface ----------------------------------------------

    def missing_features(self, features: Mapping[str, Any]) -> List[str]:
        """Fitted features absent (or non-numeric) in this state."""
        return [n for n in self.feature_order if _as_float(features.get(n)) is None]

    def explain(
        self,
        features: Mapping[str, Any],
        allowed_flows: Optional[Iterable[str]] = None,
    ) -> tuple:
        """Return (chosen_flow, diagnostics).

        diagnostics carries , set when any fitted feature is
        missing from the state. Missing features are imputed with the fit-time
        mean, which makes the decision look feature-driven when it is not; the
        marker keeps that distinction visible in the decision record rather
        than silently absorbed.
        """
        missing = self.missing_features(features)
        chosen = self.select(features, allowed_flows)
        spec = self.flows.get(chosen) or {}
        z = self._standardise(features)
        feat_term = sum(float(w) * zi for w, zi in zip(spec.get("weights") or [], z))
        intercept = float(spec.get("intercept", 0.0))
        denom = abs(feat_term) + abs(intercept)
        measured = len(self.feature_order) - len(missing)
        return chosen, {
            "fallback_reason": "insufficient_features" if missing else None,
            "missing_features": missing,
            "measured_features": measured,
            # Fewer than half the features measured => the score is carried by
            # the flow's fitted intercept (its average past performance)
            # rather than by this design's diagnosis.
            "confidence": "low" if measured * 2 < len(self.feature_order) else "normal",
            "feature_share": (abs(feat_term) / denom) if denom else 0.0,
            "policy_version": self.version,
        }

    def select(
        self,
        features: Mapping[str, Any],
        allowed_flows: Optional[Iterable[str]] = None,
    ) -> str:
        """Return exactly one flow. No hedging, no multi-candidate output.

        `allowed_flows` restricts the catalogue to what the caller can
        actually dispatch; flows the policy has no weights for are ignored.
        Ties are broken alphabetically.
        """
        scores = self.score(features)
        if allowed_flows is not None:
            allowed = {f for f in allowed_flows if f}
            scores = {f: s for f, s in scores.items() if f in allowed}
            if not scores:
                raise ValueError(
                    "auto_top8: no overlap between allowed_flows and fitted flows"
                )
        best = max(scores.values())
        return _TIE_BREAK([f for f, s in scores.items() if s == best])[0]


def load_policy(path: Optional[str] = None) -> AutoTop8Policy:
    """Load the fitted weights and return a ready policy."""
    resolved = path or DEFAULT_WEIGHTS_PATH
    if not os.path.isabs(resolved):
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        resolved = os.path.join(here, resolved)
    with open(resolved) as handle:
        return AutoTop8Policy(json.load(handle))


# --------------------------------------------------------------------------
# Pipeline adapter. Everything below maps the surrounding pipeline's shapes
# onto the pure interface above; the scoring logic itself lives entirely in
# AutoTop8Policy and knows nothing about analysis contexts or expansion
# requests.
# --------------------------------------------------------------------------

def features_from_analysis_context(context: Mapping[str, Any]) -> Dict[str, Any]:
    """Pull the four fitted features out of a build_analysis_context_v2 dict.

    Works at any node in a chain, not just the pristine root: it reads
    whatever the per-node extraction produced and leaves the rest missing,
    which the scorer treats as the fit-time mean.
    """
    if not isinstance(context, Mapping):
        return {}
    cpf = context.get("critical_path_features") or {}
    util = context.get("utilization_summary") or {}
    spread = ((context.get("rapidwright") or {}).get("critical_path_spread") or {})

    dsps = _as_float(util.get("dsps"))
    brams = _as_float(util.get("brams"))
    total_hardblocks = None
    if dsps is not None and brams is not None:
        total_hardblocks = dsps + brams

    fanout = context.get("fanout") or {}
    return {
        "route_delay_ratio": cpf.get("route_delay_ratio"),
        "max_logic_levels": cpf.get("max_logic_levels"),
        "total_hardblocks": total_hardblocks,
        "max_fanout": fanout.get("max_fanout"),
        # Retained for models fitted with it; the deployed model does not use
        # it, and no Vivado-only probe can supply it (RapidWright provides it).
        "cp_spread_avg_distance": spread.get("avg_max_distance"),
    }


class AutoTop8ExpansionPolicy:
    """Expansion policy that returns exactly one flow per frontier node.

    Deliberately not a ranked list: auto_top8's contract is a single argmax
    choice per call, so the S0 condition executes one flow per iteration with
    no hedging and no multi-candidate comparison.
    """

    def __init__(self, policy: AutoTop8Policy, policy_name: str = "auto_top8") -> None:
        self._policy = policy
        self.policy_name = policy_name

    @property
    def version(self) -> str:
        return self._policy.version

    def usage_summary(self) -> Dict[str, Any]:
        return {
            "provider": None,
            "model": None,
            "total_llm_calls": 0,
            "total_tokens": None,
            "total_cost_usd": None,
        }

    def propose(self, request: Mapping[str, Any]) -> Any:
        # Imported lazily so the pure scorer above stays importable (and
        # testable) without pulling in the pipeline package.
        from experiments.ablation.runtime.expansion.policies.core import (
            ExpansionDecision,
            TransitionCandidate,
        )

        frontier = [n for n in (request.get("frontier") or []) if isinstance(n, dict)]
        allowed_flows = list(request.get("allowed_flows") or [])
        contexts = request.get("analysis_context_by_node") or {}
        existing = {tuple(seq) for seq in (request.get("existing_sequences") or [])}
        batch_size = int(request.get("batch_size") or request.get("branching_factor") or 1)

        if not frontier:
            return ExpansionDecision(
                stop=True, reason="No frontier nodes available", model=self.policy_name
            )

        candidates = []
        for node in frontier:
            node_id = str(node.get("node_id"))
            seq = list(node.get("flow_sequence") or [])
            last_flow = seq[-1] if seq else None
            # disallow_consecutive_repeat is enforced by the pipeline config,
            # but the choice is a single flow here, so an immediate repeat
            # would waste the whole iteration rather than one candidate.
            usable = [f for f in allowed_flows if f != last_flow] or list(allowed_flows)
            try:
                flow, diag = self._policy.explain(
                    features_from_analysis_context(contexts.get(node_id, {})),
                    usable,
                )
            except ValueError as exc:
                return ExpansionDecision(
                    stop=True, reason=f"auto_top8 could not select a flow: {exc}",
                    model=self.policy_name,
                )
            if tuple(seq + [flow]) in existing:
                continue
            candidates.append(
                TransitionCandidate(
                    target_node_id=node_id,
                    flow=flow,
                    priority=1.0,
                    expected_effect=(
                        f"auto_top8 ({diag['policy_version']}) argmax over fitted per-flow "
                        f"scores; confidence={diag['confidence']}; "
                        f"measured_features={diag['measured_features']}/{len(self._policy.feature_order)}; "
                        f"feature_share={diag['feature_share']:.2f}"
                        + (f"; fallback_reason={diag['fallback_reason']} "
                           f"(missing: {','.join(diag['missing_features'])})"
                           if diag['fallback_reason'] else "")
                    ),
                    risk="medium",
                    confidence="medium",
                )
            )
            if len(candidates) >= batch_size:
                break

        return ExpansionDecision(
            decision_type="expansion_plan" if len({c.target_node_id for c in candidates}) > 1 else "expand_node",
            target_node_id=candidates[0].target_node_id if candidates else frontier[0].get("node_id"),
            candidates=candidates,
            stop=not bool(candidates),
            reason="auto_top8 selected one flow per frontier node.",
            model=self.policy_name,
        )


def build_auto_top8_expansion_policy(*, resources: Any = None) -> AutoTop8ExpansionPolicy:
    """Factory used by the single registry registration point."""
    path = None
    config = getattr(resources, "config", None)
    if config is not None:
        path = getattr(config, "auto_top8_weights_path", None)
    return AutoTop8ExpansionPolicy(load_policy(path))
