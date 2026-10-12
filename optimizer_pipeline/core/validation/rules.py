"""Level-specific status and eligibility rules."""

from __future__ import annotations

from optimizer_pipeline.core.domain import (
    StateEligibility,
    ValidationLevel,
    ValidationRecord,
    ValidationStatus,
)

from .contracts import ValidationRequest, ValidationRequirement


ATTEMPT_STATUSES = frozenset(
    {
        ValidationStatus.CLEAN,
        ValidationStatus.WARNING,
        ValidationStatus.FALLBACK_USED,
        ValidationStatus.INVALID,
        ValidationStatus.FAILED,
    }
)
CERTIFICATION_STATUSES = frozenset(
    {ValidationStatus.NOT_RUN, ValidationStatus.PASSED, ValidationStatus.FAILED}
)
FORMAL_STATUSES = frozenset(
    {ValidationStatus.NOT_RUN, ValidationStatus.PASSED, ValidationStatus.FAILED}
)


def validate_record_contract(record: ValidationRecord) -> ValidationRecord:
    allowed = {
        ValidationLevel.ATTEMPT: ATTEMPT_STATUSES,
        ValidationLevel.CERTIFICATION: CERTIFICATION_STATUSES,
        ValidationLevel.FORMAL: FORMAL_STATUSES,
    }[record.level]
    if record.status not in allowed:
        raise ValueError(f"{record.status.value} is not valid for {record.level.value} validation")
    if record.level is ValidationLevel.ATTEMPT and not record.attempt_id:
        raise ValueError("Attempt Validation requires attempt_id")
    return record


def validate_provider_result(
    request: ValidationRequest,
    *,
    expected_level: ValidationLevel,
    record: ValidationRecord,
) -> ValidationRecord:
    validate_record_contract(record)
    if record.validation_id != request.validation_id:
        raise ValueError("validation provider changed validation_id")
    if record.run_id != request.run_id or record.scope_id != request.scope_id:
        raise ValueError("validation provider returned another run or scope")
    if record.subject_id != request.subject_id or record.level is not expected_level:
        raise ValueError("validation provider returned another subject or level")
    if record.attempt_id != request.attempt_id:
        raise ValueError("validation provider returned another attempt")
    return record


def default_attempt_eligibility(
    record: ValidationRecord,
    *,
    eligibility_id: str,
    state_id: str,
) -> StateEligibility:
    validate_record_contract(record)
    if record.level is not ValidationLevel.ATTEMPT:
        raise ValueError("eligibility can only be derived from Attempt Validation")
    eligible = record.status in {
        ValidationStatus.CLEAN,
        ValidationStatus.WARNING,
        ValidationStatus.FALLBACK_USED,
    }
    return StateEligibility(
        eligibility_id=eligibility_id,
        run_id=record.run_id,
        state_id=state_id,
        parent_eligible=eligible,
        selection_eligible=eligible,
        reason=f"default Attempt Validation mapping: {record.status.value}",
    )


def formal_not_run(request: ValidationRequest) -> ValidationRecord:
    if request.attempt_id is not None:
        # Formal is a state/artifact-level placeholder, never the mandatory
        # per-attempt validator.
        raise ValueError("Formal placeholder must not be attached as Attempt Validation")
    return ValidationRecord(
        validation_id=request.validation_id,
        run_id=request.run_id,
        scope_id=request.scope_id,
        subject_id=request.subject_id,
        level=ValidationLevel.FORMAL,
        status=ValidationStatus.NOT_RUN,
        details={"reason": "formal verification engine is outside current scope"},
    )


def requirement_satisfied(
    requirement: ValidationRequirement,
    record: ValidationRecord | None,
) -> bool:
    if record is None:
        return not requirement.required
    validate_record_contract(record)
    if record.level is not requirement.level:
        raise ValueError("validation record level does not match requirement")
    if not requirement.required and record.status is ValidationStatus.NOT_RUN:
        return True
    return record.status in requirement.accepted_statuses
