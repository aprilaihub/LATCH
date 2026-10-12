"""Prompt rendering with stable prompt hashing."""

from __future__ import annotations

import hashlib
import json
from typing import Any, Dict, Optional

from optimizer_pipeline.models import to_jsonable

from .loader import PromptLoader
from .types import PromptTemplate, RenderedPrompt


class PromptRenderer:
    def __init__(self, prompt_id: str, *, loader: Optional[PromptLoader] = None) -> None:
        self.prompt_id = prompt_id
        self.loader = loader or PromptLoader()
        self._template: Optional[PromptTemplate] = None

    @property
    def template(self) -> PromptTemplate:
        if self._template is None:
            self._template = self.loader.load(self.prompt_id)
        return self._template

    def render(self, request: Dict[str, Any], *, compact_request: Optional[Dict[str, Any]] = None) -> RenderedPrompt:
        template = self.template
        compact = to_jsonable(compact_request if compact_request is not None else request)
        compact_request_json = json.dumps(compact, indent=2, sort_keys=True)
        variables = {
            "compact_request_json": compact_request_json,
            "response_schema_text": template.response_schema_text,
            "prompt_id": template.manifest.id,
        }
        system_content = template.system_text.format(**variables).rstrip()
        user_content = template.user_text.format(**variables).rstrip()
        prompt_hash = self._prompt_hash(template)
        schema_hash = self._hash_json(template.response_schema)
        return RenderedPrompt(
            messages=[
                {"role": "system", "content": system_content},
                {"role": "user", "content": user_content},
            ],
            prompt_id=template.manifest.id,
            prompt_hash=prompt_hash,
            response_schema_hash=schema_hash,
            metadata={
                "task": template.manifest.task,
                "name": template.manifest.name,
                **template.manifest.metadata,
            },
        )

    def describe(self) -> Dict[str, Any]:
        template = self.template
        return {
            "prompt_id": template.manifest.id,
            "prompt_hash": self._prompt_hash(template),
            "response_schema_hash": self._hash_json(template.response_schema),
            "metadata": {
                "task": template.manifest.task,
                "name": template.manifest.name,
                **template.manifest.metadata,
            },
        }

    @classmethod
    def _prompt_hash(cls, template: PromptTemplate) -> str:
        data = {
            "manifest": template.manifest_data,
            "system": template.system_text,
            "user": template.user_text,
            "response_schema": template.response_schema,
        }
        return cls._hash_json(data)

    @staticmethod
    def _hash_json(value: Any) -> str:
        normalized = json.dumps(to_jsonable(value), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(normalized.encode("utf-8")).hexdigest()
