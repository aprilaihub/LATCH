"""Run the bounded Matt extraction and ordered derived-data pipeline."""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys
from typing import Callable


PACKAGE_ROOT = Path(__file__).resolve().parent
SCRIPT_ROOT = PACKAGE_ROOT / "scripts"
STAGES = (
    ("extract", "extract.py"),
    ("firstpass", "firstpass.py"),
    ("mech", "mech.py"),
    ("verify", "verify.py"),
    ("coverage", "coverage.py"),
    ("prep", "prep.py"),
)
REQUIRED_EXTRACTION_TABLES = (
    "runs.csv",
    "nodes.csv",
    "loopdec.csv",
    "diagnoses.csv",
    "polcand.csv",
)
STAGE_OUTPUTS = {
    "extract": REQUIRED_EXTRACTION_TABLES,
    "firstpass": ("matched_budget.csv", "runs_enriched.csv"),
    "mech": ("trajectory.csv", "sessions_mt.csv", "runs_enriched.csv"),
    "verify": ("analysis_sessions.csv", "session_followthrough.csv"),
    "coverage": ("runs_enriched.csv",),
    "prep": (
        "runs_enriched.csv",
        "trajectory_cf.csv",
        "tokens_cf.csv",
        "validation_cells.csv",
        "validation_levels.csv",
    ),
}
_REPEAT = re.compile(r"repeat_(\d+)")
PARTICIPANT_LABELS = ("cristian", "matt", "shu")
REFERENCE_TABLES = ("runs.csv", "runs_enriched.csv", "nodes.csv")


class ProcessingError(ValueError):
    """An explicit input, output, or stage contract was not met."""


def _absolute_directory(value: object, label: str) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise ProcessingError(f"config field {label!r} must be an absolute directory path")
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise ProcessingError(f"config field {label!r} must be absolute: {value!r}")
    path = path.resolve()
    if not path.is_dir():
        raise ProcessingError(f"required {label} directory does not exist: {path}")
    return path


def load_config(path: Path) -> dict[str, object]:
    try:
        config = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ProcessingError(f"cannot read JSON processing config {path}: {exc}") from exc
    if not isinstance(config, dict):
        raise ProcessingError("processing config must be a JSON object")
    participant_roots_value = config.get("participant_roots")
    if participant_roots_value is None:
        if config.get("participant") != "matt":
            raise ProcessingError(
                "provide participant_roots, or the legacy participant='matt' and raw_run_root fields"
            )
        raw_root = _absolute_directory(config.get("raw_run_root"), "raw_run_root")
        participant_roots = {"matt": raw_root}
    else:
        if not isinstance(participant_roots_value, dict) or not participant_roots_value:
            raise ProcessingError("config field 'participant_roots' must map participant labels to directories")
        unknown = sorted(set(participant_roots_value) - set(PARTICIPANT_LABELS))
        if unknown:
            raise ProcessingError("unknown participant label(s): " + ", ".join(unknown))
        participant_roots = {
            label: _absolute_directory(participant_roots_value[label], f"participant_roots.{label}")
            for label in PARTICIPANT_LABELS
            if label in participant_roots_value
        }
        if "matt" not in participant_roots:
            raise ProcessingError(
                "participant_roots must include 'matt'; mech.py mechanism analysis and verify.py V3 cost "
                "analysis scan Matt's tree"
            )
        if "participant" in config and config["participant"] != "matt":
            raise ProcessingError("the legacy participant field, when present, must be 'matt'")
        if config.get("raw_run_root") is not None:
            legacy_matt_root = _absolute_directory(config["raw_run_root"], "raw_run_root")
            if legacy_matt_root != participant_roots["matt"]:
                raise ProcessingError("raw_run_root conflicts with participant_roots.matt")

    s0_root = _absolute_directory(config.get("s0_sweep_root"), "s0_sweep_root")
    extras = config.get("s0_extra_roots", {})
    if not isinstance(extras, dict) or any(not isinstance(k, str) for k in extras):
        raise ProcessingError("config field 's0_extra_roots' must map benchmark names to directories")
    s0_extras = {
        name: str(_absolute_directory(value, f"s0_extra_roots.{name}"))
        for name, value in extras.items()
    }
    return {
        "participant_roots": participant_roots,
        "matt_raw_root": participant_roots["matt"],
        "s0_sweep_root": s0_root,
        "s0_extra_roots": s0_extras,
    }


