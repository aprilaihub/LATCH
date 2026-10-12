from __future__ import annotations

import asyncio
import copy
import json
import os
from pathlib import Path
from typing import Any

import pytest

from scripts.run_wp016a_canary import prepare_live_runtime

from optimizer_pipeline.dof.multi_provider import (
    MAX_OUTPUT_TOKENS, PROFILES, StrictMultiProviderTransport,
    parse_offline_provider_envelope,
)
from optimizer_pipeline.dof.provider import (
    ProviderRequestBinding, ProviderSettings, ProviderTransportError,
)
from optimizer_pipeline.dof.wp016 import canonical_bytes, sha256
from optimizer_pipeline.dof.wp016_authority import DurableJournal, create_runtime_root
from optimizer_pipeline.dof.wp016_canary import (
    LITERAL_USD_CAP, MAX_CANONICAL_REQUEST_BODY_BYTES, REQUEST_CAP,
    VALID_CANARY_TERMINALS, LiteralBudget, _load_fixture_sequences,
    client_request_id, conservative_next_request_usd, load_source_rows,
    provider_binding_receipt, run_all, runtime_identity_bindings,
    validate_credential_binding, validate_live_exchange,
)


MODEL = "openai/gpt-5.6-luna"


def _scientific_manifest() -> Path:
    value = os.environ.get("WP016A_TEST_SCIENTIFIC_INPUT_MANIFEST")
    if not value:
        pytest.skip("external WP-016A replay capsule not supplied")
    return Path(value)


def _binding() -> ProviderRequestBinding:
    profile = PROFILES[MODEL]
    return ProviderRequestBinding(
        run_id="g0r2-test", spec_hash=sha256(b"spec"),
        experiment_id="wp016a", condition_id="g0r2", repeat_id="none",
        decision_id="g0r2-decision", context_bytes=b'{}',
        prompt_bytes=b"Return one JSON object.", prompt_version="g0r2.v1",
        action_interface_version="g0r2.v1", action_interface_hash=sha256(b"action"),
        context_interface_version="g0r2.v1", context_interface_hash=sha256(b"context"),
        catalogue_version="g0r2.v1", catalogue_hash=sha256(b"catalogue"),
        capability_hash=sha256(b"capability"), mask_evidence_hash=sha256(b"mask"),
        provider_role="planner", provider=profile.provider, model_id=MODEL,
        adapter_version=profile.adapter_version,
        settings=ProviderSettings(
            temperature=None, max_tokens=MAX_OUTPUT_TOKENS, seed=None,
            timeout_seconds=60.0, max_retries=0, retry_on=(), max_requests=1,
        ),
    )


def _openai_payload(*, content: dict[str, Any] | None = None,
                    service_tier: str | None = "default",
                    completion_tokens: int = 8,
                    finish_reason: str = "stop") -> dict[str, Any]:
    payload: dict[str, Any] = {
        "id": "chatcmpl-object-123", "model": "gpt-5.6-luna",
        "system_fingerprint": "fp_wp016a_test",
        "choices": [{"message": {"content": json.dumps(content or {})},
                     "finish_reason": finish_reason}],
        "usage": {
            "prompt_tokens": 16, "completion_tokens": completion_tokens,
            "prompt_tokens_details": {"cached_tokens": 0, "cache_write_tokens": 0},
            "completion_tokens_details": {"reasoning_tokens": 1},
        },
    }
    if service_tier is not None:
        payload["service_tier"] = service_tier
    return payload


class _Response:
    def __init__(self, payload: dict[str, Any], *, status: int = 200,
                 headers: dict[str, str] | None = None):
        self.content = canonical_bytes(payload)
        self.status_code = status
        self.headers = headers or {}


class _Client:
    captured_headers: dict[str, str] | None = None

    def __init__(self, response: _Response | None, *, fail: bool = False, **kwargs: Any):
        self.response = response
        self.fail = fail
        self.hooks = kwargs["event_hooks"]["request"]

    async def post(self, _endpoint: str, **kwargs: Any) -> _Response:
        type(self).captured_headers = dict(kwargs["headers"])
        for hook in self.hooks:
            await hook({"kind": "request"})
        if self.fail:
            raise OSError("synthetic transport failure")
        assert self.response is not None
        return self.response

    async def aclose(self) -> None:
        return None


