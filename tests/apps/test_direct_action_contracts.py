from __future__ import annotations

import pytest

from optimizer_pipeline.apps.direct_action import DirectActionRequest
from optimizer_pipeline.backends import (
    ActionCatalog,
    ActionDefinition,
    ActionParameter,
    InvalidActionParameters,
    ParameterType,
    UnsafeManagedActionError,
    UnsupportedActionError,
)


CATALOG = ActionCatalog(
    (
        ActionDefinition(
            "phys_opt",
            "Existing phys-opt flow",
            (ActionParameter("directive", ParameterType.STRING, required=True),),
        ),
    )
)


def request(**overrides) -> DirectActionRequest:
    values = {
        "action_id": "action-1",
        "run_id": "run-1",
        "scope_id": "scope-1",
        "parent_state_id": "state-1",
        "input_artifact_id": "artifact-1",
        "output_uri": "output.dcp",
        "action_name": "phys_opt",
        "parameters": {"directive": "Explore"},
    }
    values.update(overrides)
    return DirectActionRequest(**values)


def test_direct_action_resolves_only_an_explicit_supported_action():
    resolved = request().resolve(CATALOG)
    assert resolved.action_type == "phys_opt"
    assert resolved.parameters.to_dict() == {"directive": "Explore"}

    with pytest.raises(ValueError, match="action_name must be explicitly provided"):
        request(action_name=" ")
    with pytest.raises(UnsupportedActionError):
        request(action_name="auto_detected_from_filename").resolve(CATALOG)
    with pytest.raises(InvalidActionParameters, match="missing required"):
        request(parameters={}).resolve(CATALOG)


def test_input_identity_cannot_change_action_resolution():
    first = request(input_artifact_id="renamed-vexriscv.dcp").resolve(CATALOG)
    second = request(input_artifact_id="arbitrary-copy.dcp").resolve(CATALOG)
    assert first.action_type == second.action_type == "phys_opt"
    assert first.parameters == second.parameters


def test_direct_action_rejects_backend_actions_marked_unsafe_for_managed_paths():
    unsafe_catalog = ActionCatalog(
        (ActionDefinition("raw_adapter", "Internal-only adapter", managed_safe=False),)
    )
    with pytest.raises(UnsafeManagedActionError, match="managed path"):
        request(action_name="raw_adapter", parameters={}).resolve(unsafe_catalog)