def _run_directories(raw_root: Path) -> list[Path]:
    return [
        path
        for path in sorted(raw_root.glob("*/*/*/repeat_*"))
        if path.is_dir() and _REPEAT.fullmatch(path.name)
    ]


def validate_inputs(config: dict[str, object]) -> dict[str, object]:
    participant_roots = config["participant_roots"]
    s0_root = config["s0_sweep_root"]
    assert isinstance(participant_roots, dict) and isinstance(s0_root, Path)
    s0_extras = config["s0_extra_roots"]
    assert isinstance(s0_extras, dict)

    raw_runs_by_participant: dict[str, list[Path]] = {}
    completed_by_participant: dict[str, dict[str, list[Path]]] = {}
    optional_counts: dict[str, dict[str, int]] = {}
    for participant, raw_root in participant_roots.items():
        if not isinstance(raw_root, Path):
            raise ProcessingError(f"participant_roots.{participant} is not a resolved directory")
        raw_runs = _run_directories(raw_root)
        raw_runs_by_participant[participant] = raw_runs
        complete_by_level: dict[str, list[Path]] = {level: [] for level in ("S1", "S2", "S3")}
        for run_dir in raw_runs:
            parts = run_dir.relative_to(raw_root).parts
            if len(parts) == 4 and parts[1] in complete_by_level and (run_dir / "pipeline_summary.json").is_file():
                complete_by_level[parts[1]].append(run_dir)
        missing_levels = [level for level, paths in complete_by_level.items() if not paths]
        if missing_levels:
            raise ProcessingError(
                f"participant_roots.{participant} must contain at least one completed run "
                f"(with pipeline_summary.json) for each S1-S3 level; missing: {', '.join(missing_levels)}"
            )
        completed_by_participant[participant] = complete_by_level

        event_paths = [run / "analysis" / "analysis_events.jsonl" for run in raw_runs]
        diagnosis_paths = [run / "analysis" / "node_diagnoses.jsonl" for run in raw_runs]
        loop_paths = [run / "analysis" / "loop_decisions.jsonl" for run in raw_runs]
        policy_paths = [run / "policy_candidates.jsonl" for run in raw_runs]
        optional_counts[participant] = {
            "repeat_directories_without_pipeline_summary": sum(
                1 for run in raw_runs if not (run / "pipeline_summary.json").is_file()
            ),
            "runs_without_analysis_events": sum(1 for path in event_paths if not path.is_file()),
            "runs_without_node_diagnoses": sum(1 for path in diagnosis_paths if not path.is_file()),
            "runs_without_loop_decisions": sum(1 for path in loop_paths if not path.is_file()),
            "runs_without_policy_candidates": sum(1 for path in policy_paths if not path.is_file()),
        }

    s0_dirs = [
        path for path in s0_root.iterdir()
        if path.is_dir() and (path / "pipeline_summary.json").is_file()
    ]
    extra_dirs = {name: Path(value) for name, value in s0_extras.items()}
    if not s0_dirs and not extra_dirs:
        raise ProcessingError(
            "s0_sweep_root must contain at least one S0 benchmark run with pipeline_summary.json"
        )

    s0_benchmarks = {path.name for path in s0_dirs} | set(extra_dirs)
    all_benchmarks = {
        run.relative_to(participant_roots[participant]).parts[0]
        for participant, runs in raw_runs_by_participant.items()
        for run in runs
    }
    missing_s0_benchmarks = sorted(all_benchmarks - s0_benchmarks)
    return {
        "raw_run_count_by_participant": {participant: len(runs) for participant, runs in raw_runs_by_participant.items()},
        "completed_run_count_by_level": {
            participant: {level: len(paths) for level, paths in levels.items()}
            for participant, levels in completed_by_participant.items()
        },
        "participants_processed": list(participant_roots),
        "participants_not_configured": [label for label in PARTICIPANT_LABELS if label not in participant_roots],
        "s0_benchmark_count": len(s0_benchmarks),
        "benchmark_count": len(all_benchmarks),
        "benchmarks_without_s0": missing_s0_benchmarks,
        "optional_raw_artifact_counts_by_participant": optional_counts,
        "optional_outputs_absent": [],
    }


