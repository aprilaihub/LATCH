"""Experiment harness utilities for research-mode frontier sweeps."""

from .schema import ExperimentSpec, load_experiment_spec
from .matrix import ExperimentRunSpec, expand_experiment_matrix
from .runner import ExperimentRunResult, run_experiment
from .preflight import ExperimentPreflightError, PreflightIssue, PreflightReport, preflight_experiment
from .collector import collect_run_rows, aggregate_rows
from .report import render_comparison_report

__all__ = [
    "ExperimentSpec",
    "ExperimentRunSpec",
    "ExperimentRunResult",
    "ExperimentPreflightError",
    "aggregate_rows",
    "collect_run_rows",
    "expand_experiment_matrix",
    "load_experiment_spec",
    "PreflightIssue",
    "PreflightReport",
    "preflight_experiment",
    "render_comparison_report",
    "run_experiment",
]
