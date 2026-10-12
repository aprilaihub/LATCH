# Ported from a722b1e; import paths only adapted.
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from integrations.llm.runtime import LLMRequest
from integrations.llm.runtime.registry import build_llm_client
from integrations.llm.runtime.adapters import MockLLMClient, OpenAIChatClient, OpenAICompatibleChatClient
from integrations.llm.runtime.provider_profiles import ProviderRequestPolicy
from experiments.ablation.runtime.artifacts import ArtifactStore
from experiments.ablation.runtime.expansion.frontier_runner import FrontierExpansionRunner
from experiments.ablation.runtime.expansion.policies.core import BaseExpansionPolicy, ExpansionDecision
from integrations.llm.runtime.decision_events import read_llm_decision_events
from optimizer_pipeline.models import DcpStateNode, NodeStatus, PipelineState, PipelineStatus
from experiments.llm.prompts import PromptLoadError, PromptLoader, PromptRenderer
from experiments.ablation.config import OptimizationConfig


class FakeChatCompletions:
    def __init__(self, *, fail_first_temperature=False, reported_cost=0.003):
        self.calls = []
        self.fail_first_temperature = fail_first_temperature
        self.reported_cost = reported_cost

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if self.fail_first_temperature and len(self.calls) == 1 and "temperature" in kwargs:
            raise RuntimeError("Unsupported value: 'temperature' does not support 0 with this model.")
        payload = {
            "decision_type": "expand_node",
            "target_node_id": "node_000_root",
            "candidates": [{"target_node_id": "node_000_root", "flow": "phys_opt", "risk": "medium", "confidence": "high"}],
            "prune_node_ids": [],
            "stop": False,
            "reason": "adapter test",
        }
        message = SimpleNamespace(content=json.dumps(payload))
        choice = SimpleNamespace(message=message)
        usage = SimpleNamespace(prompt_tokens=7, completion_tokens=5, total_tokens=12, cost=self.reported_cost)
        return SimpleNamespace(choices=[choice], usage=usage)


class FakeOpenAIClient:
    def __init__(self, completions):
        self.chat = SimpleNamespace(completions=completions)


class RecordingFactory:
    def __init__(self):
        self.kwargs = None
        self.completions = FakeChatCompletions()

    def __call__(self, **kwargs):
        self.kwargs = kwargs
        return FakeOpenAIClient(self.completions)


def test_frontier_policy_prompt_family_is_removed_by_hardcut():
    with pytest.raises(PromptLoadError, match="Prompt manifest not found"):
        PromptLoader().load("frontier_expansion.baseline")


def test_prompt_suite_v1_frontier_variants_are_removed_by_hardcut():
    prompt_ids = [
        "frontier_expansion.baseline",
        "frontier_expansion.strict_json",
        "frontier_expansion.risk_aware",
        "frontier_expansion.cost_aware",
        "frontier_expansion.retrieval_aware",
        "frontier_expansion.fewshot_minimal",
    ]

    for prompt_id in prompt_ids:
        with pytest.raises(PromptLoadError, match="Prompt manifest not found"):
            PromptLoader().load(prompt_id)


def test_prompt_renderer_includes_compact_request_and_analysis_evidence():
    with pytest.raises(PromptLoadError, match="Prompt manifest not found"):
        PromptRenderer("frontier_expansion.baseline").render({}, compact_request={})


def test_prompt_hash_is_stable_for_unchanged_files():
    with pytest.raises(PromptLoadError, match="Prompt manifest not found"):
        PromptRenderer("frontier_expansion.baseline").render({}, compact_request={"frontier_node_ids": ["node_000_root"]})


def test_missing_prompt_id_fails_with_clear_error():
    with pytest.raises(PromptLoadError, match="Prompt manifest not found"):
        PromptLoader().load("frontier_expansion.missing")


def test_malformed_response_schema_fails_with_clear_error(tmp_path: Path):
    prompt_dir = tmp_path / "frontier_expansion" / "bad_schema"
    prompt_dir.mkdir(parents=True)
    (prompt_dir / "manifest.json").write_text(
        json.dumps(
            {
                "id": "frontier_expansion.bad_schema",
                "task": "frontier_expansion",
                "name": "bad_schema",
                "system": "system.md",
                "user": "user.md",
                "response_schema": "response_schema.json",
            }
        )
    )
    (prompt_dir / "system.md").write_text("system")
    (prompt_dir / "user.md").write_text("{compact_request_json}")
    (prompt_dir / "response_schema.json").write_text("{not valid json")
    with pytest.raises(PromptLoadError, match="Malformed response schema JSON"):
        PromptLoader(tmp_path).load("frontier_expansion.bad_schema")


