"""Canonical, content-addressed mask rules for DoF experiment boundaries.

This module is deliberately dependency-free within :mod:`optimizer_pipeline.dof`
so the specification parser, policy-view projector, and action gate can all
validate the same object without an import cycle.  A syntactically plausible
digest is not sufficient: every consumer recomputes the digest over the exact
v1 manifest payload before using any rule.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
import hashlib
import json
import re
from typing import Any


MASK_MANIFEST_SCHEMA_VERSION = "dof.mask-manifest.v1"
_MASK_FIELDS = frozenset(
    {
        "version",
        "hash",
        "hidden_macro_families",
        "forbidden_sentinels",
        "known_prior_allowlist",
    }
)
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class MaskRulesValidationError(ValueError):
    """One or more failures while binding a mask to its manifest digest."""

    def __init__(self, reasons: Iterable[str]) -> None:
        self.reasons = tuple(str(reason) for reason in reasons)
        super().__init__("; ".join(self.reasons))


@dataclass(frozen=True, slots=True)
class ValidatedMaskRules:
    """Canonical mask values after exact manifest/hash validation."""

    version: str
    hash: str
    hidden_macro_families: tuple[str, ...]
    forbidden_sentinels: tuple[str, ...]
    known_prior_allowlist: tuple[str, ...]

    def to_spec_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "hash": self.hash,
            "hidden_macro_families": list(self.hidden_macro_families),
            "forbidden_sentinels": list(self.forbidden_sentinels),
            "known_prior_allowlist": list(self.known_prior_allowlist),
        }

    # Common protocol spelling used by the gate/projector validator.
    to_dict = to_spec_dict

    def to_manifest_dict(self) -> dict[str, Any]:
        return mask_manifest_payload(
            self.version,
            self.hidden_macro_families,
            self.forbidden_sentinels,
            self.known_prior_allowlist,
        ) | {"hash": self.hash}


def mask_manifest_payload(
    version: str,
    hidden_macro_families: Iterable[str] = (),
    forbidden_sentinels: Iterable[str] = (),
    known_prior_allowlist: Iterable[str] = (),
) -> dict[str, Any]:
    """Return the canonical payload whose bytes define a mask hash."""

    return {
        "schema_version": MASK_MANIFEST_SCHEMA_VERSION,
        "version": version,
        "hidden_macro_families": sorted(hidden_macro_families),
        "forbidden_sentinels": sorted(forbidden_sentinels),
        "known_prior_allowlist": sorted(known_prior_allowlist),
    }


def mask_content_hash(payload: Mapping[str, Any]) -> str:
    """Hash a canonical mask manifest payload."""

    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def create_mask_rules(
    *,
    version: str,
    hidden_macro_families: Iterable[str] = (),
    forbidden_sentinels: Iterable[str] = (),
    known_prior_allowlist: Iterable[str] = (),
) -> ValidatedMaskRules:
    """Create canonical rules and their content hash from trusted inputs."""

    parsed_version = _version(version)
    hidden = _string_tuple(
        list(hidden_macro_families), "mask.hidden_macro_families"
    )
    sentinels = _string_tuple(
        list(forbidden_sentinels), "mask.forbidden_sentinels"
    )
    priors = _string_tuple(
        list(known_prior_allowlist), "mask.known_prior_allowlist"
    )
    payload = mask_manifest_payload(parsed_version, hidden, sentinels, priors)
    return ValidatedMaskRules(
        version=parsed_version,
        hash=mask_content_hash(payload),
        hidden_macro_families=hidden,
        forbidden_sentinels=sentinels,
        known_prior_allowlist=priors,
    )


def validate_mask_rules(mask: Any) -> ValidatedMaskRules:
    """Return canonical rules only when ``mask`` matches its declared hash.

    Experiment ``MaskSpec`` instances are accepted through their exact
    ``to_dict()`` representation.  Exact specification mappings and immutable
    manifest mappings are also accepted, which keeps offline evidence probes
    straightforward without trusting arbitrary attributes.
    """

    if mask is None:
        raise MaskRulesValidationError(("mask_missing",))
    if isinstance(mask, Mapping):
        try:
            raw = dict(mask)
        except Exception as exc:
            raise MaskRulesValidationError(
                (f"mask_mapping_copy_failed:{type(exc).__name__}",)
            ) from exc
    else:
        to_dict = getattr(mask, "to_dict", None)
        if not callable(to_dict):
            raise MaskRulesValidationError(("mask_not_validatable_object",))
        try:
            raw_value = to_dict()
        except Exception as exc:
            raise MaskRulesValidationError(
                (f"mask.to_dict_failed:{type(exc).__name__}",)
            ) from exc
        if not isinstance(raw_value, Mapping):
            raise MaskRulesValidationError(("mask.to_dict_not_object",))
        try:
            raw = dict(raw_value)
        except Exception as exc:
            raise MaskRulesValidationError(
                (f"mask_mapping_copy_failed:{type(exc).__name__}",)
            ) from exc

    reasons: list[str] = []
    if not all(isinstance(key, str) for key in raw):
        reasons.append("mask.has_non_string_key")
    schema_version_present = "schema_version" in raw
    schema_version = raw.pop("schema_version", None)
    if schema_version_present and schema_version != MASK_MANIFEST_SCHEMA_VERSION:
        reasons.append(f"mask.schema_version_unsupported:{schema_version}")
    string_keys = {key for key in raw if isinstance(key, str)}
    unknown = sorted(string_keys - _MASK_FIELDS)
    missing = sorted(_MASK_FIELDS - string_keys)
    if unknown:
        reasons.append("mask.unknown_fields:" + ",".join(unknown))
    if missing:
        reasons.append("mask.missing_fields:" + ",".join(missing))
    if reasons:
        raise MaskRulesValidationError(reasons)

    version = _version(raw["version"])
    hidden = _string_tuple(
        raw["hidden_macro_families"], "mask.hidden_macro_families"
    )
    sentinels = _string_tuple(
        raw["forbidden_sentinels"], "mask.forbidden_sentinels"
    )
    priors = _string_tuple(
        raw["known_prior_allowlist"], "mask.known_prior_allowlist"
    )
    digest = raw["hash"]
    if not isinstance(digest, str) or not _SHA256_RE.fullmatch(digest):
        raise MaskRulesValidationError(("mask.hash_not_sha256",))
    expected = mask_content_hash(
        mask_manifest_payload(version, hidden, sentinels, priors)
    )
    if digest != expected:
        raise MaskRulesValidationError(("mask.hash_content_mismatch",))
    return ValidatedMaskRules(
        version=version,
        hash=digest,
        hidden_macro_families=hidden,
        forbidden_sentinels=sentinels,
        known_prior_allowlist=priors,
    )


def _version(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise MaskRulesValidationError(("mask.version_not_nonempty_string",))
    if value != value.strip():
        raise MaskRulesValidationError(("mask.version_not_canonical",))
    return value


def _string_tuple(value: Any, path: str) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)) or not all(
        isinstance(item, str) and item.strip() for item in value
    ):
        raise MaskRulesValidationError((f"{path}_not_string_array",))
    if any(item != item.strip() for item in value):
        raise MaskRulesValidationError((f"{path}_contains_noncanonical_string",))
    if len(value) != len(set(value)):
        raise MaskRulesValidationError((f"{path}_contains_duplicates",))
    return tuple(sorted(value))


__all__ = [
    "MASK_MANIFEST_SCHEMA_VERSION",
    "MaskRulesValidationError",
    "ValidatedMaskRules",
    "create_mask_rules",
    "mask_content_hash",
    "mask_manifest_payload",
    "validate_mask_rules",
]
