#!/usr/bin/env python3
"""Run one RapidWright census in an isolated, single-DCP process."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))
from scripts.run_wp014c_admission import rapidwright_net_census


def main() -> int:
    parser = argparse.ArgumentParser(allow_abbrev=False)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--limit", type=int, default=64)
    args = parser.parse_args()
    result = rapidwright_net_census(args.root, limit=args.limit)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    temporary = args.output.with_suffix(args.output.suffix + ".tmp")
    temporary.write_text(json.dumps(result, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, args.output)
    # JPype's embedded JVM can keep an otherwise completed orchestrator alive.
    # Each helper owns exactly one JVM, so explicit shutdown is safe and makes
    # lifecycle completion observable to the parent process.
    import jpype

    if jpype.isJVMStarted():
        jpype.shutdownJVM()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
