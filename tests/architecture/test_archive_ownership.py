from __future__ import annotations

import ast
import importlib
from pathlib import Path


REPO = Path(__file__).resolve().parents[2]


def test_autonomous_optimizer_is_not_in_active_root_module():
    module = importlib.import_module("dcp_optimizer")
    assert not hasattr(module, "DCPOptimizer")
    assert not hasattr(module, "DCPOptimizerBase")
    assert not hasattr(module, "FPGAOptimizerTest")


def test_active_python_does_not_import_archive():
    violations: list[str] = []
    roots = [REPO / "optimizer_pipeline", REPO / "experiments", REPO / "scripts", REPO / "examples"]
    for root in roots:
        for path in root.rglob("*.py"):
            if "refactor_stage0_golden" in path.parts:
                continue
            tree = ast.parse(path.read_text(), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import) and any(alias.name == "archive" or alias.name.startswith("archive.") for alias in node.names):
                    violations.append(str(path.relative_to(REPO)))
                if isinstance(node, ast.ImportFrom) and node.module and (node.module == "archive" or node.module.startswith("archive.")):
                    violations.append(str(path.relative_to(REPO)))
    assert violations == []


def test_retired_agent_does_not_supply_a_parallel_runtime_owner():
    # Historical documentation is external evidence, not a runtime dependency.
    assert not (REPO / "archive" / "autonomous_dcp_optimizer.py").exists()
    assert not (REPO / "archive" / "__init__.py").exists()
