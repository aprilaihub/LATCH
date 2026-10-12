from __future__ import annotations

import argparse
import asyncio
import copy
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator
import pytest

import optimizer_pipeline.dof.wp016_completion as completion
import scripts.run_wp016a_completion as completion_cli
from optimizer_pipeline.dof.wp016 import canonical_bytes
from optimizer_pipeline.dof.wp016_authority import (
    AuthorityLease, DurableJournal, consume_once, create_runtime_root,
)
from optimizer_pipeline.dof.wp016_canary import (
    file_sha256, provider_binding_receipt,
)
from optimizer_pipeline.dof.wp016_inputs import (
    write_environment_manifest, write_runtime_manifest,
)


ROOT = Path(__file__).resolve().parents[1]
ATTEMPT2_ENV = "WP016A_TEST_ATTEMPT2_CANARY_RESULT"


def _scientific_manifest() -> Path:
    value = os.environ.get("WP016A_TEST_SCIENTIFIC_INPUT_MANIFEST")
    if not value:
        pytest.skip("external WP-016A replay capsule not supplied")
    return Path(value)


def _attempt2_result() -> Path:
    value = os.environ.get(ATTEMPT2_ENV)
    if not value:
        pytest.skip("immutable Attempt-2 canary result not supplied")
    return Path(value)


