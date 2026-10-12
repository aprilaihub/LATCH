from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from optimizer_pipeline.backends import (
    AnalysisProbeKind,
    AnalysisProbeRequest,
    Backend,
    BackendExecutionRequest,
    backend_session,
)
from optimizer_pipeline.backends.xilinx import (
    REPO_ROOT,
    XILINX_FLOW_NAMES,
    XilinxBackend,
    xilinx_action_catalog,
)
from optimizer_pipeline.backends.xilinx.legacy import run_test_mode
from optimizer_pipeline.core.domain import (
    ActionSpecification,
    ArtifactKind,
    ArtifactReference,
    Attempt,
    OutcomeStatus,
)


def request(tmp_path: Path) -> BackendExecutionRequest:
    action = ActionSpecification(
        "action-1", "run-1", "scope-1", "phys_opt", {"directive": "Explore"},
    )
    attempt = Attempt(
        "attempt-1", "run-1", "scope-1", "state-root", action.action_id,
    )
    artifact = ArtifactReference(
        "artifact-1", "run-1", "scope-1", ArtifactKind.DCP,
        str(tmp_path / "opaque-input.dcp"),
    )
    return BackendExecutionRequest(
        attempt,
        action,
        artifact,
        str(tmp_path / "output.dcp"),
        str(tmp_path / "workspace"),
    )


def test_xilinx_catalog_preserves_every_existing_flow_name_exactly():
    assert tuple(action.name for action in xilinx_action_catalog().actions) == XILINX_FLOW_NAMES
    assert "phys_opt" in XILINX_FLOW_NAMES
    assert "place_route" in XILINX_FLOW_NAMES
    assert "manual_action" in XILINX_FLOW_NAMES
    assert "synthesized_recipe" in XILINX_FLOW_NAMES


def test_xilinx_backend_executes_explicit_flow_without_filename_detection(tmp_path):
    calls = []

    async def fake_run(input_dcp, output_dcp, **kwargs):
        calls.append((input_dcp, output_dcp, kwargs))
        output_dcp.write_text("mock dcp")
        summary = Path(kwargs["run_dir"]) / "run_summary.json"
        summary.write_text(json.dumps({"final_wns_ns": 0.1}))
        return 0

    backend = XilinxBackend(run_flow=fake_run)
    assert isinstance(backend, Backend)

    async def run():
        async with backend_session(backend) as active:
            return await active.execute(request(tmp_path))

    result = asyncio.run(run())
    assert result.status is OutcomeStatus.SUCCEEDED
    assert result.metrics.get("final_wns_ns") == 0.1
    assert calls[0][2]["flow"] == "phys_opt"
    assert calls[0][0].name == "opaque-input.dcp"


def test_xilinx_backend_analysis_probe_owns_mcp_lifecycle(tmp_path):
    events = []

    class FakeTester:
        def __init__(self, *, debug, run_dir):
            events.append(("init", debug, Path(run_dir)))

        async def start_servers(self):
            events.append("start_servers")

        async def analyze_design_for_flow_selection(self, dcp):
            events.append(("analyze", Path(dcp)))
            return {"timing": {"wns": -0.2}}

        async def cleanup(self):
            events.append("cleanup")

    execution = request(tmp_path)
    probe = AnalysisProbeRequest(
        execution.input_artifact,
        (AnalysisProbeKind.TIMING_SUMMARY, AnalysisProbeKind.ROUTE_STATUS),
        str(tmp_path / "probe"),
    )
    backend = XilinxBackend(tester_cls=FakeTester)

    async def run():
        async with backend_session(backend) as active:
            return await active.probe(probe)

    result = asyncio.run(run())
    assert result.artifact_id == execution.input_artifact.artifact_id
    assert result.evidence.get("requested_probes") == ("TIMING_SUMMARY", "ROUTE_STATUS")
    assert events[-1] == "cleanup"


def test_moved_backend_resolves_mcp_servers_from_repository_root():
    assert REPO_ROOT == Path(__file__).resolve().parents[2]
    assert (REPO_ROOT / "RapidWrightMCP" / "server.py").is_file()
    assert (REPO_ROOT / "VivadoMCP" / "vivado_mcp_server.py").is_file()


def test_legacy_flow_host_rejects_missing_action_before_using_filename(tmp_path):
    with pytest.raises(ValueError, match="explicit Xilinx flow is required"):
        asyncio.run(run_test_mode(tmp_path / "logicnets.dcp", tmp_path / "output.dcp"))
