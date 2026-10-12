from __future__ import annotations

from pathlib import Path
import re

from optimizer_pipeline.apps.fpl_contest.args import build_contest_parser


REPO = Path(__file__).resolve().parents[2]


def test_cli_reference_covers_every_active_contest_option():
    reference = (REPO / "docs" / "reference" / "cli-reference.md").read_text()
    options = {
        action.option_strings[0]
        for action in build_contest_parser()._actions
        if action.option_strings and action.option_strings[0] != "-h"
    }
    missing = sorted(option for option in options if f"`{option}`" not in reference)
    assert missing == []


def test_makefile_does_not_duplicate_contest_parser_defaults():
    makefile = (REPO / "Makefile").read_text()
    delegated = (
        "RUN_MODE",
        "PIPELINE",
        "POLICY",
        "ANALYSIS",
        "LLM_PROVIDER",
        "BUDGET_RUNS",
        "SINGLE_PATH_DEPTH",
        "PARALLELISM",
        "KNOWLEDGE_MODE",
        "RESCUE_MODE",
        "RESCUE_TRIGGER",
        "RESCUE_MAX_RUNS",
    )
    for variable in delegated:
        assert re.search(rf"^{variable}\s*\?=\s*$", makefile, re.MULTILINE)


def test_glossary_names_stable_business_concepts():
    glossary = (REPO / "docs" / "reference" / "glossary.md").read_text()
    for term in (
        "Run",
        "Execution Scope",
        "Design State",
        "Action Specification",
        "Attempt",
        "Execution Outcome",
        "Validation Record",
        "Artifact Reference",
        "Budget Event",
        "Result Selection",
    ):
        assert f"| {term} |" in glossary