def _write(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")


def _token_fixture(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> tuple[Path, dict[str, Path], dict[str, Any]]:
    source = _attempt2_result()
    science = _scientific_manifest()
    bridge = completion.attempt2_bridge_receipt(
        json.loads(source.read_text()), source_file_sha256=file_sha256(source),
    )
    manifests = tmp_path / "manifests"
    manifests.mkdir()
    values = completion.r08_static_manifests(
        bridge=bridge,
        provider_binding=provider_binding_receipt(credential="unit-test-credential"),
    )
    for name, value in values.items():
        _write(manifests / name, value)

    result = asyncio.run(completion.run_completion(
        live=False, scientific_input_manifest=science,
        run_nonce=completion.OFFLINE_REPLAY_NONCE,
    ))
    replay = tmp_path / completion.R08_OFFLINE_REPLAY
    _write(replay, completion.offline_replay_receipt(result))
    bridge_path = manifests / completion.R08_ATTEMPT2_BRIDGE
    template = manifests / completion.R08_COMBINED_TEMPLATE
    environment = tmp_path / "environment.json"
    runtime = tmp_path / "runtime.json"
    runtime_base = tmp_path / "runtime-base"
    write_environment_manifest(environment, repository_root=ROOT)
    write_runtime_manifest(runtime, runtime_base=runtime_base, cell_count=5)

    monkeypatch.setenv("OPENAI_API_KEY", "unit-test-credential")
    monkeypatch.setattr(
        completion, "clean_repository_identity", lambda: ("a" * 40, "b" * 40),
    )
    now = datetime.now(timezone.utc)
    token = {
        "schema_version": "research-flow.wp016a-completion-live-token.v1",
        "wp_id": "WP-016A",
        "revision": "R08",
        "stage": "FIVE_CELL_COMPLETION",
        "decision": "GO_LIVE_COMPLETION_CANARY",
        "approved_commit": "a" * 40,
        "approved_tree": "b" * 40,
        "controller_sha256": completion.completion_controller_source_sha256(
            ROOT / "scripts" / "run_wp016a_completion.py"
        ),
        "completion_manifest_sha256": file_sha256(
            manifests / completion.R08_COMPLETION_MANIFEST
        ),
        "terminal_taxonomy_sha256": file_sha256(
            manifests / completion.R08_TERMINAL_TAXONOMY
        ),
        "attempt2_bridge_receipt_sha256": file_sha256(bridge_path),
        "combined_ledger_template_sha256": file_sha256(template),
        "provider_binding_sha256": file_sha256(
            manifests / completion.R08_PROVIDER_BINDING
        ),
        "budget_guard_sha256": file_sha256(
            manifests / completion.R08_BUDGET_GUARD
        ),
        "offline_replay_receipt_sha256": file_sha256(replay),
        "scientific_input_manifest_sha256": file_sha256(science),
        "environment_manifest_sha256": file_sha256(environment),
        "runtime_manifest_sha256": file_sha256(runtime),
        "literal_usd_cap": 0.5,
        "maximum_provider_requests": 20,
        "run_nonce": "wp016a-r08-unit-token-nonce-0001",
        "issued_at": (now - timedelta(minutes=1)).isoformat(),
        "expires_at": (now + timedelta(hours=1)).isoformat(),
        "issued_by": "GPT_PRO_UNIT_FIXTURE_NOT_AUTHORITY",
        "one_shot": True,
    }
    token_path = tmp_path / "token.json"
    _write(token_path, token)
    paths = {
        "controller_path": ROOT / "scripts" / "run_wp016a_completion.py",
        "manifest_dir": manifests,
        "attempt2_canary_result": source,
        "attempt2_bridge_path": bridge_path,
        "combined_ledger_template_path": template,
        "offline_replay_receipt_path": replay,
        "scientific_input_manifest": science,
        "environment_manifest": environment,
        "runtime_manifest_path": runtime,
        "runtime_base": runtime_base,
    }
    return token_path, paths, token


def test_r08_token_schema_is_completion_specific_and_exact_20() -> None:
    schema = json.loads(completion.COMPLETION_TOKEN_SCHEMA.read_text())
    props = schema["properties"]
    assert schema["$id"] == "research-flow.wp016a-completion-live-token.v1"
    assert props["revision"]["const"] == "R08"
    assert props["stage"]["const"] == "FIVE_CELL_COMPLETION"
    assert props["decision"]["const"] == "GO_LIVE_COMPLETION_CANARY"
    assert props["maximum_provider_requests"]["const"] == 20
    assert props["literal_usd_cap"]["const"] == 0.5


def test_r08_token_schema_rejects_24_and_six_cell_canary_shape(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _path, _paths, token = _token_fixture(tmp_path, monkeypatch)
    schema = json.loads(completion.COMPLETION_TOKEN_SCHEMA.read_text())
    token["maximum_provider_requests"] = 24
    errors = list(Draft202012Validator(schema).iter_errors(token))
    assert errors
    token["maximum_provider_requests"] = 20
    token["decision"] = "GO_LIVE_CANARY"
    token.pop("attempt2_bridge_receipt_sha256")
    assert list(Draft202012Validator(schema).iter_errors(token))


def test_completion_validator_binds_every_required_surface(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    token_path, paths, _token = _token_fixture(tmp_path, monkeypatch)
    value = completion.validate_completion_live_token(token_path, **paths)
    assert value["maximum_provider_requests"] == 20
    assert value["decision"] == "GO_LIVE_COMPLETION_CANARY"


@pytest.mark.parametrize(
    "target",
    ["bridge", "template", "manifest", "taxonomy", "budget", "replay"],
)
def test_completion_validator_rejects_each_bound_surface_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, target: str,
) -> None:
    token_path, paths, _token = _token_fixture(tmp_path, monkeypatch)
    selected = {
        "bridge": paths["attempt2_bridge_path"],
        "template": paths["combined_ledger_template_path"],
        "manifest": paths["manifest_dir"] / completion.R08_COMPLETION_MANIFEST,
        "taxonomy": paths["manifest_dir"] / completion.R08_TERMINAL_TAXONOMY,
        "budget": paths["manifest_dir"] / completion.R08_BUDGET_GUARD,
        "replay": paths["offline_replay_receipt_path"],
    }[target]
    value = json.loads(selected.read_text())
    value["r08_test_drift"] = True
    _write(selected, value)
    with pytest.raises(RuntimeError):
        completion.validate_completion_live_token(token_path, **paths)


def test_credential_drift_fails_before_runtime_or_consumption(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    token_path, paths, _token = _token_fixture(tmp_path, monkeypatch)
    monkeypatch.setenv("OPENAI_API_KEY", "drifted")
    with pytest.raises(RuntimeError, match="NO_GO_CREDENTIAL_IDENTITY_DRIFT"):
        completion.validate_completion_live_token(token_path, **paths)
    assert not paths["runtime_base"].exists()


def test_five_cell_runtime_root_and_one_shot_reuse_firewall(tmp_path: Path) -> None:
    token = {"run_nonce": "wp016a-r08-runtime-nonce-0001"}
    root = create_runtime_root(
        runtime_base=tmp_path / "runtime",
        run_nonce=token["run_nonce"],
        cell_bindings=completion.completion_identity_bindings(token["run_nonce"]),
        expected_cell_count=5,
    )
    assert len(list((root / "cells").iterdir())) == 5
    lease = consume_once(token=token, authority_root=tmp_path / "authority")
    lease.close()
    with pytest.raises(FileExistsError):
        consume_once(token=token, authority_root=tmp_path / "authority")


def test_completion_cli_cannot_enter_live_without_exact_token() -> None:
    with pytest.raises(SystemExit, match="R08_LIVE_REQUIRES_ALL_HASH_BOUND_INPUTS"):
        completion_cli.main(["--live-completion"])


def test_live_cli_dispatches_completion_not_six_cell_controller(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime_root = tmp_path / "runtime-root"
    for name in ("authority", "journal", "final"):
        (runtime_root / name).mkdir(parents=True, exist_ok=True)
    journal = DurableJournal(runtime_root)
    token = {"run_nonce": "wp016a-r08-live-dispatch-nonce"}
    calls: list[dict[str, Any]] = []

    monkeypatch.setattr(
        completion_cli, "validate_completion_live_token",
        lambda *_args, **_kwargs: token,
    )
    monkeypatch.setattr(
        completion_cli, "prepare_completion_live_runtime",
        lambda **_kwargs: (runtime_root, journal),
    )
    monkeypatch.setattr(
        completion_cli, "consume_once",
        lambda **_kwargs: AuthorityLease(
            "token-hash", token["run_nonce"], tmp_path / "consumed.json", -1,
        ),
    )

    async def fake_run_completion(**kwargs: Any) -> dict[str, Any]:
        calls.append(kwargs)
        return {
            "status": "PASS_COMPLETION_INTEGRATION",
            "mode": "LIVE_COMPLETION_CANARY",
            "configurations_replayed": 5,
            "provider_calls": 0,
            "backend_calls": 0,
        }

    monkeypatch.setattr(completion_cli, "run_completion", fake_run_completion)
    monkeypatch.setattr(
        completion_cli, "combine_completion_evidence",
        lambda _bridge, _result: {"status": "PASS_WP016A_INTEGRATION_COVERAGE"},
    )
    bridge = tmp_path / "bridge.json"
    _write(bridge, {})
    namespace = argparse.Namespace(**{
        "authority": tmp_path / "token.json",
        "manifest_dir": tmp_path / "manifests",
        "attempt2_canary_result": tmp_path / "source.json",
        "attempt2_bridge": bridge,
        "combined_ledger_template": tmp_path / "template.json",
        "offline_replay_receipt": tmp_path / "replay.json",
        "scientific_input_manifest": tmp_path / "science.json",
        "environment_manifest": tmp_path / "environment.json",
        "runtime_manifest": tmp_path / "runtime.json",
        "runtime_base": tmp_path / "runtime-base",
        "output_dir": None,
        "run_nonce": None,
    })
    assert completion_cli._run_live(namespace) == 0
    assert len(calls) == 1
    assert calls[0]["live"] is True
    assert calls[0]["run_nonce"] == token["run_nonce"]


def test_attempt2_bridge_v2_is_identity_complete_and_immutable() -> None:
    source = _attempt2_result()
    bridge = completion.attempt2_bridge_receipt(
        json.loads(source.read_text()), source_file_sha256=file_sha256(source),
    )
    assert bridge["schema_version"] == "wp016a.r08.attempt2-bridge-receipt.v2"
    assert bridge["original_status_rewritten"] is False
    assert bridge["source_commit"] == completion.ATTEMPT2_SOURCE_COMMIT
    assert bridge["source_tree"] == completion.ATTEMPT2_SOURCE_TREE
    for key in (
        "configuration_id", "cell_id", "session_id", "run_id", "log_id",
        "output_id",
    ):
        assert bridge[key].startswith("wp016a-")
    assert bridge["request_id_summary"]["request_count"] == 4
    assert bridge["request_id_summary"]["client_request_ids_unique"] == 4


def test_combined_uniqueness_covers_attempt2_and_all_five_completion_cells() -> None:
    source = _attempt2_result()
    bridge = completion.attempt2_bridge_receipt(
        json.loads(source.read_text()), source_file_sha256=file_sha256(source),
    )
    result = asyncio.run(completion.run_completion(
        live=False, scientific_input_manifest=_scientific_manifest(),
        run_nonce=completion.OFFLINE_REPLAY_NONCE,
    ))
    ledger = completion.combine_completion_evidence(bridge, result)
    assert ledger["status"] == "PASS_WP016A_INTEGRATION_COVERAGE"
    assert set(ledger["runtime_identity_unique_counts"].values()) == {6}
    collided = copy.deepcopy(result)
    collided["cells"][0]["cell_id"] = bridge["cell_id"]
    rejected = completion.combine_completion_evidence(bridge, collided)
    assert rejected["status"] == "NO_GO_WP016A_INTEGRATION_COVERAGE"
    assert rejected["runtime_identity_unique_counts"]["cell_id"] == 5


def test_combined_pass_does_not_require_proposal_commit() -> None:
    source = _attempt2_result()
    bridge = completion.attempt2_bridge_receipt(
        json.loads(source.read_text()), source_file_sha256=file_sha256(source),
    )
    result = asyncio.run(completion.run_completion(
        live=False, scientific_input_manifest=_scientific_manifest(),
        run_nonce=completion.OFFLINE_REPLAY_NONCE,
    ))
    result["cells"][0]["terminal"] = "TURN_LIMIT_EXHAUSTED"
    result["cells"][0]["terminal_origin"] = "FINITE_PROTOCOL_BUDGET"
    result["cells"][0]["proposal_outcome"] = "NO_PROPOSAL_TURN_LIMIT"
    assert completion.combine_completion_evidence(bridge, result)["status"] == (
        "PASS_WP016A_INTEGRATION_COVERAGE"
    )


def test_fixed_nonce_offline_outputs_are_byte_identical() -> None:
    source = _attempt2_result()
    bridge = completion.attempt2_bridge_receipt(
        json.loads(source.read_text()), source_file_sha256=file_sha256(source),
    )
    first = asyncio.run(completion.run_completion(
        live=False, scientific_input_manifest=_scientific_manifest(),
        run_nonce=completion.OFFLINE_REPLAY_NONCE,
    ))
    second = asyncio.run(completion.run_completion(
        live=False, scientific_input_manifest=_scientific_manifest(),
        run_nonce=completion.OFFLINE_REPLAY_NONCE,
    ))
    assert canonical_bytes(completion.offline_replay_receipt(first)) == canonical_bytes(
        completion.offline_replay_receipt(second)
    )
    assert canonical_bytes(completion.combine_completion_evidence(bridge, first)) == (
        canonical_bytes(completion.combine_completion_evidence(bridge, second))
    )


def test_r08_remains_zero_call_non_scientific_and_wp016b_blocked() -> None:
    manifest = completion.completion_manifest()
    assert manifest["provider_calls_at_r08"] == 0
    assert manifest["backend_calls_at_r08"] == 0
    assert manifest["scientific_analysis_eligible"] is False
    assert manifest["wp016b_authorised"] is False
    assert tuple(
        row["source_r8_configuration_id"] for row in manifest["cells"]
    ) == completion.COMPLETION_SOURCE_IDS
    assert completion.ATTEMPT2_G_LOW_SOURCE not in completion.COMPLETION_SOURCE_IDS
