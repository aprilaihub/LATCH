from __future__ import annotations

import importlib.util
from pathlib import Path
import sys


REPO = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "audit_wp013_e0_processes", REPO / "scripts" / "audit_wp013_e0_processes.py"
)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


def test_scan_ignores_its_own_auditor_process(tmp_path: Path) -> None:
    result = MODULE.scan(tmp_path / "not-a-live-run", 1)
    assert result["audit_index"] == 1
    assert result["exact_run_live_count"] == 0
    assert result["audit_sha256"]


def test_global_markers_cover_all_wp013_backend_processes() -> None:
    assert "run_flow_matrix.py" in MODULE.GLOBAL_EXECUTABLE_MARKERS
    assert "dcp_optimizer.py" in MODULE.GLOBAL_EXECUTABLE_MARKERS
    assert "vivado_mutation_worker.py" in MODULE.GLOBAL_EXECUTABLE_MARKERS
