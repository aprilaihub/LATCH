"""Typed prompt template records."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List


@dataclass(frozen=True)
class PromptManifest:
    id: str
    task: str
    name: str
    system: str
    user: str
    response_schema: str
    template_engine: str = "python_format"
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class PromptTemplate:
    manifest: PromptManifest
    root: Path
    system_text: str
    user_text: str
    response_schema: Dict[str, Any]
    response_schema_text: str
    manifest_data: Dict[str, Any]


@dataclass(frozen=True)
class RenderedPrompt:
    messages: List[Dict[str, str]]
    prompt_id: str
    prompt_hash: str
    response_schema_hash: str
    metadata: Dict[str, Any] = field(default_factory=dict)
