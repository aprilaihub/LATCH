"""Provider and policy contracts for the three validation levels."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from optimizer_pipeline.core.domain import ValidationLevel, ValidationRecord, ValidationStatus


@dataclass(frozen=True, slots=True)
class ValidationRequest:
    validation_id: str
    run_id: str
    scope_id: str
    subject_id: str
    artifact_id: str
    attempt_id: str | None = None


@runtime_checkable
class ValidationProvider(Protocol):
    """Async adapter implemented by a concrete validator outside core."""

    @property
    def level(self) -> ValidationLevel: ...

    async def validate(self, request: ValidationRequest) -> ValidationRecord: ...


@dataclass(frozen=True, slots=True)
class ValidationRequirement:
    level: ValidationLevel
    required: bool
    accepted_statuses: frozenset[ValidationStatus]

    def __post_init__(self) -> None:
        if self.required and not self.accepted_statuses:
            raise ValueError("required validation must define accepted statuses")


@dataclass(frozen=True, slots=True)
class ValidationPolicy:
    """Application-declared gates; Attempt Validation is always mandatory."""

    certification_required: bool = False
    certification_accepted_statuses: frozenset[ValidationStatus] = frozenset({ValidationStatus.PASSED})

    @property
    def requirements(self) -> tuple[ValidationRequirement, ...]:
        return (
            ValidationRequirement(
                level=ValidationLevel.ATTEMPT,
                required=True,
                accepted_statuses=frozenset(
                    {
                        ValidationStatus.CLEAN,
                        ValidationStatus.WARNING,
                        ValidationStatus.FALLBACK_USED,
                    }
                ),
            ),
            ValidationRequirement(
                level=ValidationLevel.CERTIFICATION,
                required=self.certification_required,
                accepted_statuses=self.certification_accepted_statuses,
            ),
            ValidationRequirement(
                level=ValidationLevel.FORMAL,
                required=False,
                accepted_statuses=frozenset({ValidationStatus.NOT_RUN, ValidationStatus.PASSED}),
            ),
        )
