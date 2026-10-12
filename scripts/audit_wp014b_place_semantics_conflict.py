#!/usr/bin/env python3
"""Audit the duplicated official UG835 place_design surfaces against Vivado help."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
from typing import Any


SCHEMA = "dof.wp014b-place-semantics-conflict.v1"
INSTALLED_VALUE = re.compile(r"^\s*\*\s+([A-Za-z][A-Za-z0-9_+.-]*)\b", re.MULTILINE)
OFFICIAL_VALUE = re.compile(
    r"- listitem \[ref=[^\]]+\]:\n\s+- code \[ref=[^\]]+\]: "
    r"([A-Za-z][A-Za-z0-9_+.-]*)"
)
ARTICLE_START = re.compile(r"^\s*- article \[ref=[^\]]+\]:", re.MULTILINE)


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _installed_directives(text: str) -> list[str]:
    block = text.split("Supported values include:", 1)[1].split("Important:", 1)[0]
    return sorted(set(INSTALLED_VALUE.findall(block)))


def _official_variants(snapshot: str) -> list[dict[str, Any]]:
    starts = [match.start() for match in ARTICLE_START.finditer(snapshot)]
    starts.append(len(snapshot))
    variants = []
    for begin, end in zip(starts, starts[1:]):
        article = snapshot[begin:end]
        if 'heading "place_design"' not in article or "Supported values include:" not in article:
            continue
        block = article.split("Supported values include:", 1)[1].split('generic [ref=', 1)[0]
        # The first generic after the value list is the Important callout.  If
        # the snapshot renderer changes indentation, fall back to the whole
        # article while retaining only list-item code tokens.
        values = sorted(set(OFFICIAL_VALUE.findall(block)))
        if not values:
            important = article.find('"Important:"')
            bounded = article[:important] if important >= 0 else article
            values = sorted(set(OFFICIAL_VALUE.findall(bounded.split("Supported values include:", 1)[1])))
        toc = re.search(r"tocId%3D([A-Za-z0-9_-]+)", article)
        variants.append(
            {
                "toc_id": toc.group(1) if toc else None,
                "directives": values,
                "article_sha256": hashlib.sha256(article.encode()).hexdigest(),
            }
        )
    return variants


def audit(installed_help: Path, official_snapshot: Path) -> dict[str, Any]:
    installed_text = installed_help.read_text(errors="replace")
    snapshot = official_snapshot.read_text(errors="replace")
    installed = _installed_directives(installed_text)
    variants = _official_variants(snapshot)
    metadata = {
        "document_id_ug835_present": "Document ID: UG835" in snapshot,
        "version_2025_1_english_present": "2025.1 English" in snapshot,
        "release_date_2025_06_16_present": "2025-06-16" in snapshot,
        "source_url": next(
            (line.removeprefix("- Page URL: ").strip() for line in snapshot.splitlines() if line.startswith("- Page URL: ")),
            None,
        ),
    }
    distinct = {tuple(row["directives"]) for row in variants}
    official_internal_conflict = len(distinct) > 1
    installed_matches = [row["toc_id"] for row in variants if row["directives"] == installed]
    materially_different = [
        row for row in variants if set(row["directives"]) != set(installed)
    ]
    return {
        "schema_version": SCHEMA,
        "installed_help_path": str(installed_help),
        "installed_help_sha256": digest(installed_help),
        "official_snapshot_path": str(official_snapshot),
        "official_snapshot_sha256": digest(official_snapshot),
        "official_metadata": metadata,
        "installed_directives": installed,
        "official_variants": variants,
        "official_internal_conflict": official_internal_conflict,
        "installed_matching_official_toc_ids": installed_matches,
        "materially_different_official_variants": materially_different,
        "conflict_changes_action_vocabulary": bool(materially_different),
        "stop_condition": "OFFICIAL_2025_1_TOPIC_IDENTITY_REQUIRED_BEFORE_REAL_ADMISSION",
        "requires_protocol_owner_alignment": bool(
            metadata["document_id_ug835_present"]
            and metadata["version_2025_1_english_present"]
            and official_internal_conflict
            and materially_different
        ),
        "real_dcp_admission_started": False,
        "implementation_heavy_calls": 0,
    }


def main() -> int:
    parser = argparse.ArgumentParser(allow_abbrev=False)
    parser.add_argument("--installed-help", type=Path, required=True)
    parser.add_argument("--official-snapshot", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    value = audit(args.installed_help, args.official_snapshot)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(value, sort_keys=True, indent=2) + "\n")
    print(json.dumps({
        "installed_directives": value["installed_directives"],
        "official_variant_count": len(value["official_variants"]),
        "requires_protocol_owner_alignment": value["requires_protocol_owner_alignment"],
    }, sort_keys=True))
    return 2 if value["requires_protocol_owner_alignment"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
