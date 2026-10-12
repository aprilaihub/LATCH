"""Strict JSON experiment specification for the WP-002 mock runner.

The parser is deliberately implemented with the standard library.  Every
schema object has a closed field set and every level/adapter/catalogue binding
is checked before a caller may dispatch work.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
import hashlib
import json
import math
from pathlib import Path, PurePosixPath
import re
from types import MappingProxyType
from typing import Any

from .catalogues import (
    CLEAN_MACRO_CATALOGUE_VERSION,
    L4_REFERENCE_CATALOGUE_HASH,
    L4_REFERENCE_CATALOGUE_VERSION,
    NO_ACTION_CATALOGUE_HASH,
    NO_ACTION_CATALOGUE_VERSION,
    STRICT_L3_CATALOGUE_VERSION,
    clean_macro_catalogue,
    clean_macro_families,
    strict_l3_catalogue,
)
from .contracts import DoFLevel, EventClass, LevelStatus, UnknownRegime
from .manifests import capability_manifest, interface_fingerprints
from .masks import (
    MaskRulesValidationError,
    create_mask_rules,
    mask_manifest_payload,
    validate_mask_rules,
)


EXPERIMENT_SPEC_SCHEMA_VERSION = "dof.experiment-spec.v2"
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_GIT_SHA1_RE = re.compile(r"^[0-9a-f]{40}$")
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_PATHISH_RE = re.compile(r"(?:^[/~]|^[A-Za-z]:[/\\]|file://|[/\\])")


class SpecValidationError(ValueError):
    """A fail-closed experiment-spec validation error."""

    def __init__(self, reasons: Iterable[str]) -> None:
        self.reasons = tuple(str(reason) for reason in reasons)
        super().__init__("; ".join(self.reasons))


def canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def content_hash(value: Any) -> str:
    data = value if isinstance(value, bytes) else canonical_json_bytes(value)
    return hashlib.sha256(data).hexdigest()


def _strict_object(
    value: Any,
    *,
    path: str,
    allowed: Iterable[str],
    required: Iterable[str] | None = None,
) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or not all(isinstance(key, str) for key in value):
        raise SpecValidationError((f"{path}_not_object",))
    allowed_set = frozenset(allowed)
    required_set = frozenset(allowed_set if required is None else required)
    unknown = sorted(set(value) - allowed_set)
    missing = sorted(required_set - set(value))
    reasons: list[str] = []
    if unknown:
        reasons.append(f"{path}.unknown_fields:" + ",".join(unknown))
    if missing:
        reasons.append(f"{path}.missing_fields:" + ",".join(missing))
    if reasons:
        raise SpecValidationError(reasons)
    return value


def _text(value: Any, path: str, *, allow_not_run: bool = False) -> str:
    if not isinstance(value, str) or not value.strip():
        raise SpecValidationError((f"{path}_not_nonempty_string",))
    result = value.strip()
    if result == "NOT_RUN" and not allow_not_run:
        raise SpecValidationError((f"{path}_not_run_not_allowed",))
    return result


def _identifier(value: Any, path: str) -> str:
    result = _text(value, path)
    if not _ID_RE.fullmatch(result):
        raise SpecValidationError((f"{path}_not_canonical_identifier",))
    return result


def _sha(value: Any, path: str, *, allow_not_run: bool = False) -> str:
    result = _text(value, path, allow_not_run=allow_not_run)
    if allow_not_run and result == "NOT_RUN":
        return result
    if not _SHA256_RE.fullmatch(result):
        raise SpecValidationError((f"{path}_not_sha256",))
    return result


def _enum(value: Any, enum_type: type[Any], path: str) -> Any:
    if isinstance(value, enum_type):
        return value
    if not isinstance(value, str):
        raise SpecValidationError((f"{path}_unknown:{value}",))
    lowered = value.lower()
    for member in enum_type:
        if value == member.value or lowered == member.name.lower() or lowered == str(member.value).lower():
            return member
    raise SpecValidationError((f"{path}_unknown:{value}",))


def _nonnegative_int(value: Any, path: str, *, nullable: bool = False) -> int | None:
    if nullable and value is None:
        return None
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise SpecValidationError((f"{path}_not_nonnegative_integer",))
    return value


def _positive_number(value: Any, path: str, *, nullable: bool = False) -> float | None:
    if nullable and value is None:
        return None
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise SpecValidationError((f"{path}_not_positive_number",))
    result = float(value)
    if not math.isfinite(result) or result <= 0:
        raise SpecValidationError((f"{path}_not_positive_number",))
    return result


def _string_tuple(value: Any, path: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(item, str) and item for item in value):
        raise SpecValidationError((f"{path}_not_string_array",))
    if len(value) != len(set(value)):
        raise SpecValidationError((f"{path}_contains_duplicates",))
    return tuple(value)


def _freeze_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({str(key): _freeze_json(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_json(item) for item in value)
    if value is None or isinstance(value, (str, int, float, bool)):
        canonical_json_bytes(value)
        return value
    raise SpecValidationError((f"non_json_value:{type(value).__name__}",))


def _thaw_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _thaw_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw_json(item) for item in value]
    return value


@dataclass(frozen=True, slots=True)
class RootDcpSpec:
    internal_ref: str
    sha256: str
    model_pseudonym: str

    @classmethod
    def from_dict(cls, value: Any) -> "RootDcpSpec":
        data = _strict_object(
            value,
            path="root_dcp",
            allowed=("internal_ref", "sha256", "model_pseudonym"),
        )
        ref = _text(data["internal_ref"], "root_dcp.internal_ref")
        pure = PurePosixPath(ref)
        if pure.is_absolute() or ref in {".", ".."} or ".." in pure.parts or "\\" in ref or "://" in ref:
            raise SpecValidationError(("root_dcp.internal_ref_must_be_relative",))
        return cls(
            internal_ref=pure.as_posix(),
            sha256=_sha(data["sha256"], "root_dcp.sha256"),
            model_pseudonym=_identifier(data["model_pseudonym"], "root_dcp.model_pseudonym"),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "internal_ref": self.internal_ref,
            "sha256": self.sha256,
            "model_pseudonym": self.model_pseudonym,
        }


@dataclass(frozen=True, slots=True)
class ModelSpec:
    provider_role: str
    provider: str
    model_id: str
    model_version: str
    decision_adapter: str
    adapter_version: str

    @classmethod
    def from_dict(cls, value: Any) -> "ModelSpec":
        names = (
            "provider_role", "provider", "model_id", "model_version",
            "decision_adapter", "adapter_version",
        )
        data = _strict_object(value, path="model", allowed=names)
        return cls(**{name: _text(data[name], f"model.{name}", allow_not_run=True) for name in names})

    @property
    def has_active_model(self) -> bool:
        return any(
            value.lower() not in {"none", "not_run"}
            for value in (self.provider_role, self.provider, self.model_id)
        )

    @property
    def has_decision_adapter(self) -> bool:
        return self.decision_adapter.lower() not in {"none", "not_run"}

    def to_dict(self) -> dict[str, Any]:
        return {name: getattr(self, name) for name in self.__dataclass_fields__}


@dataclass(frozen=True, slots=True)
class ValidationSpec:
    validation_id: str
    validation_version: str
    scoring_id: str
    scoring_version: str

    @classmethod
    def from_dict(cls, value: Any) -> "ValidationSpec":
        names = ("validation_id", "validation_version", "scoring_id", "scoring_version")
        data = _strict_object(value, path="validation", allowed=names)
        return cls(**{name: _identifier(data[name], f"validation.{name}") for name in names})

    def to_dict(self) -> dict[str, Any]:
        return {name: getattr(self, name) for name in self.__dataclass_fields__}


@dataclass(frozen=True, slots=True)
class BudgetSpec:
    max_heavy_calls: int
    max_light_mutation_calls: int
    max_read_calls: int | None
    max_validation_calls: int | None
    max_llm_calls: int
    max_outer_attempts: int
    whole_run_wall_time_seconds: float
    max_usd: float | None
    unknown_pricing_policy: str

    @classmethod
    def from_dict(cls, value: Any) -> "BudgetSpec":
        names = (
            "max_heavy_calls", "max_light_mutation_calls", "max_read_calls",
            "max_validation_calls", "max_llm_calls", "max_outer_attempts",
            "whole_run_wall_time_seconds", "max_usd", "unknown_pricing_policy",
        )
        data = _strict_object(value, path="budget", allowed=names)
        policy = _text(data["unknown_pricing_policy"], "budget.unknown_pricing_policy")
        if policy not in {"reject", "allow_not_run"}:
            raise SpecValidationError((f"budget.unknown_pricing_policy_unknown:{policy}",))
        return cls(
            max_heavy_calls=_nonnegative_int(data["max_heavy_calls"], "budget.max_heavy_calls"),
            max_light_mutation_calls=_nonnegative_int(data["max_light_mutation_calls"], "budget.max_light_mutation_calls"),
            max_read_calls=_nonnegative_int(data["max_read_calls"], "budget.max_read_calls", nullable=True),
            max_validation_calls=_nonnegative_int(data["max_validation_calls"], "budget.max_validation_calls", nullable=True),
            max_llm_calls=_nonnegative_int(data["max_llm_calls"], "budget.max_llm_calls"),
            max_outer_attempts=_nonnegative_int(data["max_outer_attempts"], "budget.max_outer_attempts"),
            whole_run_wall_time_seconds=_positive_number(data["whole_run_wall_time_seconds"], "budget.whole_run_wall_time_seconds"),
            max_usd=_positive_number(data["max_usd"], "budget.max_usd", nullable=True),
            unknown_pricing_policy=policy,
        )

    def limits_by_event_class(self) -> dict[EventClass, int | None]:
        return {
            EventClass.IMPLEMENTATION_HEAVY: self.max_heavy_calls,
            EventClass.MUTATION_LIGHT: self.max_light_mutation_calls,
            EventClass.READ_ONLY: self.max_read_calls,
            EventClass.VALIDATION: self.max_validation_calls,
            EventClass.LLM_REQUEST: self.max_llm_calls,
            EventClass.OUTER_ATTEMPT: self.max_outer_attempts,
        }

    def to_dict(self) -> dict[str, Any]:
        return {name: getattr(self, name) for name in self.__dataclass_fields__}


@dataclass(frozen=True, slots=True)
class CatalogueBindingSpec:
    capability_version: str
    capability_hash: str
    catalogue_version: str
    catalogue_hash: str

    @classmethod
    def from_dict(cls, value: Any) -> "CatalogueBindingSpec":
        names = ("capability_version", "capability_hash", "catalogue_version", "catalogue_hash")
        data = _strict_object(value, path="catalogue", allowed=names)
        return cls(
            capability_version=_text(data["capability_version"], "catalogue.capability_version"),
            capability_hash=_sha(data["capability_hash"], "catalogue.capability_hash"),
            catalogue_version=_text(data["catalogue_version"], "catalogue.catalogue_version"),
            catalogue_hash=_sha(data["catalogue_hash"], "catalogue.catalogue_hash"),
        )

    def to_dict(self) -> dict[str, Any]:
        return {name: getattr(self, name) for name in self.__dataclass_fields__}


@dataclass(frozen=True, slots=True)
class InterfaceSpec:
    prompt_version: str
    prompt_hash: str
    action_schema_version: str
    action_schema_hash: str
    context_version: str
    context_hash: str

    @classmethod
    def from_dict(cls, value: Any) -> "InterfaceSpec":
        names = (
            "prompt_version", "prompt_hash", "action_schema_version",
            "action_schema_hash", "context_version", "context_hash",
        )
        data = _strict_object(value, path="interface", allowed=names)
        kwargs: dict[str, str] = {}
        for name in names:
            kwargs[name] = (
                _sha(data[name], f"interface.{name}", allow_not_run=True)
                if name.endswith("_hash")
                else _text(data[name], f"interface.{name}", allow_not_run=True)
            )
        return cls(**kwargs)

    def to_dict(self) -> dict[str, Any]:
        return {name: getattr(self, name) for name in self.__dataclass_fields__}


@dataclass(frozen=True, slots=True)
class MaskSpec:
    version: str
    hash: str
    hidden_macro_families: tuple[str, ...] = ()
    forbidden_sentinels: tuple[str, ...] = ()
    known_prior_allowlist: tuple[str, ...] = ()

    @staticmethod
    def manifest_payload(
        version: str,
        hidden_macro_families: Iterable[str] = (),
        forbidden_sentinels: Iterable[str] = (),
        known_prior_allowlist: Iterable[str] = (),
    ) -> dict[str, Any]:
        return mask_manifest_payload(
            version,
            hidden_macro_families,
            forbidden_sentinels,
            known_prior_allowlist,
        )

    @classmethod
    def create(
        cls,
        *,
        version: str,
        hidden_macro_families: Iterable[str] = (),
        forbidden_sentinels: Iterable[str] = (),
        known_prior_allowlist: Iterable[str] = (),
    ) -> "MaskSpec":
        try:
            rules = create_mask_rules(
                version=version,
                hidden_macro_families=hidden_macro_families,
                forbidden_sentinels=forbidden_sentinels,
                known_prior_allowlist=known_prior_allowlist,
            )
        except MaskRulesValidationError as exc:
            raise SpecValidationError(exc.reasons) from exc
        return cls(
            rules.version,
            rules.hash,
            rules.hidden_macro_families,
            rules.forbidden_sentinels,
            rules.known_prior_allowlist,
        )

    @classmethod
    def from_dict(cls, value: Any) -> "MaskSpec":
        names = ("version", "hash", "hidden_macro_families", "forbidden_sentinels", "known_prior_allowlist")
        data = _strict_object(value, path="mask", allowed=names)
        try:
            rules = validate_mask_rules(data)
        except MaskRulesValidationError as exc:
            raise SpecValidationError(exc.reasons) from exc
        return cls(
            rules.version,
            rules.hash,
            rules.hidden_macro_families,
            rules.forbidden_sentinels,
            rules.known_prior_allowlist,
        )

    def to_manifest_dict(self) -> dict[str, Any]:
        # Revalidate at the evidence boundary so a directly constructed,
        # content-invalid dataclass cannot be published as trusted evidence.
        return validate_mask_rules(self).to_manifest_dict()

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "hash": self.hash,
            "hidden_macro_families": list(self.hidden_macro_families),
            "forbidden_sentinels": list(self.forbidden_sentinels),
            "known_prior_allowlist": list(self.known_prior_allowlist),
        }


@dataclass(frozen=True, slots=True)
class RepositoryConstraintSpec:
    repository_mode: str
    required_head: str | None
    require_tracked_clean: bool
    untracked_policy: str

    @classmethod
    def from_dict(cls, value: Any) -> "RepositoryConstraintSpec":
        names = (
            "repository_mode",
            "required_head",
            "require_tracked_clean",
            "untracked_policy",
        )
        data = _strict_object(
            value,
            path="provenance.repository_constraint",
            allowed=names,
        )
        if data["repository_mode"] != "git":
            raise SpecValidationError(
                ("provenance.repository_constraint.repository_mode_must_be_git",)
            )
        required_head = data["required_head"]
        if required_head is not None and (
            not isinstance(required_head, str)
            or not _GIT_SHA1_RE.fullmatch(required_head)
        ):
            raise SpecValidationError(
                ("provenance.repository_constraint.required_head_invalid",)
            )
        if not isinstance(data["require_tracked_clean"], bool):
            raise SpecValidationError(
                (
                    "provenance.repository_constraint."
                    "require_tracked_clean_not_boolean",
                )
            )
        untracked_policy = data["untracked_policy"]
        if untracked_policy not in {"record_only", "require_none"}:
            raise SpecValidationError(
                ("provenance.repository_constraint.untracked_policy_invalid",)
            )
        return cls(
            repository_mode="git",
            required_head=required_head,
            require_tracked_clean=data["require_tracked_clean"],
            untracked_policy=untracked_policy,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "repository_mode": self.repository_mode,
            "required_head": self.required_head,
            "require_tracked_clean": self.require_tracked_clean,
            "untracked_policy": self.untracked_policy,
        }


@dataclass(frozen=True, slots=True)
class ToolVersionSpec:
    name: str
    version: str

    @classmethod
    def from_dict(cls, value: Any, index: int) -> "ToolVersionSpec":
        data = _strict_object(value, path=f"provenance.tool_versions[{index}]", allowed=("name", "version"))
        name = _identifier(data["name"], f"provenance.tool_versions[{index}].name")
        version = _text(data["version"], f"provenance.tool_versions[{index}].version", allow_not_run=True)
        if _PATHISH_RE.search(version):
            raise SpecValidationError((f"provenance.tool_versions[{index}].version_contains_path",))
        return cls(name, version)

    def to_dict(self) -> dict[str, str]:
        return {"name": self.name, "version": self.version}


@dataclass(frozen=True, slots=True)
class ProvenanceSpec:
    repository_constraint: RepositoryConstraintSpec
    tool_versions: tuple[ToolVersionSpec, ...]
    environment_manifest_version: str
    environment_manifest_hash: str
    execution_mode: str

    @classmethod
    def from_dict(cls, value: Any) -> "ProvenanceSpec":
        names = (
            "repository_constraint", "tool_versions",
            "environment_manifest_version", "environment_manifest_hash", "execution_mode",
        )
        data = _strict_object(value, path="provenance", allowed=names)
        raw_tools = data["tool_versions"]
        if not isinstance(raw_tools, list):
            raise SpecValidationError(("provenance.tool_versions_not_array",))
        tools = tuple(ToolVersionSpec.from_dict(item, index) for index, item in enumerate(raw_tools))
        if len({item.name for item in tools}) != len(tools):
            raise SpecValidationError(("provenance.tool_versions_duplicate_name",))
        mode = _text(data["execution_mode"], "provenance.execution_mode")
        if mode != "mock":
            raise SpecValidationError(("provenance.execution_mode_must_be_mock",))
        return cls(
            repository_constraint=RepositoryConstraintSpec.from_dict(
                data["repository_constraint"]
            ),
            tool_versions=tuple(sorted(tools, key=lambda item: item.name)),
            environment_manifest_version=_text(data["environment_manifest_version"], "provenance.environment_manifest_version", allow_not_run=True),
            environment_manifest_hash=_sha(data["environment_manifest_hash"], "provenance.environment_manifest_hash", allow_not_run=True),
            execution_mode=mode,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "repository_constraint": self.repository_constraint.to_dict(),
            "tool_versions": [item.to_dict() for item in self.tool_versions],
            "environment_manifest_version": self.environment_manifest_version,
            "environment_manifest_hash": self.environment_manifest_hash,
            "execution_mode": self.execution_mode,
        }


def _validate_mock_response(value: Any) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise SpecValidationError(("policy.mock_response_not_object",))
    if not value:
        return MappingProxyType({})
    if set(value) == {"raw_text"}:
        return _freeze_json({"raw_text": _text(value["raw_text"], "policy.mock_response.raw_text")})
    data = _strict_object(value, path="policy.mock_response", allowed=("kind", "steps"))
    kind = _text(data["kind"], "policy.mock_response.kind").upper()
    if kind not in {"STOP", "ACTION", "PLAN"}:
        raise SpecValidationError((f"policy.mock_response.kind_unknown:{kind}",))
    if not isinstance(data["steps"], list):
        raise SpecValidationError(("policy.mock_response.steps_not_array",))
    steps: list[dict[str, Any]] = []
    for index, raw in enumerate(data["steps"]):
        item = _strict_object(raw, path=f"policy.mock_response.steps[{index}]", allowed=("action_key", "parameters"))
        parameters = _strict_object(
            item["parameters"], path=f"policy.mock_response.steps[{index}].parameters",
            allowed=("directive",), required=(),
        )
        if "directive" in parameters and not isinstance(parameters["directive"], str):
            raise SpecValidationError((f"policy.mock_response.steps[{index}].parameters.directive_not_string",))
        steps.append({
            "action_key": _text(item["action_key"], f"policy.mock_response.steps[{index}].action_key"),
            "parameters": dict(parameters),
        })
    if kind == "STOP" and steps:
        raise SpecValidationError(("policy.mock_response.stop_has_steps",))
    if kind == "ACTION" and len(steps) != 1:
        raise SpecValidationError(("policy.mock_response.action_requires_one_step",))
    if kind == "PLAN" and not steps:
        raise SpecValidationError(("policy.mock_response.plan_requires_steps",))
    return _freeze_json({"kind": kind, "steps": steps})


@dataclass(frozen=True, slots=True)
class PolicySpec:
    mode: str
    version: str
    config_hash: str
    mock_response: Mapping[str, Any] = field(default_factory=dict)

    @staticmethod
    def hash_payload(mode: str, version: str, mock_response: Mapping[str, Any]) -> str:
        return content_hash({"mode": mode, "version": version, "mock_response": _thaw_json(mock_response)})

    @classmethod
    def create(cls, *, mode: str, version: str, mock_response: Mapping[str, Any]) -> "PolicySpec":
        response = _validate_mock_response(mock_response)
        return cls(mode, version, cls.hash_payload(mode, version, response), response)

    @classmethod
    def from_dict(cls, value: Any) -> "PolicySpec":
        data = _strict_object(
            value, path="policy", allowed=("mode", "version", "config_hash", "mock_response"),
            required=("mode", "version", "mock_response"),
        )
        mode = _text(data["mode"], "policy.mode")
        version = _text(data["version"], "policy.version")
        response = _validate_mock_response(data["mock_response"])
        expected = cls.hash_payload(mode, version, response)
        supplied = data.get("config_hash")
        if supplied is not None and _sha(supplied, "policy.config_hash") != expected:
            raise SpecValidationError(("policy.config_hash_content_mismatch",))
        return cls(mode, version, expected, response)

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "version": self.version,
            "config_hash": self.config_hash,
            "mock_response": _thaw_json(self.mock_response),
        }


@dataclass(frozen=True, slots=True)
class MockObservationSpec:
    source: Mapping[str, Any]

    @classmethod
    def from_dict(
        cls,
        value: Any,
        *,
        regime: UnknownRegime,
        mask: MaskSpec,
    ) -> "MockObservationSpec":
        # Reuse the projector's single source-schema validator so spec loading
        # and model projection cannot drift into two different allowlists.
        from .projector import ProjectionError, _validate_source

        try:
            normalized = _validate_source(value, regime, mask)
        except ProjectionError as exc:
            raise SpecValidationError(exc.reasons) from exc
        return cls(_freeze_json(normalized))

    def to_dict(self) -> dict[str, Any]:
        return _thaw_json(self.source)

    def to_projector_source(self) -> dict[str, Any]:
        return self.to_dict()


@dataclass(frozen=True, slots=True)
class ExperimentSpec:
    experiment_id: str
    condition_id: str
    repeat_id: str
    root_dcp: RootDcpSpec
    level: DoFLevel
    unknown_regime: UnknownRegime
    model: ModelSpec
    validation: ValidationSpec
    budget: BudgetSpec
    catalogue: CatalogueBindingSpec
    interface: InterfaceSpec
    mask: MaskSpec
    provenance: ProvenanceSpec
    policy: PolicySpec
    mock_observation: MockObservationSpec
    schema_version: str = EXPERIMENT_SPEC_SCHEMA_VERSION
    source_path: Path | None = field(default=None, compare=False, repr=False)

    @classmethod
    def from_dict(
        cls,
        value: Any,
        *,
        source_path: str | Path | None = None,
    ) -> "ExperimentSpec":
        names = (
            "schema_version", "experiment_id", "condition_id", "repeat_id",
            "root_dcp", "level", "unknown_regime", "model", "validation",
            "budget", "catalogue", "interface", "mask", "provenance",
            "policy", "mock_observation",
        )
        data = _strict_object(value, path="spec", allowed=names)
        if data["schema_version"] != EXPERIMENT_SPEC_SCHEMA_VERSION:
            raise SpecValidationError((f"spec.schema_version_unsupported:{data['schema_version']}",))
        level = _enum(data["level"], DoFLevel, "spec.level")
        regime = _enum(data["unknown_regime"], UnknownRegime, "spec.unknown_regime")
        model = ModelSpec.from_dict(data["model"])
        budget = BudgetSpec.from_dict(data["budget"])
        catalogue = CatalogueBindingSpec.from_dict(data["catalogue"])
        interface = InterfaceSpec.from_dict(data["interface"])
        mask = MaskSpec.from_dict(data["mask"])
        policy = PolicySpec.from_dict(data["policy"])
        observation = MockObservationSpec.from_dict(
            data["mock_observation"], regime=regime,
            mask=mask,
        )
        result = cls(
            experiment_id=_identifier(data["experiment_id"], "spec.experiment_id"),
            condition_id=_identifier(data["condition_id"], "spec.condition_id"),
            repeat_id=_identifier(data["repeat_id"], "spec.repeat_id"),
            root_dcp=RootDcpSpec.from_dict(data["root_dcp"]),
            level=level,
            unknown_regime=regime,
            model=model,
            validation=ValidationSpec.from_dict(data["validation"]),
            budget=budget,
            catalogue=catalogue,
            interface=interface,
            mask=mask,
            provenance=ProvenanceSpec.from_dict(data["provenance"]),
            policy=policy,
            mock_observation=observation,
            source_path=Path(source_path) if source_path is not None else None,
        )
        result._validate_combinations()
        return result

    @property
    def status(self) -> LevelStatus:
        return LevelStatus.REFERENCE_PENDING if self.level is DoFLevel.L4 else LevelStatus.ACTIVE

    @property
    def spec_hash(self) -> str:
        return content_hash(self.identity_payload())

    @property
    def run_id(self) -> str:
        return f"dof-{self.spec_hash[:24]}"

    @property
    def action_catalogue_version(self) -> str:
        return self.catalogue.catalogue_version

    @property
    def action_catalogue_hash(self) -> str:
        return self.catalogue.catalogue_hash

    def resolve_root(self, spec_path: str | Path | None = None) -> Path:
        source = Path(spec_path) if spec_path is not None else self.source_path
        if source is None:
            raise ValueError("spec_path is required when the spec was not loaded from a file")
        base = source.resolve().parent
        candidate = (base / self.root_dcp.internal_ref).resolve()
        try:
            candidate.relative_to(base)
        except ValueError as exc:
            raise ValueError("root_dcp internal_ref escapes the spec directory") from exc
        return candidate

    def identity_payload(self) -> dict[str, Any]:
        # source_path and resolved filesystem/tool locations are deliberately
        # absent.  Only the relative root reference and declared fingerprints
        # participate in identity.
        return self.to_dict(include_derived=False)

    semantic_dict = identity_payload

    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes(self.identity_payload())

    def to_dict(self, *, include_derived: bool = False) -> dict[str, Any]:
        payload = {
            "schema_version": self.schema_version,
            "experiment_id": self.experiment_id,
            "condition_id": self.condition_id,
            "repeat_id": self.repeat_id,
            "root_dcp": self.root_dcp.to_dict(),
            "level": self.level.value,
            "unknown_regime": self.unknown_regime.value,
            "model": self.model.to_dict(),
            "validation": self.validation.to_dict(),
            "budget": self.budget.to_dict(),
            "catalogue": self.catalogue.to_dict(),
            "interface": self.interface.to_dict(),
            "mask": self.mask.to_dict(),
            "provenance": self.provenance.to_dict(),
            "policy": self.policy.to_dict(),
            "mock_observation": self.mock_observation.to_dict(),
        }
        if include_derived:
            payload["derived"] = {
                "status": self.status.value,
                "spec_hash": self.spec_hash,
                "run_id": self.run_id,
            }
        return payload

    def _validate_combinations(self) -> None:
        reasons: list[str] = []
        unknown_hidden_families = sorted(
            set(self.mask.hidden_macro_families) - clean_macro_families()
        )
        if unknown_hidden_families:
            reasons.append(
                "mask.hidden_macro_families_unknown:"
                + ",".join(unknown_hidden_families)
            )
        if self.level in {DoFLevel.L0, DoFLevel.L1}:
            if self.model.has_active_model or self.model.has_decision_adapter:
                reasons.append(f"{self.level.value}_model_or_adapter_must_be_none")
            if self.budget.max_llm_calls != 0:
                reasons.append(f"{self.level.value}_llm_budget_must_be_zero")
        if self.level in {DoFLevel.L2, DoFLevel.L3} and not self.model.has_decision_adapter:
            reasons.append(f"{self.level.value}_requires_decision_adapter")

        response = self.policy.mock_response
        response_kind = str(response.get("kind", "")) if isinstance(response, Mapping) else ""
        has_raw = isinstance(response, Mapping) and "raw_text" in response
        expected_modes = {
            DoFLevel.L0: "baseline",
            DoFLevel.L1: "deterministic_schedule",
            DoFLevel.L2: "mock_adapter",
            DoFLevel.L3: "mock_adapter",
            DoFLevel.L4: "reference_pending",
        }
        if self.policy.mode != expected_modes[self.level]:
            reasons.append(f"{self.level.value}_policy_mode_must_be_{expected_modes[self.level]}")
        if self.level in {DoFLevel.L0, DoFLevel.L4} and response:
            reasons.append(f"{self.level.value}_mock_response_must_be_empty")
        if self.level is DoFLevel.L1 and (has_raw or response_kind != "PLAN"):
            reasons.append("L1_requires_typed_plan_response")
        if self.level is DoFLevel.L2 and not has_raw and response_kind not in {"ACTION", "STOP"}:
            reasons.append("L2_response_must_be_action_stop_or_raw")
        if self.level is DoFLevel.L3 and not has_raw and response_kind not in {"ACTION", "PLAN", "STOP"}:
            reasons.append("L3_response_must_be_action_plan_stop_or_raw")

        expected_catalogues = {
            DoFLevel.L0: (NO_ACTION_CATALOGUE_VERSION, NO_ACTION_CATALOGUE_HASH),
            DoFLevel.L1: (CLEAN_MACRO_CATALOGUE_VERSION, clean_macro_catalogue(DoFLevel.L1).sha256),
            DoFLevel.L2: (CLEAN_MACRO_CATALOGUE_VERSION, clean_macro_catalogue(DoFLevel.L2).sha256),
            DoFLevel.L3: (STRICT_L3_CATALOGUE_VERSION, strict_l3_catalogue().sha256),
            DoFLevel.L4: (L4_REFERENCE_CATALOGUE_VERSION, L4_REFERENCE_CATALOGUE_HASH),
        }
        expected_version, expected_hash = expected_catalogues[self.level]
        if self.catalogue.catalogue_version != expected_version:
            reasons.append(f"{self.level.value}_catalogue_version_mismatch")
        if self.catalogue.catalogue_hash != expected_hash:
            reasons.append(f"{self.level.value}_catalogue_hash_mismatch")

        expected_capability = capability_manifest(self.level)
        if self.catalogue.capability_version != expected_capability.version:
            reasons.append(f"{self.level.value}_capability_version_mismatch")
        if self.catalogue.capability_hash != expected_capability.sha256:
            reasons.append(f"{self.level.value}_capability_hash_mismatch")

        active_interface = self.level in {DoFLevel.L2, DoFLevel.L3}
        interface_values = self.interface.to_dict()
        if active_interface and any(value == "NOT_RUN" for value in interface_values.values()):
            reasons.append(f"{self.level.value}_interface_must_be_content_addressed")
        if interface_values != interface_fingerprints(self.level):
            reasons.append(f"{self.level.value}_interface_binding_mismatch")
        if self.level is DoFLevel.L0 and self.budget.max_heavy_calls != 0:
            reasons.append("L0_heavy_budget_must_be_zero")
        if self.level is DoFLevel.L0 and self.budget.max_light_mutation_calls != 0:
            reasons.append("L0_light_mutation_budget_must_be_zero")
        if self.model.has_active_model and self.budget.max_llm_calls == 0:
            reasons.append("active_model_requires_positive_llm_budget")
        if reasons:
            raise SpecValidationError(reasons)


def load_experiment_spec(path: str | Path) -> ExperimentSpec:
    source = Path(path)
    try:
        raw = source.read_text(encoding="utf-8")
    except OSError:
        raise
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SpecValidationError((f"invalid_json:{exc.msg}",)) from exc
    return ExperimentSpec.from_dict(payload, source_path=source)


# Short compatibility aliases used by the runner/report layer.
RootArtifactSpec = RootDcpSpec
PromptContextSpec = InterfaceSpec
