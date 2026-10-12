"""Backend-contract adapter over the behavior-preserved Xilinx flow host."""

from __future__ import annotations

import inspect
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable

from optimizer_pipeline.backends.base import (
    ActionCatalog,
    ActionDefinition,
    AnalysisProbeRequest,
    AnalysisProbeResult,
    BackendExecutionRequest,
    BackendExecutionResult,
)
from optimizer_pipeline.core.domain import OutcomeStatus

from .legacy import FPGAOptimizerTest, XILINX_FLOW_NAMES, run_test_mode


RunFlow = Callable[..., Awaitable[Any]]


def xilinx_action_catalog() -> ActionCatalog:
    """Expose existing flow names without translating or renaming parameters."""

    return ActionCatalog(
        tuple(
            ActionDefinition(
                name,
                f"Behavior-preserved Xilinx flow: {name}",
                allow_extra_parameters=True,
            )
            for name in XILINX_FLOW_NAMES
        )
    )


class XilinxBackend:
    name = "xilinx_vivado"
    catalog = xilinx_action_catalog()

    def __init__(
        self,
        *,
        debug: bool = False,
        max_nets: int = 5,
        run_flow: RunFlow = run_test_mode,
        tester_cls: type = FPGAOptimizerTest,
    ) -> None:
        self.debug = debug
        self.max_nets = max_nets
        self.run_flow = run_flow
        self.tester_cls = tester_cls
        self._started = False

    async def start(self) -> None:
        if self._started:
            raise RuntimeError("Xilinx backend is already started")
        self._started = True

    async def close(self) -> None:
        self._started = False

    async def execute(self, request: BackendExecutionRequest) -> BackendExecutionResult:
        self._require_started()
        self.catalog.validate(request.action, require_managed_safe=True)
        started = datetime.now(timezone.utc)
        workspace = Path(request.workspace_uri)
        workspace.mkdir(parents=True, exist_ok=True)
        result = await self.run_flow(
            Path(request.input_artifact.uri),
            Path(request.output_uri),
            debug=self.debug,
            max_nets=self.max_nets,
            run_dir=workspace,
            flow=request.action.action_type,
            optimizer_params=request.action.parameters.to_dict(),
        )
        finished = datetime.now(timezone.utc)
        exit_code = _exit_code(result)
        metrics = _read_json(workspace / "run_summary.json")
        metrics.setdefault("exit_code", exit_code)
        return BackendExecutionResult(
            OutcomeStatus.SUCCEEDED if exit_code == 0 else OutcomeStatus.FAILED,
            started,
            finished,
            request.output_uri if exit_code == 0 else None,
            metrics,
            tuple(
                str(path)
                for path in (workspace / "vivado.log", workspace / "rapidwright.log")
                if path.exists()
            ),
            None if exit_code == 0 else "XilinxFlowFailed",
            None if exit_code == 0 else f"flow returned exit code {exit_code}",
        )

    async def probe(self, request: AnalysisProbeRequest) -> AnalysisProbeResult:
        self._require_started()
        workspace = Path(request.workspace_uri)
        tester = self.tester_cls(debug=self.debug, run_dir=workspace)
        try:
            await _maybe_await(tester.start_servers())
            evidence = await _maybe_await(
                tester.analyze_design_for_flow_selection(Path(request.artifact.uri))
            )
        finally:
            await _maybe_await(tester.cleanup())
        payload = dict(evidence) if isinstance(evidence, dict) else {}
        payload["requested_probes"] = [probe.value for probe in request.probes]
        return AnalysisProbeResult(request.artifact.artifact_id, payload)

    def _require_started(self) -> None:
        if not self._started:
            raise RuntimeError("Xilinx backend must be started before use")


def _exit_code(value: Any) -> int:
    if isinstance(value, bool):
        return 0 if value else 1
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0 if value is None else 1


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


async def _maybe_await(value: Any) -> Any:
    return await value if inspect.isawaitable(value) else value