def _csv_rows(path: Path) -> list[dict[str, str]]:
    try:
        with path.open(newline="", encoding="utf-8") as stream:
            return list(csv.DictReader(stream))
    except (OSError, UnicodeError, csv.Error) as exc:
        raise ProcessingError(f"could not read processing CSV table {path}: {exc}") from exc


def _finite_numeric_values(rows: list[dict[str, str]], column: str) -> list[float]:
    values = []
    for row in rows:
        raw = row.get(column, "")
        try:
            value = float(raw)
        except (TypeError, ValueError):
            continue
        if math.isfinite(value):
            values.append(value)
    return values


def _run_totals(rows: list[dict[str, str]]) -> dict[str, object]:
    totals: dict[str, object] = {"num_runs": len(rows)}
    if not rows:
        return totals
    columns = set(rows[0])
    for column in ("cost_usd", "cost_used"):
        if column in columns:
            values = _finite_numeric_values(rows, column)
            totals[column] = {"count": len(values), "sum": sum(values)}
    fmax_column = next(
        (column for column in ("best_fmax_mhz", "final_fmax_mhz", "fmax_mhz") if column in columns),
        None,
    )
    if fmax_column:
        values = _finite_numeric_values(rows, fmax_column)
        totals["final_fmax_mhz"] = {
            "source_column": fmax_column,
            "count": len(values),
            "sum": sum(values),
            "mean": sum(values) / len(values) if values else None,
        }
    return totals


def _check_stage_outputs(data_dir: Path, stage: str) -> dict[str, object]:
    counts: dict[str, int] = {}
    table_rows: dict[str, list[dict[str, str]]] = {}
    for name in STAGE_OUTPUTS[stage]:
        path = data_dir / name
        if not path.is_file() or path.stat().st_size == 0:
            raise ProcessingError(f"stage {stage!r} did not produce its required output: {path}")
        rows = _csv_rows(path)
        if not rows:
            raise ProcessingError(f"stage {stage!r} produced an empty required table: {path}")
        table_rows[name] = rows
        counts[name] = len(rows)
    enriched_path = data_dir / "runs_enriched.csv"
    if enriched_path.is_file():
        run_rows = _csv_rows(enriched_path)
    elif "runs.csv" in table_rows:
        run_rows = table_rows["runs.csv"]
    else:
        run_rows = []
    return {
        "row_counts": counts,
        "key_totals": _run_totals(run_rows) if run_rows else None,
    }


def _check_extracted_scope(data_dir: Path) -> None:
    rows = _csv_rows(data_dir / "runs.csv")
    levels = {row.get("level") for row in rows}
    missing = [level for level in ("S0", "S1", "S2", "S3") if level not in levels]
    if missing:
        raise ProcessingError(
            "extraction completed but produced no rows for required study levels: " + ", ".join(missing)
        )


def _execute_script(script: Path, env: dict[str, str]) -> None:
    completed = subprocess.run([sys.executable, str(script)], env=env, check=False)
    if completed.returncode:
        raise ProcessingError(
            f"processing stage {script.stem!r} failed with exit code {completed.returncode}"
        )


def _indexed_rows(rows: list[dict[str, str]], columns: list[str]) -> tuple[dict[tuple[str, ...], dict[str, str]], int]:
    indexed: dict[tuple[str, ...], dict[str, str]] = {}
    duplicates = 0
    for row in rows:
        key = tuple(row.get(column, "") for column in columns)
        if key in indexed:
            duplicates += 1
            continue
        indexed[key] = row
    return indexed, duplicates


def _reference_key_columns(output_rows: list[dict[str, str]], reference_rows: list[dict[str, str]], table: str) -> list[str]:
    if not output_rows or not reference_rows:
        return []
    common = set(output_rows[0]) & set(reference_rows[0])
    participant_column = "source" if "source" in common else "participant" if "participant" in common else None
    required = ["benchmark", "level", "model", "repeat"]
    if table == "nodes.csv":
        required.append("node_id")
    if participant_column:
        required.insert(0, participant_column)
    return required if all(column in common for column in required) else []