def test_default_prompt_id_is_semantic_not_numeric():
    config = OptimizationConfig()
    assert config.prompt_id == "frontier_expansion.baseline"
    assert config.policy_type == "analysis_priority"
    assert "v0" not in config.prompt_id
    assert "v1" not in config.prompt_id
    assert not config.prompt_id.endswith("_1")


def test_mock_llm_client_returns_valid_json_response():
    response = MockLLMClient().complete_json(
        LLMRequest(provider="mock", model="mock_model", messages=[], response_format="json_object")
    )
    assert response.parsed_json is not None
    assert response.parsed_json["candidates"][0]["flow"] == "phys_opt"
    assert response.cost_usd is None


def test_mock_good_uses_current_frontier_context():
    response = MockLLMClient().complete_json(
        LLMRequest(provider="mock", model="mock_good", messages=[{"role": "user", "content": "High-value search context:\n" + json.dumps({
            "frontier_node_ids": ["node_001_phys_opt"],
            "frontier": [{"node_id": "node_001_phys_opt", "flow_sequence": ["phys_opt"]}],
            "allowed_flows": ["phys_opt", "retime"],
            "existing_sequences": [["phys_opt"]],
            "batch_size": 1,
        })}])
    )
    assert response.parsed_json["target_node_id"] == "node_001_phys_opt"
    assert response.parsed_json["candidates"][0]["flow"] == "retime"


def test_mock_profiles_emit_controlled_invalid_outputs():
    messages = [{"role": "user", "content": "High-value search context:\n" + json.dumps({
        "frontier_node_ids": ["node_001_phys_opt"],
        "frontier": [{"node_id": "node_001_phys_opt", "flow_sequence": []}],
        "allowed_flows": ["phys_opt"],
        "batch_size": 1,
    })}]
    bad_flow = MockLLMClient().complete_json(
        LLMRequest(provider="mock", model="mock_bad_flow", messages=messages)
    )
    malformed = MockLLMClient().complete_json(
        LLMRequest(provider="mock", model="mock_malformed_json", messages=messages)
    )
    assert bad_flow.parsed_json["candidates"][0]["flow"] == "mock_unknown_flow"
    assert malformed.parsed_json is None
    assert "not valid JSON" in malformed.content


def test_openai_chat_adapter_uses_json_response_format_and_usage():
    completions = FakeChatCompletions()
    client = OpenAIChatClient(api_key="test", client=FakeOpenAIClient(completions))
    response = client.complete_json(
        LLMRequest(provider="openai", model="gpt-5.4", messages=[{"role": "user", "content": "hi"}])
    )
    assert completions.calls[0]["response_format"] == {"type": "json_object"}
    assert response.prompt_tokens == 7
    assert response.completion_tokens == 5
    assert response.total_tokens == 12
    assert response.cost_usd == 0.003
    assert response.provider_reported_cost_usd == 0.003
    assert response.cost_source == "provider_reported"


def test_openai_chat_adapter_estimates_missing_cost_from_pricing_profile():
    from integrations.llm.runtime.pricing import PricingProfile

    completions = FakeChatCompletions(reported_cost=None)
    client = OpenAIChatClient(
        api_key="test",
        client=FakeOpenAIClient(completions),
        pricing_profile=PricingProfile(name="unit-test", prompt_usd_per_million_tokens=1.0, completion_usd_per_million_tokens=2.0),
    )
    response = client.complete_json(
        LLMRequest(provider="openai", model="gpt-test", messages=[{"role": "user", "content": "hi"}])
    )
    assert response.provider_reported_cost_usd is None
    assert response.estimated_cost_usd == 0.000017
    assert response.cost_usd == 0.000017
    assert response.cost_source == "estimated_pricing_profile"
    assert response.pricing_profile_id == "unit-test"


