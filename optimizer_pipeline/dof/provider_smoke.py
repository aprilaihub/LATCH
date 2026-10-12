"""Frozen synthetic-context provider smoke with zero backend dispatch."""

from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path
from typing import Any, Mapping

from optimizer_pipeline.core.domain import ArtifactKind

from .catalogues import clean_macro_catalogue
from .contracts import DesignStage, DoFLevel, EventClass
from .evidence import ImmutableEvidenceWriter
from .gate import ActionGate
from .manifests import capability_manifest, interface_fingerprints
from .masks import create_mask_rules
from .provider import (
    DoFProviderObserver,
    ProviderRequestBinding,
    ProviderRunLedger,
    ProviderSettings,
    ProviderTaskCancelled,
    StrictProviderParseError,
    parse_exact_action_proposal,
    verify_provider_attempts,
    write_provider_attempt_trace,
)
from .provider_transport import (
    OpenAIChatCompletionsTransport,
    reject_openai_sdk_ambient_configuration,
)
from .provider_request_capabilities import (
    CanonicalOutboundRequestBinding,
    ProviderModelRequestCapability,
    ProviderRequestCapabilityError,
    build_canonical_outbound_request,
    provider_sdk_distribution_manifest,
    validate_provider_sdk_distribution_manifest,
    verify_canonical_outbound_binding,
)
from .provenance import observe_repository


PROVIDER_SMOKE_PLAN_SCHEMA = "dof.wp004a-provider-smoke-plan.v1"
PROVIDER_SMOKE_RESULT_SCHEMA = "dof.wp004a-provider-smoke-result.v1"
PROVIDER_SMOKE_MANIFEST_SCHEMA = "dof.wp004a-provider-smoke-manifest.v1"
PROVIDER_SMOKE_KIND = "TECHNICAL_PROVIDER_SMOKE_NON_STUDY"
PROVIDER_SMOKE_RUN_ID = "wp004a-provider-technical-smoke"
PROVIDER_SMOKE_DECISION_ID = "wp004a-provider-decision-001"
PROVIDER_CONFIG_URI = "configs/providers/openai.json"
MODEL_CONFIG_URI = "configs/analysis_run_openai_cached.json"
PROVIDER_BASE_URL = "https://api.openai.com/v1"
PROVIDER_MODEL_ID = "gpt-5.4-mini"
PROVIDER_CREDENTIAL_ENV_NAME = "OPENAI_API_KEY"


class ProviderSmokePlanError(ValueError):
    pass


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def _hash(value: Any) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _is_lower_hex(value: Any, length: int) -> bool:
    return (
        isinstance(value, str)
        and len(value) == length
        and all(character in "0123456789abcdef" for character in value)
    )


def _provider_smoke_settings() -> ProviderSettings:
    return ProviderSettings(
        temperature=None,
        max_tokens=300,
        seed=None,
        response_format="json_object",
        timeout_seconds=60.0,
        max_retries=0,
        retry_on=(),
        max_requests=1,
        max_usd=0.25,
        max_cost_per_request_usd=0.25,
        # The API response carries usage but no provider-billed USD amount.
        # Preserve reported cost as unknown and account the declared upper.
        unknown_cost_policy="allow_unpriced",
        unknown_usage_policy="reject",
    )


def _profile_uri(provider: Mapping[str, Any], model_id: str) -> str:
    profiles = provider.get("request_capability_profile_by_model")
    if not isinstance(profiles, Mapping) or set(profiles) != {model_id}:
        raise ProviderSmokePlanError("deterministic_capability_profile_mapping_mismatch")
    uri = profiles.get(model_id)
    if (
        not isinstance(uri, str)
        or not uri
        or Path(uri).is_absolute()
        or ".." in Path(uri).parts
    ):
        raise ProviderSmokePlanError("deterministic_capability_profile_uri_invalid")
    return uri


def _load_bound_profile(
    root: Path,
    provider: Mapping[str, Any],
    model_id: str,
) -> tuple[str, Path, ProviderModelRequestCapability]:
    uri = _profile_uri(provider, model_id)
    path = (root / uri).resolve()
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise ProviderSmokePlanError("capability_profile_escapes_repository") from exc
    try:
        profile = ProviderModelRequestCapability.load(path)
    except ProviderRequestCapabilityError as exc:
        raise ProviderSmokePlanError(str(exc)) from exc
    if (
        profile.provider != provider.get("provider")
        or profile.adapter_type != provider.get("adapter_type")
        or profile.model_id != model_id
        or profile.adapter_version != OpenAIChatCompletionsTransport.adapter_version
        or profile.sdk_operation != OpenAIChatCompletionsTransport.sdk_operation
        or profile.sdk_operation_version
        != OpenAIChatCompletionsTransport.sdk_operation_version
    ):
        raise ProviderSmokePlanError("capability_profile_binding_mismatch")
    return uri, path, profile


