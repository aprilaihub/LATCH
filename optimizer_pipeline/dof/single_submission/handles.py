"""Stable per-trial typed pseudonymous object handles."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import hmac
import re
from types import MappingProxyType
from typing import Any, Iterable, Mapping


HANDLE_SCHEMA_VERSION = "dof.single-submission.object-handles.v1"
_TYPE_RE = re.compile(r"^[a-z][a-z0-9_]{0,23}$")
_HANDLE_RE = re.compile(r"^(?P<kind>[a-z][a-z0-9_]{0,23})_h(?P<token>[0-9a-f]{24})$")


class HandleError(ValueError):
    """A public handle could not be safely resolved."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


@dataclass(frozen=True, slots=True)
class PrivateObject:
    handle: str
    object_type: str
    canonical_identity: str
    attributes: Mapping[str, Any]
    source_tool: str = "vivado"

    def __post_init__(self) -> None:
        object.__setattr__(self, "attributes", MappingProxyType(dict(self.attributes)))


class TrialHandleRegistry:
    """One root/trial-bound handle registry.

    The secret and private identities are never part of public evidence.  A
    handle is stable for the registry lifetime, but a handle from another
    trial/root cannot resolve because both bindings participate in the HMAC.
    """

    def __init__(self, *, trial_id: str, root_sha256: str, secret: bytes) -> None:
        if not trial_id:
            raise ValueError("trial_id_required")
        if not re.fullmatch(r"[0-9a-f]{64}", root_sha256):
            raise ValueError("root_sha256_invalid")
        if not isinstance(secret, bytes) or len(secret) < 32:
            raise ValueError("handle_secret_must_have_at_least_32_bytes")
        self._trial_id = trial_id
        self._root_sha256 = root_sha256
        self._secret = bytes(secret)
        self._by_handle: dict[str, PrivateObject] = {}
        self._by_identity: dict[tuple[str, str], str] = {}

    @property
    def trial_binding_hash(self) -> str:
        return hashlib.sha256(
            f"{self._trial_id}\0{self._root_sha256}".encode("utf-8")
        ).hexdigest()

    @property
    def root_sha256(self) -> str:
        """Return the immutable root binding without exposing private identities."""

        return self._root_sha256

    def register(
        self,
        *,
        object_type: str,
        canonical_identity: str,
        attributes: Mapping[str, Any] | None = None,
        source_tool: str = "vivado",
    ) -> str:
        if not _TYPE_RE.fullmatch(object_type):
            raise HandleError("object_type_invalid")
        if not canonical_identity or "\x00" in canonical_identity:
            raise HandleError("canonical_identity_invalid")
        key = (object_type, canonical_identity)
        existing = self._by_identity.get(key)
        if existing is not None:
            current = self._by_handle[existing]
            if dict(current.attributes) != dict(attributes or {}):
                raise HandleError("identity_attributes_changed_within_trial")
            return existing
        message = "\0".join(
            (self._trial_id, self._root_sha256, object_type, canonical_identity)
        ).encode("utf-8")
        token = hmac.new(self._secret, message, hashlib.sha256).hexdigest()[:24]
        handle = f"{object_type}_h{token}"
        if handle in self._by_handle:
            raise HandleError("handle_collision")
        obj = PrivateObject(
            handle,
            object_type,
            canonical_identity,
            dict(attributes or {}),
            source_tool,
        )
        self._by_handle[handle] = obj
        self._by_identity[key] = handle
        return handle

    def resolve(self, handle: str, *, expected_type: str | None = None) -> PrivateObject:
        match = _HANDLE_RE.fullmatch(handle or "")
        if match is None:
            raise HandleError("handle_syntax_invalid")
        if expected_type is not None and match.group("kind") != expected_type:
            raise HandleError("handle_type_mismatch")
        try:
            obj = self._by_handle[handle]
        except KeyError as exc:
            raise HandleError("unknown_or_stale_handle") from exc
        if expected_type is not None and obj.object_type != expected_type:
            raise HandleError("handle_type_mismatch")
        return obj

    def objects(self, *, object_type: str | None = None) -> tuple[PrivateObject, ...]:
        """Return a deterministic private resolver view for the compiler."""

        values = (
            item
            for item in self._by_handle.values()
            if object_type is None or item.object_type == object_type
        )
        return tuple(sorted(values, key=lambda item: (item.object_type, item.canonical_identity)))

    def public_manifest(self) -> dict[str, Any]:
        counts: dict[str, int] = {}
        for obj in self._by_handle.values():
            counts[obj.object_type] = counts.get(obj.object_type, 0) + 1
        payload = {
            "schema_version": HANDLE_SCHEMA_VERSION,
            "trial_binding_hash": self.trial_binding_hash,
            "object_counts": dict(sorted(counts.items())),
            "handles": sorted(self._by_handle),
        }
        return payload | {"manifest_sha256": hashlib.sha256(repr(payload).encode()).hexdigest()}

    def validate_cross_tool_identity_map(
        self,
        *,
        vivado_identities: Iterable[str],
        rapidwright_to_vivado: Mapping[str, str],
    ) -> dict[str, Any]:
        vivado = tuple(vivado_identities)
        mapped = tuple(rapidwright_to_vivado.values())
        errors: list[str] = []
        if len(vivado) != len(set(vivado)):
            errors.append("vivado_identity_not_unique")
        if len(rapidwright_to_vivado) != len(set(rapidwright_to_vivado)):
            errors.append("rapidwright_identity_not_unique")
        if len(mapped) != len(set(mapped)):
            errors.append("rapidwright_many_to_one_mapping")
        unknown = sorted(set(mapped) - set(vivado))
        if unknown:
            errors.append("mapped_vivado_identity_unknown")
        return {
            "schema_version": "dof.single-submission.cross-tool-identity-audit.v1",
            "vivado_count": len(vivado),
            "rapidwright_count": len(rapidwright_to_vivado),
            "mapped_count": len(mapped),
            "accepted": not errors,
            "errors": errors,
        }


__all__ = ["HANDLE_SCHEMA_VERSION", "HandleError", "PrivateObject", "TrialHandleRegistry"]