def _factory(response: _Response | None, *, fail: bool = False):
    return lambda **kwargs: _Client(response, fail=fail, **kwargs)


def _mutated_sequences(first_endpoint: dict[str, Any], *,
                       turns: int = 1, completion_tokens: int = 8,
                       finish_reason: str = "stop",
                       source_index: int = 0) -> dict[str, list[bytes]]:
    fixtures = {key: list(value) for key, value in _load_fixture_sequences().items()}
    first_source = load_source_rows()[source_index]["configuration_id"]
    template = json.loads(fixtures[first_source][0])
    rows = []
    for _index in range(turns):
        row = copy.deepcopy(template)
        row["choices"][0]["message"]["content"] = json.dumps(first_endpoint)
        row["choices"][0]["finish_reason"] = finish_reason
        row["usage"]["completion_tokens"] = completion_tokens
        rows.append(canonical_bytes(row))
    fixtures[first_source] = rows
    return fixtures


def _first_cell_sequence(endpoints: list[dict[str, Any]]) -> dict[str, list[bytes]]:
    fixtures = {key: list(value) for key, value in _load_fixture_sequences().items()}
    source = load_source_rows()[0]["configuration_id"]
    template = json.loads(fixtures[source][0])
    rows = []
    for endpoint in endpoints:
        row = copy.deepcopy(template)
        row["choices"][0]["message"]["content"] = json.dumps(endpoint)
        row["choices"][0]["finish_reason"] = "stop"
        row["usage"]["completion_tokens"] = 8
        rows.append(canonical_bytes(row))
    fixtures[source] = rows
    return fixtures


def test_valid_terminal_set_is_exact() -> None:
    assert VALID_CANARY_TERMINALS == {
        "COMMIT_RECEIVED", "NO_VALID_SUBMISSION_RECEIVED",
    }


def test_manifest_and_runtime_identity_contract_are_exact() -> None:
    nonce = "wp016a-g0r2-identity"
    rows = load_source_rows()
    bindings = runtime_identity_bindings(rows, nonce)
    assert len(bindings) == 6
    for source, binding in zip(rows, bindings, strict=True):
        for kind in ("configuration", "cell", "session", "run", "log", "output"):
            assert binding[kind + "_id"].startswith("wp016a-" + kind + "__")
            assert binding[kind + "_id"] == (
                "wp016a-" + kind + "__" + sha256({
                    "wp": "WP-016A", "source": source["configuration_id"],
                    "kind": kind, "run_nonce": nonce,
                })[:24]
            )


def test_no_valid_submission_is_a_valid_cell_and_all_six_continue() -> None:
    result = asyncio.run(run_all(
        live=False, scientific_input_manifest=_scientific_manifest(),
        fixture_sequences=_mutated_sequences({
            "endpoint": "NO_VALID_SUBMISSION",
            "reason_code": "INSUFFICIENT_VISIBLE_EVIDENCE",
        }),
    ))
    assert result["status"] == "PASS_CANARY_INTEGRATION"
    assert result["configurations_replayed"] == 6
    assert result["cells"][0]["terminal"] == "NO_VALID_SUBMISSION_RECEIVED"
    assert all(cell["terminal"] in VALID_CANARY_TERMINALS for cell in result["cells"])


@pytest.mark.parametrize(
    "endpoint,turns,completion_tokens,finish_reason,terminal",
    [
        ({"endpoint": "LIST_CAPABILITIES"}, 1, 4097, "stop", "OUTPUT_LIMIT_EXHAUSTED"),
        ({"endpoint": "LIST_CAPABILITIES"}, 1, 8, "length", "TRUNCATED_RESPONSE"),
        ({"endpoint": "GET_EXAMPLE", "content_id": "EXAMPLE/G/NOT_REGISTERED"},
         1, 8, "stop", "RETRIEVAL_NOT_FOUND"),
    ],
)
def test_typed_first_cell_negative_outcome_continues_later_cells(
    endpoint: dict[str, Any], turns: int, completion_tokens: int,
    finish_reason: str, terminal: str,
) -> None:
    result = asyncio.run(run_all(
        live=False, scientific_input_manifest=_scientific_manifest(),
        fixture_sequences=_mutated_sequences(
            endpoint, turns=turns, completion_tokens=completion_tokens,
            finish_reason=finish_reason,
        ),
    ))
    assert result["status"] == "PASS_CANARY_INTEGRATION"
    assert result["configurations_replayed"] == 6
    assert result["cells"][0]["terminal"] == terminal
    assert result["cells"][0]["integration_status"] == "COMPLETE_TYPED_CELL_OUTCOME"
    assert result["cells"][0]["shared_integration_defect"] is False
    assert result["later_cells_not_executed"] == 0
    assert len(result["completed_cell_ids_and_terminals"]) == 6
    assert result["failed_turn"] is None
    assert result["failed_cell_id"] is None


