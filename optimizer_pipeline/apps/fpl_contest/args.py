"""Argument parser for the contest-compatible optimizer entrypoint."""

from __future__ import annotations

import argparse
from pathlib import Path

from optimizer_pipeline.apps.fpl_contest.analysis_profiles import ANALYSIS_CHOICES

REPO_ROOT = Path(__file__).resolve().parents[3]

PIPELINE_CHOICES = (
    "contest_default",
    "fixed",
    "linear",
    "frontier",
    "analysis_single_path",
    "analysis_frontier",
    "analysis_single_path_frontier",
    "custom",
)

POLICY_CHOICES = ("analysis_priority", "deterministic", "reliability", "retrieval")
RUN_MODE_CHOICES = ("contest-strict", "research-dev")
LLM_PROVIDER_CHOICES = ("none", "openrouter", "openai", "local")
KNOWLEDGE_MODE_CHOICES = ("off", "reference_manual")
RESCUE_MODE_CHOICES = ("off", "deterministic", "llm", "synthesis", "synthesis_loop", "manual_loop")
RESCUE_TRIGGER_CHOICES = ("no_improvement", "always")
RESCUE_SYNTHESIS_FALLBACK_CHOICES = ("deterministic", "fail")
RESCUE_LOOP_POLICY_CHOICES = ("fixed_attempts", "stop_after_first_improvement", "external")
RESCUE_PARENT_STRATEGY_CHOICES = ("current_best", "root", "round_robin_safe_parents")
RESCUE_EVIDENCE_MODE_CHOICES = ("compact", "planner")
RESCUE_HISTORY_SCOPE_CHOICES = ("in_run", "known_public")


