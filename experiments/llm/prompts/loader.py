"""Prompt manifest loader."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Optional

from .types import PromptManifest, PromptTemplate


class PromptLoadError(RuntimeError):
    """Raised when a prompt manifest or template cannot be loaded."""


class PromptLoader:
    def __init__(self, prompt_root: Optional[Path | str] = None) -> None:
        self.prompt_root = Path(prompt_root) if prompt_root else Path(__file__).resolve().parents[3] / "prompts"

    def load(self, prompt_id: str) -> PromptTemplate:
        if not prompt_id or "." not in prompt_id:
            raise PromptLoadError(f"Prompt id must use task.name form: {prompt_id!r}")
        task, name = prompt_id.split(".", 1)
        prompt_dir = self.prompt_root / task / name
        manifest_path = prompt_dir / "manifest.json"
        if not manifest_path.is_file():
            raise PromptLoadError(f"Prompt manifest not found for {prompt_id!r}: {manifest_path}")
        manifest_data = self._read_json(manifest_path, label="prompt manifest")
        manifest = self._manifest_from_data(manifest_data, prompt_id=prompt_id)
        if manifest.template_engine != "python_format":
            raise PromptLoadError(f"Unsupported prompt template engine for {prompt_id!r}: {manifest.template_engine}")

        system_text = self._read_text(prompt_dir / manifest.system, label="system prompt")
        user_text = self._read_text(prompt_dir / manifest.user, label="user prompt")
        response_schema = self._read_json(prompt_dir / manifest.response_schema, label="response schema")
        response_schema_text = self._schema_text(response_schema)
        return PromptTemplate(
            manifest=manifest,
            root=prompt_dir,
            system_text=system_text,
            user_text=user_text,
            response_schema=response_schema,
            response_schema_text=response_schema_text,
            manifest_data=manifest_data,
        )

    @staticmethod
    def _manifest_from_data(data: Dict[str, Any], *, prompt_id: str) -> PromptManifest:
        required = ["id", "task", "name", "system", "user", "response_schema"]
        missing = [key for key in required if not data.get(key)]
        if missing:
            raise PromptLoadError(f"Prompt manifest {prompt_id!r} missing required field(s): {', '.join(missing)}")
        if data["id"] != prompt_id:
            raise PromptLoadError(f"Prompt manifest id mismatch: expected {prompt_id!r}, got {data['id']!r}")
        return PromptManifest(
            id=str(data["id"]),
            task=str(data["task"]),
            name=str(data["name"]),
            system=str(data["system"]),
            user=str(data["user"]),
            response_schema=str(data["response_schema"]),
            template_engine=str(data.get("template_engine") or "python_format"),
            metadata=dict(data.get("metadata") or {}),
        )

    @staticmethod
    def _read_text(path: Path, *, label: str) -> str:
        if not path.is_file():
            raise PromptLoadError(f"Missing {label}: {path}")
        return path.read_text()

    @staticmethod
    def _read_json(path: Path, *, label: str) -> Dict[str, Any]:
        if not path.is_file():
            raise PromptLoadError(f"Missing {label}: {path}")
        try:
            data = json.loads(path.read_text())
        except json.JSONDecodeError as exc:
            raise PromptLoadError(f"Malformed {label} JSON at {path}: {exc}") from exc
        if not isinstance(data, dict):
            raise PromptLoadError(f"{label} must be a JSON object: {path}")
        return data

    @staticmethod
    def _schema_text(schema: Dict[str, Any]) -> str:
        text = schema.get("schema_text")
        if isinstance(text, str) and text.strip():
            return text.rstrip()
        return json.dumps(schema, indent=2, sort_keys=True)
