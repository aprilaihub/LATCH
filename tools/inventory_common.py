"""Shared deterministic classification for refactoring inventory tools."""

from __future__ import annotations

from pathlib import Path
import subprocess


def tracked_files(repo: Path) -> list[Path]:
    result = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        cwd=repo,
        check=True,
        stdout=subprocess.PIPE,
    )
    return [repo / item.decode() for item in result.stdout.split(b"\0") if item]


def relative(path: Path, repo: Path) -> str:
    return path.relative_to(repo).as_posix()


def classify(path: str) -> tuple[str, str, str]:
    """Return lifecycle class, current owner, and hardcut migration target."""
    if path.startswith("docs/archive/"):
        return "archive", "historical documentation", "remain archived; never imported by runtime"
    if path.startswith("shared/matt_experiment_data/"):
        return "archive", "shared historical evidence", "remain archived; never imported by runtime"
    if path.startswith("tests/"):
        return "test", "test suite", "migrate with the behavior under test"
    if path.startswith("experiments/promptsearch/"):
        return "experimental", "prompt-search experiment", "experiments/llm/promptsearch"
    if path.startswith("experiments/"):
        return "experimental", "experiment", "experiments"
    if path.startswith("examples/"):
        return "experimental", "example/experiment", "experiments/examples using app CLIs"
    if path.startswith("docs/"):
        return "documentation", "documentation", "active platform documentation or archive"
    if path.startswith("tools/"):
        return "active", "architecture tooling", "tools/audit"
    if path == "dcp_optimizer.py":
        return "active", "mixed root application/backend", "thin forwarder to apps"
    if path == "validate_dcps.py":
        return "active", "functional validator application", "core validation adapter + certification app"
    if path == "Makefile":
        return "active", "developer command surface", "independent app entry points"
    if path.startswith(("VivadoMCP/", "RapidWrightMCP/")):
        return "active", "Xilinx tool service", "backends/xilinx services"
    if path.startswith("scripts/"):
        return "active", "managed/internal command surface", "apps, tools, or experiments by command purpose"
    if path.startswith("optimizer_pipeline/apps/fpl_contest/"):
        return "active", "FPL contest application", "apps/fpl_contest"
    if path.startswith("optimizer_pipeline/capabilities/search/"):
        return "active", "search capability", "capabilities/search"
    if path.startswith("optimizer_pipeline/capabilities/intervention/"):
        return "active", "intervention capability", "capabilities/intervention"
    if path.startswith(("optimizer_pipeline/llm/", "optimizer_pipeline/prompts/")):
        return "active", "core-owned LLM integration", "experiments/llm"
    if path.startswith("optimizer_pipeline/real_analysis_provider"):
        return "active", "Xilinx analysis probe", "backends/xilinx analysis"
    if path.startswith("optimizer_pipeline/capabilities/analysis/"):
        return "active", "analysis capability", "capabilities/analysis"
    if path.startswith(("optimizer_pipeline/storage/", "optimizer_pipeline/run/", "optimizer_pipeline/domain/")):
        return "active", "legacy runtime/domain/storage", "core domain/runtime/store"
    if path.startswith(("optimizer_pipeline/execution/", "optimizer_pipeline/executor")):
        return "active", "mixed execution/backend", "core runtime + backends/xilinx"
    if path.startswith("optimizer_pipeline/"):
        return "active", "mixed optimizer package", "core, backend, or capability by responsibility"
    return "active", "repository support", "review during package convergence"


def markdown_table(headers: list[str], rows: list[list[str]]) -> str:
    def clean(value: str) -> str:
        return value.replace("|", "\\|").replace("\n", "<br>")

    rendered = ["| " + " | ".join(headers) + " |", "| " + " | ".join("---" for _ in headers) + " |"]
    rendered.extend("| " + " | ".join(clean(value) for value in row) + " |" for row in rows)
    return "\n".join(rendered)
