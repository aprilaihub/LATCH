#!/usr/bin/env python3
"""Rebuild a non-canonical workspace.db catalog from per-run run.db files."""

from __future__ import annotations

import argparse
from pathlib import Path
import sys

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from optimizer_pipeline.core.store import rebuild_workspace_catalog


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("roots", nargs="+", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    count = rebuild_workspace_catalog(args.output, args.roots)
    print(f"cataloged {count} run(s) in {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
