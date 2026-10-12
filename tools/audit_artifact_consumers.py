#!/usr/bin/env python3
"""Inventory legacy runtime artifact producers, consumers, and assertions."""

from __future__ import annotations

import argparse
from collections import Counter
from pathlib import Path
import re

from inventory_common import classify, markdown_table, relative, tracked_files


ARTIFACT_FAMILIES: dict[str, tuple[str, ...]] = {
    "transition journal": ("transition_journal.jsonl", "transition_journal_summary.json"),
    "state graph": ("state_graph.json", "root_metrics_resolved.json"),
    "pipeline summary": ("pipeline_summary.json", "results_summary.json", "fixed_sequence_summary.json"),
    "runtime projection": ("pipeline_state.json", "node_state.json", "best_node.json", "failures.json", "graph_stats.json", "budget_curve.json"),
    "execution result": ("run_summary.json", "executor_run_summary.json", "dcp_transition_result", "qor_score.json", "validation_report.json"),
    "policy candidates": ("policy_candidates.jsonl", "policy_candidates_summary.json", "candidate_priors.jsonl"),
    "contest publication": ("official_contest_trace.jsonl", "official_qor_trace.jsonl", "best_history.jsonl", "published_best.json"),
    "analysis records": (
        "analysis_runs.jsonl",
        "analysis_events.jsonl",
        "analysis_feedback.jsonl",
        "analysis_cost_summary.json",
        "analysis_context.json",
        "policy_events.json",
    ),
    "rescue records": (
        "rescue_decision.json",
        "manual_rescue_decision.json",
        "rescue_attempt_",
        "rescue_evidence_",
        "rescue_synthesis_audit",
        "manual_rescue_plan_",
        "manual_action_",
        "manual_episode_",
    ),
}
TEXT_SUFFIXES = {".py", ".sh", ".md", ".txt", ".json", ".yaml", ".yml"}
PRODUCER_HINT = re.compile(r"write|append|dump|open\([^\n]*(?:['\"]w|['\"]a)|touch|copy", re.IGNORECASE)
CONSUMER_HINT = re.compile(r"read|load|glob|rglob|exists|is_file|open\(", re.IGNORECASE)
ASSERTION_HINT = re.compile(r"assert|test_|fixture|expected", re.IGNORECASE)


def roles_for_line(line: str, lifecycle: str) -> set[str]:
    if lifecycle == "archive":
        return {"archive-reference"}
    if lifecycle == "documentation":
        return {"documentation"}
    roles: set[str] = set()
    if PRODUCER_HINT.search(line):
        roles.add("producer")
    if CONSUMER_HINT.search(line):
        roles.add("consumer")
    if lifecycle == "test" or ASSERTION_HINT.search(line):
        roles.add("assertion")
    return roles or {"reference"}


def inventory(repo: Path) -> list[dict[str, str]]:
    records: dict[str, dict[str, object]] = {}
    own_reports = {
        "tools/audit_artifact_consumers.py",
    }
    for path in tracked_files(repo):
        rel = relative(path, repo)
        if rel in own_reports or (path.suffix not in TEXT_SUFFIXES and path.name != "Makefile"):
            continue
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        lifecycle, owner, target = classify(rel)
        families: set[str] = set()
        roles: set[str] = set()
        references = 0
        for index, line in enumerate(lines):
            matched = {family for family, names in ARTIFACT_FAMILIES.items() if any(name in line for name in names)}
            if not matched:
                continue
            families.update(matched)
            context = "\n".join(lines[max(0, index - 2) : index + 3])
            roles.update(roles_for_line(context, lifecycle))
            references += 1
        if families:
            records[rel] = {
                "path": rel,
                "families": families,
                "roles": roles,
                "references": references,
                "class": lifecycle,
                "owner": owner,
                "migration_target": target,
            }
    rows: list[dict[str, str]] = []
    for rel, record in records.items():
        rows.append(
            {
                "path": rel,
                "families": ", ".join(sorted(record["families"])),
                "roles": ", ".join(sorted(record["roles"])),
                "references": str(record["references"]),
                "class": str(record["class"]),
                "owner": str(record["owner"]),
                "migration_target": str(record["migration_target"]),
            }
        )
    return sorted(rows, key=lambda row: (row["class"], row["path"]))


def render(rows: list[dict[str, str]]) -> str:
    class_counts = Counter(row["class"] for row in rows)
    family_counts: Counter[str] = Counter()
    role_counts: Counter[str] = Counter()
    for row in rows:
        family_counts.update(row["families"].split(", "))
        role_counts.update(row["roles"].split(", "))
    table_rows = [
        [row["path"], row["families"], row["roles"], row["references"], row["class"], row["migration_target"]]
        for row in rows
    ]
    return "\n".join(
        [
            "# Legacy Artifact Producer and Consumer Inventory",
            "",
            "Generated by `tools/audit_artifact_consumers.py`. Every listed active producer or consumer is part of the atomic AWP-40 cut; test assertions migrate with their behavior, experimental consumers migrate to DB queries/exports, and archive references stay inert.",
            "",
            f"Files by class: {', '.join(f'{key}={class_counts[key]}' for key in sorted(class_counts))}.",
            f"Files by artifact family: {', '.join(f'{key}={family_counts[key]}' for key in sorted(family_counts))}.",
            f"Files by detected role: {', '.join(f'{key}={role_counts[key]}' for key in sorted(role_counts))}.",
            "",
            markdown_table(["Path", "Artifact families", "Detected roles", "Refs", "Class", "Migration target"], table_rows),
            "",
            "## Hardcut rule",
            "",
            "The inventory is a migration gate, not a compatibility promise. AWP-40 must switch active and in-repo experimental consumers in the same merge unit that disables producers. The runtime must not dual-write. Historical archive references are not rewritten unless they are found to execute in an active path.",
            "",
        ]
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--output", type=Path, help="Write Markdown to this path instead of stdout.")
    args = parser.parse_args()
    report = render(inventory(args.repo.resolve()))
    if args.output:
        args.output.write_text(report, encoding="utf-8")
    else:
        print(report, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
