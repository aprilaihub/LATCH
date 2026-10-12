# Ported from a722b1e; import paths only adapted.
import asyncio
import json
import time
from pathlib import Path

from optimizer_pipeline import DeterministicAnalyzer, OutputValidator
from experiments.ablation.config import OptimizationConfig
from experiments.ablation.runtime.artifacts import ArtifactStore
from experiments.ablation.runtime.expansion.frontier_runner import FrontierExpansionRunner
from experiments.ablation.runtime.expansion.policies.core import DeterministicExpansionPolicy

from experiments.ablation.runtime.analysis.dynamic_loop import AnalysisLoopState, MockLLMSelfStopPolicy, OpenAIAnalysisDecisionPolicy, ProviderAnalysisDecisionPolicy
from experiments.ablation.runtime.analysis.controller import OnlineAnalysisController
from experiments.ablation.runtime.analysis.report_probe_backends import _parse_report_text
from experiments.ablation.runtime.analysis.schemas import AnalysisBudget, AnalysisLoopDecision, AnalysisStopDecision, AnalysisToolCall
from experiments.ablation.runtime.analysis.tool_plan_policies import StaticAnalysisToolPlanPolicy
from experiments.ablation.runtime.analysis.tools.artifact_tools import build_optimization_state_context
from integrations.llm.runtime.types import LLMResponse
from tests.latch.test_analysis import FakeExecutor, fake_analysis_provider


class ContextNode:
    def __init__(self, node_id, parent_id, depth, sequence, flow, score, fmax, validation_status=None):
        self.node_id = node_id
        self.parent_id = parent_id
        self.depth = depth
        self.flow_sequence = list(sequence)
        self.selected_flow = flow
        self.score = score
        self.metrics = {"pure_qor_score": score, "final_fmax_mhz": fmax}
        self.status = "COMPLETED"
        self.validation_status = validation_status
        self.validation_warnings = []
        self.validation_errors = []
        self.fallback_used = False


class FakeFreshReportBackend:
    name = "fake_fresh_vivado"
    fresh_vivado_report = True
    mutates_design = False

    def report_timing_summary(self, *, args, context):
        return {
            "mode": "fake_fresh_report_probe",
            "available": True,
            "fresh_vivado_report": True,
            "mutates_design": False,
            "probe_backend": self.name,
            "timing": {"wns_ns": -0.1, "fmax_mhz": 390.0},
            "evidence_provenance": {
                "probe_backend": self.name,
                "fresh_vivado_report": True,
                "mutates_design": False,
            },
        }

    def report_route_status(self, *, args, context):
        return {
            "mode": "fake_fresh_report_probe",
            "available": True,
            "fresh_vivado_report": True,
            "mutates_design": False,
            "probe_backend": self.name,
            "route_status": {"is_fully_routed": True},
            "evidence_provenance": {
                "probe_backend": self.name,
                "fresh_vivado_report": True,
                "mutates_design": False,
            },
        }

    def report_utilization(self, *, args, context):
        return {
            "mode": "fake_fresh_report_probe",
            "available": True,
            "fresh_vivado_report": True,
            "mutates_design": False,
            "probe_backend": self.name,
            "utilization_summary": {"lut_utilization_pct": 52.0},
            "evidence_provenance": {
                "probe_backend": self.name,
                "fresh_vivado_report": True,
                "mutates_design": False,
            },
        }


def _call(tool_name, *, success=True, fresh=True, timed_out=False, elapsed=0.01):
    return AnalysisToolCall(
        tool_name=tool_name,
        args={},
        started_at="now",
        elapsed_sec=elapsed,
        success=success,
        output={"available": True, "fresh_vivado_report": fresh},
        tool_class="report_probe" if tool_name.startswith("vivado_report_") else "inspect",
        timed_out=timed_out,
    )


def test_optimization_state_context_includes_history_deltas_and_legal_flows():
    root = ContextNode("node_000_root", None, 0, [], None, 10.0, 300.0)
    child = ContextNode("node_001_phys", "node_000_root", 1, ["phys_opt"], "phys_opt", 12.0, 320.0)
    plateau = ContextNode("node_002_retime", "node_001_phys", 2, ["phys_opt", "retime"], "retime", 12.0, 320.0)
    nodes = {node.node_id: node for node in [root, child, plateau]}
    context = build_optimization_state_context(
        node=plateau,
        nodes=nodes,
        analysis_contexts={
            "node_002_retime": {
                "timing": {"final_wns_ns": -0.1, "final_fmax_mhz": 320.0},
                "runtime": {"duration_seconds": 5},
            }
        },
        allowed_flows=["phys_opt", "retime", "memory_path_opt"],
    )

    steps = context["trajectory_outcome_table"]["steps"]
    assert steps[-1]["action"] == "retime"
    assert steps[-1]["delta_pure_qor_from_parent"] == 0.0
    assert context["trajectory_outcome_table"]["plateau_steps"]
    assert "retime" not in context["run_budget_context"]["legal_next_flows"]
    illegal = {item["flow"]: item["reason"] for item in context["run_budget_context"]["illegal_next_flows"]}
    assert illegal["retime"] == "consecutive_repeat"
    effects = context["observed_action_effects"]
    assert effects["phys_opt"]["best_delta_pure_qor"] == 2.0
    assert effects["retime"]["plateau"] == 1


def test_optimization_state_context_records_invalid_motifs():
    root = ContextNode("node_000_root", None, 0, [], None, 10.0, 300.0)
    spread = ContextNode("node_001_spread", "node_000_root", 1, ["congestion_spread"], "congestion_spread", 14.0, 320.0)
    invalid = ContextNode("node_002_retime", "node_001_spread", 2, ["congestion_spread", "retime"], "retime", -100.0, 0.0, validation_status="INVALID")
    invalid.status = "FAILED"
    invalid.validation_errors = ["executor failed with exit_code=1"]
    nodes = {node.node_id: node for node in [root, spread, invalid]}
    context = build_optimization_state_context(
        node=spread,
        nodes=nodes,
        analysis_contexts={},
        allowed_flows=["congestion_spread", "retime", "phys_opt"],
    )

    motifs = {row["motif"]: row for row in context["invalid_sequence_motifs"]}
    assert "congestion_spread -> retime" in motifs
    assert motifs["congestion_spread -> retime"]["invalid"] == 1
    assert context["invalid_action_effects"]["retime"]["invalid"] == 1


