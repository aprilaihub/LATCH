"""Memory-aware, root-round-robin scheduler for complete validation cases."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Mapping


SCHEDULER_VERSION = "dof.validation-scheduler.v3"
GIB = 1024**3
MAX_VALIDATION_CONCURRENCY = 20
DEFAULT_MEMORY_ESTIMATES = {"normal": 8 * GIB, "high": 64 * GIB, "unknown": 64 * GIB}


@dataclass(frozen=True, slots=True)
class ValidationCase:
    case_id: str
    root_id: str
    memory_class: str = "unknown"


@dataclass(frozen=True, slots=True)
class ValidationSeed:
    case_id: str
    root_id: str
    seed_id: str
    stimulus_seed: int
    memory_class: str = "unknown"


def linux_memory_bytes(path: Path = Path("/proc/meminfo")) -> tuple[int, int]:
    values = {}
    for line in path.read_text().splitlines():
        key, raw = line.split(":", 1)
        values[key] = int(raw.strip().split()[0]) * 1024
    return values["MemTotal"], values["MemAvailable"]


def root_round_robin(cases: Iterable[ValidationCase]) -> list[ValidationCase]:
    queues: dict[str, list[ValidationCase]] = {}
    for case in cases:
        queues.setdefault(case.root_id, []).append(case)
    ordered = []
    roots = sorted(queues)
    while any(queues.values()):
        for root in roots:
            if queues[root]:
                ordered.append(queues[root].pop(0))
    return ordered


def seed_root_round_robin(units: Iterable[ValidationSeed]) -> list[ValidationSeed]:
    queues: dict[str, list[ValidationSeed]] = {}
    for unit in units:
        queues.setdefault(unit.root_id, []).append(unit)
    ordered: list[ValidationSeed] = []
    roots = sorted(queues)
    while any(queues.values()):
        for root in roots:
            if queues[root]:
                ordered.append(queues[root].pop(0))
    return ordered


class MemoryAwareSeedScheduler:
    """Global scheduler whose admission unit is exactly ``(case, seed)``."""

    def __init__(
        self,
        units: Iterable[ValidationSeed],
        *,
        maximum: int = 16,
        target: int = 16,
        reserve_fraction: float = 0.20,
        estimates: Mapping[str, int] = DEFAULT_MEMORY_ESTIMATES,
    ) -> None:
        if not 1 <= target <= maximum <= MAX_VALIDATION_CONCURRENCY:
            raise ValueError("validation_seed_concurrency_must_be_1_to_20")
        if not 0.20 <= reserve_fraction < 1:
            raise ValueError("validation_memory_reserve_below_20_percent")
        self.pending = seed_root_round_robin(units)
        self.maximum = maximum
        self.target = target
        self.reserve_fraction = reserve_fraction
        self.estimates = dict(estimates)

    def refill(
        self, *, running: Iterable[ValidationSeed], total_memory: int,
        available_memory: int,
    ) -> list[ValidationSeed]:
        active = list(running)
        slots = min(self.maximum, self.target) - len(active)
        if slots <= 0:
            return []
        allowance = max(0, available_memory - int(total_memory * self.reserve_fraction))
        selected: list[ValidationSeed] = []
        while self.pending and len(selected) < slots:
            # Scan the pending round-robin order so one temporarily expensive
            # unit does not block smaller roots behind it.
            admitted_index = None
            for index, unit in enumerate(self.pending):
                estimate = int(self.estimates.get(unit.memory_class, self.estimates["unknown"]))
                if estimate <= allowance:
                    admitted_index = index
                    allowance -= estimate
                    break
            if admitted_index is None:
                break
            selected.append(self.pending.pop(admitted_index))
        return selected


class MemoryAwareValidationScheduler:
    def __init__(
        self,
        cases: Iterable[ValidationCase],
        *,
        maximum: int = 16,
        target: int = 16,
        reserve_fraction: float = 0.20,
        estimates: Mapping[str, int] = DEFAULT_MEMORY_ESTIMATES,
    ) -> None:
        if not 1 <= target <= maximum <= MAX_VALIDATION_CONCURRENCY:
            raise ValueError("validation_concurrency_must_be_1_to_20")
        if not 0.20 <= reserve_fraction < 1:
            raise ValueError("validation_memory_reserve_below_20_percent")
        self.pending = root_round_robin(cases)
        self.maximum = maximum
        self.target = target
        self.reserve_fraction = reserve_fraction
        self.estimates = dict(estimates)

    def refill(self, *, running: Iterable[ValidationCase], total_memory: int, available_memory: int) -> list[ValidationCase]:
        active = list(running)
        slots = min(self.maximum, self.target) - len(active)
        if slots <= 0:
            return []
        reserve = int(total_memory * self.reserve_fraction)
        allowance = max(0, available_memory - reserve)
        selected = []
        while self.pending and len(selected) < slots:
            case = self.pending[0]
            estimate = int(self.estimates.get(case.memory_class, self.estimates["unknown"]))
            if estimate > allowance:
                break
            selected.append(self.pending.pop(0))
            allowance -= estimate
        return selected


__all__ = [
    "DEFAULT_MEMORY_ESTIMATES", "MemoryAwareValidationScheduler",
    "MemoryAwareSeedScheduler", "SCHEDULER_VERSION", "ValidationCase",
    "ValidationSeed", "linux_memory_bytes", "root_round_robin",
    "seed_root_round_robin",
]
