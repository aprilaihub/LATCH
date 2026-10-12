"""Xilinx/Vivado backend implementation and legacy flow catalog."""

from .adapter import XilinxBackend, xilinx_action_catalog
from .legacy import (
    DCPOptimizerBase,
    FPGAOptimizerTest,
    REPO_ROOT,
    XILINX_FLOW_NAMES,
    ensure_vivado_on_path,
    parse_timing_summary_static,
    restore_terminal_log,
    run_test_mode,
    setup_terminal_log,
)

__all__ = [
    "DCPOptimizerBase",
    "FPGAOptimizerTest",
    "REPO_ROOT",
    "XILINX_FLOW_NAMES",
    "XilinxBackend",
    "ensure_vivado_on_path",
    "parse_timing_summary_static",
    "restore_terminal_log",
    "run_test_mode",
    "setup_terminal_log",
    "xilinx_action_catalog",
]
