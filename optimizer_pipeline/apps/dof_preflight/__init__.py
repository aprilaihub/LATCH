"""Strict command-line boundary for the bounded L0 real-DCP preflight."""

from .entry import (
    build_dof_preflight_parser,
    main,
    run_dof_preflight_entry,
)

__all__ = ["build_dof_preflight_parser", "main", "run_dof_preflight_entry"]
