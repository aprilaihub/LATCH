from __future__ import annotations

import ast
from pathlib import Path


REPO = Path(__file__).resolve().parents[2]
CONTRACT = REPO / "optimizer_pipeline" / "capabilities" / "intervention" / "contracts.py"


def test_intervention_contract_has_no_provider_root_or_toolchain_dependency():
    tree = ast.parse(CONTRACT.read_text(), filename=str(CONTRACT))
    imports: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.append(node.module)
    forbidden = (
        "dcp_optimizer",
        "experiments.llm.runtime",
        "experiments.llm.prompts",
        "optimizer_pipeline.execution",
        "VivadoMCP",
        "RapidWrightMCP",
    )
    assert [
        name for name in imports
        if any(name == prefix or name.startswith(prefix + ".") for prefix in forbidden)
    ] == []