def _repository_input_path(root: Path, uri: Any, label: str) -> Path:
    if (
        not isinstance(uri, str)
        or not uri
        or Path(uri).is_absolute()
        or ".." in Path(uri).parts
    ):
        raise ProviderSmokePlanError(f"provider_runtime_{label}_uri_invalid")
    path = (root / uri).resolve()
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise ProviderSmokePlanError(
            f"provider_runtime_{label}_escapes_repository"
        ) from exc
    return path


def _expected_context_and_prompt(catalogue: Any) -> tuple[dict[str, Any], bytes]:
    context = {
        "schema_version": "dof.wp004a-synthetic-context.v1",
        "study_status": PROVIDER_SMOKE_KIND,
        "decision_id": PROVIDER_SMOKE_DECISION_ID,
        "level": "L2",
        "regime": "known",
        "design_stage": "ROUTED",
        "observation": {
            "source": "synthetic",
            "timing_pressure": "moderate",
            "placement_pressure": "low",
            "routing_pressure": "moderate",
            "real_benchmark_data": False,
        },
        "legal_actions": [
            {"action_key": item.name, "parameters": {}}
            for item in catalogue.actions
        ],
        "stop_allowed": True,
    }
    prompt = (
        "Return exactly one JSON object and no surrounding text. "
        "Use schema_version action_proposal.v1, proposal_id wp004a-provider-proposal-001, "
        f"decision_id {PROVIDER_SMOKE_DECISION_ID}, catalogue_version {catalogue.version}, "
        f"catalogue_hash {catalogue.sha256}. kind must be ACTION with exactly one step "
        "using macro.phys_opt, macro.retime, or macro.place_route with parameters {}, "
        "or kind STOP with steps []. Do not invent actions, fields, paths, tools, or metrics."
    ).encode("utf-8")
    return context, prompt


