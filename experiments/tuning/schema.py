"""Experiment specification loading for frontier LLM sweeps.

The MVP parser intentionally supports a small JSON-compatible YAML subset so the
research harness does not add a dependency just to read simple matrices.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional


@dataclass(frozen=True)
class ExperimentSpec:
    experiment_id: str
    input_dcps: List[str]
    base: Dict[str, Any] = field(default_factory=dict)
    variants: List[Dict[str, Any]] = field(default_factory=list)
    matrix: Dict[str, List[Any]] = field(default_factory=dict)
    repeats: int = 1
    output_root: Optional[str] = None
    strict_experiment: bool = False

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ExperimentSpec":
        if not isinstance(data, dict):
            raise ValueError("Experiment spec must be a mapping")
        experiment_id = str(data.get("experiment_id") or "").strip()
        if not experiment_id:
            raise ValueError("experiment_id is required")
        input_dcps = data.get("input_dcps")
        if not isinstance(input_dcps, list) or not input_dcps:
            raise ValueError("input_dcps must be a non-empty list")
        matrix = data.get("matrix") or {}
        if not isinstance(matrix, dict):
            raise ValueError("matrix must be a mapping")
        normalized_matrix: Dict[str, List[Any]] = {}
        for key, values in matrix.items():
            if not isinstance(values, list) or not values:
                raise ValueError(f"matrix.{key} must be a non-empty list")
            normalized_matrix[str(key)] = list(values)
        repeats = int(data.get("repeats") or 1)
        if repeats < 1:
            raise ValueError("repeats must be at least one")
        base = data.get("base") or {}
        if not isinstance(base, dict):
            raise ValueError("base must be a mapping")
        variants = data.get("variants") or []
        if not isinstance(variants, list):
            raise ValueError("variants must be a list")
        normalized_variants: List[Dict[str, Any]] = []
        for index, variant in enumerate(variants):
            if not isinstance(variant, dict):
                raise ValueError(f"variants[{index}] must be a mapping")
            normalized_variants.append(dict(variant))
        output_root = data.get("output_root")
        return cls(
            experiment_id=experiment_id,
            input_dcps=[str(path) for path in input_dcps],
            base=dict(base),
            variants=normalized_variants,
            matrix=normalized_matrix,
            repeats=repeats,
            output_root=str(output_root) if output_root else None,
            strict_experiment=bool(data.get("strict_experiment", False)),
        )


def load_experiment_spec(path: Path) -> ExperimentSpec:
    text = Path(path).read_text()
    stripped = text.lstrip()
    if stripped.startswith("{"):
        data = json.loads(text)
    else:
        data = parse_yaml_subset(text)
    return ExperimentSpec.from_dict(data)


def parse_yaml_subset(text: str) -> Dict[str, Any]:
    lines = _preprocess_lines(text)
    root: Dict[str, Any] = {}
    stack: List[tuple[int, Any]] = [(-1, root)]
    for index, (indent, content) in enumerate(lines):
        while stack and indent <= stack[-1][0]:
            stack.pop()
        if not stack:
            raise ValueError(f"Invalid indentation near: {content}")
        parent = stack[-1][1]
        if content.startswith("- "):
            if not isinstance(parent, list):
                raise ValueError(f"List item without list parent near: {content}")
            item_text = content[2:].strip()
            if not item_text:
                item: Any = {}
                parent.append(item)
                stack.append((indent, item))
                continue
            if ":" in item_text:
                key, raw_value = item_text.split(":", 1)
                key = key.strip()
                raw_value = raw_value.strip()
                item = {key: _parse_scalar(raw_value)} if raw_value else {key: [] if _next_content_is_list(lines, index, indent) else {}}
                parent.append(item)
                stack.append((indent, item))
                continue
            parent.append(_parse_scalar(item_text))
            continue
        if ":" not in content:
            raise ValueError(f"Expected key/value pair near: {content}")
        key, raw_value = content.split(":", 1)
        key = key.strip()
        raw_value = raw_value.strip()
        if not isinstance(parent, dict):
            raise ValueError(f"Mapping entry without mapping parent near: {content}")
        if raw_value:
            parent[key] = _parse_scalar(raw_value)
            continue
        container: Any = [] if _next_content_is_list(lines, index, indent) else {}
        parent[key] = container
        stack.append((indent, container))
    return root


def _preprocess_lines(text: str) -> List[tuple[int, str]]:
    rows: List[tuple[int, str]] = []
    for raw in text.splitlines():
        without_comment = raw.split("#", 1)[0].rstrip()
        if not without_comment.strip():
            continue
        indent = len(without_comment) - len(without_comment.lstrip(" "))
        rows.append((indent, without_comment.strip()))
    return rows


def _next_content_is_list(lines: List[tuple[int, str]], index: int, parent_indent: int) -> bool:
    for next_indent, next_content in lines[index + 1:]:
        if next_indent <= parent_indent:
            return False
        return next_content.startswith("- ")
    return False


def _parse_scalar(value: str) -> Any:
    value = value.strip()
    if value.startswith("[") and value.endswith("]"):
        inner = value[1:-1].strip()
        if not inner:
            return []
        return [_parse_scalar(part.strip()) for part in inner.split(",")]
    lowered = value.lower()
    if lowered in {"true", "false"}:
        return lowered == "true"
    if lowered in {"null", "none"}:
        return None
    if (value.startswith('"') and value.endswith('"')) or (value.startswith("'") and value.endswith("'")):
        return value[1:-1]
    try:
        return int(value)
    except ValueError:
        pass
    try:
        return float(value)
    except ValueError:
        return value