def build_contest_parser() -> argparse.ArgumentParser:
    """Build the single official optimizer parser.

    The default path delegates to the frontier-expansion profile adapter.  The
    ``linear`` pipeline name is a single-path frontier preset.
    """

    parser = argparse.ArgumentParser(
        description="FPGA design optimization entrypoint",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python dcp_optimizer.py input.dcp
  python dcp_optimizer.py input.dcp --pipeline linear --single-path-depth 3 --budget-runs 3
  python dcp_optimizer.py input.dcp --pipeline frontier --budget-runs 4 --parallelism 2
  python dcp_optimizer.py input.dcp --mock --pipeline fixed --plan memory_path_opt,phys_opt
        """,
    )
    parser.add_argument("input_dcp", type=Path, help="Input design checkpoint (.dcp)")
    parser.add_argument(
        "--output",
        "-o",
        type=Path,
        dest="output_dcp",
        help="Output optimized checkpoint. Default: <input_name>_optimized-<timestamp>.dcp next to input.",
    )
    parser.add_argument("--run-dir", type=Path, default=None, help="Run artifact directory; default dcp_optimizer_run-<timestamp>.")
    parser.add_argument("--config", type=Path, default=REPO_ROOT / "configs" / "frontier_expansion_default.json")
    parser.add_argument("--mock", action="store_true", help="Use mock analysis and execution without Vivado.")
    parser.add_argument("--no-dotenv", action="store_true", help="Do not read repo or cwd .env files automatically.")

    parser.add_argument("--run-mode", choices=RUN_MODE_CHOICES, default="contest-strict")
    parser.add_argument("--pipeline", choices=PIPELINE_CHOICES, default="contest_default")
    parser.add_argument("--policy", choices=POLICY_CHOICES, default="deterministic")
    parser.add_argument("--analysis", choices=ANALYSIS_CHOICES, default="none")
    parser.add_argument("--llm-provider", choices=LLM_PROVIDER_CHOICES, default="none")
    parser.add_argument("--plan", default=None, help="Comma-separated fixed flow sequence.")
    parser.add_argument("--budget-runs", type=int, default=6, help="Maximum transition executions.")
    parser.add_argument("--single-path-depth", type=int, default=3, help="Single-path frontier depth.")
    parser.add_argument("--parallelism", type=int, default=1, help="Maximum concurrent transition executions.")
    parser.add_argument("--custom-pipeline", default=None, help="Custom async pipeline as module:function.")
    parser.add_argument(
        "--rescue-mode",
        choices=RESCUE_MODE_CHOICES,
        default="off",
        help="Optional no-improvement rescue branch; disabled by default.",
    )
    parser.add_argument(
        "--rescue-trigger",
        choices=RESCUE_TRIGGER_CHOICES,
        default="no_improvement",
        help="Condition that permits the rescue branch.",
    )
    parser.add_argument("--rescue-max-runs", type=int, default=1, help="Maximum rescue transitions after the main profile.")
    parser.add_argument("--rescue-max-episodes", type=int, default=1, help="Maximum manual rescue episodes for manual_loop.")
    parser.add_argument(
        "--rescue-min-main-delta-mhz",
        type=float,
        default=0.1,
        help="Treat the main profile as no-improvement when best Fmax delta is at or below this threshold.",
    )
    parser.add_argument("--rescue-synthesis-max-steps", type=int, default=8, help="Maximum primitive steps in a synthesized rescue recipe.")
    parser.add_argument(
        "--rescue-synthesis-fallback",
        choices=RESCUE_SYNTHESIS_FALLBACK_CHOICES,
        default="deterministic",
        help="Fallback when synthesis fails or returns an invalid recipe.",
    )
    parser.add_argument(
        "--rescue-loop-policy",
        choices=RESCUE_LOOP_POLICY_CHOICES,
        default="fixed_attempts",
        help="Control how synthesis_loop consumes attempts; ROI decisions remain outside rescue.",
    )
    parser.add_argument(
        "--rescue-parent-strategy",
        choices=RESCUE_PARENT_STRATEGY_CHOICES,
        default="current_best",
        help="Parent selection strategy for synthesis_loop attempts.",
    )
    parser.add_argument(
        "--rescue-manual-parent-selector",
        choices=("last_scratch", "current_best", "root"),
        default="last_scratch",
        help="Manual-loop checkpoint parent selector; outer strategy controls when rescue is invoked.",
    )
    parser.add_argument(
        "--rescue-manual-min-publish-delta-mhz",
        type=float,
        default=0.01,
        help="Minimum manual-loop Fmax gain required before a clean checkpoint may replace the incumbent.",
    )
    parser.add_argument(
        "--rescue-manual-allow-nonclean-publish",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="Allow WARNING/FALLBACK_USED manual checkpoints to publish; disabled by default.",
    )
    parser.add_argument(
        "--rescue-evidence-mode",
        choices=RESCUE_EVIDENCE_MODE_CHOICES,
        default="compact",
        help="Evidence context mode for synthesis rescue; synthesis_loop promotes compact to planner.",
    )
    parser.add_argument(
        "--rescue-history-scope",
        choices=RESCUE_HISTORY_SCOPE_CHOICES,
        default="in_run",
        help="History visible to rescue synthesis: current run only or curated known-public priors.",
    )
    parser.add_argument(
        "--rescue-enable-act-probes",
        action="store_true",
        help="Expose limited act-probe analysis tools to rescue; disabled by default.",
    )
    parser.add_argument(
        "--rescue-max-act-probes",
        type=int,
        default=0,
        help="Maximum limited act-probe tool calls for rescue analysis.",
    )
    parser.add_argument(
        "--rescue-act-probe-wall-sec",
        type=float,
        default=300.0,
        help="Wall-time cap for limited act-probe evidence gathering.",
    )
    parser.add_argument(
        "--rescue-allow-invalid-scratch",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Allow manual_loop to continue from invalid scratch checkpoints; invalid scratch is never published.",
    )

    parser.add_argument("--api-key", default=None, help="OpenRouter/OpenAI-compatible API key override.")
    parser.add_argument("--model", default=None, help="LLM model label for policy or analysis loops.")
    parser.add_argument("--llm-cost-budget-usd", type=float, default=None, help="Optional LLM cost budget for this benchmark.")
    parser.add_argument(
        "--knowledge-mode",
        choices=KNOWLEDGE_MODE_CHOICES,
        default=None,
        help="Inject static optimizer reference manual into analysis LLM prompts; default off.",
    )
    parser.add_argument(
        "--knowledge-base-path",
        type=Path,
        default=None,
        help="Knowledgebase JSON path; defaults to the bundled reference manual when --knowledge-mode=reference_manual.",
    )
    parser.add_argument("--debug", action="store_true", help="Enable verbose execution artifacts.")
    parser.add_argument("--max-nets", type=int, default=5, help="Maximum high-fanout nets for deterministic test flows.")

    return parser
