"""One-physical-request OpenAI transport for the WP-004A provider smoke."""

from __future__ import annotations

import asyncio
import importlib
import os
import importlib.metadata
import importlib.util
from pathlib import Path
import sys
from typing import Any, Callable, Mapping

from .provider import (
    PROVIDER_TRANSPORT_CONTRACT_VERSION,
    ProviderBindingError,
    ProviderRequestBinding,
    ProviderTransportError,
    RawProviderResponse,
)
from .provider_request_capabilities import (
    CanonicalOutboundRequestBinding,
    ProviderModelRequestCapability,
    ProviderRequestCapabilityError,
    build_canonical_outbound_request,
    provider_sdk_distribution_manifest,
    strict_http_transport_policy,
    validate_provider_sdk_distribution_manifest,
    verify_canonical_outbound_binding,
)


OPENAI_SDK_AMBIENT_CONFIGURATION_KEYS = (
    "OPENAI_ADMIN_KEY",
    "OPENAI_BASE_URL",
    "OPENAI_CUSTOM_HEADERS",
    "OPENAI_LOG",
    "OPENAI_LOG_LEVEL",
    "OPENAI_ORG_ID",
    "OPENAI_PROJECT_ID",
    "OPENAI_WEBHOOK_SECRET",
    "ALL_PROXY",
    "CURL_CA_BUNDLE",
    "HTTPS_PROXY",
    "HTTP_PROXY",
    "NO_PROXY",
    "REQUESTS_CA_BUNDLE",
    "SSL_CERT_DIR",
    "SSL_CERT_FILE",
    "SSLKEYLOGFILE",
    "all_proxy",
    "https_proxy",
    "http_proxy",
    "no_proxy",
    "PYTHONHOME",
    "PYTHONPATH",
    "PYTHONSTARTUP",
    "PYTHONUSERBASE",
)


def reject_openai_sdk_ambient_configuration(
    environment: Mapping[str, str] | None = None,
) -> None:
    """Reject SDK-consumed ambient request/client configuration by key only."""

    source = os.environ if environment is None else environment
    present = tuple(
        key for key in OPENAI_SDK_AMBIENT_CONFIGURATION_KEYS if key in source
    )
    if present:
        raise ProviderRequestCapabilityError(
            "openai_sdk_ambient_configuration_forbidden:" + ",".join(present)
        )


def _attested_openai_module_origin() -> Path:
    try:
        distribution = importlib.metadata.distribution("openai")
    except Exception as exc:
        raise ProviderRequestCapabilityError(
            "openai_sdk_distribution_unavailable"
        ) from exc
    expected = Path(
        distribution.locate_file("openai/__init__.py")
    ).resolve(strict=True)
    spec = importlib.util.find_spec("openai")
    if spec is None or spec.origin is None:
        raise ProviderRequestCapabilityError("openai_sdk_module_unavailable")
    try:
        observed = Path(spec.origin).resolve(strict=True)
    except OSError as exc:
        raise ProviderRequestCapabilityError(
            "openai_sdk_module_origin_unavailable"
        ) from exc
    if observed != expected:
        raise ProviderRequestCapabilityError(
            "openai_sdk_module_origin_mismatch"
        )
    return expected


