"""Provider, model, prompt, and pricing ownership for intervention proposals."""

from __future__ import annotations

import json
from typing import Any, Dict, Optional

from optimizer_pipeline.capabilities.intervention.generation import GeneratedInterventionProposal
from integrations.llm.runtime import LLMRequest, LLMUsageAccumulator, build_llm_client


class ProviderInterventionGenerator:
    """Generate untrusted JSON; managed capabilities perform typed validation."""

    def __init__(self, config: Any, *, client: Optional[Any] = None) -> None:
        self.config = config
        self.provider = str(
            getattr(config, "analysis_loop_provider", None)
            or getattr(config, "provider", None)
            or "none"
        ).strip().lower()
        self.model = str(
            getattr(config, "analysis_loop_model", None)
            or getattr(config, "model", None)
            or ("gpt-5.4-mini" if self.provider == "openai" else "openai/gpt-5.4-mini")
        )
        self.client = client
        self.usage = LLMUsageAccumulator()

    def usage_summary(self) -> Dict[str, Any]:
        return self.usage.to_dict()

    def generate_recipe(self, *, context: Dict[str, Any], catalog: Dict[str, Any]) -> GeneratedInterventionProposal:
        payload = {
            "task": "Generate exactly one guarded FPGA implementation recipe from the primitive catalog.",
            "context": context,
            "primitive_catalog": catalog,
            "output_schema": {
                "recipe_id": "short snake_case id",
                "summary": "one sentence",
                "risk_notes": ["short warnings"],
                "steps": [{
                    "step_id": "step_01",
                    "primitive": "one primitive_catalog name",
                    "args": {"directive": "only for typed Vivado primitives"},
                    "rationale": "short reason",
                }],
            },
        }
        system = (
            "You are a bounded FPGA implementation recipe synthesizer. Return exactly one JSON object and no extra text. "
            "Use only primitive_catalog names. Do not output candidate_priorities. Do not write Tcl, shell, file paths, "
            "write_checkpoint, or run_tcl. You may use macro primitives such as macro.unroute_design; their Tcl is fixed "
            "by the runtime. You may use flow.* primitives only as named existing optimizer flows and without args; for "
            "optimizer flows use names like flow.retime, never bare names like retime. For typed Vivado primitives, use "
            "only allowlisted directives from the catalog. Prefer short recipes with 3 to 6 mutating steps. When context "
            "contains rescue_attempt_plan, satisfy its objective and parent intent. When context contains attempt_diagnoses, "
            "avoid repeating invalid, regressed, or no-gain motifs. Use known_public_prior only as a benchmark-specific "
            "prior when it is present in the context."
        )
        return self._complete("recipe", system, payload)

    def generate_manual(
        self,
        *,
        kind: str,
        context: Dict[str, Any],
        catalog: Dict[str, Any],
        plan: Optional[Dict[str, Any]],
        action_index: Optional[int],
    ) -> GeneratedInterventionProposal:
        if kind == "plan":
            task = "Generate a multi-step manual rescue plan as guidance for the CURRENT implementation state. Do not execute it."
            output_schema: Dict[str, Any] = {
                "plan_id": "short snake_case id",
                "hypothesis": "what timing/route issue this manual intervention tests",
                "target_bottleneck": "short bottleneck label",
                "revise_conditions": ["when to change the plan"],
                "abort_conditions": ["when to abandon this branch"],
                "steps": [{
                    "action_id": "action_000",
                    "primitive": "one manual primitive_catalog name",
                    "args": {"directive": "only for typed Vivado primitives"},
                    "rationale": "short reason",
                    "expected_observation": "what should improve or change",
                }],
            }
        else:
            task = "Choose exactly one next manual rescue action that is valid for the CURRENT implementation state and consistent with the current plan."
            output_schema = {
                "action_id": "action_000",
                "primitive": "one manual primitive_catalog name",
                "args": {"directive": "only for typed Vivado primitives"},
                "rationale": "short reason",
                "expected_observation": "what should improve or change",
            }
        payload = {
            "task": task,
            "context": context,
            "known_public_manual_prior": _manual_known_public_prior(context),
            "current_plan": plan,
            "action_index": action_index,
            "primitive_catalog": catalog,
            "output_schema": output_schema,
        }
        system = (
            "You are a bounded FPGA manual rescue planner. Return exactly one JSON object. Use only manual.* "
            "primitive_catalog names. Never use flow.*, optimizer names like retime or phys_opt, raw Tcl, shell, file "
            "paths, or checkpoint-writing commands. The runtime executes one manual action, then reports timing, route, "
            "utilization, validation, and diagnosis before asking again. Respect context.manual_design_state as a hard "
            "precondition: on a routed/placed checkpoint, place_design and route_design are normally no-ops. A fresh "
            "global implementation must progress in this order: manual.unroute_all, manual.unplace_all, "
            "manual.place_design, optional manual.phys_opt_design, manual.route_design, optional post-route "
            "manual.phys_opt_design. Do not skip a required reset step."
        )
        return self._complete(f"manual_{kind}", system, payload)

    def _complete(self, kind: str, system: str, payload: Dict[str, Any]) -> GeneratedInterventionProposal:
        if self.provider in {"", "none"}:
            raise RuntimeError("llm_provider_not_configured")
        client = self.client or build_llm_client(
            provider=self.provider,
            model=self.model,
            provider_base_url=getattr(self.config, "provider_base_url", None),
            provider_api_key_env=getattr(self.config, "provider_api_key_env", None),
            pricing_profile=getattr(self.config, "pricing_profile", None),
        )
        response = client.complete_json(
            LLMRequest(
                provider=self.provider,
                model=self.model,
                temperature=0.0,
                response_format="json_object",
                timeout_sec=getattr(self.config, "llm_timeout_sec", None),
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": json.dumps(payload, sort_keys=True)},
                ],
            )
        )
        self.usage.record(response)
        parsed = response.parsed_json if isinstance(response.parsed_json, dict) else _load_json_object(response.content or "{}")
        return GeneratedInterventionProposal(
            dict(parsed),
            {
                "adapter": "ProviderInterventionGenerator",
                "kind": kind,
                "provider": self.provider,
                "model": self.model,
                "llm_response": {
                    "content": response.content,
                    "parsed_json": response.parsed_json,
                    "prompt_tokens": response.prompt_tokens,
                    "completion_tokens": response.completion_tokens,
                    "total_tokens": response.total_tokens,
                    "cost_usd": response.cost_usd,
                    "estimated_cost_usd": response.estimated_cost_usd,
                    "provider_reported_cost_usd": response.provider_reported_cost_usd,
                    "cost_source": response.cost_source,
                    "pricing_profile_id": response.pricing_profile_id,
                    "latency_sec": response.latency_sec,
                },
            },
        )


def _load_json_object(content: str) -> Dict[str, Any]:
    parsed = json.loads((content or "{}").strip())
    if not isinstance(parsed, dict):
        raise ValueError("intervention response is not a JSON object")
    return parsed


def _manual_known_public_prior(context: Dict[str, Any]) -> Dict[str, Any]:
    evidence = context.get("rescue_evidence") if isinstance(context.get("rescue_evidence"), dict) else {}
    prior = evidence.get("known_public_prior") if isinstance(evidence.get("known_public_prior"), dict) else {}
    recipes = prior.get("fixed_recipe") if isinstance(prior.get("fixed_recipe"), list) else []
    if "rescue_place_reset_retime" not in recipes:
        return {}
    return {
        "source": "known_public",
        "reason": "Public fixed rescue recipe maps to this manual-only motif.",
        "steps": [
            "manual.unroute_all", "manual.unplace_all", "manual.place_design(Explore)",
            "manual.phys_opt_design(AddRetime)", "manual.route_design(Explore)",
            "manual.phys_opt_design(Explore)",
        ],
    }