def freeze_provider_smoke_plan(repository_anchor: str | Path) -> dict[str, Any]:
    root = Path(repository_anchor).resolve()
    provider_path = root / PROVIDER_CONFIG_URI
    model_path = root / MODEL_CONFIG_URI
    provider = json.loads(provider_path.read_text())
    model_config = json.loads(model_path.read_text())
    if (
        provider.get("provider") != "openai"
        or provider.get("adapter_type") != "openai"
        or provider.get("api_key_env") != PROVIDER_CREDENTIAL_ENV_NAME
        or provider.get("base_url") != PROVIDER_BASE_URL
    ):
        raise ProviderSmokePlanError("deterministic_provider_config_mismatch")
    model_id = model_config.get("model")
    if model_id != PROVIDER_MODEL_ID:
        raise ProviderSmokePlanError("deterministic_model_config_missing")
    profile_uri, profile_path, profile = _load_bound_profile(
        root,
        provider,
        model_id,
    )
    sdk_distribution = provider_sdk_distribution_manifest()
    repository = observe_repository(root)
    if repository.tracked_tree_dirty:
        raise ProviderSmokePlanError("tracked_product_tree_not_clean")
    catalogue = clean_macro_catalogue(DoFLevel.L2)
    capability = capability_manifest(DoFLevel.L2)
    interfaces = interface_fingerprints(DoFLevel.L2)
    mask = create_mask_rules(version="wp004a-provider-empty-mask.v1")
    context, prompt = _expected_context_and_prompt(catalogue)
    context_bytes = _canonical_bytes(context)
    settings = _provider_smoke_settings()
    base = {
        "schema_version": PROVIDER_SMOKE_PLAN_SCHEMA,
        "study_status": PROVIDER_SMOKE_KIND,
        "run_id": PROVIDER_SMOKE_RUN_ID,
        "selection_rule": {
            "provider": "first checked-in native production JSON provider: configs/providers/openai.json",
            "model": "model from lexicographically first checked-in openai analysis config with a concrete model",
            "request_capability": "exact checked-in provider/model profile",
            "provider_substitution_allowed": False,
        },
        "repository_constraint": {
            "head_commit": repository.head_commit,
            "head_tree": repository.head_tree,
            "binary_diff_sha256": repository.binary_diff_sha256,
            "tracked_tree_dirty": False,
        },
        "provider_config": {
            "uri": PROVIDER_CONFIG_URI,
            "sha256": hashlib.sha256(provider_path.read_bytes()).hexdigest(),
            "provider": provider["provider"],
            "adapter_type": provider["adapter_type"],
            "base_url": provider["base_url"],
            "credential_env_name": provider["api_key_env"],
            "request_capability_profile_uri": profile_uri,
        },
        "model_config": {
            "uri": MODEL_CONFIG_URI,
            "sha256": hashlib.sha256(model_path.read_bytes()).hexdigest(),
            "model_id": model_id,
        },
        "request_capability_profile": {
            "uri": profile_uri,
            "file_sha256": hashlib.sha256(profile_path.read_bytes()).hexdigest(),
            "profile_sha256": profile.sha256,
            "profile": profile.to_dict(),
        },
        "sdk_distribution": sdk_distribution,
        "context_bytes_b64": base64.b64encode(context_bytes).decode("ascii"),
        "context_sha256": hashlib.sha256(context_bytes).hexdigest(),
        "prompt_bytes_b64": base64.b64encode(prompt).decode("ascii"),
        "prompt_sha256": hashlib.sha256(prompt).hexdigest(),
        "interfaces": interfaces,
        "catalogue": {
            "version": catalogue.version,
            "sha256": catalogue.sha256,
            "actions": [item.name for item in catalogue.actions],
            "stop_allowed": True,
        },
        "capability_hash": capability.sha256,
        "mask_evidence_hash": mask.hash,
        "limits": {
            "transport_requests": 1,
            "retries": 0,
            "fallbacks": 0,
            "max_usd": 0.25,
            "backend_dispatches": 0,
            "settings": settings.to_dict(),
        },
        "adapter_version": OpenAIChatCompletionsTransport.adapter_version,
    }
    spec_hash = _hash(base)
    binding = ProviderRequestBinding(
        run_id=PROVIDER_SMOKE_RUN_ID,
        spec_hash=spec_hash,
        experiment_id="wp004a-provider-smoke",
        condition_id="synthetic-l2-known",
        repeat_id="repeat-0",
        decision_id=PROVIDER_SMOKE_DECISION_ID,
        context_bytes=context_bytes,
        prompt_bytes=prompt,
        prompt_version=interfaces["prompt_version"],
        action_interface_version=interfaces["action_schema_version"],
        action_interface_hash=interfaces["action_schema_hash"],
        context_interface_version=interfaces["context_version"],
        context_interface_hash=interfaces["context_hash"],
        catalogue_version=catalogue.version,
        catalogue_hash=catalogue.sha256,
        capability_hash=capability.sha256,
        mask_evidence_hash=mask.hash,
        provider_role="catalogue_agent",
        provider="openai",
        model_id=model_id,
        adapter_version=OpenAIChatCompletionsTransport.adapter_version,
        settings=settings,
    )
    payload = base | {
        "spec_hash": spec_hash,
        "request_binding": binding.to_evidence_dict(),
        "request_contract_hash": binding.request_contract_hash,
    }
    outbound = build_canonical_outbound_request(
        binding,
        profile,
        base_url=provider["base_url"],
        sdk_distribution_manifest=sdk_distribution,
    )
    payload["canonical_outbound_binding"] = outbound.binding.to_dict()
    return payload | {"plan_sha256": _hash(payload)}


