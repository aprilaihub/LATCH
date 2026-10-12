from __future__ import annotations

import ast
from pathlib import Path


REPO = Path(__file__).resolve().parents[2]
BACKEND_BASE = REPO / "optimizer_pipeline" / "backends" / "base.py"
DIRECT_ACTION = REPO / "optimizer_pipeline" / "apps" / "direct_action"


def imported_modules(path: Path) -> tuple[str, ...]:
    tree = ast.parse(path.read_text(), filename=str(path))
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.append(node.module)
    return tuple(names)


def test_backend_contract_is_independent_of_root_apps_and_xilinx_implementations():
    forbidden = (
        "dcp_optimizer",
        "optimizer_pipeline.apps",
        "optimizer_pipeline.capabilities",
        "optimizer_pipeline.executor",
        "optimizer_pipeline.execution",
        "optimizer_pipeline.validators",
        "VivadoMCP",
        "RapidWrightMCP",
    )
    imports = imported_modules(BACKEND_BASE)
    assert [
        name
        for name in imports
        if any(name == prefix or name.startswith(prefix + ".") for prefix in forbidden)
    ] == []


def test_direct_action_contract_does_not_import_root_or_legacy_runtime():
    imports = tuple(
        name
        for path in DIRECT_ACTION.rglob("*.py")
        for name in imported_modules(path)
    )
    forbidden = (
        "dcp_optimizer",
        "optimizer_pipeline.execution",
        "optimizer_pipeline.run",
        "optimizer_pipeline.storage",
    )
    assert [
        name
        for name in imports
        if any(name == prefix or name.startswith(prefix + ".") for prefix in forbidden)
    ] == []
