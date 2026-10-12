from __future__ import annotations

import ast
from pathlib import Path


REPO = Path(__file__).resolve().parents[2]
CORE = REPO / "optimizer_pipeline" / "core"


def test_core_does_not_import_apps_experiments_providers_or_root_entry():
    forbidden = (
        "dcp_optimizer",
        "optimizer_pipeline.apps",
        "optimizer_pipeline.backends",
        "experiments.llm.runtime",
        "experiments.llm.prompts",
        "optimizer_pipeline.validators",
        "optimizer_pipeline.validation_log_parser",
        "experiments",
        "RapidWrightMCP",
        "VivadoMCP",
    )
    violations: list[tuple[str, str]] = []
    for path in CORE.rglob("*.py"):
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                names.append(node.module)
            for name in names:
                if any(name == prefix or name.startswith(prefix + ".") for prefix in forbidden):
                    violations.append((str(path.relative_to(REPO)), name))
    assert violations == []


def test_core_domain_is_not_a_legacy_reexport_surface():
    source = "\n".join(path.read_text() for path in CORE.rglob("*.py"))
    for legacy_name in ("OptimizationRunContext", "DcpTransitionRecord", "TransitionJournal"):
        assert legacy_name not in source
