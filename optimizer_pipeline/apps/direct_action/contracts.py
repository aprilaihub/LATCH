"""Explicit direct-action application request contract."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping

from optimizer_pipeline.backends.base import ActionCatalog
from optimizer_pipeline.core.domain import ActionSpecification, FrozenMap, frozen_map


@dataclass(frozen=True, slots=True)
class DirectActionRequest:
    action_id: str
    run_id: str
    scope_id: str
    parent_state_id: str
    input_artifact_id: str
    output_uri: str
    action_name: str
    parameters: FrozenMap = field(default_factory=FrozenMap)

    def __post_init__(self) -> None:
        for name in (
            "action_id",
            "run_id",
            "scope_id",
            "parent_state_id",
            "input_artifact_id",
            "output_uri",
            "action_name",
        ):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be explicitly provided")
        if isinstance(self.parameters, Mapping):
            object.__setattr__(self, "parameters", frozen_map(self.parameters))
        elif not isinstance(self.parameters, FrozenMap):
            raise TypeError("parameters must be FrozenMap or mapping")

    def resolve(self, catalog: ActionCatalog) -> ActionSpecification:
        action = ActionSpecification(
            action_id=self.action_id,
            run_id=self.run_id,
            scope_id=self.scope_id,
            action_type=self.action_name,
            parameters=self.parameters,
        )
        catalog.validate(action, require_managed_safe=True)
        return action
