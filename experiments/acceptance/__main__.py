"""Print a checklist by default. Tool and paid phases need separate approval."""
import argparse
import json
from pathlib import Path
from .runner import AcceptanceBatch


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--phase", choices=["checklist", "tools", "paid", "report"], default="checklist")
    parser.add_argument("--authorized-tools", action="store_true", help="Use only after separate user tool authorization")
    parser.add_argument("--authorized-paid", action="store_true", help="Use only after separate user paid-run authorization")
    parser.add_argument("--price-evidence", type=Path, help="Fresh official cost JSON; may refresh evidence, never change approved rates/limits")
    args = parser.parse_args()
    batch = None
    try:
        if args.phase == "tools" and not args.authorized_tools:
            raise ValueError("Separate no-model tool authorization is required")
        if args.phase == "paid" and not (args.authorized_tools and args.authorized_paid):
            raise ValueError("Separate paid authorization is required after no-model acceptance")
        batch = AcceptanceBatch(json.loads(args.config.read_text()), args.config)
        if args.phase in {"checklist", "report"}:
            print(json.dumps(batch.checklist() if args.phase == "checklist" else batch.report(), indent=2))
        else:
            batch.run(args.phase, args.price_evidence)
            print(json.dumps(batch.report(), indent=2))
    except (ValueError, RuntimeError, OSError, KeyError, ImportError) as exc:
        if batch and args.phase in {"tools", "paid"}:
            print(json.dumps(batch.report(), indent=2))
        parser.exit(2, "Acceptance stopped: " + type(exc).__name__ + "; inspect restricted evidence, no retry.\n")


if __name__ == "__main__":
    main()
