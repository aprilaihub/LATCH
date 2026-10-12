from __future__ import annotations

import ast
from pathlib import Path


REPO = Path(__file__).resolve().parents[2]
EXTERNAL_LLM = REPO / "experiments" / "llm"
CORE = REPO / "optimizer_pipeline" / "core"


def imports_under(root: Path) -> tuple[str, ...]:
    names: list[str] = []
    for path in root.rglob("*.py"):
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                names.append(node.module)
    return tuple(names)


def test_external_llm_contracts_remain_provider_neutral_while_adapter_owns_provider():
    imports: list[str] = []
    for path in (
        EXTERNAL_LLM / "contracts.py",
        EXTERNAL_LLM / "loop.py",
        EXTERNAL_LLM / "validation.py",
    ):
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imports.append(node.module)
    forbidden = (
        "experiments.llm.runtime",
        "experiments.llm.prompts",
        "optimizer_pipeline.pricing",
        "dcp_optimizer",
    )
    assert [
        name for name in imports
        if any(name == prefix or name.startswith(prefix + ".") for prefix in forbidden)
    ] == []
    provider_adapter = (REPO / "integrations/llm/intervention_generator.py").read_text()
    assert "integrations.llm.runtime" in provider_adapter
    compatibility = (EXTERNAL_LLM / "intervention_generator.py").read_text()
    assert "integrations.llm.intervention_generator" in compatibility


def test_core_never_imports_external_experiment_adapter():
    assert [
        name for name in imports_under(CORE)
        if name == "experiments" or name.startswith("experiments.")
    ] == []
