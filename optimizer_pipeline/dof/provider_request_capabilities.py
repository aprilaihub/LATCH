"""Exact provider/model request capabilities and canonical outbound binding.

The strict provider transport must not discover request compatibility by
sending a request and repairing it afterwards.  This module loads one exact,
versioned provider/model capability profile and is the sole builder for the
SDK-facing request kwargs used by both recording fakes and the real transport.
Credentials and authorization headers never enter the canonical binding.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import hashlib
import importlib.metadata
import json
import math
from pathlib import Path
from pathlib import PurePosixPath
import platform
import re
import sys
from types import MappingProxyType
from typing import Any

from .provider import ProviderBindingError, ProviderRequestBinding


PROVIDER_REQUEST_CAPABILITY_SCHEMA = "dof.provider-model-request-capability.v1"
CANONICAL_OUTBOUND_REQUEST_SCHEMA = "dof.canonical-outbound-request-binding.v1"
PROVIDER_SDK_DISTRIBUTION_SCHEMA = "dof.provider-sdk-runtime-binding.v2"

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_OUTBOUND_FIELD_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
_TOKEN_ALIAS_FIELDS = frozenset({"max_tokens", "max_completion_tokens"})
_RESERVED_NON_TOKEN_FIELDS = frozenset(
    {"model", "messages", "response_format", "temperature", "seed", "timeout"}
)
_FIELD_POLICIES = frozenset({"include", "omit"})
_PROFILE_FIELDS = frozenset(
    {
        "schema_version",
        "profile_version",
        "provider",
        "adapter_type",
        "adapter_version",
        "model_id",
        "model_revision_policy",
        "sdk_operation",
        "sdk_operation_version",
        "token_limit_parameter",
        "response_format",
        "temperature_policy",
        "seed_policy",
        "timeout",
        "client_retry_policy",
        "http_transport_policy",
        "disallowed_simultaneous_fields",
    }
)
_STRICT_HTTP_TRANSPORT_POLICY = {
    "implementation": "openai.DefaultAsyncHttpxClient",
    "follow_redirects": False,
    "trust_env": False,
    "http2": False,
    "request_counter": "httpx.async-request-event-hook.v1",
    "cleanup_deadline_policy": "single-nonrenewable-deadline.v1",
    "cleanup_reserve_seconds": 1.0,
}
_PROVIDER_RUNTIME_DISTRIBUTIONS = (
    "annotated-types",
    "anyio",
    "certifi",
    "distro",
    "h11",
    "httpcore",
    "httpx",
    "idna",
    "jiter",
    "openai",
    "pydantic",
    "pydantic-core",
    "sniffio",
    "tqdm",
    "typing-extensions",
    "typing-inspection",
)
_PROVIDER_RUNTIME_IMPORT_NAMES = {
    "annotated-types": "annotated_types",
    "anyio": "anyio",
    "certifi": "certifi",
    "distro": "distro",
    "h11": "h11",
    "httpcore": "httpcore",
    "httpx": "httpx",
    "idna": "idna",
    "jiter": "jiter",
    "openai": "openai",
    "pydantic": "pydantic",
    "pydantic-core": "pydantic_core",
    "sniffio": "sniffio",
    "tqdm": "tqdm",
    "typing-extensions": "typing_extensions",
    "typing-inspection": "typing_inspection",
}


class ProviderRequestCapabilityError(ProviderBindingError):
    """A capability/profile defect detected before provider entry."""


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


def _exact_fields(
    value: Any,
    expected: frozenset[str],
    label: str,
) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ProviderRequestCapabilityError(f"{label}_not_object")
    keys = set(value)
    if any(not isinstance(key, str) for key in keys):
        raise ProviderRequestCapabilityError(f"{label}_non_string_key")
    unknown = sorted(keys - expected)
    missing = sorted(expected - keys)
    reasons = []
    if unknown:
        reasons.append(f"{label}_unknown_fields:" + ",".join(unknown))
    if missing:
        reasons.append(f"{label}_missing_fields:" + ",".join(missing))
    if reasons:
        raise ProviderRequestCapabilityError(";".join(reasons))
    return value


def _nonempty_string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise ProviderRequestCapabilityError(f"{label}_not_canonical_string")
    return value


def _json_clone(value: Any) -> Any:
    def thaw(item: Any) -> Any:
        if isinstance(item, Mapping):
            return {str(key): thaw(child) for key, child in item.items()}
        if isinstance(item, (list, tuple)):
            return [thaw(child) for child in item]
        return item

    try:
        return json.loads(_canonical_bytes(thaw(value)))
    except (TypeError, ValueError) as exc:
        raise ProviderRequestCapabilityError("capability_value_not_canonical_json") from exc


def strict_http_transport_policy() -> dict[str, Any]:
    """Return the one-request HTTP policy required by the strict adapter."""

    return _json_clone(_STRICT_HTTP_TRANSPORT_POLICY)


def provider_sdk_distribution_manifest() -> dict[str, Any]:
    """Hash the Python/OpenAI HTTP runtime closure without persisting paths."""

    distributions = []
    for distribution_name in _PROVIDER_RUNTIME_DISTRIBUTIONS:
        try:
            distribution = importlib.metadata.distribution(distribution_name)
        except importlib.metadata.PackageNotFoundError as exc:
            raise ProviderRequestCapabilityError(
                f"provider_runtime_distribution_unavailable:{distribution_name}"
            ) from exc
        root = Path(distribution.locate_file("")).resolve(strict=True)
        records = []
        for packaged in distribution.files or ():
            relative = PurePosixPath(str(packaged))
            if "__pycache__" in relative.parts or relative.suffix == ".pyc":
                continue
            metadata_file = (
                relative.parts
                and relative.parts[0].endswith(".dist-info")
                and relative.name in {"METADATA", "WHEEL"}
            )
            if relative.parts and relative.parts[0].endswith(".dist-info"):
                if not metadata_file:
                    continue
            target = Path(distribution.locate_file(packaged)).resolve(strict=True)
            try:
                target.relative_to(root)
            except ValueError:
                # Console entry points outside site-packages are not imported
                # by this adapter and are not part of the runtime closure.
                continue
            if not target.is_file():
                raise ProviderRequestCapabilityError(
                    f"provider_runtime_file_unavailable:{distribution_name}"
                )
            raw = target.read_bytes()
            records.append(
                {
                    "relative_name": relative.as_posix(),
                    "sha256": hashlib.sha256(raw).hexdigest(),
                    "size_bytes": len(raw),
                }
            )
        records.sort(key=lambda item: item["relative_name"])
        if not records:
            raise ProviderRequestCapabilityError(
                f"provider_runtime_content_unavailable:{distribution_name}"
            )
        distributions.append(
            {
                "distribution": distribution_name,
                "import_name": _PROVIDER_RUNTIME_IMPORT_NAMES[
                    distribution_name
                ],
                "version": distribution.version,
                "file_count": len(records),
                "total_size_bytes": sum(item["size_bytes"] for item in records),
                "content_sha256": _hash(records),
            }
        )
    executable = Path(sys.executable).resolve(strict=True)
    executable_bytes = executable.read_bytes()
    payload = {
        "schema_version": PROVIDER_SDK_DISTRIBUTION_SCHEMA,
        "python_runtime": {
            "implementation": platform.python_implementation(),
            "version": platform.python_version(),
            "executable_sha256": hashlib.sha256(executable_bytes).hexdigest(),
            "executable_size_bytes": len(executable_bytes),
        },
        "distributions": distributions,
    }
    return payload | {"manifest_sha256": _hash(payload)}


def validate_provider_sdk_distribution_manifest(
    value: Mapping[str, Any],
) -> dict[str, Any]:
    source = _exact_fields(
        value,
        frozenset(
            {
                "schema_version",
                "python_runtime",
                "distributions",
                "manifest_sha256",
            }
        ),
        "sdk_distribution_manifest",
    )
    payload = dict(source)
    claimed = payload.pop("manifest_sha256")
    if (
        payload.get("schema_version") != PROVIDER_SDK_DISTRIBUTION_SCHEMA
        or claimed != _hash(payload)
    ):
        raise ProviderRequestCapabilityError("sdk_distribution_manifest_invalid")
    runtime = payload.get("python_runtime")
    rows = payload.get("distributions")
    if (
        not isinstance(runtime, Mapping)
        or set(runtime) != {
            "implementation",
            "version",
            "executable_sha256",
            "executable_size_bytes",
        }
        or not isinstance(runtime.get("implementation"), str)
        or not runtime["implementation"]
        or not isinstance(runtime.get("version"), str)
        or not runtime["version"]
        or not _SHA256_RE.fullmatch(str(runtime.get("executable_sha256")))
        or not isinstance(runtime.get("executable_size_bytes"), int)
        or runtime["executable_size_bytes"] <= 0
        or not isinstance(rows, list)
        or [row.get("distribution") for row in rows]
        != list(_PROVIDER_RUNTIME_DISTRIBUTIONS)
        or any(
            not isinstance(row, Mapping)
            or set(row)
            != {
                "distribution",
                "import_name",
                "version",
                "file_count",
                "total_size_bytes",
                "content_sha256",
            }
            or not isinstance(row.get("version"), str)
            or not row["version"]
            or row.get("import_name")
            != _PROVIDER_RUNTIME_IMPORT_NAMES[row["distribution"]]
            or not isinstance(row.get("file_count"), int)
            or row["file_count"] <= 0
            or not isinstance(row.get("total_size_bytes"), int)
            or row["total_size_bytes"] <= 0
            or not _SHA256_RE.fullmatch(str(row.get("content_sha256")))
            for row in rows
        )
    ):
        raise ProviderRequestCapabilityError("sdk_distribution_manifest_invalid")
    return payload | {"manifest_sha256": claimed}


@dataclass(frozen=True, slots=True)
class ProviderModelRequestCapability:
    profile_version: str
    provider: str
    adapter_type: str
    adapter_version: str
    model_id: str
    model_revision_policy: str
    sdk_operation: str
    sdk_operation_version: str
    token_limit_parameter: str | None
    response_format_policy: str
    response_format_value: Mapping[str, Any] | None
    temperature_policy: str
    seed_policy: str
    timeout_placement: str
    timeout_parameter: str
    client_max_retries: int
    http_transport_policy: Mapping[str, Any]
    disallowed_simultaneous_fields: tuple[tuple[str, str], ...]
    schema_version: str = PROVIDER_REQUEST_CAPABILITY_SCHEMA

    def __post_init__(self) -> None:
        if self.schema_version != PROVIDER_REQUEST_CAPABILITY_SCHEMA:
            raise ProviderRequestCapabilityError("unsupported_capability_schema")
        for name in (
            "profile_version",
            "provider",
            "adapter_type",
            "adapter_version",
            "model_id",
            "sdk_operation",
            "sdk_operation_version",
        ):
            _nonempty_string(getattr(self, name), name)
        if self.model_revision_policy not in {
            "provider_reported_optional",
            "exact_model_id_provider_revision_optional",
        }:
            raise ProviderRequestCapabilityError("unknown_model_revision_policy")
        if self.token_limit_parameter is not None and (
            not isinstance(self.token_limit_parameter, str)
            or not _OUTBOUND_FIELD_RE.fullmatch(self.token_limit_parameter)
            or self.token_limit_parameter in _RESERVED_NON_TOKEN_FIELDS
        ):
            raise ProviderRequestCapabilityError("unknown_token_limit_parameter")
        if self.response_format_policy not in _FIELD_POLICIES:
            raise ProviderRequestCapabilityError("unknown_response_format_policy")
        if self.response_format_policy == "include":
            if not isinstance(self.response_format_value, Mapping) or not self.response_format_value:
                raise ProviderRequestCapabilityError(
                    "included_response_format_requires_shape"
                )
            frozen_response = MappingProxyType(_json_clone(self.response_format_value))
        else:
            if self.response_format_value is not None:
                raise ProviderRequestCapabilityError(
                    "omitted_response_format_cannot_have_shape"
                )
            frozen_response = None
        object.__setattr__(self, "response_format_value", frozen_response)
        if self.temperature_policy not in _FIELD_POLICIES:
            raise ProviderRequestCapabilityError("unknown_temperature_policy")
        if self.seed_policy not in _FIELD_POLICIES:
            raise ProviderRequestCapabilityError("unknown_seed_policy")
        if self.timeout_placement not in {"client", "request"}:
            raise ProviderRequestCapabilityError("unknown_timeout_placement")
        if self.timeout_parameter != "timeout":
            raise ProviderRequestCapabilityError("unknown_timeout_parameter")
        if self.client_max_retries != 0:
            raise ProviderRequestCapabilityError("sdk_retries_must_be_zero")
        http_policy = _json_clone(self.http_transport_policy)
        if (
            http_policy != strict_http_transport_policy()
            or type(http_policy.get("follow_redirects")) is not bool
            or type(http_policy.get("trust_env")) is not bool
            or type(http_policy.get("http2")) is not bool
            or not isinstance(http_policy.get("cleanup_reserve_seconds"), (int, float))
            or isinstance(http_policy.get("cleanup_reserve_seconds"), bool)
        ):
            raise ProviderRequestCapabilityError("http_transport_policy_mismatch")
        object.__setattr__(
            self,
            "http_transport_policy",
            MappingProxyType(strict_http_transport_policy()),
        )
        aliases = tuple(tuple(item) for item in self.disallowed_simultaneous_fields)
        if any(
            len(item) != 2
            or any(field not in _TOKEN_ALIAS_FIELDS for field in item)
            or item[0] == item[1]
            for item in aliases
        ):
            raise ProviderRequestCapabilityError(
                "invalid_disallowed_simultaneous_fields"
            )
        normalized = tuple(sorted(tuple(sorted(item)) for item in aliases))
        required_alias = tuple(sorted(_TOKEN_ALIAS_FIELDS))
        if normalized.count(required_alias) != 1 or len(normalized) != 1:
            raise ProviderRequestCapabilityError(
                "token_alias_exclusion_must_be_declared_exactly_once"
            )
        object.__setattr__(self, "disallowed_simultaneous_fields", normalized)

    @classmethod
    def from_mapping(
        cls,
        value: Mapping[str, Any],
    ) -> "ProviderModelRequestCapability":
        source = _exact_fields(value, _PROFILE_FIELDS, "capability_profile")
        response_format = _exact_fields(
            source["response_format"],
            frozenset({"policy", "value"}),
            "response_format",
        )
        timeout = _exact_fields(
            source["timeout"],
            frozenset({"placement", "parameter"}),
            "timeout",
        )
        retry = _exact_fields(
            source["client_retry_policy"],
            frozenset({"max_retries"}),
            "client_retry_policy",
        )
        http_transport = _exact_fields(
            source["http_transport_policy"],
            frozenset(
                {
                    "implementation",
                    "follow_redirects",
                    "trust_env",
                    "http2",
                    "request_counter",
                    "cleanup_deadline_policy",
                    "cleanup_reserve_seconds",
                }
            ),
            "http_transport_policy",
        )
        raw_aliases = source["disallowed_simultaneous_fields"]
        if not isinstance(raw_aliases, list) or not all(
            isinstance(item, list) for item in raw_aliases
        ):
            raise ProviderRequestCapabilityError(
                "disallowed_simultaneous_fields_not_array"
            )
        token_parameter = source["token_limit_parameter"]
        if token_parameter == "omit":
            token_parameter = None
        return cls(
            schema_version=source["schema_version"],
            profile_version=source["profile_version"],
            provider=source["provider"],
            adapter_type=source["adapter_type"],
            adapter_version=source["adapter_version"],
            model_id=source["model_id"],
            model_revision_policy=source["model_revision_policy"],
            sdk_operation=source["sdk_operation"],
            sdk_operation_version=source["sdk_operation_version"],
            token_limit_parameter=token_parameter,
            response_format_policy=response_format["policy"],
            response_format_value=response_format["value"],
            temperature_policy=source["temperature_policy"],
            seed_policy=source["seed_policy"],
            timeout_placement=timeout["placement"],
            timeout_parameter=timeout["parameter"],
            client_max_retries=retry["max_retries"],
            http_transport_policy=http_transport,
            disallowed_simultaneous_fields=tuple(
                tuple(item) for item in raw_aliases
            ),
        )

    @classmethod
    def load(cls, path: str | Path) -> "ProviderModelRequestCapability":
        try:
            value = json.loads(Path(path).read_bytes())
        except (OSError, json.JSONDecodeError) as exc:
            raise ProviderRequestCapabilityError(
                "capability_profile_unavailable_or_invalid"
            ) from exc
        return cls.from_mapping(value)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "profile_version": self.profile_version,
            "provider": self.provider,
            "adapter_type": self.adapter_type,
            "adapter_version": self.adapter_version,
            "model_id": self.model_id,
            "model_revision_policy": self.model_revision_policy,
            "sdk_operation": self.sdk_operation,
            "sdk_operation_version": self.sdk_operation_version,
            "token_limit_parameter": (
                "omit"
                if self.token_limit_parameter is None
                else self.token_limit_parameter
            ),
            "response_format": {
                "policy": self.response_format_policy,
                "value": (
                    None
                    if self.response_format_value is None
                    else _json_clone(self.response_format_value)
                ),
            },
            "temperature_policy": self.temperature_policy,
            "seed_policy": self.seed_policy,
            "timeout": {
                "placement": self.timeout_placement,
                "parameter": self.timeout_parameter,
            },
            "client_retry_policy": {"max_retries": self.client_max_retries},
            "http_transport_policy": _json_clone(self.http_transport_policy),
            "disallowed_simultaneous_fields": [
                list(item) for item in self.disallowed_simultaneous_fields
            ],
        }

    @property
    def sha256(self) -> str:
        return _hash(self.to_dict())


@dataclass(frozen=True, slots=True)
class CanonicalOutboundRequestBinding:
    capability_profile_version: str
    capability_profile_hash: str
    provider_request_contract_hash: str
    adapter_version: str
    sdk_operation_version: str
    sdk_distribution_manifest_hash: str
    prompt_sha256: str
    context_sha256: str
    messages_sha256: str
    outbound_top_level_keys: tuple[str, ...]
    outbound_top_level_key_set_hash: str
    outbound_request_kwargs_hash: str
    client_option_keys: tuple[str, ...]
    client_options_hash: str
    canonical_request_binding_hash: str
    schema_version: str = CANONICAL_OUTBOUND_REQUEST_SCHEMA

    def __post_init__(self) -> None:
        if self.schema_version != CANONICAL_OUTBOUND_REQUEST_SCHEMA:
            raise ProviderRequestCapabilityError(
                "unsupported_canonical_outbound_schema"
            )
        for name in (
            "capability_profile_version",
            "adapter_version",
            "sdk_operation_version",
        ):
            _nonempty_string(getattr(self, name), name)
        for name in (
            "capability_profile_hash",
            "provider_request_contract_hash",
            "sdk_distribution_manifest_hash",
            "prompt_sha256",
            "context_sha256",
            "messages_sha256",
            "outbound_top_level_key_set_hash",
            "outbound_request_kwargs_hash",
            "client_options_hash",
            "canonical_request_binding_hash",
        ):
            if not _SHA256_RE.fullmatch(str(getattr(self, name))):
                raise ProviderRequestCapabilityError(f"{name}_not_sha256")
        for name in ("outbound_top_level_keys", "client_option_keys"):
            values = tuple(getattr(self, name))
            if values != tuple(sorted(set(values))) or any(
                not isinstance(item, str) or not item for item in values
            ):
                raise ProviderRequestCapabilityError(f"{name}_not_sorted_unique")
            object.__setattr__(self, name, values)
        if _hash(list(self.outbound_top_level_keys)) != self.outbound_top_level_key_set_hash:
            raise ProviderRequestCapabilityError("outbound_top_level_key_set_hash_mismatch")

    @classmethod
    def from_mapping(
        cls,
        value: Mapping[str, Any],
    ) -> "CanonicalOutboundRequestBinding":
        expected = frozenset(
            {
                "schema_version",
                "capability_profile_version",
                "capability_profile_hash",
                "provider_request_contract_hash",
                "adapter_version",
                "sdk_operation_version",
                "sdk_distribution_manifest_hash",
                "prompt_sha256",
                "context_sha256",
                "messages_sha256",
                "outbound_top_level_keys",
                "outbound_top_level_key_set_hash",
                "outbound_request_kwargs_hash",
                "client_option_keys",
                "client_options_hash",
                "canonical_request_binding_hash",
            }
        )
        source = _exact_fields(value, expected, "canonical_outbound_binding")
        return cls(
            schema_version=source["schema_version"],
            capability_profile_version=source["capability_profile_version"],
            capability_profile_hash=source["capability_profile_hash"],
            provider_request_contract_hash=source["provider_request_contract_hash"],
            adapter_version=source["adapter_version"],
            sdk_operation_version=source["sdk_operation_version"],
            sdk_distribution_manifest_hash=source[
                "sdk_distribution_manifest_hash"
            ],
            prompt_sha256=source["prompt_sha256"],
            context_sha256=source["context_sha256"],
            messages_sha256=source["messages_sha256"],
            outbound_top_level_keys=tuple(source["outbound_top_level_keys"]),
            outbound_top_level_key_set_hash=source[
                "outbound_top_level_key_set_hash"
            ],
            outbound_request_kwargs_hash=source["outbound_request_kwargs_hash"],
            client_option_keys=tuple(source["client_option_keys"]),
            client_options_hash=source["client_options_hash"],
            canonical_request_binding_hash=source[
                "canonical_request_binding_hash"
            ],
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "capability_profile_version": self.capability_profile_version,
            "capability_profile_hash": self.capability_profile_hash,
            "provider_request_contract_hash": self.provider_request_contract_hash,
            "adapter_version": self.adapter_version,
            "sdk_operation_version": self.sdk_operation_version,
            "sdk_distribution_manifest_hash": (
                self.sdk_distribution_manifest_hash
            ),
            "prompt_sha256": self.prompt_sha256,
            "context_sha256": self.context_sha256,
            "messages_sha256": self.messages_sha256,
            "outbound_top_level_keys": list(self.outbound_top_level_keys),
            "outbound_top_level_key_set_hash": self.outbound_top_level_key_set_hash,
            "outbound_request_kwargs_hash": self.outbound_request_kwargs_hash,
            "client_option_keys": list(self.client_option_keys),
            "client_options_hash": self.client_options_hash,
            "canonical_request_binding_hash": self.canonical_request_binding_hash,
        }


@dataclass(frozen=True, slots=True)
class CanonicalOutboundRequest:
    request_kwargs: Mapping[str, Any]
    client_options: Mapping[str, Any]
    binding: CanonicalOutboundRequestBinding

    def __post_init__(self) -> None:
        request = _json_clone(self.request_kwargs)
        client = _json_clone(self.client_options)
        if not isinstance(request, dict) or not isinstance(client, dict):
            raise ProviderRequestCapabilityError("canonical_outbound_not_object")
        object.__setattr__(self, "request_kwargs", MappingProxyType(request))
        object.__setattr__(self, "client_options", MappingProxyType(client))

    def request_kwargs_dict(self) -> dict[str, Any]:
        return _json_clone(self.request_kwargs)

    def client_options_dict(self) -> dict[str, Any]:
        return _json_clone(self.client_options)


def build_canonical_outbound_request(
    binding: ProviderRequestBinding,
    profile: ProviderModelRequestCapability,
    *,
    base_url: str,
    sdk_distribution_manifest: Mapping[str, Any],
) -> CanonicalOutboundRequest:
    """Build the only accepted SDK kwargs for this exact provider/model."""

    if not isinstance(binding, ProviderRequestBinding):
        raise ProviderRequestCapabilityError("provider_binding_type_mismatch")
    if not isinstance(profile, ProviderModelRequestCapability):
        raise ProviderRequestCapabilityError("capability_profile_absent")
    mismatch = []
    if binding.provider != profile.provider:
        mismatch.append("provider")
    if binding.model_id != profile.model_id:
        mismatch.append("model")
    if binding.adapter_version != profile.adapter_version:
        mismatch.append("adapter")
    if mismatch:
        raise ProviderRequestCapabilityError(
            "capability_binding_mismatch:" + ",".join(mismatch)
        )
    sdk_distribution = validate_provider_sdk_distribution_manifest(
        sdk_distribution_manifest
    )
    if (
        not isinstance(base_url, str)
        or base_url != base_url.strip()
        or not base_url.startswith("https://")
        or any(character in base_url for character in ("@", "?", "#"))
    ):
        raise ProviderRequestCapabilityError("explicit_https_base_url_required")
    settings = binding.settings
    if settings.max_retries != 0 or settings.retry_on or settings.max_requests != 1:
        raise ProviderRequestCapabilityError("strict_single_request_policy_mismatch")

    request: dict[str, Any] = {
        "model": binding.model_id,
        "messages": [
            {
                "role": "system",
                "content": binding.prompt_bytes.decode("utf-8"),
            },
            {
                "role": "user",
                "content": binding.context_bytes.decode("utf-8"),
            },
        ],
    }
    token_parameter = profile.token_limit_parameter
    if token_parameter is None:
        if settings.max_tokens is not None:
            raise ProviderRequestCapabilityError(
                "omitted_token_limit_requires_unset_value"
            )
    else:
        if settings.max_tokens is None:
            raise ProviderRequestCapabilityError(
                "included_token_limit_requires_value"
            )
        request[token_parameter] = settings.max_tokens

    if profile.response_format_policy == "include":
        if settings.response_format != "json_object":
            raise ProviderRequestCapabilityError(
                "response_format_setting_profile_mismatch"
            )
        request["response_format"] = _json_clone(profile.response_format_value)

    if profile.temperature_policy == "include":
        if settings.temperature is None:
            raise ProviderRequestCapabilityError("included_temperature_requires_value")
        if not math.isfinite(float(settings.temperature)):
            raise ProviderRequestCapabilityError("temperature_not_finite")
        request["temperature"] = settings.temperature
    elif settings.temperature is not None:
        raise ProviderRequestCapabilityError(
            "omitted_temperature_requires_unset_value"
        )

    if profile.seed_policy == "include":
        if settings.seed is None:
            raise ProviderRequestCapabilityError("included_seed_requires_value")
        request["seed"] = settings.seed
    elif settings.seed is not None:
        raise ProviderRequestCapabilityError("omitted_seed_requires_unset_value")

    client_options: dict[str, Any] = {
        "base_url": base_url,
        "max_retries": profile.client_max_retries,
        # This is declarative evidence.  The strict transport replaces the
        # mapping with the one matching bound client object immediately before
        # AsyncOpenAI construction.
        "http_client": _json_clone(profile.http_transport_policy),
    }
    if profile.timeout_placement == "client":
        client_options[profile.timeout_parameter] = settings.timeout_seconds
    else:
        request[profile.timeout_parameter] = settings.timeout_seconds

    for first, second in profile.disallowed_simultaneous_fields:
        if first in request and second in request:
            raise ProviderRequestCapabilityError(
                f"contradictory_outbound_fields:{first},{second}"
            )

    request_keys = tuple(sorted(request))
    client_keys = tuple(sorted(client_options))
    request_hash = _hash(request)
    request_key_hash = _hash(list(request_keys))
    client_hash = _hash(client_options)
    binding_payload = {
        "schema_version": CANONICAL_OUTBOUND_REQUEST_SCHEMA,
        "capability_profile_version": profile.profile_version,
        "capability_profile_hash": profile.sha256,
        "provider_request_contract_hash": binding.request_contract_hash,
        "adapter_version": profile.adapter_version,
        "sdk_operation_version": profile.sdk_operation_version,
        "sdk_distribution_manifest_hash": sdk_distribution["manifest_sha256"],
        "prompt_sha256": binding.prompt_hash,
        "context_sha256": binding.context_hash,
        "messages_sha256": _hash(request["messages"]),
        "outbound_top_level_keys": list(request_keys),
        "outbound_top_level_key_set_hash": request_key_hash,
        "outbound_request_kwargs_hash": request_hash,
        "client_option_keys": list(client_keys),
        "client_options_hash": client_hash,
    }
    canonical_binding = CanonicalOutboundRequestBinding(
        **binding_payload,
        canonical_request_binding_hash=_hash(binding_payload),
    )
    return CanonicalOutboundRequest(request, client_options, canonical_binding)


def verify_canonical_outbound_binding(
    actual: CanonicalOutboundRequestBinding,
    expected: CanonicalOutboundRequestBinding,
) -> None:
    if not isinstance(actual, CanonicalOutboundRequestBinding) or not isinstance(
        expected, CanonicalOutboundRequestBinding
    ):
        raise ProviderRequestCapabilityError("canonical_outbound_binding_absent")
    if actual.to_dict() != expected.to_dict():
        raise ProviderRequestCapabilityError("canonical_outbound_binding_mismatch")


__all__ = [
    "CANONICAL_OUTBOUND_REQUEST_SCHEMA",
    "PROVIDER_REQUEST_CAPABILITY_SCHEMA",
    "PROVIDER_SDK_DISTRIBUTION_SCHEMA",
    "CanonicalOutboundRequest",
    "CanonicalOutboundRequestBinding",
    "ProviderModelRequestCapability",
    "ProviderRequestCapabilityError",
    "build_canonical_outbound_request",
    "provider_sdk_distribution_manifest",
    "strict_http_transport_policy",
    "validate_provider_sdk_distribution_manifest",
    "verify_canonical_outbound_binding",
]