def test_fresh_report_parser_extracts_timing_diagnostics():
    parsed = _parse_report_text(
        "timing_summary",
        """
        WNS(ns)  -0.904
        TNS(ns)  -120.5
        Failing Endpoints 1823
        Data Path Delay: 2.101ns  logic 0.901ns route 1.200ns
        Logic Levels: 7
        """,
    )

    assert parsed["timing"]["wns_ns"] == -0.904
    assert parsed["timing"]["failing_endpoints"] == 1823
    assert parsed["critical_path_features"]["route_delay_ratio"] > 0.55
    # 0.5712 is inside the old 0.55-0.65 disagreement band: the retired probe
    # vocabulary called this "route", the canonical one calls it "mixed".
    assert parsed["critical_path_features"]["dominant_path_type"] == "mixed"


def test_report_parser_reads_real_vivado_design_timing_summary_table():
    """Vivado emits a column table, not ``LABEL value`` pairs.

    The values sit two lines below their own headings, past the dashes row, and
    the whole block lands well beyond the payload preview cut-off.
    """
    report = "\n".join(
        ["filler line"] * 200
        + [
            "    WNS(ns)      TNS(ns)  TNS Failing Endpoints  TNS Total Endpoints      WHS(ns)  ",
            "    -------      -------  ---------------------  -------------------      -------  ",
            "     -1.654     -671.139                   1937                 3550        0.042  ",
            "",
            "  Data Path Delay:        3.148ns  (logic 0.507ns (16.105%)  route 2.641ns (83.895%))",
            "  Logic Levels:           5  (CARRY8=2 LUT2=2 LUT6=1)",
        ]
    )

    parsed = _parse_report_text("timing_summary", report)

    assert parsed["timing"]["wns_ns"] == -1.654
    assert parsed["timing"]["tns_ns"] == -671.139
    assert parsed["timing"]["failing_endpoints"] == 1937
    assert parsed["critical_path_features"]["max_logic_levels"] == 5
    assert parsed["critical_path_features"]["dominant_path_type"] == "routing_dominated"


def test_report_probe_uses_the_canonical_dominant_path_vocabulary():
    """The probe backend must not reintroduce a second route/logic vocabulary.

    A separate classifier here once used route/logic at 0.55/0.45 while
    analysis_schema used routing_dominated/logic_dominated at 0.65/0.35, so the
    same design could be labelled differently depending on which path produced
    the evidence. Both now go through classify_dominant_path_type.
    """
    from experiments.ablation.runtime.analysis_schema import classify_dominant_path_type

    for ratio in (0.20, 0.45, 0.5712, 0.6275, 0.8389):
        report = (
            f"  Data Path Delay: 10.000ns  (logic {10 * (1 - ratio):.3f}ns  "
            f"route {10 * ratio:.3f}ns)\n  Logic Levels:           5\n"
        )
        parsed = _parse_report_text("timing_summary", report)
        assert parsed["critical_path_features"]["dominant_path_type"] == classify_dominant_path_type(ratio)


def test_report_parser_ignores_per_clock_timing_tables():
    """Only the design-level table is read; per-clock rows lead with a label."""
    report = "\n".join(
        [
            "Clock                 WNS(ns)      TNS(ns)  TNS Failing Endpoints  ",
            "-----                 -------      -------  ---------------------  ",
            "clk_fpl26contest       -9.999     -999.999                     11  ",
        ]
    )

    parsed = _parse_report_text("timing_summary", report)

    assert parsed["timing"]["wns_ns"] is None


def test_mock_llm_closed_loop_policy_calls_tools_then_stops():
    policy = MockLLMSelfStopPolicy(required_tools=["load_node_summary", "query_retrieval_memory"])
    state0 = AnalysisLoopState(
        session_id="s0",
        node_id="node_000_root",
        trigger_reason="root_online_analysis",
        elapsed_sec=0.0,
        remaining_budget_sec=10.0,
        allowed_flows=["phys_opt", "auto_pblock"],
        required_tools=["load_node_summary", "query_retrieval_memory"],
    )
    d0 = policy.decide_next_tool(state=state0, available_tool_names=["load_node_summary", "query_retrieval_memory"])
    assert d0.continue_analysis is True
    assert d0.next_tool_name == "load_node_summary"

    state_done = AnalysisLoopState(
        session_id="s0",
        node_id="node_000_root",
        trigger_reason="root_online_analysis",
        elapsed_sec=1.0,
        remaining_budget_sec=9.0,
        allowed_flows=["phys_opt", "auto_pblock"],
        required_tools=["load_node_summary", "query_retrieval_memory"],
        tool_calls=[type("C", (), {"tool_name": "load_node_summary"})(), type("C", (), {"tool_name": "query_retrieval_memory"})()],
        evidence_summary={"timing": {"wns_ns": -0.5}, "route_status": {"is_fully_routed": True}, "retrieval_by_flow": {}},
    )
    d1 = policy.decide_next_tool(state=state_done, available_tool_names=["load_node_summary", "query_retrieval_memory"])
    assert d1.continue_analysis is False
    assert d1.candidate_priors


