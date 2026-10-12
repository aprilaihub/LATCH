"""Redraw the historical WP015 diagnostic plots from a completed analysis.

This does not select or rebuild the curated manuscript panels.
"""
import argparse
import json
from pathlib import Path

from .wp015_analysis import figures


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True, help="Completed WP015 analysis directory")
    parser.add_argument("--output", type=Path, required=True, help="New, separate figure directory")
    args = parser.parse_args()
    source, output = args.input.resolve(strict=True), args.output.resolve()
    if source == output or source in output.parents or output in source.parents:
        parser.error("Input and output trees must be separate")
    if output.exists():
        parser.error("Output already exists")
    rows = [json.loads(line) for line in (source / "canonical-jobs.jsonl").read_text().splitlines() if line.strip()]
    tables = {path.stem: json.loads(path.read_text()) for path in (source / "tables").glob("*.json")}
    paths = figures(rows, tables, output)
    print(json.dumps({"figures": [str(path) for path in paths], "historical_diagnostic_plots": True}))


if __name__ == "__main__":
    main()
