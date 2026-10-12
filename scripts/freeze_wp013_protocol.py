#!/usr/bin/env python3
"""Emit and verify the immutable WP-013 Gate-A protocol freeze."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from optimizer_pipeline.dof.single_submission.specs import (  # noqa: E402
    protocol_freeze_hash,
    protocol_freeze_payload,
)


def main() -> int:
    parser = argparse.ArgumentParser(allow_abbrev=False)
    parser.add_argument("--output", type=Path, default=REPO / "configs" / "wp013" / "protocol-freeze-v1.json")
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    payload = protocol_freeze_payload() | {"bundle_sha256": protocol_freeze_hash()}
    encoded = json.dumps(payload, sort_keys=True, indent=2, ensure_ascii=False) + "\n"
    if args.check:
        if not args.output.exists() or args.output.read_text() != encoded:
            print(f"protocol freeze mismatch: {args.output}", file=sys.stderr)
            return 1
        print(payload["bundle_sha256"])
        return 0
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(encoded)
    print(payload["bundle_sha256"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
