"""Deeply immutable JSON-compatible values for domain annotations."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping


@dataclass(frozen=True, slots=True)
class FrozenMap:
    items: tuple[tuple[str, object], ...] = ()

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any] | None = None) -> "FrozenMap":
        if not value:
            return cls()
        return cls(tuple(sorted((str(key), freeze_value(item)) for key, item in value.items())))

    def get(self, key: str, default: object = None) -> object:
        return next((value for item_key, value in self.items if item_key == key), default)

    def to_dict(self) -> dict[str, Any]:
        return {key: thaw_value(value) for key, value in self.items}


def freeze_value(value: Any) -> object:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, FrozenMap):
        return value
    if isinstance(value, Mapping):
        return FrozenMap.from_mapping(value)
    if isinstance(value, (list, tuple)):
        return tuple(freeze_value(item) for item in value)
    raise TypeError(f"value is not JSON-compatible: {type(value).__name__}")


def thaw_value(value: object) -> Any:
    if isinstance(value, FrozenMap):
        return value.to_dict()
    if isinstance(value, tuple):
        return [thaw_value(item) for item in value]
    return value


def frozen_map(value: Mapping[str, Any] | FrozenMap | None = None) -> FrozenMap:
    return value if isinstance(value, FrozenMap) else FrozenMap.from_mapping(value)