def test_openai_chat_adapter_retries_without_temperature_when_rejected():
    completions = FakeChatCompletions(fail_first_temperature=True)
    client = OpenAIChatClient(api_key="test", client=FakeOpenAIClient(completions))
    client.complete_json(
        LLMRequest(provider="openai", model="gpt-5.4", messages=[{"role": "user", "content": "hi"}], temperature=0.0)
    )
    assert "temperature" in completions.calls[0]
    assert "temperature" not in completions.calls[1]


def test_provider_request_policy_controls_openai_kwargs():
    completions = FakeChatCompletions()
    client = OpenAIChatClient(
        api_key="test",
        client=FakeOpenAIClient(completions),
        request_policy=ProviderRequestPolicy(
            response_format_policy="omit",
            temperature_policy="omit",
            max_tokens_param="max_completion_tokens",
        ),
    )
    client.complete_json(
        LLMRequest(
            provider="openai",
            model="gpt-test",
            messages=[{"role": "user", "content": "hi"}],
            temperature=0.3,
            max_tokens=42,
        )
    )
    call = completions.calls[0]
    assert "response_format" not in call
    assert "temperature" not in call
    assert call["max_completion_tokens"] == 42
    assert "max_tokens" not in call


def test_provider_request_policy_zero_only_temperature():
    completions = FakeChatCompletions()
    client = OpenAIChatClient(
        api_key="test",
        client=FakeOpenAIClient(completions),
        request_policy=ProviderRequestPolicy(temperature_policy="zero_only"),
    )
    client.complete_json(
        LLMRequest(provider="openai", model="gpt-test", messages=[{"role": "user", "content": "hi"}], temperature=0.3)
    )
    client.complete_json(
        LLMRequest(provider="openai", model="gpt-test", messages=[{"role": "user", "content": "hi"}], temperature=0.0)
    )
    assert "temperature" not in completions.calls[0]
    assert completions.calls[1]["temperature"] == 0.0


def test_openai_compatible_adapter_uses_base_url():
    factory = RecordingFactory()
    client = OpenAICompatibleChatClient(api_key="test", base_url="https://example.invalid/api", client_factory=factory)
    client.complete_json(
        LLMRequest(provider="openai_compatible", model="compatible-model", messages=[{"role": "user", "content": "hi"}])
    )
    assert factory.kwargs["api_key"] == "test"
    assert factory.kwargs["base_url"] == "https://example.invalid/api"


