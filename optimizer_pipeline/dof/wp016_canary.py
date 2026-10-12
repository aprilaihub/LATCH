"""WP-016A six-cell integration canary with a hard live-authority firewall.

The default and Gate-0 path is fully offline.  The live transport can only be
reached after a schema-valid token is bound to the clean repository state and
every controller/manifest receipt reviewed at Gate 0.
"""

from __future__ import annotations

import asyncio
import copy
import csv
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import inspect
import json
import os
from pathlib import Path
import subprocess
import sys
from typing import Any, Callable, Mapping, Sequence

from jsonschema import Draft202012Validator

from .compact_session import CompactSessionAdapter
from .multi_provider import (
    MAX_CANONICAL_INPUT_BYTES,
    MAX_OUTPUT_TOKENS,
    PROFILES,
    ProviderInferenceContractError,
    StrictMultiProviderTransport,
    compile_request_body,
    parse_offline_provider_envelope,
)
from .single_submission.language import CompileContext
from .single_submission.observation_v4 import CommonQuerySurfaceV4
from .single_submission.planner_v2 import SystemBinding, result_public
from .wp016_authority import DurableJournal
from .wp016_inputs import (
    verify_environment_manifest,
    verify_runtime_manifest,
    verify_scientific_input_manifest,
)
from .wp016_outcomes import (
    IntegrationStatus, PROPOSAL_TERMINALS, classify_terminal,
    shared_failure_origin,
)
from .wp016 import (
    COMPACT_TERMINAL_STATUSES,
    RetrievalBudgetState,
    canonical_bytes,
    make_retrieval_registry,
    proposal_boundary_certificate_v3,
    sha256,
)


WP_ID = "WP-016A"
MODEL = "openai/gpt-5.6-luna"
LITERAL_USD_CAP = Decimal("0.50")
REQUEST_CAP = 24
PER_REQUEST_OUTPUT_CAP = 4_096
SESSION_OUTPUT_CAP = 16_384
TURN_CAP = 4
MAX_CANONICAL_REQUEST_BODY_BYTES = 79_000
# Backwards-compatible name for the proposal-level success/NVS terminals.  It
# is deliberately no longer the integration-completeness predicate.
VALID_CANARY_TERMINALS = PROPOSAL_TERMINALS
ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))
CONFIG_ROOT = ROOT / "configs" / "wp016a"
SOURCE_CONFIGS = CONFIG_ROOT / "source-configurations.csv"
SOURCE_PAYLOADS = CONFIG_ROOT / "source-payloads"
OFFLINE_FIXTURES = CONFIG_ROOT / "offline-provider-shaped-fixtures.json"
TOKEN_SCHEMA = CONFIG_ROOT / "authority" / "WP016A_LIVE_ACTIVATION_TOKEN_V2.schema.json"
CURRENT_PRICING = CONFIG_ROOT / "openai-current-pricing-cache-audit.json"
BLOCK_ORDER = (
    ("G", "COMPACT_RETRIEVAL_LOW_RESERVE"),
    ("G", "LEGACY_STATIC"),
    ("G", "COMPACT_RETRIEVAL_MATCHED_RESERVE"),
    ("P", "LEGACY_STATIC"),
    ("P", "COMPACT_RETRIEVAL_LOW_RESERVE"),
    ("P", "COMPACT_RETRIEVAL_MATCHED_RESERVE"),
)
BACKEND_MODULE_FRAGMENTS = ("vivado", "rapidwright", "scorer", "validation_scheduler")


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


@dataclass(frozen=True, slots=True)
class ScientificInputs:
    manifest_path: Path
    manifest_sha256: str
    scientific_bundle: Path
    capture_root: Path
    hardblock_scan: Path
    retrieval_registry: Path


def load_scientific_inputs(manifest_path: Path) -> ScientificInputs:
    roles = verify_scientific_input_manifest(manifest_path)
    real = roles["real_observation"]
    capture_root = real.parents[2]
    return ScientificInputs(
        manifest_path=manifest_path.resolve(strict=True),
        manifest_sha256=file_sha256(manifest_path),
        scientific_bundle=roles["scientific_bundle_subset"],
        capture_root=capture_root,
        hardblock_scan=roles["hardblock_scan_aggregate"].parent,
        retrieval_registry=roles["closed_retrieval_registry"],
    )


def git(*args: str) -> str:
    return subprocess.check_output(["git", *args], cwd=ROOT, text=True).strip()


def clean_repository_identity() -> tuple[str, str]:
    if git("status", "--porcelain"):
        raise RuntimeError("live_authority_requires_clean_worktree")
    return git("rev-parse", "HEAD"), git("rev-parse", "HEAD^{tree}")


def controller_source_sha256(entrypoint: Path | None = None) -> str:
    paths = [
        entrypoint or ROOT / "scripts" / "run_wp016a_canary.py",
        Path(__file__).resolve(),
        ROOT / "optimizer_pipeline" / "dof" / "wp016.py",
        ROOT / "optimizer_pipeline" / "dof" / "compact_session.py",
        ROOT / "optimizer_pipeline" / "dof" / "multi_provider.py",
        ROOT / "optimizer_pipeline" / "dof" / "wp016_authority.py",
        ROOT / "optimizer_pipeline" / "dof" / "wp016_inputs.py",
    ]
    return sha256({str(path.relative_to(ROOT)): file_sha256(path) for path in paths})