def _attested_runtime_module_origins(
    manifest: Mapping[str, Any],
    *,
    import_modules: bool,
) -> dict[str, Path]:
    """Verify every executable dependency resolves to its bound distribution."""

    validated = validate_provider_sdk_distribution_manifest(manifest)
    origins: dict[str, Path] = {}
    for row in validated["distributions"]:
        distribution_name = row["distribution"]
        import_name = row["import_name"]
        try:
            distribution = importlib.metadata.distribution(distribution_name)
            root = Path(distribution.locate_file("")).resolve(strict=True)
        except Exception as exc:
            raise ProviderRequestCapabilityError(
                f"provider_runtime_distribution_unavailable:{distribution_name}"
            ) from exc
        allowed_files: set[Path] = set()
        for packaged in distribution.files or ():
            try:
                target = Path(distribution.locate_file(packaged)).resolve(
                    strict=True
                )
                target.relative_to(root)
            except (OSError, ValueError):
                continue
            if target.is_file():
                allowed_files.add(target)
        spec = importlib.util.find_spec(import_name)
        if spec is None or spec.origin is None:
            raise ProviderRequestCapabilityError(
                f"provider_runtime_module_unavailable:{import_name}"
            )
        try:
            origin = Path(spec.origin).resolve(strict=True)
        except OSError as exc:
            raise ProviderRequestCapabilityError(
                f"provider_runtime_module_origin_unavailable:{import_name}"
            ) from exc
        if origin not in allowed_files:
            raise ProviderRequestCapabilityError(
                f"provider_runtime_module_origin_mismatch:{import_name}"
            )
        if import_modules:
            module = importlib.import_module(import_name)
            module_path = getattr(module, "__file__", None)
            if module_path is None or Path(module_path).resolve(strict=True) != origin:
                raise ProviderRequestCapabilityError(
                    f"provider_runtime_import_origin_mismatch:{import_name}"
                )
        elif import_name in sys.modules:
            module_path = getattr(sys.modules[import_name], "__file__", None)
            if module_path is None or Path(module_path).resolve(strict=True) != origin:
                raise ProviderRequestCapabilityError(
                    f"provider_runtime_import_origin_mismatch:{import_name}"
                )
        origins[import_name] = origin
    return origins


