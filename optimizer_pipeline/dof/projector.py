"""The single model-visible context boundary for L2/L3 experiments.

The projector accepts a deliberately small source schema, derives the legal
action schema from a trusted catalogue, applies the condition mask, and only
then serializes the model context.  It never forwards arbitrary dictionaries.
Path/repository hygiene is universal.  Forced- and natural-unknown projection
additionally redact identity/fingerprint/prior sentinels.  Mask metadata itself
is audit evidence and never enters the policy-visible context.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import hashlib
import json
import math
import re
from types import MappingProxyType
from typing import Any, Iterable, Mapping

from .catalogues import clean_macro_catalogue, clean_macro_families
from .contracts import DesignStage, DoFLevel, EventClass, UnknownRegime
from .masks import (
    MaskRulesValidationError,
    validate_mask_rules,
)


PROJECTED_CONTEXT_SCHEMA_VERSION = "dof_decision_context.v2"

_SOURCE_FIELDS = frozenset(
    {
        "condition",
        "state",
        "history",
        "budget_remaining",
        "validation",
        "stop_conditions",
        "known_priors",
    }
)
_SOURCE_REQUIRED = frozenset(_SOURCE_FIELDS - {"known_priors"})
_CONDITION_FIELDS = frozenset({"pseudonym"})
_STATE_FIELDS = frozenset(
    {"pseudonym", "lineage_pseudonym", "stage", "measurements"}
)
_MEASUREMENT_GROUPS = frozenset(
    {"timing", "route", "placement", "utilization", "congestion"}
)
_MEASUREMENT_FIELDS: dict[str, frozenset[str]] = {
    "timing": frozenset(
        {
            "wns_ns",
            "tns_ns",
            "failing_endpoints",
            "clock_period_ns",
            "estimated_fmax_mhz",
        }
    ),
    "route": frozenset(
        {"status", "unrouted_nets", "routing_errors", "route_overflow"}
    ),
    "placement": frozenset(
        {"status", "placed_cells", "unplaced_cells", "placement_density"}
    ),
    "utilization": frozenset(
        {
            "lut",
            "ff",
            "dsp",
            "bram",
            "uram",
            "lut_percent",
            "ff_percent",
            "dsp_percent",
            "bram_percent",
            "uram_percent",
        }
    ),
    "congestion": frozenset(
        {"level", "hotspots", "max_overuse", "horizontal", "vertical"}
    ),
}
_HISTORY_FIELDS = frozenset(
    {
        "decision_index",
        "actions",
        "parent_qor",
        "child_qor",
        "delta_qor",
        "valid",
        "endpoint",
        "actual_heavy_calls",
    }
)
_HISTORY_REQUIRED = _HISTORY_FIELDS
_HISTORY_ACTION_FIELDS = frozenset({"action_key", "parameters"})
_HISTORY_QOR_FIELDS = frozenset({"fmax_mhz", "wns_ns"})
_VALIDATION_FIELDS = frozenset({"status", "errors", "warnings"})
_VALIDATION_REQUIRED = frozenset(_VALIDATION_FIELDS)
_BUDGET_FIELDS = frozenset(
    {event.value for event in EventClass}
    | {"WALL_TIME_SECONDS", "USD"}
)
_PSEUDONYM_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_SHA_PATTERN = re.compile(r"(?<![0-9A-Fa-f])[0-9A-Fa-f]{40,64}(?![0-9A-Fa-f])")
_PATH_OR_REPOSITORY_PATTERN = re.compile(
    # Any slash/backslash is treated as a coordinate, not scientific status
    # text.  The extension arm also catches bare report/journal filenames.
    r"[/\\]|"
    r"\b[^\s/\\]+\.(?:"
    r"dcp|tcl|sh|bash|zsh|csh|ksh|ps1|"
    r"json|jsonl|yaml|yml|csv|md|markdown|pdf|"
    r"jou|rpt|log|xdc|db|sqlite|sqlite3|parquet|arrow|feather|"
    r"v|sv|vh|svh|vhd|vhdl|xci|edf|edif|ngc|bit|ltx|xpr|"
    r"xsa|bd|mcs|prm|mmi|bmm|ncd|pcf|ucf|sdf|sdc|wcfg|wdb|"
    r"vcd|saif|ipynb|pkl|pickle|npy|npz|joblib|onnx|pt|pth|"
    r"ckpt|h5|hdf5|dat|bin|"
    r"py|pyc|xml|txt|rst|toml|ini|cfg|conf|lock|patch|diff|"
    r"zip|tar|tgz|gz|bz2|xz"
    r")\b",
    flags=re.IGNORECASE,
)
_REPOSITORY_BASENAME_PATTERN = re.compile(
    r"(?<![A-Za-z0-9_.-])(?:README|Makefile|Dockerfile|Rakefile|Gemfile|"
    r"Pipfile|AGENTS|LICENSE|CHANGELOG)(?=$|[^A-Za-z0-9_-])|"
    r"(?<![A-Za-z0-9_.-])(?:"
    r"\.[A-Za-z_][A-Za-z0-9_.-]*|"
    r"__pycache__|\.pytest_cache|\.mypy_cache|\.ruff_cache|"
    r"\.venv|node_modules)(?=$|[^A-Za-z0-9_.-])",
    flags=re.IGNORECASE,
)
_REPOSITORY_REFERENCE_PATTERN = re.compile(
    r"\b(?:git|repo|repository)[ _.:=-]*(?:commit|branch|worktree|"
    r"status|log|diff|patch|journal|report|results?)\b",
    flags=re.IGNORECASE,
)
_STATIC_PRIOR_PATTERN = re.compile(
    r"\b(?:expected[_ -]?fmax|known[_ -]?winner|winning[_ -]?action|"
    r"measured[_ -]?winner|known[_ -]?(?:public[_ -]?)?recipe)\b",
    flags=re.IGNORECASE,
)


class ProjectionError(ValueError):
    def __init__(self, reasons: Iterable[str]) -> None:
        self.reasons = tuple(str(reason) for reason in reasons)
        super().__init__("; ".join(self.reasons))


@dataclass(frozen=True, slots=True)
class ProjectedContext:
    context: Mapping[str, Any]
    serialized: str
    context_hash: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": PROJECTED_CONTEXT_SCHEMA_VERSION,
            "context": _thaw_json(self.context),
            "serialized": self.serialized,
            "context_hash": self.context_hash,
        }


class DecisionContextProjector:
    """Compile exact source evidence into one hashable L2/L3 context."""

    def project(
        self,
        *,
        level: DoFLevel | str,
        regime: UnknownRegime | str,
        source: Mapping[str, Any],
        catalogue: Any,
        mask: Any,
    ) -> ProjectedContext:
        parsed_level = _coerce_enum(level, DoFLevel)
        parsed_regime = _coerce_enum(regime, UnknownRegime)
        reasons: list[str] = []
        if parsed_level not in {DoFLevel.L2, DoFLevel.L3}:
            reasons.append(f"projector_level_not_l2_or_l3:{level}")
        if parsed_regime is None:
            reasons.append(f"unknown_regime:{regime}")
        if not isinstance(source, Mapping):
            reasons.append("source_not_object")
        if reasons:
            raise ProjectionError(reasons)
        assert parsed_level is not None and parsed_regime is not None

        try:
            mask_rules = validate_mask_rules(mask)
        except MaskRulesValidationError as exc:
            raise ProjectionError(exc.reasons) from exc
        unknown_hidden_families = sorted(
            set(mask_rules.hidden_macro_families) - clean_macro_families()
        )
        if unknown_hidden_families:
            raise ProjectionError(
                (
                    "mask.hidden_macro_families_unknown:"
                    + ",".join(unknown_hidden_families),
                )
            )

        normalized = _validate_source(source, parsed_regime, mask_rules)
        expected_stage = normalized["state"]["stage"]
        catalogue_hash = _catalogue_hash(catalogue)
        if not catalogue_hash:
            raise ProjectionError(("catalogue_hash_missing_or_invalid",))
        catalogue_version = str(getattr(catalogue, "version", "") or "")
        if not catalogue_version:
            raise ProjectionError(("catalogue_version_missing",))

        action_schema: list[dict[str, Any]] = []
        for action in sorted(
            tuple(getattr(catalogue, "actions", ())),
            key=lambda item: str(getattr(item, "name", "")),
        ):
            name = str(getattr(action, "name", "") or "")
            family = str(getattr(action, "family", "") or "")
            if not name or not family:
                raise ProjectionError(("catalogue_action_missing_name_or_family",))
            if (
                parsed_level is DoFLevel.L2
                and family in mask_rules.hidden_macro_families
            ):
                continue
            parameters = _plain_mapping(getattr(action, "parameters_schema", {}))
            _validate_json(parameters)
            # Internal operations, paths, fixed Tcl macros and cost bindings are
            # intentionally absent from the policy-visible schema.
            action_schema.append(
                {
                    "action_key": name,
                    "parameters_schema": parameters,
                }
            )

        # Sanitize only observation-derived values.  Trusted protocol schema,
        # catalogue names, and legal-action bytes are assembled afterwards and
        # are assertion-only: a mask that collides with those structural values
        # must fail closed, never silently rewrite the action interface.
        normalized = _sanitize_policy_paths(normalized)
        source_mask_tokens = _mask_source_semantic_tokens(
            mask_rules.version,
            mask_rules.hidden_macro_families,
        )
        if parsed_regime in {
            UnknownRegime.FORCED_UNKNOWN,
            UnknownRegime.NATURAL_UNKNOWN,
        }:
            neutral_marker = _neutral_redaction_token(
                mask_rules.forbidden_sentinels + source_mask_tokens
            )
            normalized = _sanitize_unknown_identity(
                normalized,
                mask_rules.forbidden_sentinels,
                neutral_marker,
            )
        else:
            neutral_marker = _neutral_redaction_token(source_mask_tokens)
        normalized = _sanitize_semantic_tokens(
            normalized,
            source_mask_tokens,
            neutral_marker,
        )
        _assert_semantic_tokens_clean(normalized, source_mask_tokens)
        normalized = _repair_and_validate_source_pseudonyms(
            normalized,
            marker=neutral_marker,
            expected_stage=expected_stage,
        )

        condition = normalized["condition"]
        state = normalized["state"]
        context: dict[str, Any] = {
            "schema_version": PROJECTED_CONTEXT_SCHEMA_VERSION,
            "condition": {
                "pseudonym": condition["pseudonym"],
                "regime": parsed_regime.value,
                "level": parsed_level.value,
                "catalogue_version": catalogue_version,
            },
            "current_state": state,
            "legal_actions": [item["action_key"] for item in action_schema],
            "action_schema": action_schema,
            "history": normalized["history"],
            "budget_remaining": normalized["budget_remaining"],
            "validation": normalized["validation"],
            "stop_conditions": normalized["stop_conditions"],
        }
        if parsed_regime is UnknownRegime.KNOWN and normalized.get("known_priors"):
            context["known_priors"] = normalized["known_priors"]

        # These checks inspect the final model-visible bytes without mutating
        # trusted protocol or catalogue structure.
        _assert_policy_paths_clean(context)
        if parsed_regime in {
            UnknownRegime.FORCED_UNKNOWN,
            UnknownRegime.NATURAL_UNKNOWN,
        }:
            _assert_unknown_identity_clean(
                context, mask_rules.forbidden_sentinels
            )
        # Exact mask version bytes are never model-visible at either L2 or L3.
        _assert_semantic_tokens_clean(context, (mask_rules.version,))
        if parsed_level is DoFLevel.L2:
            _assert_semantic_tokens_clean(context, source_mask_tokens)

        serialized = json.dumps(
            context,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
        _assert_serialized_policy_view_clean(
            serialized,
            mask_rules.forbidden_sentinels,
            unknown=parsed_regime
            in {UnknownRegime.FORCED_UNKNOWN, UnknownRegime.NATURAL_UNKNOWN},
        )
        return ProjectedContext(
            context=_freeze_json(context),
            serialized=serialized,
            context_hash=hashlib.sha256(serialized.encode("utf-8")).hexdigest(),
        )


# Concise public alias.
ContextProjector = DecisionContextProjector


def _validate_source(
    source: Mapping[str, Any],
    regime: UnknownRegime,
    mask: Any,
) -> dict[str, Any]:
    try:
        mask_rules = validate_mask_rules(mask)
    except MaskRulesValidationError as exc:
        raise ProjectionError(exc.reasons) from exc
    _check_fields(source, _SOURCE_FIELDS, _SOURCE_REQUIRED, "source")

    condition = _require_mapping(source["condition"], "source.condition")
    _check_fields(
        condition,
        _CONDITION_FIELDS,
        _CONDITION_FIELDS,
        "source.condition",
    )
    condition_pseudonym = _pseudonym(
        condition["pseudonym"], "source.condition.pseudonym"
    )

    state = _require_mapping(source["state"], "source.state")
    _check_fields(state, _STATE_FIELDS, _STATE_FIELDS, "source.state")
    stage = _coerce_enum(state["stage"], DesignStage)
    if stage is None:
        raise ProjectionError((f"source.state.stage_unknown:{state['stage']}",))
    measurements = _require_mapping(
        state["measurements"], "source.state.measurements"
    )
    _check_fields(
        measurements,
        _MEASUREMENT_GROUPS,
        frozenset(),
        "source.state.measurements",
    )
    normalized_measurements: dict[str, Any] = {}
    for group, values in measurements.items():
        group_values = _require_mapping(
            values, f"source.state.measurements.{group}"
        )
        _check_fields(
            group_values,
            _MEASUREMENT_FIELDS[group],
            frozenset(),
            f"source.state.measurements.{group}",
        )
        for key, value in group_values.items():
            _validate_measurement(value, f"source.state.measurements.{group}.{key}")
        normalized_measurements[group] = dict(group_values)

    history_value = source["history"]
    if not isinstance(history_value, list):
        raise ProjectionError(("source.history_not_array",))
    history: list[dict[str, Any]] = []
    for index, raw in enumerate(history_value):
        item = _require_mapping(raw, f"source.history[{index}]")
        _check_fields(
            item,
            _HISTORY_FIELDS,
            _HISTORY_REQUIRED,
            f"source.history[{index}]",
        )
        if not isinstance(item["decision_index"], int) or isinstance(
            item["decision_index"], bool
        ) or item["decision_index"] < 0:
            raise ProjectionError((f"source.history[{index}].decision_index_invalid",))
        actions = item["actions"]
        if not isinstance(actions, list) or not actions:
            raise ProjectionError((f"source.history[{index}].actions_invalid",))
        normalized_actions: list[dict[str, Any]] = []
        for action_index, raw_action in enumerate(actions):
            action = _require_mapping(
                raw_action, f"source.history[{index}].actions[{action_index}]"
            )
            _check_fields(
                action,
                _HISTORY_ACTION_FIELDS,
                _HISTORY_ACTION_FIELDS,
                f"source.history[{index}].actions[{action_index}]",
            )
            if not isinstance(action["action_key"], str) or not action["action_key"]:
                raise ProjectionError(
                    (f"source.history[{index}].actions[{action_index}].action_key_invalid",)
                )
            parameters = _require_mapping(
                action["parameters"],
                f"source.history[{index}].actions[{action_index}].parameters",
            )
            _validate_json(parameters)
            normalized_actions.append(
                {"action_key": action["action_key"], "parameters": dict(parameters)}
            )
        normalized_qor: dict[str, dict[str, Any]] = {}
        for name in ("parent_qor", "child_qor", "delta_qor"):
            qor = _require_mapping(item[name], f"source.history[{index}].{name}")
            _check_fields(
                qor,
                _HISTORY_QOR_FIELDS,
                _HISTORY_QOR_FIELDS,
                f"source.history[{index}].{name}",
            )
            for metric, value in qor.items():
                _validate_measurement(
                    value, f"source.history[{index}].{name}.{metric}"
                )
            normalized_qor[name] = dict(qor)
        if not isinstance(item["endpoint"], str) or not item["endpoint"]:
            raise ProjectionError((f"source.history[{index}].endpoint_invalid",))
        if not isinstance(item["valid"], bool):
            raise ProjectionError((f"source.history[{index}].valid_not_boolean",))
        if (
            not isinstance(item["actual_heavy_calls"], int)
            or isinstance(item["actual_heavy_calls"], bool)
            or item["actual_heavy_calls"] < 0
        ):
            raise ProjectionError(
                (f"source.history[{index}].actual_heavy_calls_invalid",)
            )
        history.append(
            {
                "decision_index": item["decision_index"],
                "actions": normalized_actions,
                **normalized_qor,
                "valid": item["valid"],
                "endpoint": item["endpoint"],
                "actual_heavy_calls": item["actual_heavy_calls"],
            }
        )

    budget = _require_mapping(source["budget_remaining"], "source.budget_remaining")
    _check_fields(
        budget,
        _BUDGET_FIELDS,
        frozenset(),
        "source.budget_remaining",
    )
    normalized_budget: dict[str, Any] = {}
    for raw_key, value in budget.items():
        key = str(getattr(raw_key, "value", raw_key))
        if key not in _BUDGET_FIELDS:
            raise ProjectionError((f"source.budget_remaining.unknown_field:{key}",))
        if not isinstance(value, (int, float)) or isinstance(value, bool) or value < 0 or not math.isfinite(float(value)):
            raise ProjectionError((f"source.budget_remaining.{key}_invalid",))
        normalized_budget[key] = value

    validation = _require_mapping(source["validation"], "source.validation")
    _check_fields(
        validation,
        _VALIDATION_FIELDS,
        _VALIDATION_REQUIRED,
        "source.validation",
    )
    if not isinstance(validation["status"], str) or not validation["status"]:
        raise ProjectionError(("source.validation.status_invalid",))
    for name in ("errors", "warnings"):
        if not isinstance(validation[name], list) or not all(
            isinstance(value, str) for value in validation[name]
        ):
            raise ProjectionError((f"source.validation.{name}_not_string_array",))

    stop_conditions = source["stop_conditions"]
    if not isinstance(stop_conditions, list) or not all(
        isinstance(value, str) for value in stop_conditions
    ):
        raise ProjectionError(("source.stop_conditions_not_string_array",))

    known_priors: dict[str, Any] = {}
    raw_priors = source.get("known_priors", {})
    if not isinstance(raw_priors, Mapping):
        raise ProjectionError(("source.known_priors_not_object",))
    if regime is UnknownRegime.KNOWN:
        allowlist = frozenset(mask_rules.known_prior_allowlist)
        undeclared = sorted(set(raw_priors) - allowlist)
        if undeclared:
            raise ProjectionError(
                ("source.known_priors_not_allowlisted:" + ",".join(undeclared),)
            )
        _validate_json(raw_priors)
        known_priors = dict(raw_priors)
    # In forced/natural unknown, the entire declared prior container is an
    # intentional suppression sink and never reaches the projected context.

    return {
        "condition": {"pseudonym": condition_pseudonym},
        "state": {
            "pseudonym": _pseudonym(
                state["pseudonym"], "source.state.pseudonym"
            ),
            "lineage_pseudonym": _pseudonym(
                state["lineage_pseudonym"], "source.state.lineage_pseudonym"
            ),
            "stage": stage.value,
            "measurements": normalized_measurements,
        },
        "history": history,
        "budget_remaining": normalized_budget,
        "validation": {
            "status": validation["status"],
            "errors": list(validation["errors"]),
            "warnings": list(validation["warnings"]),
        },
        "stop_conditions": list(stop_conditions),
        "known_priors": known_priors,
    }


def _check_fields(
    value: Mapping[str, Any],
    allowed: frozenset[str],
    required: frozenset[str],
    path: str,
) -> None:
    raw_keys = tuple(value)
    keys = {key for key in raw_keys if isinstance(key, str)}
    unknown = sorted(keys - allowed)
    missing = sorted(required - keys)
    reasons: list[str] = []
    if not all(isinstance(key, str) for key in raw_keys):
        reasons.append(f"{path}.has_non_string_key")
    if unknown:
        reasons.append(f"{path}.unknown_fields:" + ",".join(unknown))
    if missing:
        reasons.append(f"{path}.missing_fields:" + ",".join(missing))
    if reasons:
        raise ProjectionError(reasons)


def _require_mapping(value: Any, path: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ProjectionError((f"{path}_not_object",))
    if not all(isinstance(key, str) for key in value):
        raise ProjectionError((f"{path}_has_non_string_key",))
    return value


def _pseudonym(value: Any, path: str) -> str:
    if not isinstance(value, str) or not _PSEUDONYM_PATTERN.fullmatch(value):
        raise ProjectionError((f"{path}_not_canonical_pseudonym",))
    return value


def _validate_measurement(value: Any, path: str) -> None:
    if value is None or isinstance(value, (str, bool)):
        return
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if math.isfinite(float(value)):
            return
    if isinstance(value, list):
        for index, item in enumerate(value):
            _validate_measurement(item, f"{path}[{index}]")
        return
    raise ProjectionError((f"{path}_invalid_measurement",))


def _plain_mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return {str(key): _thaw_json(item) for key, item in value.items()}
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        converted = to_dict()
        if isinstance(converted, Mapping):
            return {str(key): _thaw_json(item) for key, item in converted.items()}
    return {}


def _catalogue_hash(catalogue: Any) -> str:
    value = getattr(catalogue, "sha256", None)
    value = value() if callable(value) else value
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
        return ""
    return value


def _sanitize_policy_paths(value: Any) -> Any:
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for key, item in value.items():
            text_key = str(key)
            if _contains_path_or_repository_reference(text_key):
                raise ProjectionError(("policy_view_path_in_object_key",))
            result[text_key] = _sanitize_policy_paths(item)
        return result
    if isinstance(value, (list, tuple)):
        return [_sanitize_policy_paths(item) for item in value]
    if not isinstance(value, str):
        return value
    if _contains_path_or_repository_reference(value):
        # Redact the complete field.  Partial regex replacement can leave a
        # parent directory, drive name, or repository coordinate behind.
        return "[MASKED_PATH]"
    return value


def _assert_policy_paths_clean(value: Any) -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            _assert_policy_paths_clean(str(key))
            _assert_policy_paths_clean(item)
        return
    if isinstance(value, (list, tuple)):
        for item in value:
            _assert_policy_paths_clean(item)
        return
    if isinstance(value, str) and _contains_path_or_repository_reference(value):
        raise ProjectionError(("policy_view_path_or_repository_reference_survived",))


def _contains_path_or_repository_reference(value: str) -> bool:
    return bool(
        _PATH_OR_REPOSITORY_PATTERN.search(value)
        or _REPOSITORY_BASENAME_PATTERN.search(value)
        or _REPOSITORY_REFERENCE_PATTERN.search(value)
        or _SHA_PATTERN.search(value)
    )


def _sanitize_unknown_identity(
    value: Any,
    sentinels: tuple[str, ...],
    marker: str,
) -> Any:
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for key, item in value.items():
            text_key = str(key)
            if _unknown_identity_reasons(text_key, sentinels):
                raise ProjectionError(("unknown_identity_in_object_key",))
            result[text_key] = _sanitize_unknown_identity(item, sentinels, marker)
        return result
    if isinstance(value, (list, tuple)):
        return [
            _sanitize_unknown_identity(item, sentinels, marker) for item in value
        ]
    if not isinstance(value, str):
        return value
    # Redact the complete value.  Substring deletion can turn one valid
    # identifier into another (or into an empty string), which is both
    # ambiguous evidence and a potential structural corruption route.
    if _unknown_identity_reasons(value, sentinels):
        return marker
    return value


def _neutral_redaction_token(sentinels: tuple[str, ...]) -> str:
    """Choose a deterministic canonical marker that cannot match the mask."""

    stems = ("opaque", "redacted", "withheld", "neutral", "hidden", "x", "z")
    for index in range(100):
        for stem in stems:
            candidate = f"{stem}-{index}"
            if (
                _PSEUDONYM_PATTERN.fullmatch(candidate)
                and not _contains_path_or_repository_reference(candidate)
                and not _unknown_identity_reasons(candidate, sentinels)
            ):
                return candidate
    raise ProjectionError(("unknown_redaction_token_unavailable",))


def _repair_and_validate_source_pseudonyms(
    source: Any,
    *,
    marker: str,
    expected_stage: str,
) -> dict[str, Any]:
    """Keep redacted identifiers canonical and structural stage immutable."""

    if not isinstance(source, dict):  # pragma: no cover - internal invariant
        raise ProjectionError(("sanitized_source_not_object",))
    condition = source.get("condition")
    state = source.get("state")
    if not isinstance(condition, dict) or not isinstance(state, dict):
        raise ProjectionError(("sanitized_source_shape_changed",))
    if state.get("stage") != expected_stage:
        raise ProjectionError(("unknown_identity_in_structural_source",))
    for container, key, path in (
        (condition, "pseudonym", "source.condition.pseudonym"),
        (state, "pseudonym", "source.state.pseudonym"),
        (state, "lineage_pseudonym", "source.state.lineage_pseudonym"),
    ):
        if container.get(key) == "[MASKED_PATH]":
            container[key] = marker
        _pseudonym(container.get(key), path)
    return source


def _mask_source_semantic_tokens(
    mask_version: str,
    hidden_families: tuple[str, ...],
) -> tuple[str, ...]:
    """Return model-hidden mask/version semantics for observation text."""

    hidden = frozenset(hidden_families)
    tokens: set[str] = {mask_version}
    # Hidden macro semantics are defined by the canonical L1/L2 catalogue even
    # while projecting L3.  Only observation-derived text is scrubbed at L3;
    # the trusted typed primitive schema (including AddRetime) stays intact.
    for action in clean_macro_catalogue(DoFLevel.L1).actions:
        family = str(getattr(action, "family", "") or "")
        if family not in hidden:
            continue
        name = str(getattr(action, "name", "") or "")
        tokens.update(
            {
                family,
                family.replace("_", "-"),
                family.replace("_", " "),
                name,
                name.rsplit(".", 1)[-1],
            }
        )
    return tuple(sorted((token for token in tokens if token), key=str.lower))


def _sanitize_semantic_tokens(
    value: Any,
    tokens: tuple[str, ...],
    marker: str,
) -> Any:
    if not tokens:
        return value
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for key, item in value.items():
            text_key = str(key)
            if _contains_semantic_token(text_key, tokens):
                raise ProjectionError(("hidden_l2_semantic_in_object_key",))
            result[text_key] = _sanitize_semantic_tokens(item, tokens, marker)
        return result
    if isinstance(value, (list, tuple)):
        return [_sanitize_semantic_tokens(item, tokens, marker) for item in value]
    if isinstance(value, str) and _contains_semantic_token(value, tokens):
        return marker
    return value


def _assert_semantic_tokens_clean(value: Any, tokens: tuple[str, ...]) -> None:
    if not tokens:
        return
    if isinstance(value, Mapping):
        for key, item in value.items():
            _assert_semantic_tokens_clean(str(key), tokens)
            _assert_semantic_tokens_clean(item, tokens)
        return
    if isinstance(value, (list, tuple)):
        for item in value:
            _assert_semantic_tokens_clean(item, tokens)
        return
    if isinstance(value, str) and _contains_semantic_token(value, tokens):
        raise ProjectionError(("hidden_l2_semantic_survived",))


def _contains_semantic_token(value: str, tokens: tuple[str, ...]) -> bool:
    lowered = value.lower()
    return any(token.lower() in lowered for token in tokens)


def _assert_unknown_identity_clean(
    value: Any, sentinels: tuple[str, ...]
) -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            _assert_unknown_identity_clean(str(key), sentinels)
            _assert_unknown_identity_clean(item, sentinels)
        return
    if isinstance(value, (list, tuple)):
        for item in value:
            _assert_unknown_identity_clean(item, sentinels)
        return
    if not isinstance(value, str):
        return
    reasons = _unknown_identity_reasons(value, sentinels)
    if reasons:
        raise ProjectionError(reasons)


def _unknown_identity_reasons(
    value: str, sentinels: tuple[str, ...]
) -> tuple[str, ...]:
    lowered = value.lower()
    reasons: list[str] = []
    if any(sentinel.lower() in lowered for sentinel in sentinels):
        reasons.append("unknown_sentinel_survived")
    if _SHA_PATTERN.search(value):
        reasons.append("unknown_sha_survived")
    if _STATIC_PRIOR_PATTERN.search(value):
        reasons.append("unknown_static_prior_survived")
    return tuple(reasons)


def _assert_serialized_policy_view_clean(
    serialized: str,
    sentinels: tuple[str, ...],
    *,
    unknown: bool,
) -> None:
    # Validate the decoded final bytes.  Searching JSON source directly would
    # misclassify harmless JSON escape backslashes as Windows paths.
    try:
        decoded = json.loads(serialized)
    except json.JSONDecodeError as exc:  # pragma: no cover - json.dumps owns it
        raise ProjectionError(("serialized_context_not_json",)) from exc
    _assert_policy_paths_clean(decoded)
    if unknown:
        _assert_unknown_identity_clean(decoded, sentinels)


def _validate_json(value: Any, *, path: str = "$") -> None:
    if value is None or isinstance(value, (str, bool, int)):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ProjectionError((f"{path}_non_finite",))
        return
    if isinstance(value, Mapping):
        for key, item in value.items():
            if not isinstance(key, str):
                raise ProjectionError((f"{path}_non_string_key",))
            _validate_json(item, path=f"{path}.{key}")
        return
    if isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            _validate_json(item, path=f"{path}[{index}]")
        return
    raise ProjectionError((f"{path}_non_json_value:{type(value).__name__}",))


def _coerce_enum(value: Any, enum_type: type[Enum]) -> Any | None:
    if isinstance(value, enum_type):
        return value
    text = str(getattr(value, "value", value) or "")
    for member in enum_type:
        if text == member.value or text.upper() == member.name.upper():
            return member
    return None


def _freeze_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType(
            {str(key): _freeze_json(item) for key, item in sorted(value.items())}
        )
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_json(item) for item in value)
    return value


def _thaw_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _thaw_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw_json(item) for item in value]
    return value


__all__ = [
    "ContextProjector",
    "DecisionContextProjector",
    "PROJECTED_CONTEXT_SCHEMA_VERSION",
    "ProjectedContext",
    "ProjectionError",
]
