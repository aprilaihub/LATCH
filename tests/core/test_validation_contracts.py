from __future__ import annotations

import asyncio

import pytest

from optimizer_pipeline.core.domain import (
    ValidationLevel,
    ValidationRecord,
    ValidationStatus,
)
from optimizer_pipeline.core.validation import (
    ATTEMPT_STATUSES,
    CERTIFICATION_STATUSES,
    FORMAL_STATUSES,
    ValidationPolicy,
    ValidationProvider,
    ValidationRequest,
    ValidationRequirement,
    default_attempt_eligibility,
    formal_not_run,
    requirement_satisfied,
    validate_provider_result,
    validate_record_contract,
)


def record(
    level: ValidationLevel,
    status: ValidationStatus,
    *,
    attempt_id: str | None = None,
) -> ValidationRecord:
    return ValidationRecord(
        validation_id="validation-1",
        run_id="run-1",
        scope_id="scope-1",
        subject_id="state-1",
        level=level,
        status=status,
        attempt_id=attempt_id,
    )


def request(*, attempt_id: str | None = None) -> ValidationRequest:
    return ValidationRequest(
        validation_id="validation-1",
        run_id="run-1",
        scope_id="scope-1",
        subject_id="state-1",
        artifact_id="artifact-1",
        attempt_id=attempt_id,
    )


def test_provider_contract_is_async_and_tool_agnostic():
    class FakeProvider:
        level = ValidationLevel.ATTEMPT

        async def validate(self, validation_request: ValidationRequest) -> ValidationRecord:
            return record(
                self.level,
                ValidationStatus.CLEAN,
                attempt_id=validation_request.attempt_id,
            )

    provider = FakeProvider()
    assert isinstance(provider, ValidationProvider)
    result = asyncio.run(provider.validate(request(attempt_id="attempt-1")))
    assert result.status is ValidationStatus.CLEAN


def test_attempt_validation_is_mandatory_for_every_policy():
    for policy in (
        ValidationPolicy(),
        ValidationPolicy(certification_required=True),
    ):
        attempt = policy.requirements[0]
        assert attempt == ValidationRequirement(
            level=ValidationLevel.ATTEMPT,
            required=True,
            accepted_statuses=frozenset(
                {
                    ValidationStatus.CLEAN,
                    ValidationStatus.WARNING,
                    ValidationStatus.FALLBACK_USED,
                }
            ),
        )


def test_policy_declares_optional_certification_but_formal_is_never_a_gate():
    default_requirements = {item.level: item for item in ValidationPolicy().requirements}
    assert not default_requirements[ValidationLevel.CERTIFICATION].required
    assert not default_requirements[ValidationLevel.FORMAL].required

    certification_requirements = {
        item.level: item
        for item in ValidationPolicy(certification_required=True).requirements
    }
    assert certification_requirements[ValidationLevel.CERTIFICATION].required
    assert not certification_requirements[ValidationLevel.FORMAL].required
    assert ValidationStatus.NOT_RUN in certification_requirements[ValidationLevel.FORMAL].accepted_statuses


def test_provider_result_identity_and_level_must_match_request():
    req = request(attempt_id="attempt-1")
    result = record(ValidationLevel.ATTEMPT, ValidationStatus.CLEAN, attempt_id="attempt-1")
    assert validate_provider_result(req, expected_level=ValidationLevel.ATTEMPT, record=result) is result
    wrong = ValidationRecord(
        validation_id="other",
        run_id=result.run_id,
        scope_id=result.scope_id,
        subject_id=result.subject_id,
        level=result.level,
        status=result.status,
        attempt_id=result.attempt_id,
    )
    with pytest.raises(ValueError, match="validation_id"):
        validate_provider_result(req, expected_level=ValidationLevel.ATTEMPT, record=wrong)


@pytest.mark.parametrize(
    ("level", "allowed"),
    [
        (ValidationLevel.ATTEMPT, ATTEMPT_STATUSES),
        (ValidationLevel.CERTIFICATION, CERTIFICATION_STATUSES),
        (ValidationLevel.FORMAL, FORMAL_STATUSES),
    ],
)
def test_each_validation_level_has_an_explicit_status_vocabulary(level, allowed):
    for status in ValidationStatus:
        attempt_id = "attempt-1" if level is ValidationLevel.ATTEMPT else None
        candidate = record(level, status, attempt_id=attempt_id)
        if status in allowed:
            assert validate_record_contract(candidate) is candidate
        else:
            with pytest.raises(ValueError, match="is not valid"):
                validate_record_contract(candidate)


@pytest.mark.parametrize(
    ("status", "eligible"),
    [
        (ValidationStatus.CLEAN, True),
        (ValidationStatus.WARNING, True),
        (ValidationStatus.FALLBACK_USED, True),
        (ValidationStatus.INVALID, False),
        (ValidationStatus.FAILED, False),
    ],
)
def test_default_attempt_mapping_is_the_only_source_of_default_eligibility(status, eligible):
    eligibility = default_attempt_eligibility(
        record(ValidationLevel.ATTEMPT, status, attempt_id="attempt-1"),
        eligibility_id="eligibility-1",
        state_id="state-1",
    )
    assert eligibility.parent_eligible is eligible
    assert eligibility.selection_eligible is eligible
    assert status.value in eligibility.reason


def test_non_attempt_validation_cannot_create_state_eligibility():
    with pytest.raises(ValueError, match="Attempt Validation"):
        default_attempt_eligibility(
            record(ValidationLevel.CERTIFICATION, ValidationStatus.PASSED),
            eligibility_id="eligibility-1",
            state_id="state-1",
        )


def test_formal_defaults_to_not_run_without_an_engine_dependency():
    result = formal_not_run(request())
    assert result.level is ValidationLevel.FORMAL
    assert result.status is ValidationStatus.NOT_RUN
    assert result.details.get("reason")

    with pytest.raises(ValueError, match="must not be attached"):
        formal_not_run(request(attempt_id="attempt-1"))


def test_requirements_distinguish_missing_optional_from_missing_required():
    optional = ValidationRequirement(
        ValidationLevel.CERTIFICATION,
        False,
        frozenset({ValidationStatus.PASSED}),
    )
    required = ValidationRequirement(
        ValidationLevel.CERTIFICATION,
        True,
        frozenset({ValidationStatus.PASSED}),
    )
    assert requirement_satisfied(optional, None)
    assert not requirement_satisfied(required, None)
    assert requirement_satisfied(
        optional,
        record(ValidationLevel.CERTIFICATION, ValidationStatus.NOT_RUN),
    )
    assert requirement_satisfied(
        required,
        record(ValidationLevel.CERTIFICATION, ValidationStatus.PASSED),
    )
    assert not requirement_satisfied(
        required,
        record(ValidationLevel.CERTIFICATION, ValidationStatus.FAILED),
    )


def test_requirement_rejects_wrong_level_and_empty_required_acceptance_set():
    requirement = ValidationRequirement(
        ValidationLevel.CERTIFICATION,
        True,
        frozenset({ValidationStatus.PASSED}),
    )
    with pytest.raises(ValueError, match="does not match"):
        requirement_satisfied(
            requirement,
            record(ValidationLevel.FORMAL, ValidationStatus.PASSED),
        )
    with pytest.raises(ValueError, match="accepted statuses"):
        ValidationRequirement(ValidationLevel.FORMAL, True, frozenset())
