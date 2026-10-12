"""Run configuration for DCP-state graph expansion."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from optimizer_pipeline.analysis_schema import DEFAULT_RESEARCH_FLOWS
from optimizer_pipeline.models import to_jsonable
from optimizer_pipeline.qor import QoRWeights

_METADATA_ONLY_CONFIG_KEYS = {"note", "description", "$schema"}


@dataclass
class OptimizationConfig:
    max_depth: int = 3
    branching_factor: int = 3
    beam_width: int = 2
    max_total_runs: int = 15
    max_llm_decision_attempts: int = 1
    max_parallel_jobs: int = 4
    batch_size: Optional[int] = None
    scheduler_mode: str = "async"  # async | batch
    allowed_flows: List[str] = field(default_factory=lambda: list(DEFAULT_RESEARCH_FLOWS))
    fallback_flow: str = "phys_opt"
    model: Optional[str] = None
    provider: str = "openai"
    prompt_id: str = "frontier_expansion.baseline"
    prompt_root: Optional[str] = None
    temperature: Optional[float] = 0.0
    max_tokens: Optional[int] = None
    llm_timeout_sec: Optional[float] = None
    llm_response_mode: str = "json_object"
    provider_base_url: Optional[str] = None
    provider_api_key_env: Optional[str] = None
    pricing_profile: Optional[str] = None
    llm_cost_budget_usd: Optional[float] = None
    policy_type: str = "analysis_priority"  # analysis_priority | deterministic | reliability | retrieval
    contest_clean: bool = False
    validation_mode: str = "log_aware"
    max_nets: int = 5
    debug: bool = False
    disallow_consecutive_repeat: bool = True
    prune_invalid: bool = True
    prune_severe_regression: bool = True
    allow_warning_expansion: bool = True
    llm_context_mode: str = "balanced"  # compact | balanced | full | reliability | retrieval
    llm_prune_mode: str = "advisory"  # advisory | hard
    reliability_stats_path: Optional[str] = None
    reliability_penalty_invalid: float = 0.35
    reliability_penalty_fallback: float = 0.10
    reliability_penalty_warning: float = 0.05
    reliability_penalty_regression: float = 0.15
    reliability_clean_bonus: float = 0.08
    reliability_expected_qor_gain_weight: float = 0.02
    reliability_runtime_penalty: float = 0.00002
    reliability_min_clean_prob: float = 0.0
    retrieval_index_path: Optional[str] = None
    retrieval_k: int = 5
    retrieval_success_weight: float = 0.12
    retrieval_invalid_penalty: float = 0.25
    retrieval_fallback_penalty: float = 0.08
    retrieval_expected_qor_gain_weight: float = 0.015
    retrieval_motif_bonus_weight: float = 0.20
    frontier_context_limit: int = 8
    recent_context_limit: int = 10
    runtime_penalty_is_light: bool = True
    q_weight_fmax_improvement_ratio: float = 100.0
    q_weight_wns_improvement_ns: float = 20.0
    q_weight_tns_improvement_ratio: float = 5.0
    q_weight_runtime_minutes: float = 0.0
    q_weight_invalid_output: float = -100.0
    q_weight_severe_regression: float = -50.0

    # Online budgeted deep-analysis infra. Disabled by default.
    online_deep_analysis: bool = False
    analysis_level: str = "report_probe"  # none | read_only | report_probe | act_probe
    analysis_trigger_policy: str = "naive_v1"
    analysis_stop_policy: str = "time_pressure_v1"
    analysis_decision_mode: str = "heuristic"  # heuristic | llm (reserved)
    analysis_root_budget_sec: float = 600.0
    analysis_state_budget_sec: float = 120.0
    analysis_max_states_per_expansion: int = 5
    analysis_allow_report_probes: bool = True
    analysis_allow_act_probes: bool = False
    analysis_soft_stop_frac: float = 0.55
    analysis_strong_stop_frac: float = 0.80
    analysis_tool_plan_policy: str = "static_root_timing_route_v1"
    analysis_root_tools: Optional[List[str]] = None
    analysis_keynode_tools: Optional[List[str]] = None
    analysis_report_probe_backend: str = "cached"  # cached | local_vivado | vivado_mcp
    analysis_fresh_probe_contract_mode: str = "auto"  # auto | off | required
    analysis_tool_timeout_sec: float = 300.0
    analysis_tool_loop_mode: str = "static_plan"  # static_plan | mock_llm_closed_loop | openai_llm_closed_loop
    analysis_loop_provider: str = "openai"
    analysis_loop_model: Optional[str] = None
    analysis_llm_cost_budget_usd: Optional[float] = None
    analysis_only: bool = False
    fast_skip_enabled: bool = False
    fast_skip_policy: str = "noop_fast_skip"
    knowledge_base_path: Optional[str] = None
    knowledge_mode: str = "off"  # off | reference_manual; history modes are not contest-clean.

    # Optional no-improvement rescue branch. Disabled by default; when enabled,
    # rescue still executes through the normal transition executor/validator.
    rescue_mode: str = "off"  # off | deterministic | llm | synthesis | synthesis_loop | manual_loop
    rescue_trigger: str = "no_improvement"
    rescue_max_runs: int = 1
    rescue_max_episodes: int = 1
    rescue_min_main_delta_mhz: float = 0.1
    rescue_synthesis_max_steps: int = 8
    rescue_synthesis_fallback: str = "deterministic"  # deterministic | fail
    rescue_loop_policy: str = "fixed_attempts"  # fixed_attempts | stop_after_first_improvement | external
    rescue_parent_strategy: str = "current_best"  # current_best | root | round_robin_safe_parents
    rescue_evidence_mode: str = "compact"  # compact | planner
    rescue_history_scope: str = "in_run"  # in_run | known_public
    rescue_enable_act_probes: bool = False
    rescue_max_act_probes: int = 0
    rescue_act_probe_wall_sec: float = 300.0
    rescue_allow_invalid_scratch: bool = True
    rescue_manual_parent_selector: str = "last_scratch"  # last_scratch | current_best | root
    rescue_manual_min_publish_delta_mhz: float = 0.01
    rescue_manual_allow_nonclean_publish: bool = False

    def __post_init__(self) -> None:
        self.max_depth = max(0, int(self.max_depth))
        self.branching_factor = max(1, int(self.branching_factor))
        self.beam_width = max(1, int(self.beam_width))
        self.max_total_runs = max(0, int(self.max_total_runs))
        self.max_llm_decision_attempts = max(1, int(self.max_llm_decision_attempts))
        self.max_parallel_jobs = max(1, int(self.max_parallel_jobs))
        if self.batch_size is None:
            self.batch_size = max(self.branching_factor, self.max_parallel_jobs)
        self.batch_size = max(1, int(self.batch_size))
        if self.scheduler_mode not in {"async", "batch"}:
            self.scheduler_mode = "async"
        self.frontier_context_limit = max(1, int(self.frontier_context_limit))
        self.recent_context_limit = max(1, int(self.recent_context_limit))
        self.allowed_flows = [str(flow) for flow in self.allowed_flows if flow]
        if self.fallback_flow not in self.allowed_flows and self.allowed_flows:
            self.fallback_flow = self.allowed_flows[0]
        if self.llm_context_mode not in {"compact", "balanced", "full", "reliability", "retrieval"}:
            self.llm_context_mode = "balanced"
        self.contest_clean = bool(self.contest_clean)
        self.retrieval_k = max(1, int(self.retrieval_k))
        if self.llm_prune_mode not in {"advisory", "hard"}:
            self.llm_prune_mode = "advisory"
        self.prompt_id = str(self.prompt_id or "frontier_expansion.baseline")
        if self.prompt_root is not None:
            self.prompt_root = str(self.prompt_root)
        if self.temperature is not None:
            self.temperature = float(self.temperature)
        if self.max_tokens is not None:
            self.max_tokens = max(1, int(self.max_tokens))
        if self.llm_timeout_sec is not None:
            self.llm_timeout_sec = max(0.001, float(self.llm_timeout_sec))
        self.llm_response_mode = str(self.llm_response_mode or "json_object")
        if self.llm_response_mode not in {"json_object", "text"}:
            self.llm_response_mode = "json_object"
        if self.provider_base_url is not None:
            self.provider_base_url = str(self.provider_base_url)
        if self.provider_api_key_env is not None:
            self.provider_api_key_env = str(self.provider_api_key_env)
        if self.pricing_profile is not None:
            self.pricing_profile = str(self.pricing_profile)
        if self.llm_cost_budget_usd is not None:
            self.llm_cost_budget_usd = max(0.0, float(self.llm_cost_budget_usd))
        if self.analysis_level not in {"none", "read_only", "report_probe", "act_probe"}:
            self.analysis_level = "report_probe"
        self.analysis_root_budget_sec = max(1.0, float(self.analysis_root_budget_sec))
        self.analysis_state_budget_sec = max(1.0, float(self.analysis_state_budget_sec))
        self.analysis_max_states_per_expansion = max(0, int(self.analysis_max_states_per_expansion))
        if self.analysis_root_tools is not None:
            self.analysis_root_tools = [str(tool) for tool in self.analysis_root_tools if tool]
        if self.analysis_keynode_tools is not None:
            self.analysis_keynode_tools = [str(tool) for tool in self.analysis_keynode_tools if tool]
        self.analysis_report_probe_backend = str(self.analysis_report_probe_backend or "cached")
        self.analysis_fresh_probe_contract_mode = str(self.analysis_fresh_probe_contract_mode or "auto").strip().lower()
        if self.analysis_fresh_probe_contract_mode not in {"auto", "off", "required"}:
            self.analysis_fresh_probe_contract_mode = "auto"
        self.analysis_tool_timeout_sec = max(0.001, float(self.analysis_tool_timeout_sec))
        self.analysis_tool_loop_mode = str(self.analysis_tool_loop_mode or "static_plan")
        self.analysis_loop_provider = str(self.analysis_loop_provider or "openai")
        if self.analysis_loop_model is not None:
            self.analysis_loop_model = str(self.analysis_loop_model)
        if self.analysis_llm_cost_budget_usd is not None:
            self.analysis_llm_cost_budget_usd = max(0.0, float(self.analysis_llm_cost_budget_usd))
        self.analysis_only = bool(self.analysis_only) or (self.online_deep_analysis and self.max_total_runs == 0)
        self.knowledge_mode = str(self.knowledge_mode or "off").strip().lower()
        if self.knowledge_mode not in {"off", "reference_manual", "benchmark_history", "both"}:
            self.knowledge_mode = "off"
        if self.knowledge_base_path is not None:
            self.knowledge_base_path = str(self.knowledge_base_path)
        self.rescue_mode = str(self.rescue_mode or "off").strip().lower()
        if self.rescue_mode not in {"off", "deterministic", "llm", "synthesis", "synthesis_loop", "manual_loop"}:
            self.rescue_mode = "off"
        self.rescue_trigger = str(self.rescue_trigger or "no_improvement").strip().lower()
        if self.rescue_trigger not in {"no_improvement", "always"}:
            self.rescue_trigger = "no_improvement"
        self.rescue_max_runs = max(0, int(self.rescue_max_runs))
        self.rescue_max_episodes = max(1, int(self.rescue_max_episodes))
        self.rescue_min_main_delta_mhz = max(0.0, float(self.rescue_min_main_delta_mhz))
        self.rescue_synthesis_max_steps = max(1, int(self.rescue_synthesis_max_steps))
        self.rescue_synthesis_fallback = str(self.rescue_synthesis_fallback or "deterministic").strip().lower()
        if self.rescue_synthesis_fallback not in {"deterministic", "fail"}:
            self.rescue_synthesis_fallback = "deterministic"
        self.rescue_loop_policy = str(self.rescue_loop_policy or "fixed_attempts").strip().lower()
        if self.rescue_loop_policy not in {"fixed_attempts", "stop_after_first_improvement", "external"}:
            self.rescue_loop_policy = "fixed_attempts"
        self.rescue_parent_strategy = str(self.rescue_parent_strategy or "current_best").strip().lower()
        if self.rescue_parent_strategy not in {"current_best", "root", "round_robin_safe_parents"}:
            self.rescue_parent_strategy = "current_best"
        self.rescue_evidence_mode = str(self.rescue_evidence_mode or "compact").strip().lower()
        if self.rescue_evidence_mode not in {"compact", "planner"}:
            self.rescue_evidence_mode = "compact"
        self.rescue_history_scope = str(self.rescue_history_scope or "in_run").strip().lower()
        if self.rescue_history_scope not in {"in_run", "known_public"}:
            self.rescue_history_scope = "in_run"
        self.rescue_enable_act_probes = bool(self.rescue_enable_act_probes)
        self.rescue_max_act_probes = max(0, int(self.rescue_max_act_probes))
        self.rescue_act_probe_wall_sec = max(0.001, float(self.rescue_act_probe_wall_sec))
        self.rescue_allow_invalid_scratch = bool(self.rescue_allow_invalid_scratch)
        self.rescue_manual_parent_selector = str(self.rescue_manual_parent_selector or "last_scratch").strip().lower()
        if self.rescue_manual_parent_selector not in {"last_scratch", "current_best", "root"}:
            self.rescue_manual_parent_selector = "last_scratch"
        self.rescue_manual_min_publish_delta_mhz = max(0.0, float(self.rescue_manual_min_publish_delta_mhz))
        self.rescue_manual_allow_nonclean_publish = bool(self.rescue_manual_allow_nonclean_publish)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "OptimizationConfig":
        if not isinstance(data, dict):
            raise ValueError("OptimizationConfig must be a JSON object")
        allowed = set(cls.__dataclass_fields__)  # type: ignore[attr-defined]
        unknown = sorted(set(data) - allowed - _METADATA_ONLY_CONFIG_KEYS)
        if unknown:
            raise ValueError(f"Unknown OptimizationConfig field(s): {', '.join(unknown)}")
        kwargs = {key: value for key, value in data.items() if key in allowed}
        return cls(**kwargs)

    @classmethod
    def from_json_file(cls, path: Optional[Path]) -> "OptimizationConfig":
        if not path:
            return cls()
        with open(path, "r") as handle:
            return cls.from_dict(json.load(handle))

    def to_dict(self) -> Dict[str, Any]:
        return to_jsonable(self)

    def analysis_decision_model(self) -> Optional[str]:
        return self.analysis_loop_model or self.model

    def validate_contest_clean(self) -> None:
        """Reject external priors/history when running a contest-clean search."""
        if not self.contest_clean:
            return
        violations: List[str] = []
        if self.reliability_stats_path:
            violations.append(f"reliability_stats_path={self.reliability_stats_path}")
        if self.retrieval_index_path:
            violations.append(f"retrieval_index_path={self.retrieval_index_path}")
        if self.policy_type == "retrieval":
            violations.append("policy_type=retrieval")
        if self.llm_context_mode == "retrieval":
            violations.append("llm_context_mode=retrieval")
        if self.knowledge_mode in {"benchmark_history", "both"}:
            violations.append(f"knowledge_mode={self.knowledge_mode}")
        if violations:
            raise ValueError(
                "contest_clean forbids external history/retrieval inputs: "
                + ", ".join(violations)
            )

    def qor_weights(self) -> QoRWeights:
        return QoRWeights(
            fmax_improvement_ratio=self.q_weight_fmax_improvement_ratio,
            wns_improvement_ns=self.q_weight_wns_improvement_ns,
            tns_improvement_ratio=self.q_weight_tns_improvement_ratio,
            runtime_minutes=self.q_weight_runtime_minutes,
            invalid_output=self.q_weight_invalid_output,
            severe_regression=self.q_weight_severe_regression,
        )