def test_openai_llm_policy_fails_closed_without_key(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    policy = OpenAIAnalysisDecisionPolicy(api_key=None, model="gpt-5.4-mini")
    state = AnalysisLoopState(session_id="s", node_id="n", trigger_reason="root", elapsed_sec=0, remaining_budget_sec=1)
    decision = policy.decide_next_tool(state=state, available_tool_names=["load_node_summary"])
    assert decision.continue_analysis is False
    assert "not_available" in decision.stop_reason


def test_provider_llm_policy_sends_provider_neutral_request_and_records_cost():
    class FakeProviderClient:
        def __init__(self):
            self.requests = []

        def complete_json(self, request):
            self.requests.append(request)
            return LLMResponse(
                content='{"continue_analysis": false, "stop_reason": "enough", "confidence": 0.8, "candidate_priorities": [{"flow": "phys_opt", "priority_score": 0.9, "confidence": 0.6, "risk_score": 0.2, "reason": "timing"}]}',
                provider=request.provider,
                model=request.model,
                prompt_tokens=100,
                completion_tokens=20,
                total_tokens=120,
                cost_usd=0.000165,
                estimated_cost_usd=0.000165,
                cost_source="estimated_pricing_profile",
                pricing_profile_id="openrouter:openai/gpt-5.4-mini",
            )

    fake = FakeProviderClient()
    policy = ProviderAnalysisDecisionPolicy(
        provider="openrouter",
        model="openai/gpt-5.4-mini",
        client=fake,
        cost_budget_usd=0.80,
    )
    state = AnalysisLoopState(session_id="s", node_id="n", trigger_reason="root", elapsed_sec=0, remaining_budget_sec=1)
    decision = policy.decide_next_tool(state=state, available_tool_names=["load_node_summary"])

    assert decision.continue_analysis is False
    assert decision.candidate_priorities
    assert fake.requests[0].provider == "openrouter"
    assert fake.requests[0].model == "openai/gpt-5.4-mini"
    usage = policy.usage_summary()
    assert usage["total_llm_calls"] == 1
    assert usage["total_prompt_tokens"] == 100
    assert usage["total_completion_tokens"] == 20
    assert usage["total_cost_usd"] == 0.000165
    assert usage["cost_source"] == "estimated_pricing_profile"


def test_provider_llm_policy_injects_reference_manual_without_history():
    class FakeProviderClient:
        def __init__(self):
            self.requests = []

        def complete_json(self, request):
            self.requests.append(request)
            return LLMResponse(
                content='{"continue_analysis": false, "stop_reason": "enough", "confidence": 0.8}',
                provider=request.provider,
                model=request.model,
                prompt_tokens=10,
                completion_tokens=4,
                total_tokens=14,
            )

    knowledge_context = {
        "source_schema_version": "knowledgebase.test",
        "flow_cards": {
            "phys_opt": {
                "description": "General physical optimization",
                "use_when": ["timing needs broad physical cleanup"],
            }
        },
        "benchmark_history": [{"flow_applied": "phys_opt", "fmax_delta_mhz": 999.0}],
    }
    fake = FakeProviderClient()
    policy = ProviderAnalysisDecisionPolicy(
        provider="openai",
        model="gpt-5.4-mini",
        client=fake,
        knowledge_context=knowledge_context,
    )
    state = AnalysisLoopState(session_id="s", node_id="n", trigger_reason="root", elapsed_sec=0, remaining_budget_sec=1)
    policy.decide_next_tool(state=state, available_tool_names=["load_node_summary"])

    payload = json.loads(fake.requests[0].messages[1]["content"])
    assert "knowledge_context" in payload
    assert payload["knowledge_context"]["flow_cards"]["phys_opt"]["description"] == "General physical optimization"
    assert "benchmark_history" not in payload["knowledge_context"]
    assert "static optimizer reference manual" in fake.requests[0].messages[0]["content"]


def test_provider_llm_policy_omits_knowledge_context_when_disabled():
    class FakeProviderClient:
        def __init__(self):
            self.requests = []

        def complete_json(self, request):
            self.requests.append(request)
            return LLMResponse(
                content='{"continue_analysis": false, "stop_reason": "enough", "confidence": 0.8}',
                provider=request.provider,
                model=request.model,
            )

    fake = FakeProviderClient()
    policy = ProviderAnalysisDecisionPolicy(provider="openai", model="gpt-5.4-mini", client=fake)
    state = AnalysisLoopState(session_id="s", node_id="n", trigger_reason="root", elapsed_sec=0, remaining_budget_sec=1)
    policy.decide_next_tool(state=state, available_tool_names=["load_node_summary"])

    payload = json.loads(fake.requests[0].messages[1]["content"])
    assert "knowledge_context" not in payload


def test_provider_llm_policy_stops_fail_soft_when_cost_budget_reached():
    class FakeProviderClient:
        def __init__(self):
            self.calls = 0

        def complete_json(self, request):
            self.calls += 1
            return LLMResponse(
                content='{"continue_analysis": false, "stop_reason": "first", "confidence": 0.8}',
                provider=request.provider,
                model=request.model,
                prompt_tokens=10,
                completion_tokens=10,
                total_tokens=20,
                cost_usd=0.01,
                estimated_cost_usd=0.01,
                cost_source="estimated_pricing_profile",
            )

    fake = FakeProviderClient()
    policy = ProviderAnalysisDecisionPolicy(
        provider="openrouter",
        model="openai/gpt-5.4-mini",
        client=fake,
        cost_budget_usd=0.01,
    )
    state = AnalysisLoopState(session_id="s", node_id="n", trigger_reason="root", elapsed_sec=0, remaining_budget_sec=1)
    first = policy.decide_next_tool(state=state, available_tool_names=["load_node_summary"])
    second = policy.decide_next_tool(state=state, available_tool_names=["load_node_summary"])

    assert first.stop_reason == "first"
    assert second.continue_analysis is False
    assert second.stop_reason == "analysis_cost_budget_reached"
    assert fake.calls == 1


def test_openai_llm_policy_json_mode_prompt_contains_json_word():
    captured = {}

    class FakeCompletions:
        def create(self, **kwargs):
            captured.update(kwargs)

            class Message:
                content = '{"continue_analysis": false, "stop_reason": "enough", "confidence": 0.7}'

            class Choice:
                message = Message()

            class Response:
                choices = [Choice()]
                usage = None

            return Response()

    class FakeChat:
        completions = FakeCompletions()

    class FakeClient:
        chat = FakeChat()

    policy = OpenAIAnalysisDecisionPolicy(api_key="test-key", model="gpt-5.4-mini", client=FakeClient())
    state = AnalysisLoopState(session_id="s", node_id="n", trigger_reason="root", elapsed_sec=0, remaining_budget_sec=1)
    decision = policy.decide_next_tool(state=state, available_tool_names=["load_node_summary"])
    assert decision.continue_analysis is False
    assert captured["response_format"] == {"type": "json_object"}
    prompt = " ".join(message["content"] for message in captured["messages"]).lower()
    assert "json" in prompt
    assert "do not stop" in prompt
    assert "evidence_basis" in prompt
    assert "empirical_observations" in prompt
    assert "heuristic_prior" in prompt
    assert "evidence_refs must be compact objects" in prompt
    assert "fresh_probe_contract" in prompt
    assert "cached report probes never satisfy" in prompt


def test_openai_llm_policy_soft_budget_filters_report_tools_for_non_root():
    captured = {}

    class FakeCompletions:
        def create(self, **kwargs):
            captured.update(kwargs)

            class Message:
                content = '{"continue_analysis": false, "stop_reason": "budgeted", "confidence": 0.7}'

            class Choice:
                message = Message()

            class Response:
                choices = [Choice()]
                usage = None

            return Response()

    class FakeChat:
        completions = FakeCompletions()

    class FakeClient:
        chat = FakeChat()

    policy = OpenAIAnalysisDecisionPolicy(api_key="test-key", model="gpt-5.4-mini", client=FakeClient())
    policy.total_tokens = policy.soft_token_budget
    state = AnalysisLoopState(
        session_id="s",
        node_id="node_001_phys",
        trigger_reason="node_online_analysis",
        elapsed_sec=0,
        remaining_budget_sec=1,
        optimization_state_context={"current_node_summary": {"depth": 1}},
    )
    decision = policy.decide_next_tool(
        state=state,
        available_tool_names=["load_node_summary", "vivado_report_timing_summary"],
    )

    payload = json.loads(captured["messages"][1]["content"])
    assert payload["available_tool_names"] == ["load_node_summary"]
    assert decision.payload_chars is not None
    assert decision.estimated_prompt_tokens is not None
    assert decision.analysis_token_budget_soft == 420000


def test_openai_llm_policy_keeps_required_fresh_probe_visible_under_soft_token_budget():
    captured = {}

    class FakeCompletions:
        def create(self, **kwargs):
            captured.update(kwargs)

            class Message:
                content = '{"continue_analysis": true, "next_tool_name": "vivado_report_timing_summary", "confidence": 0.7}'

            class Choice:
                message = Message()

            class Response:
                choices = [Choice()]
                usage = None

            return Response()

    class FakeChat:
        completions = FakeCompletions()

    class FakeClient:
        chat = FakeChat()

    policy = OpenAIAnalysisDecisionPolicy(api_key="test-key", model="gpt-5.4-mini", client=FakeClient())
    policy.total_tokens = policy.soft_token_budget
    state = AnalysisLoopState(
        session_id="s",
        node_id="node_001_phys",
        trigger_reason="node_online_analysis",
        elapsed_sec=0,
        remaining_budget_sec=1,
        optimization_state_context={"current_node_summary": {"depth": 1}},
        fresh_probe_contract={
            "enabled": True,
            "satisfied": False,
            "stop_allowed": False,
            "next_required_tool": "vivado_report_timing_summary",
        },
    )
    policy.decide_next_tool(
        state=state,
        available_tool_names=["load_node_summary", "vivado_report_timing_summary"],
    )

    payload = json.loads(captured["messages"][1]["content"])
    assert payload["state"]["fresh_probe_contract"]["next_required_tool"] == "vivado_report_timing_summary"
    assert "vivado_report_timing_summary" in payload["available_tool_names"]


def test_openai_llm_policy_accepts_first_json_object_with_trailing_duplicate():
    class FakeCompletions:
        def create(self, **kwargs):
            class Message:
                content = (
                    '{"continue_analysis": true, "next_tool_name": "load_node_summary", "confidence": 0.8}'
                    '\n{"continue_analysis": true, "next_tool_name": "load_node_summary", "confidence": 0.8}'
                )

            class Choice:
                message = Message()

            class Response:
                choices = [Choice()]
                usage = None

            return Response()

    class FakeChat:
        completions = FakeCompletions()

    class FakeClient:
        chat = FakeChat()

    policy = OpenAIAnalysisDecisionPolicy(api_key="test-key", model="gpt-5.4-mini", client=FakeClient())
    state = AnalysisLoopState(session_id="s", node_id="n", trigger_reason="root", elapsed_sec=0, remaining_budget_sec=1)
    decision = policy.decide_next_tool(state=state, available_tool_names=["load_node_summary"])
    assert decision.continue_analysis is True
    assert decision.next_tool_name == "load_node_summary"
    assert decision.raw_payload["_parse_warning"] == "ignored_extra_content_after_first_json_object"


def test_openai_llm_policy_treats_tool_name_payload_as_call_tool():
    class FakeCompletions:
        def create(self, **kwargs):
            class Message:
                content = '{"tool_name": "load_node_summary", "tool_args": {"node_id": "node_000_root"}}'

            class Choice:
                message = Message()

            class Response:
                choices = [Choice()]
                usage = None

            return Response()

    class FakeChat:
        completions = FakeCompletions()

    class FakeClient:
        chat = FakeChat()

    policy = OpenAIAnalysisDecisionPolicy(api_key="test-key", model="gpt-5.4-mini", client=FakeClient())
    state = AnalysisLoopState(session_id="s", node_id="node_000_root", trigger_reason="root", elapsed_sec=0, remaining_budget_sec=1)
    decision = policy.decide_next_tool(state=state, available_tool_names=["load_node_summary"])
    assert decision.continue_analysis is True
    assert decision.action == "call_tool"
    assert decision.next_tool_name == "load_node_summary"
    assert decision.next_tool_args == {"node_id": "node_000_root"}


def test_closed_loop_controller_passes_tool_budget_to_decision_policy(tmp_path: Path):
    captured = {}

    class CapturingPolicy:
        name = "capturing_closed_loop"

        def decide_next_tool(self, *, state, available_tool_names):
            captured["max_tool_calls"] = state.max_tool_calls
            return AnalysisLoopDecision(False, stop_reason="captured", confidence=1.0)

    class Node:
        node_id = "node_000_root"
        depth = 0

    controller = OnlineAnalysisController(
        run_dir=tmp_path,
        allowed_flows=["phys_opt"],
        decision_policy=CapturingPolicy(),
        enabled=True,
    )
    budget = AnalysisBudget(max_wall_time_sec=30.0, max_tool_calls=4)
    controller._run_closed_loop_plan(
        session_id="analysis_001_node_000_root",
        tools=[],
        node=Node(),
        trigger_reason="root_online_analysis",
        budget=budget,
        analysis_contexts={},
        tool_context=None,
        start_time=time.time(),
    )
    assert captured["max_tool_calls"] == 4


def test_fresh_probe_contract_builder_root_and_nonroot_requirements(tmp_path: Path):
    controller = OnlineAnalysisController(
        run_dir=tmp_path,
        allowed_flows=["phys_opt"],
        report_probe_backend=FakeFreshReportBackend(),
        fresh_probe_contract_mode="required",
        decision_policy=MockLLMSelfStopPolicy(),
        enabled=True,
    )
    budget = AnalysisBudget(max_wall_time_sec=30.0, max_tool_calls=4, max_report_probe_calls=3)
    root = type("Node", (), {"node_id": "node_000_root", "depth": 0, "validation_status": ""})()
    child = type("Node", (), {"node_id": "node_001_phys", "depth": 1, "validation_status": ""})()
    tools = {
        "vivado_report_timing_summary": object(),
        "vivado_report_route_status": object(),
        "vivado_report_utilization": object(),
    }

    root_contract = controller._fresh_probe_contract(
        node=root,
        trigger_reason="root_online_analysis",
        budget=budget,
        tool_calls=[],
        allowed_by_name=tools,
    )
    child_contract = controller._fresh_probe_contract(
        node=child,
        trigger_reason="early_depth1_key_node",
        budget=budget,
        tool_calls=[_call("vivado_report_timing_summary")],
        allowed_by_name=tools,
    )

    assert root_contract["required_tools"] == ["vivado_report_timing_summary", "vivado_report_route_status"]
    assert root_contract["satisfied"] is False
    assert root_contract["next_required_tool"] == "vivado_report_timing_summary"
    assert child_contract["required_tools"] == ["vivado_report_timing_summary"]
    assert child_contract["satisfied"] is True
    assert child_contract["stop_allowed"] is True


def test_fresh_probe_contract_cached_backend_auto_is_disabled(tmp_path: Path):
    controller = OnlineAnalysisController(
        run_dir=tmp_path,
        allowed_flows=["phys_opt"],
        report_probe_backend="cached",
        fresh_probe_contract_mode="auto",
        decision_policy=MockLLMSelfStopPolicy(),
        enabled=True,
    )
    node = type("Node", (), {"node_id": "node_001_phys", "depth": 1, "validation_status": ""})()
    contract = controller._fresh_probe_contract(
        node=node,
        trigger_reason="early_depth1_key_node",
        budget=AnalysisBudget(max_wall_time_sec=30.0, max_tool_calls=4),
        tool_calls=[],
        allowed_by_name={"vivado_report_timing_summary": object()},
    )
    assert contract["enabled"] is False
    assert contract["backend_fresh_vivado_report"] is False


def test_fresh_probe_contract_validation_node_requires_route_and_failed_probe_allows_incomplete_stop(tmp_path: Path):
    controller = OnlineAnalysisController(
        run_dir=tmp_path,
        allowed_flows=["phys_opt"],
        report_probe_backend=FakeFreshReportBackend(),
        fresh_probe_contract_mode="required",
        decision_policy=MockLLMSelfStopPolicy(),
        enabled=True,
    )
    node = type("Node", (), {"node_id": "node_002_bad", "depth": 2, "validation_status": "INVALID"})()
    contract = controller._fresh_probe_contract(
        node=node,
        trigger_reason="validation_boundary_node",
        budget=AnalysisBudget(max_wall_time_sec=30.0, max_tool_calls=4),
        tool_calls=[
            _call("vivado_report_timing_summary"),
            _call("vivado_report_route_status", success=False),
        ],
        allowed_by_name={
            "vivado_report_timing_summary": object(),
            "vivado_report_route_status": object(),
        },
    )

    assert contract["required_tools"] == ["vivado_report_timing_summary", "vivado_report_route_status"]
    assert contract["satisfied"] is False
    assert contract["incomplete"] is True
    assert contract["stop_allowed"] is True
    assert contract["incomplete_reason"] == "required_fresh_probe_failed_or_timed_out"


def test_fresh_probe_contract_appends_required_tools_missing_from_plan(tmp_path: Path):
    controller = OnlineAnalysisController(
        run_dir=tmp_path,
        allowed_flows=["phys_opt"],
        report_probe_backend=FakeFreshReportBackend(),
        fresh_probe_contract_mode="required",
        decision_policy=MockLLMSelfStopPolicy(),
        tool_plan_policy=StaticAnalysisToolPlanPolicy(
            keynode_tools=["vivado_report_route_status"],
            failure_tools=["vivado_report_route_status"],
        ),
        enabled=True,
    )
    node = type("Node", (), {"node_id": "node_002_bad", "depth": 2, "validation_status": "INVALID"})()
    tools = controller._tool_plan_policy_tools(
        node=node,
        trigger_reason="validation_boundary_node",
        budget=AnalysisBudget(max_wall_time_sec=30.0, max_tool_calls=4, allow_report_probes=True),
    )
    names = [tool.name for tool in tools]

    assert "vivado_report_route_status" in names
    assert "vivado_report_timing_summary" in names


def test_required_fresh_probe_contract_rejects_cached_backend(tmp_path: Path):
    try:
        OnlineAnalysisController(
            run_dir=tmp_path,
            allowed_flows=["phys_opt"],
            report_probe_backend="cached",
            fresh_probe_contract_mode="required",
            decision_policy=MockLLMSelfStopPolicy(),
            enabled=True,
        )
    except ValueError as exc:
        assert "fresh report-probe backend" in str(exc)
    else:
        raise AssertionError("required fresh-probe contract accepted cached backend")


def test_closed_loop_controller_preserves_priors_emitted_before_budget_stop(tmp_path: Path):
    class PriorThenBudgetPolicy:
        name = "openai_llm_closed_loop"
        model = "gpt-test-analysis"
        total_llm_calls = 0
        total_tokens = 0

        def decide_next_tool(self, *, state, available_tool_names):
            self.total_llm_calls += 1
            self.total_tokens += 10
            return AnalysisLoopDecision(
                True,
                next_tool_name="load_node_summary",
                confidence=0.6,
                action="call_tool",
                candidate_priors=[
                    {
                        "flow": "auto_pblock",
                        "priority_delta": 0.3,
                        "risk_delta": 0.1,
                        "confidence": 0.7,
                        "reason": "intermediate LLM prior",
                    }
                ],
            )

    node = type("Node", (), {"node_id": "node_000_root", "depth": 0, "flow_sequence": [], "metrics": {}, "score": 0.0})()
    controller = OnlineAnalysisController(
        run_dir=tmp_path,
        allowed_flows=["phys_opt", "auto_pblock"],
        decision_policy=PriorThenBudgetPolicy(),
        enabled=True,
    )
    session = asyncio.run(controller.run_session(
        node=node,
        trigger_reason="root_online_analysis",
        budget=AnalysisBudget(max_wall_time_sec=30.0, max_tool_calls=1),
        nodes={"node_000_root": node},
        analysis_contexts={"node_000_root": {}},
    ))

    assert session.stop_decision.stop_reason == "tool_call_budget_reached"
    assert [p.flow for p in session.candidate_priorities] == ["auto_pblock"]
    assert session.candidate_priorities[0].priority_score > 0.7


def test_closed_loop_controller_accepts_direct_candidate_priorities(tmp_path: Path):
    class DirectPriorityPolicy:
        name = "openai_llm_closed_loop"
        model = "gpt-test-analysis"
        total_llm_calls = 0
        total_tokens = 0

        def decide_next_tool(self, *, state, available_tool_names):
            self.total_llm_calls += 1
            return AnalysisLoopDecision(
                False,
                stop_reason="direct_priorities",
                confidence=0.8,
                action="stop",
                candidate_priorities=[
                    {
                        "flow": "phys_opt",
                        "priority_score": 0.91,
                        "risk_score": 0.2,
                        "confidence": 0.75,
                        "expected_effect": "Improve routing-dominated timing.",
                        "reason": "direct absolute transition priority",
                        "evidence_refs": ["vivado_report_timing_summary", "{'basis': 'trajectory_observation', 'ref': 'node_001_phys.delta_fmax'}"],
                        "evidence_basis": "trajectory_observation",
                        "expected_gain_class": "medium",
                        "novelty_score": 0.25,
                        "contraindications": ["avoid if route status is unrouted"],
                        "why_now": "exploits a current-run trajectory improvement",
                        "do_not_repeat_if": "next transition plateaus or regresses",
                    }
                ],
                empirical_observations=["phys_opt improved current trajectory"],
                heuristic_priors=["phys_opt can help post-placement timing"],
                remaining_uncertainty=["critical_path_features missing"],
            )

    node = type("Node", (), {"node_id": "node_000_root", "depth": 0, "flow_sequence": [], "metrics": {}, "score": 0.0})()
    controller = OnlineAnalysisController(
        run_dir=tmp_path,
        allowed_flows=["phys_opt", "auto_pblock"],
        decision_policy=DirectPriorityPolicy(),
        enabled=True,
    )
    session = asyncio.run(controller.run_session(
        node=node,
        trigger_reason="root_online_analysis",
        budget=AnalysisBudget(max_wall_time_sec=30.0, max_tool_calls=1),
        nodes={"node_000_root": node},
        analysis_contexts={"node_000_root": {}},
    ))

    assert [p.flow for p in session.candidate_priorities] == ["phys_opt"]
    assert session.candidate_priorities[0].priority_score == 0.91
    assert session.candidate_priorities[0].evidence_basis == "trajectory_observation"
    assert session.candidate_priorities[0].expected_gain_class == "medium"
    assert session.candidate_priorities[0].novelty_score == 0.25
    assert session.candidate_priorities[0].contraindications == ["avoid if route status is unrouted"]
    assert session.candidate_priorities[0].why_now.startswith("exploits")
    assert session.candidate_priorities[0].remaining_uncertainty
    refs = session.candidate_priorities[0].evidence_refs
    assert all(isinstance(ref, dict) for ref in refs)
    assert any(ref["basis"] == "missing_evidence" and ref["ref"] == "missing.congestion" for ref in refs)
    assert any(ref["ref"] == "node_001_phys.delta_fmax" for ref in refs)
    assert session.candidate_priorities[0].source_analysis_session_id == session.session_id
    assert session.analysis_plan.candidate_priorities[0].flow == "phys_opt"


def test_controller_forces_required_fresh_probe_before_accepting_llm_stop(tmp_path: Path):
    class StopImmediatelyPolicy:
        name = "openai_llm_closed_loop"
        model = "gpt-test-analysis"
        total_llm_calls = 0
        total_tokens = 0

        def decide_next_tool(self, *, state, available_tool_names):
            self.total_llm_calls += 1
            return AnalysisLoopDecision(
                False,
                stop_reason="llm_wants_to_stop",
                confidence=0.8,
                action="stop",
                candidate_priorities=[
                    {
                        "flow": "phys_opt",
                        "priority_score": 0.9,
                        "risk_score": 0.1,
                        "confidence": 0.8,
                        "reason": "direct priority after fresh timing",
                    }
                ],
            )

    node = type("Node", (), {"node_id": "node_001_phys", "depth": 1, "flow_sequence": ["phys_opt"], "metrics": {}, "score": 0.0, "validation_status": ""})()
    controller = OnlineAnalysisController(
        run_dir=tmp_path,
        allowed_flows=["phys_opt"],
        report_probe_backend=FakeFreshReportBackend(),
        fresh_probe_contract_mode="required",
        decision_policy=StopImmediatelyPolicy(),
        enabled=True,
    )
    session = asyncio.run(controller.run_session(
        node=node,
        trigger_reason="early_depth1_key_node",
        budget=AnalysisBudget(max_wall_time_sec=30.0, max_tool_calls=4, max_report_probe_calls=2),
        nodes={"node_001_phys": node},
        analysis_contexts={"node_001_phys": {}},
    ))

    assert [call.tool_name for call in session.tool_calls] == ["vivado_report_timing_summary"]
    assert session.loop_decisions[0].fresh_probe_override is True
    assert session.loop_decisions[-1].fresh_probe_contract["satisfied"] is True
    assert session.candidate_priorities[0].confidence == 0.8
    controller.memory.apply_session(session)
    summary = controller.summary()
    assert summary["fresh_probe_sessions_requiring"] == 1
    assert summary["fresh_probe_sessions_satisfied"] == 1
    assert summary["fresh_probe_forced_overrides"] == 1


def test_controller_caps_priority_confidence_when_fresh_probe_incomplete(tmp_path: Path):
    class StopWithPriorityPolicy:
        name = "openai_llm_closed_loop"
        model = "gpt-test-analysis"
        total_llm_calls = 0
        total_tokens = 0

        def decide_next_tool(self, *, state, available_tool_names):
            self.total_llm_calls += 1
            return AnalysisLoopDecision(
                False,
                stop_reason="budget_limited_priority",
                confidence=0.8,
                action="stop",
                candidate_priorities=[
                    {
                        "flow": "phys_opt",
                        "priority_score": 0.9,
                        "risk_score": 0.1,
                        "confidence": 0.9,
                        "reason": "priority with incomplete fresh evidence",
                    }
                ],
            )

    node = type("Node", (), {"node_id": "node_000_root", "depth": 0, "flow_sequence": [], "metrics": {}, "score": 0.0, "validation_status": ""})()
    controller = OnlineAnalysisController(
        run_dir=tmp_path,
        allowed_flows=["phys_opt"],
        report_probe_backend=FakeFreshReportBackend(),
        fresh_probe_contract_mode="required",
        decision_policy=StopWithPriorityPolicy(),
        enabled=True,
    )
    session = asyncio.run(controller.run_session(
        node=node,
        trigger_reason="root_online_analysis",
        budget=AnalysisBudget(max_wall_time_sec=30.0, max_tool_calls=4, max_report_probe_calls=1),
        nodes={"node_000_root": node},
        analysis_contexts={"node_000_root": {}},
    ))

    assert [call.tool_name for call in session.tool_calls] == ["vivado_report_timing_summary"]
    assert session.loop_decisions[-1].fresh_probe_contract["incomplete"] is True
    assert session.candidate_priorities[0].confidence == 0.45
    assert "fresh_probe_incomplete" in session.candidate_priorities[0].remaining_uncertainty


def test_controller_allows_one_final_llm_decision_after_fresh_quorum_under_time_pressure(tmp_path: Path):
    class FinalDecisionPolicy:
        name = "openai_llm_closed_loop"
        model = "gpt-test-analysis"
        total_llm_calls = 0
        total_tokens = 0

        def decide_next_tool(self, *, state, available_tool_names):
            self.total_llm_calls += 1
            if not state.fresh_probe_contract.get("satisfied"):
                return AnalysisLoopDecision(True, action="call_tool", next_tool_name="vivado_report_timing_summary")
            return AnalysisLoopDecision(
                False,
                action="stop",
                stop_reason="fresh_quorum_final_priority",
                candidate_priorities=[
                    {
                        "flow": "phys_opt",
                        "priority_score": 0.8,
                        "risk_score": 0.1,
                        "confidence": 0.7,
                        "reason": "final decision after fresh timing",
                    }
                ],
            )

    class StopAfterOneTool:
        def should_continue(self, *, elapsed_sec, budget, tool_calls, next_tool):
            if tool_calls:
                return AnalysisStopDecision(False, "strong_time_pressure_stop_unless_top3_would_change", confidence=0.85)
            return AnalysisStopDecision(True, "continue", confidence=0.5)

    node = type("Node", (), {"node_id": "node_001_phys", "depth": 1, "flow_sequence": ["phys_opt"], "metrics": {}, "score": 0.0, "validation_status": ""})()
    controller = OnlineAnalysisController(
        run_dir=tmp_path,
        allowed_flows=["phys_opt"],
        report_probe_backend=FakeFreshReportBackend(),
        fresh_probe_contract_mode="required",
        decision_policy=FinalDecisionPolicy(),
        stop_policy=StopAfterOneTool(),
        enabled=True,
    )
    session = asyncio.run(controller.run_session(
        node=node,
        trigger_reason="early_depth1_key_node",
        budget=AnalysisBudget(max_wall_time_sec=30.0, max_tool_calls=4, max_report_probe_calls=2),
        nodes={"node_001_phys": node},
        analysis_contexts={"node_001_phys": {}},
    ))

    assert [call.tool_name for call in session.tool_calls] == ["vivado_report_timing_summary"]
    assert len(session.loop_decisions) == 2
    assert session.loop_decisions[-1].fresh_probe_contract["satisfied"] is True
    assert session.stop_decision.stop_reason == "fresh_quorum_final_priority"
    assert session.candidate_priorities[0].flow == "phys_opt"


def test_controller_blocks_extra_optional_fresh_probe_after_quorum_limit(tmp_path: Path):
    class OptionalProbePolicy:
        name = "openai_llm_closed_loop"
        model = "gpt-test-analysis"
        total_llm_calls = 0
        total_tokens = 0

        def decide_next_tool(self, *, state, available_tool_names):
            self.total_llm_calls += 1
            if "vivado_report_timing_summary" not in state.evidence_summary.get("called_tools", []):
                return AnalysisLoopDecision(True, action="call_tool", next_tool_name="vivado_report_timing_summary")
            return AnalysisLoopDecision(True, action="call_tool", next_tool_name="vivado_report_utilization")

    node = type("Node", (), {"node_id": "node_001_phys", "depth": 1, "flow_sequence": ["phys_opt"], "metrics": {}, "score": 0.0, "validation_status": ""})()
    controller = OnlineAnalysisController(
        run_dir=tmp_path,
        allowed_flows=["phys_opt"],
        report_probe_backend=FakeFreshReportBackend(),
        fresh_probe_contract_mode="required",
        decision_policy=OptionalProbePolicy(),
        tool_plan_policy=StaticAnalysisToolPlanPolicy(
            keynode_tools=["vivado_report_timing_summary", "vivado_report_utilization"],
        ),
        enabled=True,
    )
    session = asyncio.run(controller.run_session(
        node=node,
        trigger_reason="early_depth1_key_node",
        budget=AnalysisBudget(max_wall_time_sec=30.0, max_tool_calls=5, max_report_probe_calls=5),
        nodes={"node_001_phys": node},
        analysis_contexts={"node_001_phys": {}},
    ))

    assert [call.tool_name for call in session.tool_calls] == ["vivado_report_timing_summary", "vivado_report_utilization"]
    assert session.stop_decision.stop_reason == "fresh_probe_optional_probe_limit_reached"


def test_controller_enforces_session_llm_call_budget(tmp_path: Path):
    class KeepCallingPolicy:
        name = "openai_llm_closed_loop"
        model = "gpt-test-analysis"
        total_llm_calls = 0
        total_tokens = 0

        def decide_next_tool(self, *, state, available_tool_names):
            self.total_llm_calls += 1
            return AnalysisLoopDecision(True, action="call_tool", next_tool_name="load_node_summary")

    node = type("Node", (), {"node_id": "node_001_phys", "depth": 1, "flow_sequence": ["phys_opt"], "metrics": {}, "score": 0.0, "validation_status": ""})()
    controller = OnlineAnalysisController(
        run_dir=tmp_path,
        allowed_flows=["phys_opt"],
        decision_policy=KeepCallingPolicy(),
        enabled=True,
    )
    session = asyncio.run(controller.run_session(
        node=node,
        trigger_reason="early_depth1_key_node",
        budget=AnalysisBudget(max_wall_time_sec=30.0, max_tool_calls=5, max_llm_calls=1),
        nodes={"node_001_phys": node},
        analysis_contexts={"node_001_phys": {}},
    ))

    assert session.stop_decision.stop_reason == "llm_call_budget_reached"
    assert len(session.loop_decisions) == 1


def test_analysis_controller_records_openai_like_usage(tmp_path: Path):
    class UsagePolicy:
        name = "openai_llm_closed_loop"
        model = "gpt-test-analysis"
        total_llm_calls = 0
        total_tokens = 0

        def decide_next_tool(self, *, state, available_tool_names):
            self.total_llm_calls += 5
            self.total_tokens += 123
            return AnalysisLoopDecision(False, stop_reason="captured_usage", confidence=1.0)

    node = type("Node", (), {"node_id": "node_000_root", "depth": 0})()
    controller = OnlineAnalysisController(
        run_dir=tmp_path,
        allowed_flows=["phys_opt"],
        decision_policy=UsagePolicy(),
        enabled=True,
    )
    session = asyncio.run(controller._run_session(
        node=node,
        trigger_reason="root_online_analysis",
        budget=AnalysisBudget(max_wall_time_sec=30.0, max_tool_calls=4),
        nodes={"node_000_root": node},
        analysis_contexts={},
    ))
    controller.memory.apply_session(session)
    summary = controller.summary()

    assert session.llm_calls == 5
    assert session.tokens == 123
    assert summary["llm_calls"] == 5
    assert summary["tokens"] == 123
    assert summary["model"] == "gpt-test-analysis"
    assert summary["provider"] == "openai"


def test_runner_usage_summary_combines_policy_and_analysis_llm_counts(tmp_path: Path):
    class FrontierUsagePolicy(DeterministicExpansionPolicy):
        def usage_summary(self):
            return {
                "provider": "openai",
                "model": "gpt-tree",
                "total_llm_calls": 2,
                "total_tokens": 20,
                "total_cost_usd": 0.02,
            }

    runner = FrontierExpansionRunner(
        config=OptimizationConfig(policy_type="deterministic"),
        artifact_store=ArtifactStore(tmp_path / "run"),
        analyzer=DeterministicAnalyzer(analysis_provider=fake_analysis_provider),
        executor=FakeExecutor(),
        output_validator=OutputValidator(),
        policy=FrontierUsagePolicy(policy_name="frontier_usage"),
    )
    runner.analysis_controller = type("AnalysisUsage", (), {
        "summary": lambda self: {
            "llm_calls": 5,
            "tokens": 50,
            "model": "gpt-analysis",
            "provider": "openai",
        }
    })()

    usage = runner._usage_summary()

    assert usage["total_llm_calls"] == 2
    assert usage["analysis_llm_calls"] == 5
    assert usage["combined_llm_calls"] == 7
    assert usage["combined_tokens"] == 70
    assert usage["analysis_model"] == "gpt-analysis"
    assert usage["analysis_provider"] == "openai"


def test_analysis_only_mock_llm_run_writes_loop_decisions(tmp_path: Path):
    input_dcp = tmp_path / "input.dcp"
    input_dcp.write_text("fake\n")
    config = OptimizationConfig(
        policy_type="deterministic",
        max_depth=1,
        max_total_runs=0,
        branching_factor=2,
        beam_width=1,
        allowed_flows=["auto_pblock", "phys_opt", "retime"],
        fallback_flow="phys_opt",
        online_deep_analysis=True,
        analysis_only=True,
        analysis_level="report_probe",
        analysis_tool_loop_mode="mock_llm_closed_loop",
        analysis_root_budget_sec=600,
    )
    runner = FrontierExpansionRunner(
        config=config,
        artifact_store=ArtifactStore(tmp_path / "run"),
        analyzer=DeterministicAnalyzer(analysis_provider=fake_analysis_provider),
        executor=FakeExecutor(),
        output_validator=OutputValidator(),
        policy=DeterministicExpansionPolicy(policy_name="deterministic"),
    )
    result = asyncio.run(runner.run(input_dcp))
    assert result.success
    run_dir = Path(result.run_dir)
    summary = json.loads((run_dir / "pipeline_summary.json").read_text())
    assert summary["analysis"]["sessions"] == 1
    assert summary["analysis"]["loop_decisions"] > 0
    assert summary["analysis"]["candidate_prior_count"] > 0
    event = json.loads((run_dir / "analysis" / "analysis_events.jsonl").read_text().splitlines()[0])
    assert event["tool_loop_mode"] == "mock_llm_closed_loop"
    assert event["analysis_run"]["status"] in {"COMPLETED", "STOPPED"}
    assert len(event["loop_decisions"]) > 0