def validate_provider_smoke_plan(plan: Mapping[str, Any]) -> dict[str, Any]:
    value = json.loads(_canonical_bytes(dict(plan)))
    claimed = value.pop("plan_sha256", None)
    if value.get("schema_version") != PROVIDER_SMOKE_PLAN_SCHEMA or claimed != _hash(value):
        raise ProviderSmokePlanError("provider_plan_schema_or_hash_mismatch")
    if set(value) != {
        "schema_version",
        "study_status",
        "run_id",
        "selection_rule",
        "repository_constraint",
        "provider_config",
        "model_config",
        "request_capability_profile",
        "sdk_distribution",
        "context_bytes_b64",
        "context_sha256",
        "prompt_bytes_b64",
        "prompt_sha256",
        "interfaces",
        "catalogue",
        "capability_hash",
        "mask_evidence_hash",
        "limits",
        "adapter_version",
        "spec_hash",
        "request_binding",
        "request_contract_hash",
        "canonical_outbound_binding",
    }:
        raise ProviderSmokePlanError("provider_plan_fields_not_exact")
    if (
        value.get("run_id") != PROVIDER_SMOKE_RUN_ID
        or value.get("selection_rule")
        != {
            "provider": "first checked-in native production JSON provider: configs/providers/openai.json",
            "model": "model from lexicographically first checked-in openai analysis config with a concrete model",
            "request_capability": "exact checked-in provider/model profile",
            "provider_substitution_allowed": False,
        }
        or value.get("adapter_version")
        != OpenAIChatCompletionsTransport.adapter_version
    ):
        raise ProviderSmokePlanError("provider_plan_fixed_identity_mismatch")
    repository = value.get("repository_constraint")
    if (
        not isinstance(repository, dict)
        or set(repository)
        != {
            "head_commit",
            "head_tree",
            "binary_diff_sha256",
            "tracked_tree_dirty",
        }
        or repository.get("tracked_tree_dirty") is not False
        or not _is_lower_hex(repository.get("head_commit"), 40)
        or not _is_lower_hex(repository.get("head_tree"), 40)
        or not _is_lower_hex(repository.get("binary_diff_sha256"), 64)
    ):
        raise ProviderSmokePlanError("provider_plan_repository_binding_mismatch")
    catalogue = clean_macro_catalogue(DoFLevel.L2)
    capability = capability_manifest(DoFLevel.L2)
    interfaces = interface_fingerprints(DoFLevel.L2)
    mask = create_mask_rules(version="wp004a-provider-empty-mask.v1")
    expected_context, expected_prompt = _expected_context_and_prompt(catalogue)
    try:
        context_bytes = base64.b64decode(
            value["context_bytes_b64"], validate=True
        )
        prompt_bytes = base64.b64decode(
            value["prompt_bytes_b64"], validate=True
        )
        context = json.loads(context_bytes)
    except (KeyError, ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProviderSmokePlanError("provider_plan_context_or_prompt_invalid") from exc
    if (
        context != expected_context
        or context_bytes != _canonical_bytes(expected_context)
        or prompt_bytes != expected_prompt
        or value.get("context_sha256")
        != hashlib.sha256(context_bytes).hexdigest()
        or value.get("prompt_sha256")
        != hashlib.sha256(prompt_bytes).hexdigest()
        or value.get("interfaces") != interfaces
        or value.get("catalogue")
        != {
            "version": catalogue.version,
            "sha256": catalogue.sha256,
            "actions": [item.name for item in catalogue.actions],
            "stop_allowed": True,
        }
        or value.get("capability_hash") != capability.sha256
        or value.get("mask_evidence_hash") != mask.hash
    ):
        raise ProviderSmokePlanError("provider_plan_synthetic_binding_mismatch")
    profile_info = value.get("request_capability_profile")
    if (
        not isinstance(profile_info, dict)
        or set(profile_info)
        != {"uri", "file_sha256", "profile_sha256", "profile"}
        or not _is_lower_hex(profile_info.get("file_sha256"), 64)
        or not _is_lower_hex(profile_info.get("profile_sha256"), 64)
    ):
        raise ProviderSmokePlanError("provider_request_capability_profile_missing")
    limits = value.get("limits")
    expected_settings = _provider_smoke_settings()
    if (
        value.get("study_status") != PROVIDER_SMOKE_KIND
        or not isinstance(limits, dict)
        or set(limits)
        != {
            "transport_requests",
            "retries",
            "fallbacks",
            "max_usd",
            "backend_dispatches",
            "settings",
        }
        or limits
        != {
            "transport_requests": 1,
            "retries": 0,
            "fallbacks": 0,
            "max_usd": 0.25,
            "backend_dispatches": 0,
            "settings": expected_settings.to_dict(),
        }
    ):
        raise ProviderSmokePlanError("provider_plan_limits_mismatch")
    binding = _binding_from_evidence(value["request_binding"])
    settings = binding.settings
    if (
        limits.get("settings") != settings.to_dict()
        or settings.to_dict() != expected_settings.to_dict()
    ):
        raise ProviderSmokePlanError("provider_plan_settings_mismatch")
    expected_provider = {
        "uri": PROVIDER_CONFIG_URI,
        "sha256": value.get("provider_config", {}).get("sha256"),
        "provider": "openai",
        "adapter_type": "openai",
        "base_url": PROVIDER_BASE_URL,
        "credential_env_name": PROVIDER_CREDENTIAL_ENV_NAME,
        "request_capability_profile_uri": (
            "configs/providers/openai_gpt-5.4-mini_chat_completions.json"
        ),
    }
    expected_model = {
        "uri": MODEL_CONFIG_URI,
        "sha256": value.get("model_config", {}).get("sha256"),
        "model_id": PROVIDER_MODEL_ID,
    }
    if (
        value.get("provider_config") != expected_provider
        or not _is_lower_hex(expected_provider["sha256"], 64)
    ):
        raise ProviderSmokePlanError("provider_plan_provider_binding_mismatch")
    if (
        value.get("model_config") != expected_model
        or not _is_lower_hex(expected_model["sha256"], 64)
    ):
        raise ProviderSmokePlanError("provider_plan_model_binding_mismatch")
    if binding.request_contract_hash != value.get("request_contract_hash"):
        raise ProviderSmokePlanError("provider_request_contract_hash_mismatch")
    if binding.to_evidence_dict() != value["request_binding"]:
        raise ProviderSmokePlanError("provider_request_binding_not_canonical")
    base = dict(value)
    for key in (
        "spec_hash",
        "request_binding",
        "request_contract_hash",
        "canonical_outbound_binding",
    ):
        base.pop(key)
    if (
        value.get("spec_hash") != _hash(base)
        or binding.spec_hash != value.get("spec_hash")
        or binding.run_id != PROVIDER_SMOKE_RUN_ID
        or binding.experiment_id != "wp004a-provider-smoke"
        or binding.condition_id != "synthetic-l2-known"
        or binding.repeat_id != "repeat-0"
        or binding.decision_id != PROVIDER_SMOKE_DECISION_ID
        or binding.provider_role != "catalogue_agent"
        or binding.context_bytes != context_bytes
        or binding.prompt_bytes != prompt_bytes
        or binding.prompt_version != interfaces["prompt_version"]
        or binding.action_interface_version
        != interfaces["action_schema_version"]
        or binding.action_interface_hash != interfaces["action_schema_hash"]
        or binding.context_interface_version != interfaces["context_version"]
        or binding.context_interface_hash != interfaces["context_hash"]
        or binding.catalogue_version != catalogue.version
        or binding.catalogue_hash != catalogue.sha256
        or binding.capability_hash != capability.sha256
        or binding.mask_evidence_hash != mask.hash
    ):
        raise ProviderSmokePlanError("provider_plan_spec_or_request_binding_mismatch")
    try:
        sdk_distribution = validate_provider_sdk_distribution_manifest(
            value["sdk_distribution"]
        )
    except (KeyError, ProviderRequestCapabilityError) as exc:
        raise ProviderSmokePlanError("provider_sdk_distribution_invalid") from exc
    try:
        profile = ProviderModelRequestCapability.from_mapping(
            profile_info["profile"]
        )
    except (KeyError, ProviderRequestCapabilityError) as exc:
        raise ProviderSmokePlanError("provider_request_capability_profile_invalid") from exc
    if (
        profile.sha256 != profile_info.get("profile_sha256")
        or profile_info.get("uri")
        != value.get("provider_config", {}).get(
            "request_capability_profile_uri"
        )
        or profile.provider != binding.provider
        or profile.model_id != binding.model_id
        or profile.adapter_version != binding.adapter_version
    ):
        raise ProviderSmokePlanError("provider_request_capability_binding_mismatch")
    try:
        expected_outbound = CanonicalOutboundRequestBinding.from_mapping(
            value["canonical_outbound_binding"]
        )
        rebuilt = build_canonical_outbound_request(
            binding,
            profile,
            base_url=PROVIDER_BASE_URL,
            sdk_distribution_manifest=sdk_distribution,
        )
        verify_canonical_outbound_binding(rebuilt.binding, expected_outbound)
    except (KeyError, ProviderRequestCapabilityError) as exc:
        raise ProviderSmokePlanError("canonical_outbound_binding_mismatch") from exc
    return value | {"plan_sha256": claimed}


def _validate_runtime_inputs(
    plan: Mapping[str, Any],
) -> tuple[
    ProviderModelRequestCapability,
    CanonicalOutboundRequestBinding,
]:
    root = Path(__file__).resolve().parents[2]
    repository = observe_repository(root)
    expected_repository = plan["repository_constraint"]
    if (
        repository.tracked_tree_dirty
        or repository.head_commit != expected_repository["head_commit"]
        or repository.head_tree != expected_repository["head_tree"]
        or repository.binary_diff_sha256
        != expected_repository["binary_diff_sha256"]
    ):
        raise ProviderSmokePlanError("provider_runtime_repository_mismatch")

    provider_path = _repository_input_path(
        root, plan["provider_config"]["uri"], "provider_config"
    )
    model_path = _repository_input_path(
        root, plan["model_config"]["uri"], "model_config"
    )
    profile_path = _repository_input_path(
        root, plan["request_capability_profile"]["uri"], "capability_profile"
    )
    try:
        provider_bytes = provider_path.read_bytes()
        model_bytes = model_path.read_bytes()
        profile_bytes = profile_path.read_bytes()
    except OSError as exc:
        raise ProviderSmokePlanError("provider_runtime_config_unavailable") from exc
    if hashlib.sha256(provider_bytes).hexdigest() != plan["provider_config"]["sha256"]:
        raise ProviderSmokePlanError("provider_runtime_provider_config_mismatch")
    if hashlib.sha256(model_bytes).hexdigest() != plan["model_config"]["sha256"]:
        raise ProviderSmokePlanError("provider_runtime_model_config_mismatch")
    if (
        hashlib.sha256(profile_bytes).hexdigest()
        != plan["request_capability_profile"]["file_sha256"]
    ):
        raise ProviderSmokePlanError("provider_runtime_capability_file_mismatch")
    try:
        provider = json.loads(provider_bytes)
        model = json.loads(model_bytes)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProviderSmokePlanError("provider_runtime_config_not_json") from exc
    profile_uri = _profile_uri(provider, PROVIDER_MODEL_ID)
    expected_provider = {
        "uri": PROVIDER_CONFIG_URI,
        "sha256": hashlib.sha256(provider_bytes).hexdigest(),
        "provider": provider.get("provider"),
        "adapter_type": provider.get("adapter_type"),
        "base_url": provider.get("base_url"),
        "credential_env_name": provider.get("api_key_env"),
        "request_capability_profile_uri": profile_uri,
    }
    expected_model = {
        "uri": MODEL_CONFIG_URI,
        "sha256": hashlib.sha256(model_bytes).hexdigest(),
        "model_id": model.get("model"),
    }
    if (
        expected_provider != plan["provider_config"]
        or expected_provider["provider"] != "openai"
        or expected_provider["adapter_type"] != "openai"
        or expected_provider["base_url"] != PROVIDER_BASE_URL
        or expected_provider["credential_env_name"]
        != PROVIDER_CREDENTIAL_ENV_NAME
    ):
        raise ProviderSmokePlanError("provider_runtime_provider_binding_mismatch")
    if (
        expected_model != plan["model_config"]
        or expected_model["model_id"] != PROVIDER_MODEL_ID
    ):
        raise ProviderSmokePlanError("provider_runtime_model_binding_mismatch")
    if profile_uri != plan["request_capability_profile"]["uri"]:
        raise ProviderSmokePlanError("provider_runtime_profile_uri_mismatch")
    profile = ProviderModelRequestCapability.load(profile_path)
    if profile.sha256 != plan["request_capability_profile"]["profile_sha256"]:
        raise ProviderSmokePlanError("provider_runtime_capability_hash_mismatch")
    observed_sdk_distribution = provider_sdk_distribution_manifest()
    if observed_sdk_distribution != plan.get("sdk_distribution"):
        raise ProviderSmokePlanError("provider_runtime_sdk_distribution_mismatch")
    binding = _binding_from_evidence(plan["request_binding"])
    prepared = build_canonical_outbound_request(
        binding,
        profile,
        base_url=PROVIDER_BASE_URL,
        sdk_distribution_manifest=observed_sdk_distribution,
    )
    expected = CanonicalOutboundRequestBinding.from_mapping(
        plan["canonical_outbound_binding"]
    )
    verify_canonical_outbound_binding(prepared.binding, expected)
    reject_openai_sdk_ambient_configuration()
    return profile, expected


def write_provider_smoke_plan(plan: Mapping[str, Any], path: str | Path) -> None:
    validated = validate_provider_smoke_plan(plan)
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        raise FileExistsError(target)
    target.write_bytes(json.dumps(validated, sort_keys=True, indent=2).encode() + b"\n")


def load_provider_smoke_plan(path: str | Path) -> dict[str, Any]:
    return validate_provider_smoke_plan(json.loads(Path(path).read_text()))


def execute_provider_smoke(
    plan: Mapping[str, Any],
    output_dir: str | Path,
    *,
    credential: str | None = None,
    transport_factory: Any = OpenAIChatCompletionsTransport,
) -> dict[str, Any]:
    validated = validate_provider_smoke_plan(plan)
    profile, expected_outbound = _validate_runtime_inputs(validated)
    if getattr(
        transport_factory, "requires_canonical_outbound_binding", None
    ) is not True:
        raise ProviderSmokePlanError(
            "provider_transport_canonical_binding_required"
        )
    output = Path(output_dir).resolve()
    output.mkdir(parents=True, exist_ok=False)
    binding = _binding_from_evidence(validated["request_binding"])
    writer = ImmutableEvidenceWriter(output, binding.run_id, binding.spec_hash)
    plan_sidecar = writer.write_json(
        "evidence/provider-smoke-plan.json",
        validated,
        role="provider_smoke_plan",
        schema_version=PROVIDER_SMOKE_PLAN_SCHEMA,
    )
    credential_value = credential
    if credential_value is None:
        credential_value = os.environ.get(validated["provider_config"]["credential_env_name"])
    attempts = ()
    decision = None
    transport = None
    cancellation: ProviderTaskCancelled | None = None
    if not credential_value:
        endpoint = "PROVIDER_UNAVAILABLE"
        reasons = ["configured_provider_credential_unavailable"]
    else:
        transport = transport_factory(
            api_key=credential_value,
            base_url=validated["provider_config"]["base_url"],
            capability_profile=profile,
            sdk_distribution_manifest=validated["sdk_distribution"],
            expected_outbound_binding=expected_outbound,
        )
        if getattr(
            transport, "requires_canonical_outbound_binding", None
        ) is not True:
            raise ProviderSmokePlanError(
                "provider_transport_canonical_binding_required"
            )
        ledger = ProviderRunLedger.for_binding(binding)
        try:
            decision = DoFProviderObserver(run_ledger=ledger).request(
                binding, transport
            )
        except ProviderTaskCancelled as exc:
            cancellation = exc
            attempts = exc.attempts
            endpoint = exc.endpoint
            reasons = list(exc.reasons)
        else:
            attempts = decision.attempts
            endpoint = decision.endpoint
            reasons = list(decision.reasons)
    trace_sidecar = write_provider_attempt_trace(
        writer,
        attempts,
        binding=binding,
    )
    parser_result: dict[str, Any] = {
        "status": "NOT_RUN",
        "reasons": ["no raw response bytes"],
        "proposal": None,
        "proposal_hash": None,
    }
    gate_result: dict[str, Any] = {
        "accepted": False,
        "endpoint": "NOT_RUN",
        "reasons": ["strict parser did not produce a proposal"],
    }
    if attempts and attempts[-1].raw_response_bytes_b64:
        raw = base64.b64decode(attempts[-1].raw_response_bytes_b64)
        try:
            proposal = parse_exact_action_proposal(raw)
        except StrictProviderParseError as exc:
            parser_result = {
                "status": "REJECTED",
                "reasons": list(exc.reasons),
                "proposal": None,
                "proposal_hash": None,
            }
        else:
            parser_result = {
                "status": "PARSED",
                "reasons": [],
                "proposal": proposal.to_dict(),
                "proposal_hash": proposal.proposal_hash,
            }
            gate = ActionGate().evaluate(
                proposal,
                level=DoFLevel.L2,
                expected_decision_id=binding.decision_id,
                catalogue=clean_macro_catalogue(DoFLevel.L2),
                expected_catalogue_hash=binding.catalogue_hash,
                current_stage=DesignStage.ROUTED,
                budget_remaining={item.value: 10 for item in EventClass},
                mask=create_mask_rules(version="wp004a-provider-empty-mask.v1"),
            )
            gate_result = gate.to_dict()
    requests = len(attempts)
    physical_requests = 0 if transport is None else transport.physical_requests
    if (
        not isinstance(physical_requests, int)
        or isinstance(physical_requests, bool)
        or not 0 <= physical_requests <= 1
        or physical_requests > requests
        or (
            decision is not None
            and decision.accepted
            and physical_requests != 1
        )
    ):
        raise ProviderSmokePlanError("provider_physical_request_count_mismatch")
    prepared_outbound = (
        None
        if transport is None
        else getattr(transport, "prepared_outbound_binding", None)
    )
    if transport is not None and requests:
        if not isinstance(prepared_outbound, CanonicalOutboundRequestBinding):
            raise ProviderSmokePlanError("provider_actual_outbound_binding_missing")
        try:
            verify_canonical_outbound_binding(
                prepared_outbound,
                expected_outbound,
            )
        except ProviderRequestCapabilityError as exc:
            raise ProviderSmokePlanError("provider_actual_outbound_binding_mismatch") from exc
    if verify_provider_attempts(attempts, binding):
        raise ProviderSmokePlanError("provider_attempt_trace_verification_failed")
    summary = {
        "schema_version": PROVIDER_SMOKE_RESULT_SCHEMA,
        "study_status": PROVIDER_SMOKE_KIND,
        "endpoint": endpoint,
        "reasons": reasons,
        "plan_sha256": validated["plan_sha256"],
        "request_contract_hash": binding.request_contract_hash,
        "request_capability_profile_hash": profile.sha256,
        "prompt_sha256": expected_outbound.prompt_sha256,
        "context_sha256": expected_outbound.context_sha256,
        "messages_sha256": expected_outbound.messages_sha256,
        "canonical_request_binding_hash": (
            expected_outbound.canonical_request_binding_hash
        ),
        "outbound_top_level_keys": list(
            expected_outbound.outbound_top_level_keys
        ),
        "outbound_top_level_key_set_hash": (
            expected_outbound.outbound_top_level_key_set_hash
        ),
        "outbound_request_kwargs_hash": (
            expected_outbound.outbound_request_kwargs_hash
        ),
        "client_options_hash": expected_outbound.client_options_hash,
        "actual_canonical_request_binding_hash": (
            None
            if prepared_outbound is None
            else prepared_outbound.canonical_request_binding_hash
        ),
        "transport_requests": requests,
        "physical_transport_requests": physical_requests,
        "transport_send_attempts": (
            0 if transport is None else getattr(transport, "send_attempts", requests)
        ),
        "sdk_create_calls": (
            0 if transport is None else getattr(transport, "sdk_create_calls", requests)
        ),
        "transport_cleanup_status": (
            "NOT_APPLICABLE"
            if transport is None
            else getattr(transport, "cleanup_status", "NOT_REPORTED")
        ),
        "transport_cleanup_error": (
            None
            if transport is None
            else getattr(transport, "cleanup_error", None)
        ),
        "retries": 0,
        "fallbacks": 0,
        "backend_dispatches": 0,
        "parser_result": parser_result,
        "gate_result": gate_result,
        "provider_decision_accepted": None if decision is None else decision.accepted,
        "attempt_status": None if not attempts else attempts[-1].status.value,
        "actual_provider": None if not attempts else attempts[-1].actual_provider,
        "returned_model_id": None if not attempts else attempts[-1].returned_model_id,
        "returned_model_revision": None if not attempts else attempts[-1].returned_model_revision,
        "input_tokens": None if not attempts else attempts[-1].input_tokens,
        "output_tokens": None if not attempts else attempts[-1].output_tokens,
        "total_tokens": None if not attempts else attempts[-1].total_tokens,
        "reported_cost_usd": None if not attempts else attempts[-1].cost_usd,
        "accounted_cost_usd": 0.0 if not attempts else attempts[-1].accounted_cost_usd,
        "latency_seconds": None if not attempts else attempts[-1].latency_seconds,
        "secrets_persisted": False,
    }
    result_sidecar = writer.write_json(
        "evidence/provider-result.json",
        summary,
        role="provider_smoke_result",
        schema_version=PROVIDER_SMOKE_RESULT_SCHEMA,
        kind=ArtifactKind.REPORT,
    )
    manifest = {
        "schema_version": PROVIDER_SMOKE_MANIFEST_SCHEMA,
        "spec_hash": binding.spec_hash,
        "artifacts": [
            {
                "role": item.role,
                "uri": item.uri,
                "sha256": item.sha256,
                "size_bytes": item.size_bytes,
            }
            for item in (plan_sidecar, trace_sidecar, result_sidecar)
        ],
    }
    manifest_bytes = json.dumps(manifest, sort_keys=True, indent=2).encode() + b"\n"
    (output / "evidence-manifest.json").write_bytes(manifest_bytes)
    summary["evidence_manifest_sha256"] = hashlib.sha256(manifest_bytes).hexdigest()
    if cancellation is not None:
        raise cancellation
    return summary


def _binding_from_evidence(value: Mapping[str, Any]) -> ProviderRequestBinding:
    settings = ProviderSettings(**value["settings"])
    return ProviderRequestBinding(
        run_id=value["run_id"],
        spec_hash=value["spec_hash"],
        experiment_id=value["experiment_id"],
        condition_id=value["condition_id"],
        repeat_id=value["repeat_id"],
        decision_id=value["decision_id"],
        context_bytes=base64.b64decode(value["context_bytes_b64"], validate=True),
        prompt_bytes=base64.b64decode(value["prompt_bytes_b64"], validate=True),
        prompt_version=value["prompt_version"],
        action_interface_version=value["action_interface_version"],
        action_interface_hash=value["action_interface_hash"],
        context_interface_version=value["context_interface_version"],
        context_interface_hash=value["context_interface_hash"],
        catalogue_version=value["catalogue_version"],
        catalogue_hash=value["catalogue_hash"],
        capability_hash=value["capability_hash"],
        mask_evidence_hash=value["mask_evidence_hash"],
        provider_role=value["provider_role"],
        provider=value["configured_provider"],
        model_id=value["configured_model_id"],
        adapter_version=value["adapter_version"],
        settings=settings,
    )


__all__ = [
    "PROVIDER_SMOKE_KIND",
    "ProviderSmokePlanError",
    "execute_provider_smoke",
    "freeze_provider_smoke_plan",
    "load_provider_smoke_plan",
    "validate_provider_smoke_plan",
    "write_provider_smoke_plan",
]
