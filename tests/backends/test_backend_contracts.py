from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest

from optimizer_pipeline.apps.direct_action import DirectActionRequest
from optimizer_pipeline.backends import (
    ActionCatalog,
    ActionDefinition,
    ActionParameter,
    AnalysisProbeKind,
    AnalysisProbeRequest,
    AnalysisProbeResult,
    Backend,
    BackendExecutionRequest,
    BackendExecutionResult,
    InvalidActionParameters,
    ParameterType,
    UnsupportedActionError,
    backend_session,
)
from optimizer_pipeline.core.domain import (
    ActionSpecification,
    ArtifactKind,
    ArtifactReference,
    Attempt,
    OutcomeStatus,
)


NOW = datetime(2026, 1, 2, tzinfo=timezone.utc)
CATALOG = ActionCatalog(
    (
        ActionDefinition(
            "phys_opt_design",
            "Existing physical optimization flow",
            (
                ActionParameter(
                    "directive", ParameterType.STRING, required=True,
                    allowed_values=("Explore", "AggressiveExplore"),
                ),
                ActionParameter("retime", ParameterType.BOOLEAN),
            ),
        ),
        ActionDefinition("route_design", "Existing route flow", allow_extra_parameters=True),
    )
)


def execution_facts():
    action = ActionSpecification(
        "action-1", "run-1", "scope-1", "phys_opt_design",
        {"directive": "Explore", "retime": True}, NOW,
    )
    attempt = Attempt(
        "attempt-1", "run-1", "scope-1", "state-root", action.action_id,
        scheduled_at=NOW,
    )
    artifact = ArtifactReference(
        "artifact-in", "run-1", "scope-1", ArtifactKind.DCP,
        "mystery-name.dcp", created_at=NOW,
    )
    return action, attempt, artifact


class MockBackend:
    name = "mock"
    catalog = CATALOG

    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.events: list[str] = []

    async def start(self) -> None:
        self.events.append("start")

    async def execute(self, request):
        self.events.append(f"execute:{request.action.action_type}")
        if self.fail:
            raise RuntimeError("tool failure")
        self.catalog.validate(request.action)
        return BackendExecutionResult(
            OutcomeStatus.SUCCEEDED, NOW, NOW,
            output_uri=request.output_uri, metrics={"wns": 0.1},
        )

    async def probe(self, request):
        self.events.append("probe:" + ",".join(probe.value for probe in request.probes))
        return AnalysisProbeResult(request.artifact.artifact_id, {"wns": -0.1}, NOW)

    async def close(self) -> None:
        self.events.append("close")


def test_action_catalog_preserves_exact_names_and_validates_typed_parameters():
    action, _, _ = execution_facts()
    assert CATALOG.validate(action).name == "phys_opt_design"
    assert [definition.name for definition in CATALOG.actions] == [
        "phys_opt_design", "route_design",
    ]
    with pytest.raises(UnsupportedActionError, match="does not support"):
        CATALOG.definition("filename_detected_flow")
    with pytest.raises(InvalidActionParameters, match="missing required"):
        CATALOG.validate(
            ActionSpecification("bad-1", "run-1", "scope-1", "phys_opt_design", created_at=NOW)
        )
    with pytest.raises(InvalidActionParameters, match="must be one of"):
        CATALOG.validate(
            ActionSpecification(
                "bad-2", "run-1", "scope-1", "phys_opt_design",
                {"directive": "Invented"}, NOW,
            )
        )


def test_direct_action_requires_explicit_name_and_does_not_infer_from_artifact():
    request = DirectActionRequest(
        action_id="action-1",
        run_id="run-1",
        scope_id="scope-1",
        parent_state_id="state-root",
        input_artifact_id="design_phys_opt_design.dcp",
        output_uri="output.dcp",
        action_name="route_design",
        parameters={"directive": "NoTimingRelaxation"},
    )
    resolved = request.resolve(CATALOG)
    assert resolved.action_type == "route_design"
    assert resolved.action_type not in request.input_artifact_id
    with pytest.raises(ValueError, match="action_name must be explicitly provided"):
        DirectActionRequest(
            "action-2", "run-1", "scope-1", "state-root",
            "phys_opt_design.dcp", "output.dcp", "",
        )


def test_mock_backend_executes_and_probes_through_independent_contract():
    action, attempt, artifact = execution_facts()
    execution = BackendExecutionRequest(
        attempt, action, artifact, "output.dcp", "workspace/run-1",
    )
    probe = AnalysisProbeRequest(
        artifact,
        (AnalysisProbeKind.TIMING_SUMMARY, AnalysisProbeKind.ROUTE_STATUS),
        "workspace/probe-1",
    )
    backend = MockBackend()
    assert isinstance(backend, Backend)

    async def run():
        async with backend_session(backend) as active:
            result = await active.execute(execution)
            analysis = await active.probe(probe)
        return result, analysis

    result, analysis = asyncio.run(run())
    assert result.status is OutcomeStatus.SUCCEEDED
    assert analysis.artifact_id == artifact.artifact_id
    assert backend.events == [
        "start", "execute:phys_opt_design",
        "probe:TIMING_SUMMARY,ROUTE_STATUS", "close",
    ]


def test_execution_request_rejects_mixed_identity_or_scope():
    action, attempt, artifact = execution_facts()
    wrong_action = ActionSpecification(
        "other-action", action.run_id, action.scope_id, action.action_type,
        {"directive": "Explore"}, NOW,
    )
    with pytest.raises(ValueError, match="IDs do not match"):
        BackendExecutionRequest(attempt, wrong_action, artifact, "output.dcp", "workspace")
    other_scope = ArtifactReference(
        "artifact-other", "run-1", "other-scope", ArtifactKind.DCP,
        "input.dcp", created_at=NOW,
    )
    with pytest.raises(ValueError, match="execution scope"):
        BackendExecutionRequest(attempt, action, other_scope, "output.dcp", "workspace")


def test_backend_lifecycle_closes_after_failure():
    action, attempt, artifact = execution_facts()
    request = BackendExecutionRequest(attempt, action, artifact, "output.dcp", "workspace")
    backend = MockBackend(fail=True)

    async def run():
        async with backend_session(backend) as active:
            await active.execute(request)

    with pytest.raises(RuntimeError, match="tool failure"):
        asyncio.run(run())
    assert backend.events == ["start", "execute:phys_opt_design", "close"]