def test_turn_limit_terminal_is_typed_and_later_cells_continue() -> None:
    result = asyncio.run(run_all(
        live=False, scientific_input_manifest=_scientific_manifest(),
        fixture_sequences=_mutated_sequences(
            {"endpoint": "LIST_CAPABILITIES"}, turns=4, source_index=2,
        ),
    ))
    assert result["status"] == "PASS_CANARY_INTEGRATION"
    assert result["configurations_replayed"] == 6
    assert result["cells"][2]["terminal"] == "TURN_LIMIT_EXHAUSTED"
    assert result["cells"][2]["terminal_origin"] == "FINITE_PROTOCOL_BUDGET"
    assert result["cells"][2]["proposal_outcome"] == "NO_PROPOSAL_TURN_LIMIT"
    assert result["later_cells_not_executed"] == 0
    assert len(result["completed_cell_ids_and_terminals"]) == 6
    assert result["failed_turn"] is None
    assert result["last_provider_turn"] == 4


def test_live_like_oversize_trajectory_recovers_to_nvs_and_continues() -> None:
    result = asyncio.run(run_all(
        live=False, scientific_input_manifest=_scientific_manifest(),
        fixture_sequences=_first_cell_sequence([
            {
                "endpoint": "GET_EXAMPLE",
                "content_id": "EXAMPLE/G/G_BOUNDARY_NONPREFIX_MULTI_NET",
            },
            {
                "endpoint": "GET_SCHEMA_FRAGMENT",
                "content_id": "SCHEMA/G/program_schema",
            },
            {
                "endpoint": "NO_VALID_SUBMISSION",
                "reason_code": "DYNAMIC_BUDGET_EXHAUSTED",
            },
        ]),
    ))
    assert result["status"] == "PASS_CANARY_INTEGRATION"
    assert result["schema_version"] == "wp016a.controller-replay.v5"
    assert result["configurations_replayed"] == 6
    assert [turn["canonical_event"] for turn in result["cells"][0]["turns"]] == [
        "RETRIEVAL_OK", "RETRIEVAL_ITEM_TOO_LARGE",
        "NO_VALID_SUBMISSION_RECEIVED",
    ]
    assert result["cells"][0]["terminal"] == "NO_VALID_SUBMISSION_RECEIVED"
    assert result["cells"][0]["compact_adapter_evidence"]["semantic_events"][1][
        "canonical_event"
    ] == "RETRIEVAL_ITEM_TOO_LARGE"
    frontier_turn = result["cells"][0]["turns"][1]
    assert frontier_turn["frontier_semantics"] == "RETRIEVABLE_DESCENDANT_FRONTIER"
    assert frontier_turn["requested_content_bytes_charged"] == 0
    assert frontier_turn["frontier_metadata_bytes"] > 0
    assert frontier_turn["retrieval_bytes"] > result["cells"][0]["turns"][0]["retrieval_bytes"]
    assert frontier_turn["semantic_remaining_bytes"] < result["cells"][0]["turns"][0][
        "semantic_remaining_bytes"
    ]