def _compare_reference_table(output_path: Path, reference_path: Path) -> dict[str, object]:
    output_rows = _csv_rows(output_path)
    reference_rows = _csv_rows(reference_path)
    result: dict[str, object] = {
        "output_row_count": len(output_rows),
        "reference_row_count": len(reference_rows),
        "row_count_delta": len(output_rows) - len(reference_rows),
    }
    key_columns = _reference_key_columns(output_rows, reference_rows, output_path.name)
    if not key_columns:
        result["key_comparison"] = "not_comparable_missing_key_columns"
        return result
    output_index, output_duplicates = _indexed_rows(output_rows, key_columns)
    reference_index, reference_duplicates = _indexed_rows(reference_rows, key_columns)
    shared = output_index.keys() & reference_index.keys()
    result.update(
        {
            "key_columns": key_columns,
            "shared_key_count": len(shared),
            "output_only_key_count": len(output_index.keys() - reference_index.keys()),
            "reference_only_key_count": len(reference_index.keys() - output_index.keys()),
            "duplicate_output_key_count": output_duplicates,
            "duplicate_reference_key_count": reference_duplicates,
        }
    )
    common_columns = (set(output_rows[0]) & set(reference_rows[0])) - set(key_columns)
    numeric_comparisons: dict[str, object] = {}
    for column in sorted(common_columns):
        output_values = []
        reference_values = []
        absolute_deltas = []
        different = 0
        for key in shared:
            left = output_index[key].get(column, "")
            right = reference_index[key].get(column, "")
            try:
                left_value, right_value = float(left), float(right)
            except (TypeError, ValueError):
                continue
            if not (math.isfinite(left_value) and math.isfinite(right_value)):
                continue
            output_values.append(left_value)
            reference_values.append(right_value)
            delta = abs(left_value - right_value)
            absolute_deltas.append(delta)
            if not math.isclose(left_value, right_value, rel_tol=1e-9, abs_tol=1e-9):
                different += 1
        if output_values:
            numeric_comparisons[column] = {
                "matched_numeric_values": len(output_values),
                "different_value_count": different,
                "output_sum": sum(output_values),
                "reference_sum": sum(reference_values),
                "sum_delta": sum(output_values) - sum(reference_values),
                "max_absolute_delta": max(absolute_deltas),
            }
    result["numeric_columns"] = numeric_comparisons
    return result


def compare_processed(output_data_dir: Path, reference_dir: Path) -> dict[str, object]:
    comparisons: dict[str, object] = {}
    for name in REFERENCE_TABLES:
        output_path = output_data_dir / name
        reference_path = reference_dir / name
        if not reference_path.is_file():
            comparisons[name] = {"status": "missing_from_reference"}
        elif not output_path.is_file():
            comparisons[name] = {"status": "missing_from_output"}
        else:
            try:
                comparisons[name] = _compare_reference_table(output_path, reference_path)
            except ProcessingError as exc:
                comparisons[name] = {"status": "comparison_error", "detail": str(exc)}
    return {
        "reference_dir": str(reference_dir),
        "mode": "descriptive_comparison_only_no_equality_gate",
        "tables": comparisons,
    }