def load_source_rows() -> list[dict[str, str]]:
    with SOURCE_CONFIGS.open(encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    if len(rows) != 6:
        raise ValueError("exactly_six_source_configurations_required")
    keyed = {(row["capability_task"], row["arm"]): row for row in rows}
    if set(keyed) != set(BLOCK_ORDER):
        raise ValueError("six_configuration_block_mismatch")
    ordered = [keyed[key] for key in BLOCK_ORDER]
    for row in ordered:
        if row["root_id"] != "transfer_boom_soc_v2" or row["model_system"] != MODEL:
            raise ValueError("frozen_root_or_model_mismatch")
        if int(row["turn_limit"]) != TURN_CAP or int(row["per_request_output_cap"]) != PER_REQUEST_OUTPUT_CAP:
            raise ValueError("frozen_turn_or_output_cap_mismatch")
        path = SOURCE_PAYLOADS / f"{row['configuration_id']}.json"
        if not path.is_file() or file_sha256(path) != row["serialized_file_sha256"]:
            raise ValueError("source_payload_hash_mismatch:" + row["configuration_id"])
        body = read_json(path)
        if sha256(body) != row["canonical_payload_sha256"]:
            raise ValueError("canonical_source_payload_hash_mismatch:" + row["configuration_id"])
    return ordered


def wp016_identity(kind: str, source_id: str, run_nonce: str) -> str:
    return f"wp016a-{kind}__" + sha256({
        "wp": WP_ID, "source": source_id, "kind": kind, "run_nonce": run_nonce,
    })[:24]


def runtime_identity_bindings(rows: Sequence[Mapping[str, Any]],
                              run_nonce: str) -> list[dict[str, str]]:
    return [
        {
            kind + "_id": wp016_identity(kind, str(row["configuration_id"]), run_nonce)
            for kind in ("configuration", "cell", "session", "run", "log", "output")
        }
        for row in rows
    ]


def client_request_id(run_nonce: str, cell_id: str, turn: int) -> str:
    value = "wp016a-client__" + sha256({
        "run_nonce": run_nonce, "cell_id": cell_id, "turn": turn,
    })
    value.encode("ascii")
    if len(value) > 512:
        raise RuntimeError("client_request_id_length_invalid")
    return value


def credential_identity_sha256(secret: str) -> str:
    if not secret:
        raise RuntimeError("NO_GO_CREDENTIAL_MISSING")
    return hashlib.sha256(
        b"WP016A_OPENAI_CREDENTIAL_V1\0" + secret.encode("utf-8")
    ).hexdigest()


def canary_manifest() -> dict[str, Any]:
    cells = []
    for position, row in enumerate(load_source_rows(), start=1):
        source = row["configuration_id"]
        cells.append({
            "block_position": position,
            "source_r8_configuration_id": source,
            "identity_templates": {
                kind: f"wp016a-{kind}__sha256(wp,source,kind,token.run_nonce)[:24]"
                for kind in ("configuration", "cell", "session", "run", "log", "output")
            },
            "runtime_path_templates": {
                "cell": "cells/<cell_id>",
                "session": "cells/<cell_id>/session/<session_id>",
                "run": "cells/<cell_id>/run/<run_id>",
                "log": "cells/<cell_id>/log/<log_id>",
                "output": "cells/<cell_id>/output/cell-result.json",
            },
            "root_id": row["root_id"],
            "model_system": row["model_system"],
            "capability": row["capability_task"],
            "arm": row["arm"],
            "active_dynamic_capacity_bytes": int(row["active_dynamic_capacity_bytes"]),
            "source_payload_sha256": row["serialized_file_sha256"],
        })
    value = {
        "schema_version": "wp016a.canary-manifest.v2",
        "scientific_analysis_eligible": False,
        "must_rerun_in_wp016b": True,
        "literal_usd_cap": float(LITERAL_USD_CAP),
        "maximum_provider_requests": REQUEST_CAP,
        "identity_nonce_source": "hash_bound_activation_token.run_nonce",
        "canonical_configuration_identity_kind": "configuration",
        "cells": cells,
    }
    return value | {"manifest_sha256": sha256(value)}


def provider_binding_receipt(*, credential: str | None = None) -> dict[str, Any]:
    profile = PROFILES[MODEL]
    secret = os.environ.get(profile.credential_env, "") if credential is None else credential
    pricing_audit = read_json(CURRENT_PRICING)
    pricing = pricing_audit["standard_short_context_usd_per_million_tokens"]
    value = {
        "schema_version": "wp016a.provider-binding.v2",
        "canonical_model_id": profile.canonical_model_id,
        "api_model_id": profile.api_model_id,
        "provider": profile.provider,
        "route": profile.route,
        "base_url_hash": sha256(profile.base_url.encode()),
        "endpoint": profile.endpoint,
        "credential_env_name": profile.credential_env,
        "credential_identity_sha256": credential_identity_sha256(secret),
        "credential_identity_private_metadata": True,
        "adapter_version": profile.adapter_version,
        "provider_guard_bytes": MAX_CANONICAL_INPUT_BYTES,
        "per_request_output_tokens": MAX_OUTPUT_TOKENS,
        "pricing_snapshot_id": "wp016a-openai-current-pricing-2026-08-20",
        "pricing_snapshot_sha256": file_sha256(CURRENT_PRICING),
        "input_usd_per_million_tokens": pricing["input"],
        "output_usd_per_million_tokens": pricing["output"],
        "retry_count": 0,
        "fallback_count": 0,
        "repair_count": 0,
        "application_cache": False,
        "payload_response_deduplication": False,
        "prior_response_reuse": False,
        "prompt_cache_mode": "EXPLICIT_NO_BREAKPOINTS",
        "explicit_cache_breakpoints": [],
        "service_tier": "default",
        "expected_cached_input_tokens": 0,
        "expected_cache_write_tokens": 0,
    }
    return value | {"receipt_sha256": sha256(value)}


def validate_credential_binding(provider_binding_path: Path, *,
                                environment: Mapping[str, str] | None = None) -> str:
    receipt = read_json(provider_binding_path)
    claimed = receipt.pop("receipt_sha256", None)
    if claimed != sha256(receipt):
        raise RuntimeError("provider_binding_receipt_invalid")
    source = os.environ if environment is None else environment
    key_name = str(receipt.get("credential_env_name"))
    secret = source.get(key_name, "")
    if not secret:
        raise RuntimeError("NO_GO_CREDENTIAL_MISSING")
    observed = credential_identity_sha256(secret)
    if observed != receipt.get("credential_identity_sha256"):
        raise RuntimeError("NO_GO_CREDENTIAL_IDENTITY_DRIFT")
    return observed


def conservative_next_request_usd(
    canonical_request_body_bytes: int = MAX_CANONICAL_REQUEST_BODY_BYTES,
) -> Decimal:
    if not 0 <= canonical_request_body_bytes <= MAX_CANONICAL_REQUEST_BODY_BYTES:
        raise RuntimeError("canonical_request_body_cap_exceeded")
    pricing = read_json(CURRENT_PRICING)["standard_short_context_usd_per_million_tokens"]
    # One canonical request-body UTF-8 byte per input token is the budget-only
    # conservative bound.  Scientific context treatment remains 65,536 bytes.
    input_cost = Decimal(canonical_request_body_bytes) * Decimal(str(pricing["input"])) / Decimal(1_000_000)
    output_cost = Decimal(MAX_OUTPUT_TOKENS) * Decimal(str(pricing["output"])) / Decimal(1_000_000)
    return input_cost + output_cost


def current_openai_derived_cost(
    usage: Mapping[str, Any], *, model: str = MODEL,
) -> Decimal:
    """Price one OpenAI exchange.

    WP-016A keeps its reviewed Luna-only pricing receipt.  Later WP-016
    continuations may explicitly select another frozen OpenAI model and then
    use the effective-dated WP-015 pricing authority.  The default path is
    deliberately byte-for-byte compatible with R10.
    """
    if model != MODEL:
        from .wp015_provider import derive_cost_usd

        cost, _window = derive_cost_usd(model, usage)
        return cost
    pricing_document = read_json(CURRENT_PRICING)
    pricing = pricing_document["standard_short_context_usd_per_million_tokens"]
    input_multiplier = output_multiplier = Decimal(1)
    if int(usage["input_tokens"]) > int(pricing_document["long_context_threshold_tokens"]):
        input_multiplier = Decimal(2)
        output_multiplier = Decimal("1.5")
    million = Decimal(1_000_000)
    inputs = (
        Decimal(int(usage["uncached_input_tokens"])) * Decimal(str(pricing["input"]))
        + Decimal(int(usage["cached_input_tokens"])) * Decimal(str(pricing["cached_input"]))
        + Decimal(int(usage["cache_write_input_tokens"])) * Decimal(str(pricing["cache_write"]))
    ) * input_multiplier
    outputs = (
        Decimal(int(usage["output_tokens"])) * Decimal(str(pricing["output"]))
        * output_multiplier
    )
    return (inputs + outputs) / million


def validate_live_exchange(
    exchange: Mapping[str, Any], *, canonical_request_body_bytes: int,
) -> tuple[dict[str, Any], str]:
    native_usage = dict(exchange.get("native_usage") or {})
    cached_tokens = int(native_usage.get("cached_input_tokens", 0))
    cache_write_tokens = int(native_usage.get("cache_write_input_tokens", 0))
    if cached_tokens != 0 or cache_write_tokens != 0:
        raise RuntimeError("NO_GO_PROVIDER_CACHE_POLICY_DRIFT")
    if exchange.get("returned_service_tier") != "default":
        raise RuntimeError("NO_GO_OPENAI_SERVICE_TIER_MISMATCH")
    request_id = exchange.get("openai_x_request_id")
    if not isinstance(request_id, str) or not request_id.strip():
        raise RuntimeError("NO_GO_OPENAI_X_REQUEST_ID_MISSING")
    if int(native_usage.get("input_tokens", -1)) > canonical_request_body_bytes:
        raise RuntimeError("NO_GO_PROVIDER_USAGE_EXCEEDS_CONSERVATIVE_BOUND")
    return native_usage, request_id


def budget_guard_receipt(maximum_provider_requests: int = REQUEST_CAP) -> dict[str, Any]:
    if not 1 <= maximum_provider_requests <= REQUEST_CAP:
        raise ValueError("maximum_provider_requests_out_of_range")
    value = {
        "schema_version": "wp016a.literal-budget-guard.v2",
        "literal_usd_cap": str(LITERAL_USD_CAP),
        "maximum_provider_requests": maximum_provider_requests,
        "scientific_prompt_context_guard_bytes": MAX_CANONICAL_INPUT_BYTES,
        "maximum_canonical_request_body_bytes": MAX_CANONICAL_REQUEST_BODY_BYTES,
        "budget_input_token_bound": MAX_CANONICAL_REQUEST_BODY_BYTES,
        "output_token_bound_per_request": MAX_OUTPUT_TOKENS,
        "conservative_next_request_usd": str(conservative_next_request_usd()),
        "pre_request_rule": (
            "known_charged_so_far + conservative_unknown_cost_liability "
            "+ exact_body_byte_bound <= 0.50"
        ),
        "all_request_bound_usd": str(
            conservative_next_request_usd() * maximum_provider_requests
        ),
        "prompt_cache_mode": "EXPLICIT_NO_BREAKPOINTS",
        "service_tier": "default",
        "cache_write_guard_if_reenabled": "must_use_cache_write_rate_and_obtain_new_authority_if_24_request_bound_exceeds_cap",
        "guard_function_sha256": sha256(inspect.getsource(conservative_next_request_usd).encode()),
    }
    return value | {"receipt_sha256": sha256(value)}


def assert_no_backend_imports() -> None:
    # Frozen construction-session modules define backend helpers transitively,
    # but this controller must have no direct import or dispatcher wiring to
    # them.  Audit the controller source itself; actual call counters remain
    # hard-coded and independently checked in every result.
    source = Path(__file__).read_text(encoding="utf-8")
    import_lines = [line.strip().lower() for line in source.splitlines()
                    if line.lstrip().startswith(("import ", "from "))]
    offenders = sorted(line for line in import_lines
                       if any(fragment in line for fragment in BACKEND_MODULE_FRAGMENTS))
    if offenders:
        raise RuntimeError("backend_import_wired:" + ",".join(offenders))


def _source_job(reference: str, new_job_id: str,
                inputs: ScientificInputs) -> tuple[dict[str, Any], dict[str, Any]]:
    bundle = read_json(inputs.scientific_bundle)
    source = next(row for row in bundle["jobs"] if row["job_id"] == reference)
    job = dict(source) | {"job_id": new_job_id, "model": source["model_key"], "study_track": source["track"]}
    return bundle, job


def _frozen_session(row: Mapping[str, Any], *, inputs: ScientificInputs,
                    run_nonce: str, new_identity: bool = True):
    import run_wp015_cells as wp015

    base = wp015.base

    wp015.configure()
    wp015.configure_wp015b_hardblock_compatibility(inputs.hardblock_scan)
    source_id = str(row["configuration_id"])
    model = str(row.get("model_system") or MODEL)
    job_id = (wp016_identity("job", source_id, run_nonce) if new_identity
              else str(row["configuration_job_id"]))
    bundle, job = _source_job(str(row["historical_reference_job_id"]), job_id, inputs)
    token = wp015.legacy.CURRENT_JOB.set(job)
    try:
        observation, private = base._load_trial(bundle, inputs.capture_root, str(job["root_id"]))
        contract = base._language_contract(str(job["level"]), tuple(job["admitted_action_keys"]))
        binding = SystemBinding.create(
            observation_hash=observation.sha256,
            root_binding=bundle["roots"][job["root_id"]]["sha256"],
            catalogue_version=f"dof.wp013a.{str(job['level']).lower()}.v2",
            catalogue_hash=wp015.digest(contract),
            provider="openai", model=model, route="direct", session_nonce=job_id,
        )
        context = CompileContext(
            private["registry"], "ROUTED", private["predefined_regions"], private["geometry_bounds"],
            tuple(key.removeprefix("macro.") for key in job["admitted_action_keys"]),
            fanout_admitted=bool(private.get("fanout_admitted", False)),
            hardblock_compatibility_relation=private.get("hardblock_compatibility_relation"),
        )
        session = wp015.WP015Session(
            level=job["level"], observation=observation,
            queries=CommonQuerySurfaceV4(private["registry"]), compile_context=context,
            binding=binding, query_provider=base._query_provider(private),
        )
    finally:
        wp015.legacy.CURRENT_JOB.reset(token)
    return session, context, contract, observation, job, wp015


def historical_first_turn_parity(row: Mapping[str, Any], *, inputs: ScientificInputs) -> dict[str, Any]:
    session, _context, contract, observation, job, wp015 = _frozen_session(
        row, inputs=inputs, run_nonce="historical-parity", new_identity=False,
    )
    base = wp015.base
    token = wp015.legacy.CURRENT_JOB.set(job)
    try:
        prompt = base._prompt(job["level"])
        context = base._planner_context(observation=observation, session=session, contract=contract, last_result=None)
        model = str(row.get("model_system") or MODEL)
        binding = base._binding(
            job=job, model=model, turn=0, context=context, prompt=prompt,
            catalogue_hash=wp015.digest(contract), reserve=float(LITERAL_USD_CAP),
        )
        binding = replace(binding, settings=replace(binding.settings, max_tokens=PER_REQUEST_OUTPUT_CAP))
        body = _wp016_request_body(PROFILES[model], binding)
    finally:
        wp015.legacy.CURRENT_JOB.reset(token)
    explicit = row.get("serialized_payload_path")
    expected_path = Path(str(explicit)) if explicit else SOURCE_PAYLOADS / f"{row['configuration_id']}.json"
    expected = read_json(expected_path)
    return {
        "source_configuration_id": row["configuration_id"],
        "prompt_bytes": len(prompt), "prompt_hash": hashlib.sha256(prompt).hexdigest(),
        "context_bytes": len(context), "context_hash": hashlib.sha256(context).hexdigest(),
        "canonical_payload_sha256": sha256(body),
        "expected_payload_sha256": sha256(expected),
        "historical_transport_payload_sha256": sha256(expected),
        "transport_policy_normalized": body != expected,
        "exact_model_visible_prompt_context_parity": body.get("messages") == expected.get("messages"),
        "prompt_cache_mode": body.get("prompt_cache_options"),
        "service_tier": body.get("service_tier"),
    }


def _compact_body(initial: Mapping[str, Any], history: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    body = copy.deepcopy(dict(initial))
    body["prompt_cache_options"] = {"mode": "explicit"}
    body["service_tier"] = "default"
    if not history:
        return body
    system = str(initial["messages"][0]["content"])
    initial_context = str(initial["messages"][1]["content"])
    context = canonical_bytes({
        "schema_version": "wp016a.compact-turn-context.v1",
        "initial_context": json.loads(initial_context),
        "interaction_history": list(history),
        "qor_feedback_present": False,
    }).decode("utf-8")
    body["messages"] = [{"role": "system", "content": system}, {"role": "user", "content": context}]
    return body


def _message_bytes(body: Mapping[str, Any]) -> tuple[bytes, bytes]:
    messages = body.get("messages")
    if not isinstance(messages, list) or len(messages) != 2:
        raise ValueError("canonical_two_message_body_required")
    if messages[0].get("role") != "system" or messages[1].get("role") != "user":
        raise ValueError("canonical_system_user_order_required")
    return str(messages[0]["content"]).encode(), str(messages[1]["content"]).encode()


def _wp016_request_body(profile: Any, binding: Any) -> dict[str, Any]:
    return compile_request_body(
        profile, binding, explicit_no_prompt_cache=True,
    )


@dataclass(slots=True)
class LiteralBudget:
    charged: Decimal = Decimal("0")
    requests: int = 0
    unknown_cost_attempts: int = 0
    conservative_unknown_cost_liability: Decimal = Decimal("0")
    request_cap: int = REQUEST_CAP
    literal_usd_cap: Decimal = LITERAL_USD_CAP
    request_bounder: Callable[[Mapping[str, Any], str], Decimal] | None = None
    pending_reservations: dict[str, Decimal] = field(default_factory=dict)

    def guard(self, prompt: bytes, context: bytes,
              request_body: Mapping[str, Any], *, model: str = MODEL) -> tuple[int, Decimal]:
        if self.requests + len(self.pending_reservations) >= self.request_cap:
            raise RuntimeError("request_cap_exhausted")
        if len(prompt) + len(context) > MAX_CANONICAL_INPUT_BYTES:
            raise RuntimeError("canonical_provider_guard_exceeded")
        body_bytes = len(canonical_bytes(request_body))
        if body_bytes > MAX_CANONICAL_REQUEST_BODY_BYTES:
            raise RuntimeError("canonical_request_body_cap_exceeded")
        next_bound = (
            self.request_bounder(request_body, model)
            if self.request_bounder is not None
            else conservative_next_request_usd(body_bytes)
        )
        reserved = sum(self.pending_reservations.values(), Decimal("0"))
        if (
            self.charged + self.conservative_unknown_cost_liability
            + reserved + next_bound > self.literal_usd_cap
        ):
            raise RuntimeError("literal_usd_cap_pre_request_stop")
        return body_bytes, next_bound

    def reserve(self, reservation_id: str, bound: Decimal) -> None:
        if not reservation_id or reservation_id in self.pending_reservations:
            raise RuntimeError("request_budget_reservation_identity_invalid")
        if bound < 0:
            raise RuntimeError("negative_request_budget_reservation")
        if self.requests + len(self.pending_reservations) >= self.request_cap:
            raise RuntimeError("request_cap_exhausted")
        reserved = sum(self.pending_reservations.values(), Decimal("0"))
        if (
            self.charged + self.conservative_unknown_cost_liability
            + reserved + bound > self.literal_usd_cap
        ):
            raise RuntimeError("literal_usd_cap_pre_request_stop")
        self.pending_reservations[reservation_id] = bound

    def cancel_reservation(self, reservation_id: str) -> None:
        if self.pending_reservations.pop(reservation_id, None) is None:
            raise RuntimeError("request_budget_reservation_missing")

    def _settle_reservation(self, reservation_id: str | None) -> Decimal | None:
        if reservation_id is None:
            return None
        bound = self.pending_reservations.pop(reservation_id, None)
        if bound is None:
            raise RuntimeError("request_budget_reservation_missing")
        return bound

    def record(self, actual: Decimal, *, reservation_id: str | None = None) -> None:
        if actual < 0:
            raise RuntimeError("negative_provider_charge")
        reserved_bound = self._settle_reservation(reservation_id)
        if reserved_bound is not None and actual > reserved_bound:
            raise RuntimeError("provider_charge_exceeds_reserved_liability")
        self.requests += 1
        self.charged += actual
        if (
            self.requests > self.request_cap
            or self.charged + self.conservative_unknown_cost_liability > self.literal_usd_cap
        ):
            raise RuntimeError("literal_budget_invariant_breached")

    def record_unknown_attempt(self, liability: Decimal,
                               *, reservation_id: str | None = None) -> None:
        reserved_bound = self._settle_reservation(reservation_id)
        if reserved_bound is not None and liability > reserved_bound:
            raise RuntimeError("unknown_liability_exceeds_reserved_liability")
        self.requests += 1
        self.unknown_cost_attempts += 1
        self.conservative_unknown_cost_liability += liability
        if (
            self.requests > self.request_cap
            or self.charged + self.conservative_unknown_cost_liability > self.literal_usd_cap
        ):
            raise RuntimeError("literal_budget_invariant_breached")


@dataclass(slots=True)
class ConcurrentRequestTracker:
    """Record real request overlap without treating the configured cap as observed."""

    cap: int
    inflight: int = 0
    measured_peak: int = 0
    active: dict[str, tuple[str, int]] = field(default_factory=dict)
    events: list[dict[str, Any]] = field(default_factory=list)

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()

    def start(self, *, request_id: str, cell_id: str, turn: int) -> dict[str, Any]:
        if request_id in self.active:
            raise RuntimeError("cross_cell_request_identity_reuse")
        if self.inflight >= self.cap:
            raise RuntimeError("measured_request_concurrency_cap_exceeded")
        self.inflight += 1
        self.measured_peak = max(self.measured_peak, self.inflight)
        self.active[request_id] = (cell_id, turn)
        value = {
            "request_send_timestamp_utc": self._now(),
            "measured_inflight_at_send": self.inflight,
        }
        self.events.append({
            "event": "REQUEST_SENT", "request_id": request_id,
            "cell_id": cell_id, "turn": turn, **value,
        })
        return value

    def finish(self, *, request_id: str, cell_id: str, turn: int,
               started: Mapping[str, Any]) -> dict[str, Any]:
        if self.active.pop(request_id, None) != (cell_id, turn):
            raise RuntimeError("cross_cell_request_completion_mismatch")
        inflight_at_receive = self.inflight
        self.inflight -= 1
        received = self._now()
        sent = datetime.fromisoformat(str(started["request_send_timestamp_utc"]))
        latency = (datetime.fromisoformat(received) - sent).total_seconds()
        value = {
            "response_receive_timestamp_utc": received,
            "measured_inflight_at_receive": inflight_at_receive,
            "measured_inflight_after_receive": self.inflight,
            "request_latency_seconds": latency,
        }
        self.events.append({
            "event": "RESPONSE_RECEIVED", "request_id": request_id,
            "cell_id": cell_id, "turn": turn, **value,
        })
        return value


@dataclass(slots=True)
class CanaryController:
    registry: Mapping[str, Any]
    inputs: ScientificInputs
    run_nonce: str
    journal: DurableJournal | None = None
    budget: LiteralBudget = field(default_factory=LiteralBudget)
    current_cell_id: str | None = None
    current_turn: int | None = None
    last_provider_turn: int | None = None
    last_client_request_id: str | None = None
    last_openai_x_request_id: str | None = None
    pre_request_guard: Callable[[], None] | None = None
    request_tracker: ConcurrentRequestTracker | None = None

    @staticmethod
    def _model(row: Mapping[str, Any]) -> str:
        return str(row.get("model_system") or MODEL)

    @staticmethod
    def _payload_path(row: Mapping[str, Any]) -> Path:
        explicit = row.get("serialized_payload_path")
        return Path(str(explicit)) if explicit else SOURCE_PAYLOADS / f"{row['configuration_id']}.json"

    def _binding(self, *, row: Mapping[str, Any], job: Mapping[str, Any], wp015: Any,
                 prompt: bytes, context: bytes, turn: int, contract: Mapping[str, Any]):
        token = wp015.legacy.CURRENT_JOB.set(job)
        try:
            binding = wp015._binding(
                job=job, model=self._model(row), turn=turn - 1, context=context, prompt=prompt,
                catalogue_hash=wp015.digest(contract), reserve=float(self.budget.literal_usd_cap),
            )
            return replace(binding, settings=replace(binding.settings, max_tokens=PER_REQUEST_OUTPUT_CAP))
        finally:
            wp015.legacy.CURRENT_JOB.reset(token)

    async def run_cell(self, *, row: Mapping[str, Any], canned: Sequence[bytes] | None,
                       live: bool) -> dict[str, Any]:
        source_id = str(row["configuration_id"])
        overrides = row.get("identity_overrides")
        if overrides is None:
            identities = {
                kind + "_id": wp016_identity(kind, source_id, self.run_nonce)
                for kind in ("configuration", "cell", "session", "run", "log", "output")
            }
        else:
            identities = dict(overrides)
            required = {
                kind + "_id" for kind in
                ("configuration", "cell", "session", "run", "log", "output")
            }
            if set(identities) != required or any(
                not isinstance(value, str) or not value for value in identities.values()
            ):
                raise RuntimeError("runtime_identity_overrides_invalid")
        self.current_cell_id = identities["cell_id"]
        session, compile_context, contract, observation, job, wp015 = _frozen_session(
            row, inputs=self.inputs, run_nonce=self.run_nonce,
        )
        base = wp015.base
        compact = str(row["arm"]).startswith("COMPACT")
        compact_adapter = CompactSessionAdapter(
            state=RetrievalBudgetState(
                registry=self.registry,
                capability_mask=("M", "P") if row["capability_task"] == "P" else ("M", "P", "G"),
                query_results={}, reserve_bytes=int(row["active_dynamic_capacity_bytes"]),
                history_cap_bytes=int(row["history_limit_bytes"]),
                query_response_cap_bytes=int(row["retrieval_result_limit_bytes"]),
                max_turns=TURN_CAP, max_queries=int(row["query_limit"]),
                per_request_output_cap=PER_REQUEST_OUTPUT_CAP,
                session_output_cap=SESSION_OUTPUT_CAP,
            ), frozen_session=session,
        ) if compact else None
        initial = read_json(self._payload_path(row))
        history: list[dict[str, Any]] = []
        last_result: Mapping[str, Any] | None = None
        receipts: list[dict[str, Any]] = []
        output_total = 0
        terminal = None
        candidate = None
        context_limit_evidence: dict[str, Any] | None = None
        model = self._model(row)
        profile = PROFILES[model]
        for turn in range(1, TURN_CAP + 1):
            if terminal is not None or session.terminal is not None:
                break
            if self.pre_request_guard is not None:
                self.pre_request_guard()
            self.current_turn = turn
            try:
                if compact:
                    body = _compact_body(initial, history)
                    request_previous_result = list(history)
                else:
                    request_previous_result = last_result
                    token = wp015.legacy.CURRENT_JOB.set(job)
                    try:
                        prompt = base._prompt(job["level"])
                        context = base._planner_context(
                            observation=observation, session=session, contract=contract,
                            last_result=last_result,
                        )
                    finally:
                        wp015.legacy.CURRENT_JOB.reset(token)
                    binding = self._binding(
                        row=row, job=job, wp015=wp015, prompt=prompt,
                        context=context, turn=turn, contract=contract,
                    )
                    body = _wp016_request_body(profile, binding)
                prompt, context = _message_bytes(body)
                binding = self._binding(
                    row=row, job=job, wp015=wp015, prompt=prompt,
                    context=context, turn=turn, contract=contract,
                )
                if _wp016_request_body(profile, binding) != body:
                    raise RuntimeError("provider_binding_body_drift")
            except ProviderInferenceContractError as exc:
                last_cell_provider_turn = (
                    int(receipts[-1]["turn"]) if receipts else None
                )
                if (
                    exc.contract_code != "CANONICAL_INPUT_LIMIT_EXCEEDED"
                    or turn <= 1
                    or last_cell_provider_turn != turn - 1
                ):
                    raise
                terminal = "CONTEXT_LIMIT_AFTER_MODEL_TURN"
                context_limit_evidence = {
                    **dict(exc.evidence),
                    "contract_code": exc.contract_code,
                    "attempted_planning_turn": turn,
                    "last_provider_turn": last_cell_provider_turn,
                    "blocked_turn_request_identity_created": False,
                    "blocked_turn_provider_request_issued": False,
                }
                if self.journal is not None:
                    self.journal.append(kind="typed-context-limit", value={
                        "status": terminal,
                        "cell_id": identities["cell_id"],
                        **context_limit_evidence,
                    })
                break
            body_bytes, next_request_bound = self.budget.guard(
                prompt, context, body, model=model,
            )
            request_hash = sha256(body)
            request_client_id = client_request_id(self.run_nonce, identities["cell_id"], turn)
            self.budget.reserve(request_client_id, next_request_bound)
            self.last_client_request_id = request_client_id
            request_safe = {
                "status": "READY_TO_SEND" if live else "OFFLINE_READY_TO_PARSE",
                "cell_id": identities["cell_id"],
                "turn": turn,
                "client_request_id": request_client_id,
                "request_plan": {
                    "model": model, "route": "openai_direct",
                    "output_cap": PER_REQUEST_OUTPUT_CAP,
                },
                "prompt_sha256": hashlib.sha256(prompt).hexdigest(),
                "context_sha256": hashlib.sha256(context).hexdigest(),
                "scientific_guard_input_bytes": len(prompt) + len(context),
                "canonical_request_body_bytes": body_bytes,
                "budget_input_token_bound": body_bytes,
                "transport_payload_sha256": request_hash,
                "conservative_next_request_usd": str(next_request_bound),
                "charged_so_far_usd": str(self.budget.charged),
                "unknown_liability_so_far_usd": str(
                    self.budget.conservative_unknown_cost_liability
                ),
                "reserved_liability_so_far_usd": str(sum(
                    self.budget.pending_reservations.values(), Decimal("0")
                )),
                "requests_so_far": self.budget.requests,
                "authority_and_run_nonce_verified": live,
                "raw_private_reasoning_persisted": False,
            }
            if self.journal is not None:
                self.journal.request_receipt(
                    cell_id=identities["cell_id"], turn=turn, value=request_safe,
                )
                self.journal.append(kind="request-ready", value=request_safe)
            if live:
                if self.journal is None:
                    raise RuntimeError("live_durable_journal_required")
                key = os.environ.get(profile.credential_env, "")
                transport = StrictMultiProviderTransport(
                    profile=profile, api_key=key, explicit_no_prompt_cache=True,
                    client_request_id=request_client_id,
                    expected_service_tier="default",
                    require_openai_x_request_id=True,
                )
                request_timing = (
                    self.request_tracker.start(
                        request_id=request_client_id,
                        cell_id=identities["cell_id"], turn=turn,
                    ) if self.request_tracker is not None else {
                        "request_send_timestamp_utc": datetime.now(timezone.utc).isoformat(),
                        "measured_inflight_at_send": 1,
                    }
                )
                if self.journal is not None:
                    self.journal.append(kind="request-sent", value={
                        "cell_id": identities["cell_id"], "turn": turn,
                        "client_request_id": request_client_id, **request_timing,
                    })
                try:
                    response = await transport.send(binding, 0)
                except Exception as exc:
                    response_timing = (
                        self.request_tracker.finish(
                            request_id=request_client_id,
                            cell_id=identities["cell_id"], turn=turn,
                            started=request_timing,
                        ) if self.request_tracker is not None else {
                            "response_receive_timestamp_utc": datetime.now(timezone.utc).isoformat(),
                            "measured_inflight_at_receive": 1,
                            "measured_inflight_after_receive": 0,
                            "request_latency_seconds": None,
                        }
                    )
                    if transport.physical_requests == 1:
                        self.last_provider_turn = turn
                        self.budget.record_unknown_attempt(
                            next_request_bound, reservation_id=request_client_id,
                        )
                    else:
                        self.budget.cancel_reservation(request_client_id)
                    exchange = dict(transport.last_exchange or {})
                    self.last_openai_x_request_id = exchange.get("openai_x_request_id")
                    failure_safe = {
                        "status": "NO_GO_PROVIDER_OR_ADAPTER_FAILURE",
                        "cell_id": identities["cell_id"],
                        "turn": turn, "failure_type": type(exc).__name__,
                        "client_request_id": request_client_id,
                        "openai_x_request_id": self.last_openai_x_request_id,
                        "response_object_id": exchange.get("response_object_id"),
                        "safe_transport_evidence": exchange,
                        "physical_requests": transport.physical_requests,
                        "cumulative_physical_requests": self.budget.requests,
                        "charged_cost_status": "UNKNOWN_IF_USAGE_UNAVAILABLE",
                        "conservative_unknown_cost_liability": str(
                            self.budget.conservative_unknown_cost_liability
                        ),
                        "raw_private_reasoning_persisted": False,
                        **request_timing, **response_timing,
                    }
                    self.journal.response_receipt(
                        cell_id=identities["cell_id"], turn=turn, value=failure_safe,
                    )
                    self.journal.append(kind="request-failure", value=failure_safe)
                    raise
                exchange = dict(transport.last_exchange or {})
                response_timing = (
                    self.request_tracker.finish(
                        request_id=request_client_id,
                        cell_id=identities["cell_id"], turn=turn,
                        started=request_timing,
                    ) if self.request_tracker is not None else {
                        "response_receive_timestamp_utc": datetime.now(timezone.utc).isoformat(),
                        "measured_inflight_at_receive": 1,
                        "measured_inflight_after_receive": 0,
                        "request_latency_seconds": None,
                    }
                )
                if transport.physical_requests != 1:
                    raise RuntimeError("physical_request_count_not_one")
                self.last_openai_x_request_id = exchange.get("openai_x_request_id")
            else:
                request_timing = {
                    "request_send_timestamp_utc": None,
                    "measured_inflight_at_send": 0,
                }
                response_timing = {
                    "response_receive_timestamp_utc": None,
                    "measured_inflight_at_receive": 0,
                    "measured_inflight_after_receive": 0,
                    "request_latency_seconds": 0.0,
                }
                if canned is None or turn > len(canned):
                    raise RuntimeError("offline_fixture_sequence_exhausted")
                response, exchange = parse_offline_provider_envelope(
                    profile, canned[turn - 1], request_body=body,
                    expected_service_tier="default", client_request_id=request_client_id,
                )
            self.last_provider_turn = turn
            native_usage = dict(exchange.get("native_usage") or {})
            derived_cost = current_openai_derived_cost(native_usage, model=model)
            provider_reported = exchange.get("provider_reported_cost_usd")
            provider_cost = Decimal(str(provider_reported)) if provider_reported is not None else None
            actual = (provider_cost if provider_cost is not None else derived_cost) if live else Decimal("0")
            cost_source = (
                "provider_reported_native" if provider_cost is not None
                else (
                    "derived_wp016a_current_pricing" if model == MODEL
                    else "derived_wp015_frozen_pricing"
                )
            ) if live else "offline_canned_not_billed"
            if model == MODEL:
                pricing_snapshot_id = "wp016a-openai-current-pricing-2026-08-20"
                pricing_snapshot_path = CURRENT_PRICING
            else:
                from .wp015_provider import PRICING_PATH, PRICING_SNAPSHOT_ID

                pricing_snapshot_id = PRICING_SNAPSHOT_ID
                pricing_snapshot_path = PRICING_PATH
            response_safe = {
                    "status": (
                        "RAW_AND_STRICT_RESPONSE_RECEIVED" if live
                        else "OFFLINE_CANNED_RESPONSE_PARSED"
                    ),
                    "cell_id": identities["cell_id"],
                    "turn": turn,
                    "client_request_id": request_client_id,
                    "openai_x_request_id": exchange.get("openai_x_request_id"),
                    "response_object_id": exchange.get("response_object_id"),
                    "returned_service_tier": exchange.get("returned_service_tier"),
                    "system_fingerprint": exchange.get("system_fingerprint"),
                    "safe_transport_evidence": exchange,
                    "public_final_text_utf8": response.content_bytes.decode(
                        "utf-8", errors="strict",
                    ),
                    "public_final_text_sha256": hashlib.sha256(
                        response.content_bytes,
                    ).hexdigest(),
                    "provider_reported_cost_usd": provider_reported,
                    "derived_current_cost_usd": str(derived_cost),
                    "derived_wp016a_current_cost_usd": (
                        str(derived_cost) if model == MODEL else None
                    ),
                    "cost_source": cost_source,
                    "raw_private_reasoning_persisted": False,
                    **request_timing, **response_timing,
                }
            if self.journal is not None:
                self.journal.response_receipt(
                    cell_id=identities["cell_id"], turn=turn, value=response_safe,
                )
                self.journal.append(kind="response-received", value=response_safe)
            self.budget.record(actual, reservation_id=request_client_id)
            if response.returned_model_id != model or response.returned_model_revision != "openai_direct":
                raise RuntimeError("provider_identity_mismatch")
            cached_tokens = int(native_usage.get("cached_input_tokens", 0))
            cache_write_tokens = int(native_usage.get("cache_write_input_tokens", 0))
            actual_request_id = exchange.get("openai_x_request_id")
            if live:
                assert self.journal is not None
                try:
                    native_usage, actual_request_id = validate_live_exchange(
                        exchange, canonical_request_body_bytes=body_bytes,
                    )
                except RuntimeError as exc:
                    self.journal.append(kind="live-policy-failure", value={
                        "status": str(exc),
                        "cell_id": wp016_identity("cell", source_id, self.run_nonce),
                        "turn": turn,
                        "client_request_id": request_client_id,
                        "openai_x_request_id": actual_request_id,
                        "cached_input_tokens": cached_tokens,
                        "cache_write_input_tokens": cache_write_tokens,
                    })
                    raise
            else:
                if exchange.get("returned_service_tier") != "default":
                    raise RuntimeError("NO_GO_OPENAI_SERVICE_TIER_MISMATCH")
                if int(native_usage.get("input_tokens", -1)) > body_bytes:
                    raise RuntimeError("NO_GO_PROVIDER_USAGE_EXCEEDS_CONSERVATIVE_BOUND")
            output_tokens = int(response.output_tokens or 0)
            output_total += output_tokens
            public: Mapping[str, Any]
            accounting: Mapping[str, Any]
            endpoint: Any = None
            if output_tokens > PER_REQUEST_OUTPUT_CAP or output_total > SESSION_OUTPUT_CAP:
                terminal = "OUTPUT_LIMIT_EXHAUSTED"
                public = {"status": terminal}
                accounting = {}
            elif exchange.get("final_text_truncated"):
                terminal = "TRUNCATED_RESPONSE"
                public = {"status": terminal}
                accounting = {}
            else:
                try:
                    endpoint = json.loads(response.content_bytes.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    endpoint = None
                if not isinstance(endpoint, Mapping):
                    terminal = "MODEL_CONTENT_JSON_INVALID"
                    public = {"status": terminal}
                    accounting = (
                        compact_adapter.accounting() if compact_adapter is not None else {
                            "retrieval_bytes": 0,
                            "query_result_bytes": 0,
                            "schema_feedback_bytes": 0,
                            "history_bytes": sum(
                                len(canonical_bytes(item)) for item in session.transcript
                            ),
                            "semantic_spent_bytes": 0,
                            "semantic_remaining_bytes": int(
                                row["active_dynamic_capacity_bytes"]
                            ),
                        }
                    )
                else:
                    token = wp015.legacy.CURRENT_JOB.set(job)
                    try:
                        if compact:
                            assert compact_adapter is not None
                            started = compact_adapter.begin_turn(endpoint, output_tokens=output_tokens)
                            public = compact_adapter.dispatch(endpoint) if started["status"] == "TURN_STARTED" else started
                            result = None
                        else:
                            result = session.accept(endpoint)
                            public = result_public(result)
                    finally:
                        wp015.legacy.CURRENT_JOB.reset(token)
                    if compact:
                        if public["status"] == "COMMIT_RECEIVED":
                            terminal, candidate = "COMMIT_RECEIVED", endpoint["program"]
                        elif public["status"] == "NO_VALID_SUBMISSION_RECEIVED":
                            terminal = "NO_VALID_SUBMISSION_RECEIVED"
                        elif public["status"] in COMPACT_TERMINAL_STATUSES:
                            terminal = str(public["status"])
                        accounting = compact_adapter.accounting()
                        history.append({"model_request": endpoint, "controller_response": public})
                    else:
                        assert result is not None
                        last_result = public
                        if result.status == "COMMITTED":
                            terminal, candidate = "COMMIT_RECEIVED", endpoint["candidate"]
                        elif result.status == "NO_VALID_SUBMISSION" or session.terminal == "NO_VALID_SUBMISSION":
                            terminal = "NO_VALID_SUBMISSION_RECEIVED"
                        accounting = {
                            "retrieval_bytes": 0,
                            "query_result_bytes": len(canonical_bytes(public.get("query_batch", {}))),
                            "schema_feedback_bytes": len(canonical_bytes(public.get("feedback", []))),
                            "history_bytes": sum(len(canonical_bytes(item)) for item in session.transcript),
                            "semantic_spent_bytes": 0,
                            "semantic_remaining_bytes": int(row["active_dynamic_capacity_bytes"]),
                        }
            receipts.append({
                "cell_id": identities["cell_id"],
                "session_id": identities["session_id"],
                "arm": row["arm"], "capability": row["capability_task"], "turn": turn,
                "client_request_id": request_client_id,
                "openai_x_request_id": actual_request_id,
                "response_object_id": exchange.get("response_object_id"),
                "offline_request_id": (
                    None if live else wp016_identity(
                        f"offline-request-{turn}", source_id, self.run_nonce,
                    )
                ),
                "prompt_bytes": len(prompt), "prompt_hash": hashlib.sha256(prompt).hexdigest(),
                "context_bytes": len(context), "context_hash": hashlib.sha256(context).hexdigest(),
                "scientific_guard_input_bytes": len(prompt) + len(context),
                "canonical_guard_input_bytes": len(prompt) + len(context),
                "guard_headroom_bytes": MAX_CANONICAL_INPUT_BYTES - len(prompt) - len(context),
                "canonical_request_body_bytes": body_bytes,
                "budget_input_token_bound": body_bytes,
                "transport_payload_bytes": body_bytes,
                "transport_payload_hash": request_hash,
                "request_construction": (
                    "compact_retrieval_state_context" if compact
                    else "historical_prompt_planner_context_previous_result"
                ),
                "previous_result_present": bool(request_previous_result),
                "previous_result_hash": (
                    sha256(request_previous_result) if request_previous_result is not None
                    and bool(request_previous_result) else None
                ),
                **accounting,
                "native_input_tokens": response.input_tokens,
                "input_tokens": response.input_tokens, "output_tokens": output_tokens,
                "cumulative_output_tokens": output_total,
                "finish_reason": exchange.get("finish_reason"),
                "returned_model": response.returned_model_id,
                "returned_route": response.returned_model_revision,
                "response_hash": exchange.get("response_sha256"),
                "public_final_text_utf8": response.content_bytes.decode(
                    "utf-8", errors="strict",
                ),
                "public_final_text_sha256": hashlib.sha256(
                    response.content_bytes,
                ).hexdigest(),
                "parsed_public_endpoint": dict(endpoint) if isinstance(endpoint, Mapping) else None,
                "controller_public_response": dict(public),
                "strict_parser_status": (
                    "PARSED_JSON_OBJECT" if isinstance(endpoint, Mapping)
                    else "MODEL_CONTENT_JSON_INVALID"
                ),
                "returned_service_tier": exchange.get("returned_service_tier"),
                "system_fingerprint": exchange.get("system_fingerprint"),
                "charged_cost_usd": str(actual),
                "provider_reported_cost_usd": provider_reported,
                "derived_cost_usd": str(derived_cost),
                "cost_source": cost_source,
                "pricing_snapshot_id": pricing_snapshot_id,
                "pricing_snapshot_sha256": file_sha256(pricing_snapshot_path),
                "application_response_cache": False,
                "provider_prompt_cache_read_tokens": cached_tokens,
                "provider_prompt_cache_write_tokens": cache_write_tokens,
                "payload_response_deduplication": False,
                "prior_response_reuse": False,
                "canonical_event": public.get("status"), "terminal": terminal,
                "frontier_semantics": public.get("frontier_semantics"),
                "requested_content_bytes_charged": public.get(
                    "requested_content_bytes_charged"
                ),
                "frontier_metadata_bytes": public.get("frontier_metadata_bytes"),
                **request_timing, **response_timing,
            })
            if self.journal is not None:
                self.journal.append(kind="request-complete", value={
                    "status": "RESPONSE_PARSED",
                    "cell_id": identities["cell_id"],
                    "turn": turn, "http_status": exchange.get("http_status"),
                    "client_request_id": request_client_id,
                    "openai_x_request_id": actual_request_id,
                    "response_object_id": exchange.get("response_object_id"),
                    "response_sha256": exchange.get("response_sha256"),
                    "finish_reason": exchange.get("finish_reason"),
                    "returned_model": response.returned_model_id,
                    "native_usage": native_usage,
                    "provider_reported_cost_usd": provider_reported,
                    "derived_cost_usd": str(derived_cost),
                    "cost_source": cost_source,
                    "parser_schema_outcome": public.get("status"),
                    "public_final_text_sha256": hashlib.sha256(
                        response.content_bytes,
                    ).hexdigest(),
                    "parsed_public_endpoint": dict(endpoint) if isinstance(endpoint, Mapping) else None,
                    "controller_public_response": dict(public),
                    "cell_terminal": terminal,
                })
        if terminal is None:
            terminal = "TURN_LIMIT_EXHAUSTED"
        outcome = classify_terminal(terminal)
        certificate = None
        if candidate is not None and session.committed is not None:
            certificate = proposal_boundary_certificate_v3(candidate, session.committed)
        return {
            "schema_version": "wp016a.integration-cell.v3",
            "source_r8_configuration_id": source_id,
            **identities,
            "root_id": row["root_id"], "model_system": model,
            "capability": row["capability_task"], "arm": row["arm"],
            "terminal": terminal, **outcome.public(),
            "terminal_origin_evidence": {
                "classifier_version": "wp016a.terminal-outcome-taxonomy.v1",
                "terminal_observed_without_rewrite": True,
            },
            "request_count": len(receipts), "turns": receipts,
            "proposal_boundary_certificate_v3": certificate,
            "provider_calls": len(receipts) if live else 0,
            "backend_calls": 0,
            "known_charged_cost_usd": str(sum(
                (Decimal(str(turn["charged_cost_usd"])) for turn in receipts),
                Decimal("0"),
            )),
            "unknown_cost_attempts": 0,
            "request_ids": [{
                "client_request_id": turn["client_request_id"],
                "openai_x_request_id": turn["openai_x_request_id"],
                "response_object_id": turn["response_object_id"],
            } for turn in receipts],
            "guard_maxima": {
                "scientific_guard_input_bytes": max(
                    (turn["scientific_guard_input_bytes"] for turn in receipts), default=0,
                ),
                "canonical_request_body_bytes": max(
                    (turn["canonical_request_body_bytes"] for turn in receipts), default=0,
                ),
            },
            "service_tier_evidence": {
                "requested": "default",
                "returned": [turn["returned_service_tier"] for turn in receipts],
            },
            "cache_evidence": {
                "application_response_cache": False,
                "provider_prompt_cache_read_tokens": sum(turn["provider_prompt_cache_read_tokens"] for turn in receipts),
                "provider_prompt_cache_write_tokens": sum(turn["provider_prompt_cache_write_tokens"] for turn in receipts),
                "payload_response_deduplication": False,
                "prior_response_reuse": False,
                "evidence_source": "native_usage_and_frozen_transport_policy",
            },
            "compact_adapter_evidence": None if compact_adapter is None else {
                "response_validation_count": compact_adapter.response_validation_count,
                "semantic_events": compact_adapter.semantic_events,
                "uses_retrieval_budget_state": True,
            },
            "context_limit_evidence": context_limit_evidence,
        }


def _load_fixture_sequences() -> Mapping[str, Sequence[bytes]]:
    fixture = read_json(OFFLINE_FIXTURES)
    claimed = fixture.pop("fixture_set_sha256")
    if sha256(fixture) != claimed:
        raise ValueError("offline_fixture_set_hash_mismatch")
    return {
        key: [canonical_bytes(item) for item in values]
        for key, values in fixture["responses_by_configuration"].items()
    }


def _failure_status(exc: BaseException) -> str:
    if isinstance(exc, ProviderInferenceContractError):
        return "NO_GO_CONTROLLER_OR_ADAPTER"
    message = str(exc)
    if message.startswith("NO_GO_"):
        return message.split(":", 1)[0]
    lowered = message.lower()
    if any(fragment in lowered for fragment in (
        "provider", "transport", "http", "cache", "service_tier",
        "request_id", "model_identity",
    )):
        return "NO_GO_PROVIDER_OR_TRANSPORT"
    if any(fragment in lowered for fragment in (
        "authority", "token", "credential", "environment", "manifest",
        "runtime", "worktree",
    )):
        return "NO_GO_AUTHORITY_OR_ENVIRONMENT"
    return "NO_GO_CONTROLLER_OR_ADAPTER"


def _aggregate_result(*, live: bool, controller: CanaryController,
                      inputs: ScientificInputs, parity: Sequence[Mapping[str, Any]],
                      cells: Sequence[Mapping[str, Any]], status: str,
                      stop_reason: str | None = None,
                      expected_cells: int = 6,
                      result_kind: str = "CANARY") -> dict[str, Any]:
    success_status = (
        "PASS_CANARY_INTEGRATION"
        if result_kind == "CANARY" else "PASS_COMPLETION_INTEGRATION"
    )
    integration_complete = [
        cell for cell in cells
        if cell.get("integration_status")
        == IntegrationStatus.COMPLETE_TYPED_CELL_OUTCOME.value
    ]
    failed = next(
        (cell for cell in cells if cell.get("shared_integration_defect") is True),
        None,
    )
    turns = [turn for cell in cells for turn in cell["turns"]]
    failed_turn = (
        failed["turns"][-1]["turn"]
        if failed is not None and failed.get("turns") else
        controller.current_turn if status != success_status else None
    )
    return {
        "schema_version": (
            ("wp016a.controller-replay.v5" if not live else "wp016a.live-canary.v5")
            if result_kind == "CANARY"
            else (
                "wp016a.completion-controller-replay.v1"
                if not live else "wp016a.live-completion-canary.v1"
            )
        ),
        "status": status,
        "mode": (
            ("LIVE_CANARY" if live else "OFFLINE_CANNED_ONLY")
            if result_kind == "CANARY"
            else ("LIVE_" if live else "OFFLINE_") + result_kind
        ),
        "integration_status": (
            "COMPLETE_INTEGRATION_COVERAGE"
            if status == success_status
            else "SHARED_INTEGRATION_FAILURE"
        ),
        "run_nonce_sha256": sha256(controller.run_nonce.encode()),
        "scientific_input_manifest_sha256": inputs.manifest_sha256,
        "cells": list(cells),
        "legacy_first_turn_parity": list(parity),
        "configurations_replayed": len(cells),
        "completed_cell_ids_and_terminals": [
            {"cell_id": cell["cell_id"], "terminal": cell["terminal"]}
            for cell in integration_complete
        ],
        "proposal_outcome_counts": {
            outcome: sum(cell.get("proposal_outcome") == outcome for cell in cells)
            for outcome in sorted({str(cell.get("proposal_outcome")) for cell in cells})
        },
        "failed_cell_id": failed["cell_id"] if failed is not None else controller.current_cell_id if status != success_status else None,
        "failed_turn": failed_turn,
        "last_provider_turn": controller.last_provider_turn,
        "failed_on_controller_transition_after_turn": (
            failed_turn if failed is not None else None
        ),
        "next_planning_turn_if_continued": (
            failed_turn + 1
            if isinstance(failed_turn, int) and failed_turn < TURN_CAP else None
        ),
        "later_cells_not_executed": expected_cells - len(cells),
        "stop_reason": stop_reason,
        "provider_calls": controller.budget.requests if live else 0,
        "offline_parsed_requests": controller.budget.requests if not live else 0,
        "requests_issued": controller.budget.requests,
        "backend_calls": 0,
        "charged_cost_usd": str(controller.budget.charged),
        "known_charged_cost_usd": str(controller.budget.charged),
        "unknown_cost_attempts": controller.budget.unknown_cost_attempts,
        "conservative_unknown_cost_liability": str(
            controller.budget.conservative_unknown_cost_liability
        ),
        "last_client_request_id": controller.last_client_request_id,
        "last_openai_x_request_id": controller.last_openai_x_request_id,
        "maximum_guard_usage_bytes": max(
            (turn["scientific_guard_input_bytes"] for turn in turns), default=0,
        ),
        "maximum_canonical_request_body_bytes": max(
            (turn["canonical_request_body_bytes"] for turn in turns), default=0,
        ),
        "unique_identity_counts": {
            key: len({cell[key] for cell in cells})
            for key in ("configuration_id", "cell_id", "session_id", "run_id", "log_id", "output_id")
        },
        "one_shot_remains_consumed_when_live": live,
        "unresolved_shared_integration_defect": (
            status != success_status
        ),
        "scientific_analysis_eligible": False,
    }


async def _run_rows(*, live: bool, scientific_input_manifest: Path,
                    rows: Sequence[Mapping[str, Any]], expected_cells: int,
                    request_cap: int, result_kind: str,
                    run_nonce: str,
                    journal: DurableJournal | None = None,
                    fixture_sequences: Mapping[str, Sequence[bytes]] | None = None) -> dict[str, Any]:
    assert_no_backend_imports()
    inputs = load_scientific_inputs(scientific_input_manifest)
    rows = list(rows)
    if len(rows) != expected_cells or len({str(row["configuration_id"]) for row in rows}) != expected_cells:
        raise RuntimeError("completion_row_inventory_invalid")
    if request_cap != expected_cells * TURN_CAP:
        raise RuntimeError("request_cap_not_exact_cell_turn_product")
    registry = read_json(inputs.retrieval_registry)
    controller = CanaryController(
        registry=registry, inputs=inputs, run_nonce=run_nonce, journal=journal,
        budget=LiteralBudget(request_cap=request_cap),
    )
    fixtures = None if live else (
        _load_fixture_sequences() if fixture_sequences is None else fixture_sequences
    )
    parity = [historical_first_turn_parity(row, inputs=inputs)
              for row in rows if row["arm"] == "LEGACY_STATIC"]
    if not all(row["exact_model_visible_prompt_context_parity"] for row in parity):
        raise RuntimeError("historical_legacy_first_turn_parity_failed")
    cells = []
    for row in rows:
        source = row["configuration_id"]
        try:
            cell = await controller.run_cell(
                row=row, canned=None if fixtures is None else fixtures[source], live=live,
            )
        except Exception as exc:
            status = _failure_status(exc)
            result = _aggregate_result(
                live=live, controller=controller, inputs=inputs, parity=parity,
                cells=cells, status=status, stop_reason=str(exc),
                expected_cells=expected_cells, result_kind=result_kind,
            )
            result["terminal_origin"] = shared_failure_origin(status).value
            if journal is not None:
                journal.append(kind="partial-run-failure", value=result)
            return result
        cells.append(cell)
        if journal is not None:
            journal.cell_result(cell_id=cell["cell_id"], value=cell)
        if cell["shared_integration_defect"]:
            status = "NO_GO_SHARED_INTEGRATION_FAILURE_" + str(cell["terminal"])
            result = _aggregate_result(
                live=live, controller=controller, inputs=inputs, parity=parity,
                cells=cells, status=status, stop_reason=str(cell["terminal"]),
                expected_cells=expected_cells, result_kind=result_kind,
            )
            if journal is not None:
                journal.append(kind="partial-run-shared-integration-failure", value=result)
            return result
    assert_no_backend_imports()
    if (
        len(cells) != expected_cells
        or any(cell["shared_integration_defect"] for cell in cells)
        or controller.budget.requests > request_cap
        or controller.budget.charged > LITERAL_USD_CAP
        or controller.budget.conservative_unknown_cost_liability != 0
    ):
        return _aggregate_result(
            live=live, controller=controller, inputs=inputs, parity=parity,
            cells=cells, status="NO_GO_CANARY_AGGREGATE_INVARIANT",
            stop_reason="aggregate_invariant",
            expected_cells=expected_cells, result_kind=result_kind,
        )
    final_status = (
        "PASS_CANARY_INTEGRATION"
        if result_kind == "CANARY" else "PASS_COMPLETION_INTEGRATION"
    )
    return _aggregate_result(
        live=live, controller=controller, inputs=inputs, parity=parity,
        cells=cells, status=final_status,
        expected_cells=expected_cells, result_kind=result_kind,
    )


async def run_all(*, live: bool, scientific_input_manifest: Path,
                  run_nonce: str = "offline-g0r2-replay",
                  journal: DurableJournal | None = None,
                  fixture_sequences: Mapping[str, Sequence[bytes]] | None = None) -> dict[str, Any]:
    return await _run_rows(
        live=live,
        scientific_input_manifest=scientific_input_manifest,
        rows=load_source_rows(),
        expected_cells=6,
        request_cap=REQUEST_CAP,
        result_kind="CANARY",
        run_nonce=run_nonce,
        journal=journal,
        fixture_sequences=fixture_sequences,
    )


def materialize_static_manifests(output: Path) -> dict[str, str]:
    values = {
        "canary-manifest.json": canary_manifest(),
        "provider-binding.json": provider_binding_receipt(),
        "budget-guard.json": budget_guard_receipt(),
    }
    for name, value in values.items():
        write_json(output / name, value)
    return {name: file_sha256(output / name) for name in values}


def validate_live_token(token_path: Path, replay_path: Path, manifest_dir: Path,
                        controller_path: Path, *, scientific_input_manifest: Path,
                        environment_manifest: Path, runtime_manifest_path: Path,
                        runtime_base: Path) -> dict[str, Any]:
    token, schema = read_json(token_path), read_json(TOKEN_SCHEMA)
    errors = sorted(error.message for error in Draft202012Validator(schema).iter_errors(token))
    if errors:
        raise RuntimeError("activation_token_schema_invalid:" + "|".join(errors))
    try:
        issued = datetime.fromisoformat(str(token["issued_at"]).replace("Z", "+00:00"))
        expires = datetime.fromisoformat(str(token["expires_at"]).replace("Z", "+00:00"))
    except (KeyError, ValueError) as exc:
        raise RuntimeError("activation_token_time_invalid") from exc
    now = datetime.now(timezone.utc)
    if issued.tzinfo is None or expires.tzinfo is None or not issued <= now < expires:
        raise RuntimeError("activation_token_expired_or_not_yet_valid")
    commit, tree = clean_repository_identity()
    verify_scientific_input_manifest(scientific_input_manifest)
    verify_environment_manifest(environment_manifest, repository_root=ROOT)
    verify_runtime_manifest(runtime_manifest_path, runtime_base=runtime_base)
    expected = {
        "approved_commit": commit,
        "approved_tree": tree,
        "controller_sha256": controller_source_sha256(controller_path),
        "canary_manifest_sha256": file_sha256(manifest_dir / "canary-manifest.json"),
        "provider_binding_sha256": file_sha256(manifest_dir / "provider-binding.json"),
        "budget_guard_sha256": file_sha256(manifest_dir / "budget-guard.json"),
        "offline_replay_receipt_sha256": file_sha256(replay_path),
        "scientific_input_manifest_sha256": file_sha256(scientific_input_manifest),
        "environment_manifest_sha256": file_sha256(environment_manifest),
        "runtime_manifest_sha256": file_sha256(runtime_manifest_path),
        "literal_usd_cap": float(LITERAL_USD_CAP),
        "maximum_provider_requests": REQUEST_CAP,
    }
    mismatch = {key: {"expected": value, "observed": token.get(key)}
                for key, value in expected.items() if token.get(key) != value}
    if mismatch:
        raise RuntimeError("activation_token_hash_mismatch:" + json.dumps(mismatch, sort_keys=True))
    return token


__all__ = [
    "CanaryController", "LiteralBudget", "MAX_CANONICAL_REQUEST_BODY_BYTES",
    "VALID_CANARY_TERMINALS", "budget_guard_receipt", "canary_manifest",
    "client_request_id", "conservative_next_request_usd",
    "credential_identity_sha256", "current_openai_derived_cost",
    "validate_credential_binding", "validate_live_exchange",
    "controller_source_sha256", "historical_first_turn_parity", "materialize_static_manifests",
    "provider_binding_receipt", "run_all", "runtime_identity_bindings",
    "validate_live_token", "wp016_identity",
]