def test_typed_negative_and_later_cell_results_are_durable(tmp_path: Path) -> None:
    nonce = "wp016a-g0r2-partial-durable"
    bindings = runtime_identity_bindings(load_source_rows(), nonce)
    root = create_runtime_root(
        runtime_base=tmp_path, run_nonce=nonce, cell_bindings=bindings,
    )
    journal = DurableJournal(root)
    result = asyncio.run(run_all(
        live=False, scientific_input_manifest=_scientific_manifest(),
        run_nonce=nonce, journal=journal,
        fixture_sequences=_mutated_sequences(
            {"endpoint": "LIST_CAPABILITIES"}, completion_tokens=4097,
        ),
    ))
    cell_id = bindings[0]["cell_id"]
    cell_path = root / "cells" / cell_id / "output" / "cell-result.json"
    assert json.loads(cell_path.read_text())["terminal"] == "OUTPUT_LIMIT_EXHAUSTED"
    first = json.loads(cell_path.read_text())
    assert first["integration_status"] == "COMPLETE_TYPED_CELL_OUTCOME"
    assert first["proposal_outcome"] == "NO_PROPOSAL_OUTPUT_LIMIT"
    assert result["later_cells_not_executed"] == 0
    assert len(list((root / "cells").glob("*/output/cell-result.json"))) == 6
    paths = json.loads((root / "authority" / "identity-path-bindings.json").read_text())
    assert paths["cells"][0]["output_path"].endswith("/output/cell-result.json")
    request = root / "requests" / f"{bindings[1]['cell_id']}__turn-1.json"
    response = root / "responses" / f"{bindings[1]['cell_id']}__turn-1.json"
    assert json.loads(request.read_text())["client_request_id"].startswith("wp016a-client__")
    assert json.loads(response.read_text())["schema_version"] == "wp016a.safe-response-receipt.v2"


@pytest.mark.parametrize("tier", [None, "flex", "priority"])
def test_openai_returned_service_tier_is_mandatory_and_default(tier: str | None) -> None:
    raw = canonical_bytes(_openai_payload(service_tier=tier))
    with pytest.raises(ProviderTransportError, match="NO_GO_OPENAI_SERVICE_TIER_MISMATCH"):
        parse_offline_provider_envelope(
            PROFILES[MODEL], raw, request_body={"test": True},
            expected_service_tier="default",
        )


def test_three_request_ids_are_distinct_and_header_is_forwarded() -> None:
    client_id = client_request_id("nonce", "cell", 1)
    assert client_id != client_request_id("nonce", "cell", 2)
    response = _Response(
        _openai_payload(), headers={"x-request-id": "req-header-456"},
    )
    transport = StrictMultiProviderTransport(
        profile=PROFILES[MODEL], api_key="unit-test-secret",
        explicit_no_prompt_cache=True, client_request_id=client_id,
        expected_service_tier="default", require_openai_x_request_id=True,
        client_factory=_factory(response),
    )
    asyncio.run(transport.send(_binding(), 0))
    exchange = transport.last_exchange
    assert exchange is not None
    assert _Client.captured_headers["X-Client-Request-Id"] == client_id
    assert exchange["client_request_id"] == client_id
    assert exchange["openai_x_request_id"] == "req-header-456"
    assert exchange["response_object_id"] == "chatcmpl-object-123"
    assert len({client_id, exchange["openai_x_request_id"], exchange["response_object_id"]}) == 3
    assert exchange["returned_service_tier"] == "default"
    assert exchange["system_fingerprint"] == "fp_wp016a_test"


def test_http_error_retains_header_request_id_and_transport_failure_does_not_invent_one() -> None:
    client_id = client_request_id("nonce", "cell", 2)
    http = StrictMultiProviderTransport(
        profile=PROFILES[MODEL], api_key="unit-test-secret",
        explicit_no_prompt_cache=True, client_request_id=client_id,
        expected_service_tier="default", require_openai_x_request_id=True,
        client_factory=_factory(_Response(
            {"error": "public"}, status=429,
            headers={"x-request-id": "req-error-789"},
        )),
    )
    with pytest.raises(ProviderTransportError, match="openai_http_429"):
        asyncio.run(http.send(_binding(), 0))
    assert http.last_exchange["client_request_id"] == client_id
    assert http.last_exchange["openai_x_request_id"] == "req-error-789"
    assert http.last_exchange["response_object_id"] is None

    failed = StrictMultiProviderTransport(
        profile=PROFILES[MODEL], api_key="unit-test-secret",
        explicit_no_prompt_cache=True, client_request_id=client_id,
        expected_service_tier="default", require_openai_x_request_id=True,
        client_factory=_factory(None, fail=True),
    )
    with pytest.raises(ProviderTransportError, match="openai_transport:OSError"):
        asyncio.run(failed.send(_binding(), 0))
    assert failed.last_exchange["client_request_id"] == client_id
    assert failed.last_exchange["openai_x_request_id"] is None
    assert failed.last_exchange["response_object_id"] is None