def test_local_openai_compatible_client_uses_placeholder_key_without_env(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    client = build_llm_client(provider="local", provider_base_url="http://localhost:1234/v1")
    assert isinstance(client, OpenAICompatibleChatClient)
    assert client.api_key == "local"
    assert client.base_url == "http://localhost:1234/v1"


def test_provider_profiles_resolve_known_provider_and_fail_on_typo():
    client = build_llm_client(provider="deepseek", api_key="test", model="deepseek-chat")
    assert isinstance(client, OpenAICompatibleChatClient)
    assert client.base_url == "https://api.deepseek.com"
    with pytest.raises(ValueError, match="Unknown LLM provider"):
        build_llm_client(provider="deepseak", api_key="test", model="deepseek-chat")


def test_provider_profile_credentials_use_provider_env(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-openai")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-deepseek")
    client = build_llm_client(provider="deepseek", model="deepseek-chat")
    assert isinstance(client, OpenAICompatibleChatClient)
    assert client.api_key == "sk-deepseek"


def test_explicit_api_key_overrides_provider_profile_env(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-deepseek")
    client = build_llm_client(provider="deepseek", api_key="sk-explicit", model="deepseek-chat")
    assert isinstance(client, OpenAICompatibleChatClient)
    assert client.api_key == "sk-explicit"


def test_openai_profile_still_uses_openai_env(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-openai")
    client = build_llm_client(provider="openai", model="gpt-test")
    assert isinstance(client, OpenAIChatClient)
    assert client.api_key == "sk-openai"


def test_openrouter_profile_uses_openrouter_endpoint_key_and_pricing(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-openai")
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-openrouter")
    client = build_llm_client(provider="openrouter", model="openai/gpt-5.4-mini")

    assert isinstance(client, OpenAICompatibleChatClient)
    assert client.api_key == "sk-openrouter"
    assert client.base_url == "https://openrouter.ai/api/v1"
    assert client.pricing_profile is not None
    assert client.pricing_profile.name == "openrouter:openai/gpt-5.4-mini"


@pytest.mark.parametrize(
    ("provider", "model", "env_name", "expected_key"),
    [
        ("kimi", "kimi-test", "MOONSHOT_API_KEY", "sk-kimi"),
        ("minimax", "minimax-test", "MINIMAX_API_KEY", "sk-minimax"),
        ("glm", "glm-test", "ZHIPU_API_KEY", "sk-glm"),
    ],
)
def test_provider_profiles_use_their_profile_env_when_openai_key_exists(monkeypatch, provider, model, env_name, expected_key):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-openai")
    monkeypatch.setenv(env_name, expected_key)

    client = build_llm_client(provider=provider, model=model)

    assert isinstance(client, OpenAICompatibleChatClient)
    assert client.api_key == expected_key


def test_explicit_openai_compatible_requires_base_url():
    with pytest.raises(ValueError, match="requires provider_base_url"):
        build_llm_client(provider="openai_compatible", api_key="test", model="compatible-model")


def test_missing_cost_remains_null_not_failure():
    response = MockLLMClient(prompt_tokens=3, completion_tokens=4).complete_json(
        LLMRequest(provider="mock", model="mock_model", messages=[])
    )
    assert response.total_tokens == 7
    assert response.cost_usd is None


def test_pipeline_summary_records_prompt_metadata(tmp_path: Path):
    class UsagePolicy(BaseExpansionPolicy):
        def propose(self, request):
            return ExpansionDecision(stop=True)

        def usage_summary(self):
            return {
                "provider": "openai",
                "model": "gpt-test",
                "prompt_id": "frontier_expansion.baseline",
                "prompt_hash": "a" * 64,
                "response_schema_hash": "b" * 64,
                "temperature": 0.0,
                "max_tokens": 100,
                "response_mode": "json_object",
                "total_llm_calls": 1,
                "total_prompt_tokens": 10,
                "total_completion_tokens": 5,
                "total_tokens": 15,
                "total_cost_usd": None,
                "total_latency_sec": 0.25,
            }

    runner = FrontierExpansionRunner(
        config=OptimizationConfig(policy_type="analysis_priority"),
        artifact_store=ArtifactStore(tmp_path / "run"),
        analyzer=None,
        executor=None,
        output_validator=None,
        policy=UsagePolicy(),
    )
    runner.nodes = {
        "node_000_root": DcpStateNode(
            node_id="node_000_root",
            parent_id=None,
            depth=0,
            input_dcp="input.dcp",
            output_dcp="input.dcp",
            status=NodeStatus.COMPLETED,
            score=0.0,
        )
    }
    state = PipelineState(status=PipelineStatus.COMPLETED, root_dcp="input.dcp", best_node_id="node_000_root")
    summary_path = runner._write_summary(state, None)
    summary = json.loads(summary_path.read_text())
    usage = runner._usage_summary()

    assert usage["prompt_id"] == "frontier_expansion.baseline"
    assert usage["total_prompt_tokens"] == 10
    assert usage["total_completion_tokens"] == 5
    assert usage["total_latency_sec"] == 0.25
    assert summary["llm_prompt_id"] == "frontier_expansion.baseline"
    assert summary["llm_prompt_hash"] == "a" * 64
    assert summary["llm_response_schema_hash"] == "b" * 64
    assert summary["llm_temperature"] == 0.0
    assert summary["llm_max_tokens"] == 100
    assert summary["llm_response_mode"] == "json_object"
    assert summary["total_prompt_tokens"] == 10
    assert summary["total_completion_tokens"] == 5
    assert summary["total_latency_sec"] == 0.25


def test_policy_event_records_llm_prompt_metadata(tmp_path: Path):
    class MetadataPolicy(BaseExpansionPolicy):
        def propose(self, request):
            return ExpansionDecision(stop=True)

        def last_decision_metadata(self):
            return {
                "provider": "mock",
                "model": "mock_good",
                "prompt_id": "frontier_expansion.baseline",
                "prompt_hash": "a" * 64,
                "response_schema_hash": "b" * 64,
                "temperature": 0.0,
                "response_mode": "json_object",
                "total_tokens": 7,
            }

    class Validation:
        valid = True
        llm_attempts = [
            {
                "attempt_index": 1,
                "request": {"frontier_node_ids": ["node_000_root"], "allowed_flows": ["phys_opt"]},
                "decision": {"candidates": []},
                "validation": {"valid": True, "errors": []},
                "llm_metadata": {
                    "provider": "mock",
                    "model": "mock_good",
                    "prompt_id": "frontier_expansion.baseline",
                    "prompt_hash": "a" * 64,
                    "response_schema_hash": "b" * 64,
                    "temperature": 0.0,
                    "max_tokens": 100,
                    "prompt_tokens": 3,
                    "completion_tokens": 4,
                    "cost_usd": 0.0,
                    "cost_source": "estimated_pricing_profile",
                    "latency_sec": 0.0,
                },
            }
        ]

        def to_dict(self):
            return {"valid": True, "errors": []}

    runner = FrontierExpansionRunner(
        config=OptimizationConfig(policy_type="analysis_priority"),
        artifact_store=ArtifactStore(tmp_path / "run"),
        analyzer=None,
        executor=None,
        output_validator=None,
        policy=MetadataPolicy(),
    )
    runner._record_policy_event({}, ExpansionDecision(stop=True), Validation())

    events = json.loads((tmp_path / "run" / "policy_events.json").read_text())
    event_file = json.loads((tmp_path / "run" / "policy_events" / "policy_event_000.json").read_text())
    assert events[0]["llm_metadata"]["provider"] == "mock"
    assert events[0]["llm_metadata"]["prompt_id"] == "frontier_expansion.baseline"
    assert "llm_attempts" not in events[0]
    assert "llm_attempts" not in event_file
    attempt_events = read_llm_decision_events(tmp_path / "run")
    assert attempt_events[0]["schema_version"] == "llm_decision_attempt.v1"
    assert attempt_events[0]["task"] == "frontier_expansion"
    assert attempt_events[0]["run_id"] == "run"
    assert attempt_events[0]["event_index"] == 0
    assert attempt_events[0]["attempt_index"] == 1
    assert attempt_events[0]["provider"] == "mock"
    assert attempt_events[0]["model"] == "mock_good"
    assert attempt_events[0]["prompt_id"] == "frontier_expansion.baseline"
    assert attempt_events[0]["prompt_hash"] == "a" * 64
    assert attempt_events[0]["response_schema_hash"] == "b" * 64
    assert attempt_events[0]["frontier_node_count"] == 1
    assert attempt_events[0]["allowed_flow_count"] == 1
    assert attempt_events[0]["json_parse_success"] is True
    assert attempt_events[0]["domain_validation_success"] is True
    assert attempt_events[0]["fallback_triggered_after_attempt"] is False
    assert attempt_events[0]["prompt_tokens"] == 3
    assert attempt_events[0]["completion_tokens"] == 4
    assert attempt_events[0]["cost_usd"] == 0.0
    assert attempt_events[0]["cost_source"] == "estimated_pricing_profile"
    assert attempt_events[0]["latency_sec"] == 0.0
    assert len(attempt_events[0]["request_context_hash"]) == 64


def test_policy_events_persist_sanitized_recent_event_summaries(tmp_path: Path):
    class MetadataPolicy(BaseExpansionPolicy):
        def propose(self, request):
            return ExpansionDecision(stop=True)

        def last_decision_metadata(self):
            return {
                "provider": "mock",
                "model": "mock_good",
                "prompt_id": "frontier_expansion.baseline",
                "total_tokens": 7,
            }

    class Validation:
        valid = True
        errors = []

        def __init__(self, index):
            self.rejected_payload = {
                "llm_attempts": [{"attempt_index": 1, "index": index}],
                "raw_payload_id": f"payload-{index}",
            }
            self.llm_attempts = [
                {
                    "attempt_index": 1,
                    "request": {
                        "frontier_node_ids": ["node_000_root"],
                        "allowed_flows": ["phys_opt"],
                        "recent_events": [],
                    },
                    "decision": {"candidates": []},
                    "validation": {
                        "valid": True,
                        "errors": [],
                        "llm_attempts": [{"duplicate": True}],
                    },
                    "llm_metadata": {"provider": "mock", "total_tokens": 7},
                }
            ]

        def to_dict(self):
            return {
                "valid": True,
                "errors": [],
                "rejected_payload": self.rejected_payload,
                "llm_attempts": self.llm_attempts,
            }

    runner = FrontierExpansionRunner(
        config=OptimizationConfig(policy_type="analysis_priority"),
        artifact_store=ArtifactStore(tmp_path / "run"),
        analyzer=None,
        executor=None,
        output_validator=None,
        policy=MetadataPolicy(),
    )

    serialized_sizes = []
    for index in range(15):
        request = {
            "frontier_node_ids": ["node_000_root"],
            "allowed_flows": ["phys_opt"],
            "analysis_priors": [
                {
                    "target_node_id": "node_000_root",
                    "candidate_flow": "phys_opt",
                    "priority_delta": 0.2,
                    "confidence": 0.9,
                    "analysis_session_id": "a1",
                }
            ],
            "recent_events": runner.policy_events[-5:],
        }
        runner._record_policy_event(request, ExpansionDecision(stop=True), Validation(index))
        events = json.loads((tmp_path / "run" / "policy_events.json").read_text())
        serialized_sizes.append(len(json.dumps(events, sort_keys=True)))

    events = json.loads((tmp_path / "run" / "policy_events.json").read_text())
    attempt_events = read_llm_decision_events(tmp_path / "run")
    assert len(events) == 15
    assert len(attempt_events) == 15
    assert events[-1]["request"]["analysis_priors"][0]["analysis_session_id"] == "a1"
    assert runner._recent_policy_event_summaries(limit=5)[-1]["llm_metadata"]["provider"] == "mock"

    for event in events:
        assert "llm_attempts" not in event
        assert "llm_attempts" not in event["validation"]
        assert event["validation"]["rejected_payload"]["llm_attempt_count"] == 1
        assert "llm_attempts" not in event["validation"]["rejected_payload"]
        for recent_event in event["request"].get("recent_events") or []:
            assert "request" not in recent_event
            assert "llm_attempts" not in recent_event
            assert "validation" not in recent_event
    for attempt in attempt_events:
        assert "request" not in attempt
        assert "llm_attempts" not in attempt

    deltas = [after - before for before, after in zip(serialized_sizes, serialized_sizes[1:])]
    average_delta = sum(deltas) / len(deltas)
    assert max(deltas) < average_delta * 3


def test_llm_decision_events_mark_final_fallback_attempt(tmp_path: Path):
    class MetadataPolicy(BaseExpansionPolicy):
        def propose(self, request):
            return ExpansionDecision(stop=True)

        def last_decision_metadata(self):
            return {"provider": "mock", "model": "mock_bad_flow", "prompt_id": "frontier_expansion.baseline"}

    class Validation:
        valid = False
        errors = ["fallback after invalid attempts"]
        rejected_payload = {}
        llm_attempts = [
            {
                "attempt_index": 1,
                "request": {"frontier_node_ids": ["node_000_root"], "allowed_flows": ["phys_opt"]},
                "decision": {"candidates": [{"flow": "mock_unknown_flow"}]},
                "validation": {"valid": False, "errors": ["unknown flow: mock_unknown_flow"]},
                "llm_metadata": {"provider": "mock", "model": "mock_bad_flow", "prompt_id": "frontier_expansion.baseline"},
            },
            {
                "attempt_index": 2,
                "request": {"frontier_node_ids": ["node_000_root"], "allowed_flows": ["phys_opt"]},
                "decision": {"candidates": [{"flow": "mock_unknown_flow"}]},
                "validation": {"valid": False, "errors": ["unknown flow: mock_unknown_flow"]},
                "llm_metadata": {"provider": "mock", "model": "mock_bad_flow", "prompt_id": "frontier_expansion.baseline"},
            },
        ]

        def to_dict(self):
            return {
                "valid": False,
                "errors": self.errors,
                "rejected_payload": {"llm_attempts": self.llm_attempts},
                "llm_attempts": self.llm_attempts,
            }

    runner = FrontierExpansionRunner(
        config=OptimizationConfig(policy_type="analysis_priority"),
        artifact_store=ArtifactStore(tmp_path / "run"),
        analyzer=None,
        executor=None,
        output_validator=None,
        policy=MetadataPolicy(),
    )
    runner._record_policy_event({}, ExpansionDecision(stop=True, fallback_used=True), Validation())

    attempt_events = read_llm_decision_events(tmp_path / "run")

    assert [row["fallback_triggered_after_attempt"] for row in attempt_events] == [False, True]
