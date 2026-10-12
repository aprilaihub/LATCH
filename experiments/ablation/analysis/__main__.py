"""Redraw Matt's round-6 paper figures from external processed data."""
import argparse
import os
from pathlib import Path
import runpy


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-root", type=Path, required=True, help="Contains data/runs_enriched.csv and data/nodes.csv")
    parser.add_argument("--output", type=Path, required=True, help="New directory outside the input bundle")
    args = parser.parse_args()
    source, output = args.input_root.resolve(strict=True), args.output.resolve()
    if output == source or source in output.parents or output in source.parents:
        parser.error("Input and output trees must be separate")
    if output.exists():
        parser.error("Output already exists; previous figures are not overwritten")
    for name in ("runs_enriched.csv", "nodes.csv"):
        if not (source / "data" / name).is_file():
            parser.error(f"Missing processed input: data/{name}")
    try:
        import matplotlib, pandas, numpy  # noqa: F401
    except ImportError as exc:
        parser.error(f"Install the analysis extra before drawing: {exc}")
    os.environ["PAPER_ANALYSIS_ROOT"] = str(source)
    os.environ["LATCH_FIGURE_OUTPUT"] = str(output)
    runpy.run_path(str(Path(__file__).with_name("fig_round6.py")), run_name="__main__")


if __name__ == "__main__":
    main()