def test_missing_openai_header_is_fail_closed_even_on_http_error() -> None:
    transport = StrictMultiProviderTransport(
        profile=PROFILES[MODEL], api_key="unit-test-secret",
        explicit_no_prompt_cache=True, client_request_id="client-1",
        expected_service_tier="default", require_openai_x_request_id=True,
        client_factory=_factory(_Response({"error": "public"}, status=429)),
    )
    with pytest.raises(ProviderTransportError, match="NO_GO_OPENAI_X_REQUEST_ID_MISSING"):
        asyncio.run(transport.send(_binding(), 0))
    assert transport.last_exchange["http_status"] == 429
    assert transport.last_exchange["openai_x_request_id"] is None


def test_credential_fingerprint_detects_missing_and_drift_without_secret_persistence(
    tmp_path: Path,
) -> None:
    receipt = provider_binding_receipt(credential="credential-alpha")
    path = tmp_path / "provider-binding.json"
    path.write_text(json.dumps(receipt))
    serialized = path.read_text()
    assert "credential-alpha" not in serialized
    assert validate_credential_binding(
        path, environment={"OPENAI_API_KEY": "credential-alpha"},
    ) == receipt["credential_identity_sha256"]
    with pytest.raises(RuntimeError, match="NO_GO_CREDENTIAL_MISSING"):
        validate_credential_binding(path, environment={})
    with pytest.raises(RuntimeError, match="NO_GO_CREDENTIAL_IDENTITY_DRIFT"):
        validate_credential_binding(
            path, environment={"OPENAI_API_KEY": "credential-beta"},
        )


@pytest.mark.parametrize("observed", [None, "credential-beta"])
def test_missing_or_drifted_credential_prevents_runtime_and_token_consumption(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, observed: str | None,
) -> None:
    manifests = tmp_path / "manifests"
    manifests.mkdir()
    receipt = provider_binding_receipt(credential="credential-alpha")
    (manifests / "provider-binding.json").write_text(json.dumps(receipt))
    if observed is None:
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    else:
        monkeypatch.setenv("OPENAI_API_KEY", observed)
    runtime_base = tmp_path / "runtime"
    with pytest.raises(RuntimeError, match=(
        "NO_GO_CREDENTIAL_MISSING" if observed is None
        else "NO_GO_CREDENTIAL_IDENTITY_DRIFT"
    )):
        prepare_live_runtime(
            token={"run_nonce": "wp016a-g0r2-preconsume"},
            manifest_dir=manifests, runtime_base=runtime_base,
        )
    assert not runtime_base.exists()


def test_exact_body_byte_budget_proof_and_native_usage_boundary() -> None:
    receipt = conservative_next_request_usd(MAX_CANONICAL_REQUEST_BODY_BYTES)
    assert str(receipt * REQUEST_CAP) == "0.4971648"
    assert receipt * REQUEST_CAP < LITERAL_USD_CAP
    body = {"payload": "x" * 70_000}
    budget = LiteralBudget()
    body_bytes, next_bound = budget.guard(b"p", b"c", body)
    assert 65_536 < body_bytes <= MAX_CANONICAL_REQUEST_BODY_BYTES
    assert next_bound == conservative_next_request_usd(body_bytes)
    exchange = {
        "returned_service_tier": "default", "openai_x_request_id": "req-boundary",
        "native_usage": {
            "input_tokens": body_bytes, "cached_input_tokens": 0,
            "cache_write_input_tokens": 0,
        },
    }
    validate_live_exchange(exchange, canonical_request_body_bytes=body_bytes)
    exchange["native_usage"]["input_tokens"] = body_bytes + 1
    with pytest.raises(RuntimeError, match="NO_GO_PROVIDER_USAGE_EXCEEDS_CONSERVATIVE_BOUND"):
        validate_live_exchange(exchange, canonical_request_body_bytes=body_bytes)
