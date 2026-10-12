from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import shutil

import pytest

from optimizer_pipeline.dof.multi_provider import PROFILES, parse_offline_provider_envelope
from optimizer_pipeline.dof.wp016_authority import (
    DurableJournal, consume_once, create_runtime_root,
)
from optimizer_pipeline.dof.wp016_canary import (
    load_source_rows, runtime_identity_bindings, validate_live_exchange,
)
from optimizer_pipeline.dof.wp016_inputs import (
    verify_scientific_input_manifest, write_environment_manifest,
    write_runtime_manifest, verify_environment_manifest, verify_runtime_manifest,
)


ROOT = Path(__file__).resolve().parents[1]


def _token(nonce: str = "wp016a-g0r1-unit-nonce-0001") -> dict[str, object]:
    return {
        "schema_version": "research-flow.wp016a-live-activation-token.v2",
        "run_nonce": nonce,
        "one_shot": True,
    }


def _capsule_manifest() -> Path:
    value = os.environ.get("WP016A_TEST_SCIENTIFIC_INPUT_MANIFEST")
    if not value:
        pytest.skip("external WP-016A replay capsule not supplied")
    return Path(value)


def test_atomic_one_shot_concurrent_consumption(tmp_path: Path) -> None:
    authority = tmp_path / "authority"

    def attempt() -> str:
        try:
            lease = consume_once(token=_token(), authority_root=authority)
        except FileExistsError:
            return "REJECTED_ALREADY_CONSUMED"
        lease.close()
        return "CONSUMED"

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(lambda _index: attempt(), range(2)))
    assert sorted(outcomes) == ["CONSUMED", "REJECTED_ALREADY_CONSUMED"]
    receipt = next(authority.glob("consumed__*.json"))
    assert json.loads(receipt.read_text())["status"] == "CONSUMED_STARTING"


def test_crash_after_consumption_cannot_reuse_token(tmp_path: Path) -> None:
    authority = tmp_path / "authority"
    lease = consume_once(token=_token(), authority_root=authority)
    # Closing the process lock models process death; the fsynced tombstone is
    # intentionally never rolled back.
    lease.close()
    with pytest.raises(FileExistsError):
        consume_once(token=_token(), authority_root=authority)


def test_nonce_derived_six_cell_runtime_and_collision(tmp_path: Path) -> None:
    nonce = "wp016a-g0r1-unit-nonce-0002"
    rows = load_source_rows()
    bindings = runtime_identity_bindings(rows, nonce)
    cells = [row["cell_id"] for row in bindings]
    root = create_runtime_root(runtime_base=tmp_path, run_nonce=nonce,
                               cell_bindings=bindings)
    assert len(list((root / "cells").iterdir())) == 6
    assert all((root / "cells" / cell / "session").is_dir() for cell in cells)
    with pytest.raises(FileExistsError):
        create_runtime_root(runtime_base=tmp_path, run_nonce=nonce,
                            cell_bindings=bindings)


def test_durable_journal_is_write_ahead_and_terminal(tmp_path: Path) -> None:
    nonce = "wp016a-g0r1-unit-nonce-0003"
    bindings = [{kind + "_id": f"{kind}-{index}" for kind in
                 ("configuration", "cell", "session", "run", "log", "output")}
                for index in range(6)]
    root = create_runtime_root(runtime_base=tmp_path, run_nonce=nonce,
                               cell_bindings=bindings)
    journal = DurableJournal(root)
    first = journal.append(kind="request-ready", value={"status": "READY_TO_SEND"})
    terminal = journal.terminal(status="NO_GO_TEST", value={"backend_calls": 0})
    assert json.loads(first.read_text())["sequence"] == 1
    assert json.loads(terminal.read_text())["status"] == "NO_GO_TEST"
    with pytest.raises(FileExistsError):
        journal.terminal(status="PASS", value={})


def test_scientific_capsule_hash_drift_is_rejected(tmp_path: Path) -> None:
    source = _capsule_manifest().parent
    target = tmp_path / "capsule"
    shutil.copytree(source, target)
    manifest = target / "scientific-input-manifest.json"
    roles = verify_scientific_input_manifest(manifest)
    roles["real_observation"].write_text("{}\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="scientific_input_hash_drift"):
        verify_scientific_input_manifest(manifest)


def test_environment_and_runtime_manifest_drift_fail_closed(tmp_path: Path) -> None:
    environment = tmp_path / "environment.json"
    write_environment_manifest(environment, repository_root=ROOT)
    verify_environment_manifest(environment, repository_root=ROOT)
    changed = json.loads(environment.read_text())
    changed["platform"]["machine"] = "drifted"
    environment.write_text(json.dumps(changed), encoding="utf-8")
    with pytest.raises(RuntimeError, match="environment_manifest_drift"):
        verify_environment_manifest(environment, repository_root=ROOT)

    runtime = tmp_path / "runtime.json"
    runtime_base = tmp_path / "runtime-base"
    write_runtime_manifest(runtime, runtime_base=runtime_base)
    verify_runtime_manifest(runtime, runtime_base=runtime_base)
    with pytest.raises(RuntimeError, match="runtime_manifest_drift"):
        verify_runtime_manifest(runtime, runtime_base=tmp_path / "another-base")


def test_legacy_ambiguous_cache_hit_fields_are_absent() -> None:
    source = (ROOT / "optimizer_pipeline/dof/wp016_canary.py").read_text()
    assert '"cache_hits"' not in source
    assert '"dedup_hits"' not in source


def test_openai_cache_write_usage_is_parsed_and_drift_rejected() -> None:
    payload = {
        "id": "req-cache-drift", "model": "gpt-5.6-luna",
        "choices": [{"message": {"content": "{}"}, "finish_reason": "stop"}],
        "usage": {
            "prompt_tokens": 10, "completion_tokens": 2,
            "prompt_tokens_details": {"cached_tokens": 0, "cache_write_tokens": 3},
            "completion_tokens_details": {"reasoning_tokens": 1},
        },
    }
    _response, exchange = parse_offline_provider_envelope(
        PROFILES["openai/gpt-5.6-luna"],
        json.dumps(payload).encode(), request_body={"test": True},
    )
    assert exchange["native_usage"]["cache_write_input_tokens"] == 3
    with pytest.raises(RuntimeError, match="NO_GO_PROVIDER_CACHE_POLICY_DRIFT"):
        validate_live_exchange(exchange, canonical_request_body_bytes=100)


def test_live_request_id_is_mandatory_without_offline_fallback() -> None:
    exchange = {
        "native_usage": {
            "cached_input_tokens": 0, "cache_write_input_tokens": 0,
        },
        "returned_service_tier": "default",
        "openai_x_request_id": None,
    }
    with pytest.raises(RuntimeError, match="NO_GO_OPENAI_X_REQUEST_ID_MISSING"):
        validate_live_exchange(exchange, canonical_request_body_bytes=100)
    source = (ROOT / "optimizer_pipeline/dof/wp016_canary.py").read_text()
    assert '"client_request_id": request_client_id' in source
    assert '"openai_x_request_id": actual_request_id' in source
