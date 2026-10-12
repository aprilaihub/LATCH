from __future__ import annotations

import ast
from pathlib import Path


REPO = Path(__file__).resolve().parents[2]
ACTIVE_ROOTS = (
    REPO / "optimizer_pipeline",
    REPO / "scripts",
    REPO / "experiments",
    REPO / "examples",
)


def test_active_modules_do_not_import_root_dcp_optimizer():
    violations: list[str] = []
    for root in ACTIVE_ROOTS:
        for path in root.rglob("*.py"):
            tree = ast.parse(path.read_text(), filename=str(path))
            for node in ast.walk(tree):
                names: list[str] = []
                if isinstance(node, ast.Import):
                    names.extend(alias.name for alias in node.names)
                elif isinstance(node, ast.ImportFrom) and node.module:
                    names.append(node.module)
                if "dcp_optimizer" in names:
                    violations.append(str(path.relative_to(REPO)))
    assert violations == []


def test_xilinx_backend_owns_mcp_lifecycle_and_flow_dispatch():
    source = (REPO / "optimizer_pipeline" / "backends" / "xilinx" / "legacy.py").read_text()
    assert "class DCPOptimizerBase" in source
    assert "class FPGAOptimizerTest" in source
    assert "async def run_test_mode" in source
    assert "RapidWrightMCP" in source
    assert "VivadoMCP" in source
    assert "async def main" not in source


def test_root_entry_is_a_forwarder_not_the_xilinx_implementation():
    source = (REPO / "dcp_optimizer.py").read_text()
    assert "from optimizer_pipeline.apps.fpl_contest.cli import" in source
    assert "optimizer_pipeline.backends" not in source
    assert "class DCPOptimizerBase" not in source
    assert "class FPGAOptimizerTest" not in source
    assert len(source.splitlines()) < 120