def run_processing(
    config_path: Path,
    output_dir: Path,
    executor: Callable[[Path, dict[str, str]], None] | None = None,
    reference_processed: Path | None = None,
) -> dict[str, object]:
    config = load_config(config_path)
    output_dir = output_dir.expanduser()
    if not output_dir.is_absolute():
        raise ProcessingError("--output must be an absolute path for a new directory")
    output_dir = output_dir.resolve()
    if output_dir.exists():
        raise ProcessingError(f"--output must name a new directory that does not exist: {output_dir}")
    if not output_dir.parent.is_dir():
        raise ProcessingError(f"parent of --output must already exist: {output_dir.parent}")

    if reference_processed is not None:
        reference_processed = reference_processed.expanduser()
        if not reference_processed.is_absolute():
            raise ProcessingError("--reference-processed must be an absolute directory path")
        reference_processed = reference_processed.resolve()
        if not reference_processed.is_dir():
            raise ProcessingError(f"--reference-processed directory does not exist: {reference_processed}")

    inputs = [*config["participant_roots"].values(), config["s0_sweep_root"], *config["s0_extra_roots"].values()]
    for input_root in inputs:
        input_root = Path(input_root)
        if output_dir == input_root or output_dir.is_relative_to(input_root):
            raise ProcessingError(f"--output must not be inside a raw input tree: {output_dir}")

    report = validate_inputs(config)
    output_dir.mkdir()
    data_dir = output_dir / "data"
    env = os.environ.copy()
    env.update(
        {
            "PAPER_ANALYSIS_ROOT": str(output_dir),
            "ABLATION_RAW_ROOT": str(config["matt_raw_root"]),
            "ABLATION_PARTICIPANT_ROOTS_JSON": json.dumps(
                {label: str(root) for label, root in config["participant_roots"].items()}
            ),
            "ABLATION_S0_SWEEP_ROOT": str(config["s0_sweep_root"]),
            "ABLATION_S0_EXTRA_ROOTS_JSON": json.dumps(config["s0_extra_roots"]),
        }
    )
    execute = executor or _execute_script
    completed_stages: list[str] = []
    stage_summaries: dict[str, object] = {}
    active_stage: str | None = None
    try:
        for stage, script_name in STAGES:
            active_stage = stage
            execute(SCRIPT_ROOT / script_name, env)
            stage_summaries[stage] = _check_stage_outputs(data_dir, stage)
            if stage == "extract":
                _check_extracted_scope(data_dir)
                if not (data_dir / "setaside.csv").is_file():
                    report["optional_outputs_absent"].append(
                        "data/setaside.csv (extract.py reported no set-aside rows)"
                    )
            completed_stages.append(stage)
    except Exception as exc:
        report.update(
            {
                "status": "failed_partial_processing",
                "mechanism_scan_participant": "matt",
                "v2_followthrough_participants": report["participants_processed"],
                "v3_cost_scan_participant": "matt",
                "completed_stages": completed_stages,
                "stage_summaries": stage_summaries,
                "failed_stage": active_stage,
                "failure": str(exc),
                "output_dir": str(output_dir),
                "full_historical_reproduction": False,
            }
        )
        (output_dir / "processing_report.json").write_text(
            json.dumps(report, indent=2) + "\n", encoding="utf-8"
        )
        raise

    report.update(
        {
            "status": "completed_bounded_processing",
            "mechanism_scan_participant": "matt",
            "v2_followthrough_participants": report["participants_processed"],
            "v3_cost_scan_participant": "matt",
            "completed_stages": completed_stages,
            "stage_summaries": stage_summaries,
            "key_totals": stage_summaries["prep"]["key_totals"],
            "output_dir": str(output_dir),
            "full_historical_reproduction": False,
            "scope_note": (
                "Processes all configured participant S1-S3 trees and configured S0 baselines through the "
                "RUN_ORDER phases 1-2 only; any unconfigured participants, targeted studies, figures, "
                "and independent audits are outside this entry. mech.py mechanism analysis and verify.py "
                "V3 cost analysis scan Matt only; verify.py V2 follow-through scans configured participants "
                "in original order: Cristian, Matt, Shu."
            ),
        }
    )
    if reference_processed is not None:
        report["reference_comparison"] = compare_processed(data_dir, reference_processed)
    else:
        report["reference_comparison"] = None
    report_path = output_dir / "processing_report.json"
    report_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run explicitly configured participant S0-S3 ablation processing prefix."
    )
    parser.add_argument("--config", type=Path, required=True, help="JSON config with explicit absolute raw roots")
    parser.add_argument("--output", type=Path, required=True, help="new absolute output directory")
    parser.add_argument(
        "--reference-processed",
        type=Path,
        help="optional processed-data directory for descriptive comparison (no equality gate)",
    )
    args = parser.parse_args(argv)
    try:
        report = run_processing(args.config, args.output, reference_processed=args.reference_processed)
    except ProcessingError as exc:
        parser.error(str(exc))
    print(f"Completed stages: {' -> '.join(report['completed_stages'])}")
    print(f"Processing report: {args.output / 'processing_report.json'}")
    print("Full historical reproduction: no (bounded scope; see processing_report.json)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