class OpenAIChatCompletionsTransport:
    """Strict adapter with SDK retries disabled and no response repair."""

    transport_contract_version = PROVIDER_TRANSPORT_CONTRACT_VERSION
    internal_retries_disabled = True
    requires_canonical_outbound_binding = True
    adapter_version = "dof.openai-chat-completions-transport.v2"
    sdk_operation = "chat.completions.create"
    sdk_operation_version = "dof.openai.chat-completions-create.v1"

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str,
        capability_profile: ProviderModelRequestCapability,
        sdk_distribution_manifest: Mapping[str, Any],
        expected_outbound_binding: CanonicalOutboundRequestBinding,
        client_factory: Callable[..., Any] | None = None,
        http_client_factory: Callable[..., Any] | None = None,
    ) -> None:
        if not isinstance(api_key, str) or not api_key:
            raise ProviderBindingError("provider credential unavailable")
        reject_openai_sdk_ambient_configuration()
        self._expected_openai_module_origin = _attested_openai_module_origin()
        self._api_key = api_key
        self._base_url = base_url
        if not isinstance(capability_profile, ProviderModelRequestCapability):
            raise ProviderRequestCapabilityError("capability_profile_absent")
        if capability_profile.adapter_type != "openai":
            raise ProviderRequestCapabilityError("provider_adapter_type_mismatch")
        if capability_profile.adapter_version != self.adapter_version:
            raise ProviderRequestCapabilityError("provider_adapter_version_mismatch")
        if capability_profile.sdk_operation != self.sdk_operation:
            raise ProviderRequestCapabilityError("provider_sdk_operation_mismatch")
        if capability_profile.sdk_operation_version != self.sdk_operation_version:
            raise ProviderRequestCapabilityError(
                "provider_sdk_operation_version_mismatch"
            )
        if (
            dict(capability_profile.http_transport_policy)
            != strict_http_transport_policy()
        ):
            raise ProviderRequestCapabilityError(
                "provider_http_transport_policy_mismatch"
            )
        expected_sdk = validate_provider_sdk_distribution_manifest(
            sdk_distribution_manifest
        )
        if provider_sdk_distribution_manifest() != expected_sdk:
            raise ProviderRequestCapabilityError(
                "provider_sdk_distribution_mismatch"
            )
        self._expected_runtime_origins = _attested_runtime_module_origins(
            expected_sdk,
            import_modules=False,
        )
        if not isinstance(expected_outbound_binding, CanonicalOutboundRequestBinding):
            raise ProviderRequestCapabilityError("expected_outbound_binding_absent")
        self._capability_profile = capability_profile
        self._sdk_distribution_manifest = expected_sdk
        self._expected_outbound_binding = expected_outbound_binding
        self._client_factory = client_factory
        self._http_client_factory = http_client_factory
        self.send_attempts = 0
        self.sdk_create_calls = 0
        self.physical_requests = 0
        self.cleanup_status = "NOT_ATTEMPTED"
        self.cleanup_error: str | None = None
        self.prepared_outbound_binding: CanonicalOutboundRequestBinding | None = None

    async def _observe_http_request(self, _request: Any) -> None:
        """Count at the bound httpx send hook, never at SDK method entry."""

        if self.physical_requests != 0:
            raise ProviderBindingError("single physical request transport exhausted")
        self.physical_requests += 1

    async def _bounded_close(
        self,
        *,
        client: Any,
        http_client: Any,
        use_default_client: bool,
        deadline: float,
    ) -> bool:
        if client is None and http_client is None:
            self.cleanup_status = "NOT_REQUIRED"
            return True

        async def close_once() -> None:
            if use_default_client and client is not None:
                await client.close()
            elif http_client is not None:
                await http_client.aclose()

        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            self.cleanup_status = "DEADLINE_EXHAUSTED"
            self.cleanup_error = "provider_cleanup_deadline_exhausted"
            return False
        cleanup_task = asyncio.create_task(close_once())
        try:
            await asyncio.wait_for(asyncio.shield(cleanup_task), timeout=remaining)
        except TimeoutError:
            cleanup_task.cancel()
            cleanup_task.add_done_callback(
                lambda task: None if task.cancelled() else task.exception()
            )
            self.cleanup_status = "TIMED_OUT"
            self.cleanup_error = "provider_cleanup_timed_out"
            return False
        except asyncio.CancelledError:
            cleanup_task.cancel()
            cleanup_task.add_done_callback(
                lambda task: None if task.cancelled() else task.exception()
            )
            self.cleanup_status = "CANCELLED"
            self.cleanup_error = "provider_cleanup_cancelled"
            raise
        except Exception as exc:
            self.cleanup_status = "FAILED"
            self.cleanup_error = type(exc).__name__
            return False
        self.cleanup_status = "SUCCEEDED"
        return True

    async def send(
        self,
        binding: ProviderRequestBinding,
        retry_index: int,
    ) -> RawProviderResponse:
        if retry_index != 0 or self.send_attempts != 0:
            raise ProviderBindingError("single physical request transport exhausted")
        self.send_attempts += 1
        # Start the one non-renewable request+cleanup deadline before any
        # profile rebuild, runtime re-attestation, import, or client work.
        loop = asyncio.get_running_loop()
        deadline = loop.time() + binding.settings.timeout_seconds
        if binding.adapter_version != self.adapter_version:
            raise ProviderBindingError("provider adapter version mismatch")
        prepared = build_canonical_outbound_request(
            binding,
            self._capability_profile,
            base_url=self._base_url,
            sdk_distribution_manifest=self._sdk_distribution_manifest,
        )
        verify_canonical_outbound_binding(
            prepared.binding,
            self._expected_outbound_binding,
        )
        self.prepared_outbound_binding = prepared.binding
        if _attested_openai_module_origin() != self._expected_openai_module_origin:
            raise ProviderRequestCapabilityError(
                "provider_sdk_import_binding_mismatch"
            )
        observed_runtime_origins = _attested_runtime_module_origins(
            self._sdk_distribution_manifest,
            import_modules=True,
        )
        if observed_runtime_origins != self._expected_runtime_origins:
            raise ProviderRequestCapabilityError(
                "provider_runtime_import_binding_mismatch"
            )
        module = importlib.import_module("openai")
        if (
            Path(module.__file__).resolve(strict=True)
            != self._expected_openai_module_origin
            or provider_sdk_distribution_manifest()
            != self._sdk_distribution_manifest
        ):
            raise ProviderRequestCapabilityError(
                "provider_sdk_import_binding_mismatch"
            )
        use_default_client = self._client_factory is None
        factory = self._client_factory or module.AsyncOpenAI
        http_factory = (
            self._http_client_factory or module.DefaultAsyncHttpxClient
        )
        client_options = prepared.client_options_dict()
        declared_http_policy = client_options.pop("http_client", None)
        expected_http_policy = strict_http_transport_policy()
        if declared_http_policy != expected_http_policy:
            raise ProviderRequestCapabilityError(
                "canonical_http_transport_policy_mismatch"
            )
        http_kwargs = dict(expected_http_policy)
        http_kwargs.pop("implementation")
        request_counter = http_kwargs.pop("request_counter")
        cleanup_policy = http_kwargs.pop("cleanup_deadline_policy")
        cleanup_reserve_seconds = float(
            http_kwargs.pop("cleanup_reserve_seconds")
        )
        if (
            request_counter != "httpx.async-request-event-hook.v1"
            or cleanup_policy != "single-nonrenewable-deadline.v1"
            or cleanup_reserve_seconds != 1.0
        ):
            raise ProviderRequestCapabilityError(
                "canonical_http_runtime_policy_mismatch"
            )
        http_kwargs["event_hooks"] = {"request": [self._observe_http_request]}
        request_deadline = deadline - min(
            cleanup_reserve_seconds,
            binding.settings.timeout_seconds / 2.0,
        )
        http_client = None
        client = None
        result: RawProviderResponse | None = None
        primary_error: BaseException | None = None
        try:
            http_client = http_factory(**http_kwargs)
            client = factory(
                api_key=self._api_key,
                http_client=http_client,
                **client_options,
            )
            # With max_retries=0 and the bound HTTP client configured with
            # follow_redirects=False/trust_env=False, one SDK create call can
            # emit at most one physical HTTP request.
            self.sdk_create_calls += 1
            async with asyncio.timeout_at(request_deadline):
                response = await client.chat.completions.create(
                    **prepared.request_kwargs_dict()
                )
            choices = getattr(response, "choices", None)
            content = (
                getattr(getattr(choices[0], "message", None), "content", None)
                if isinstance(choices, list) and len(choices) == 1
                else None
            )
            content_bytes = content.encode("utf-8") if isinstance(content, str) else b""
            usage = getattr(response, "usage", None)
            if usage is None:
                input_tokens = output_tokens = total_tokens = None
            else:
                input_tokens = getattr(usage, "prompt_tokens", None)
                output_tokens = getattr(usage, "completion_tokens", None)
                total_tokens = getattr(usage, "total_tokens", None)
            result = RawProviderResponse(
                content_bytes=content_bytes,
                actual_provider=binding.provider,
                provider_request_id=getattr(response, "id", None),
                returned_model_id=getattr(response, "model", None),
                returned_model_revision=getattr(
                    response, "system_fingerprint", None
                ),
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                total_tokens=total_tokens,
                cost_usd=None,
                cost_source=None,
                observed_request_contract_hash=binding.request_contract_hash,
            )
        except BaseException as exc:
            primary_error = exc
        finally:
            cleanup_ok = await self._bounded_close(
                client=client,
                http_client=http_client,
                use_default_client=use_default_client,
                deadline=deadline,
            )
        if primary_error is not None:
            raise primary_error
        if not cleanup_ok:
            raise ProviderTransportError(
                self.cleanup_error or "provider_transport_cleanup_failure"
            )
        if result is None:
            raise ProviderTransportError("provider_response_not_constructed")
        return result


__all__ = [
    "OPENAI_SDK_AMBIENT_CONFIGURATION_KEYS",
    "OpenAIChatCompletionsTransport",
    "reject_openai_sdk_ambient_configuration",
]
