"""Direct execution of one explicitly named backend action."""

from .contracts import DirectActionRequest
from .entry import build_direct_action_parser, main, run_direct_action_entry

__all__ = [
    "DirectActionRequest",
    "build_direct_action_parser",
    "main",
    "run_direct_action_entry",
]
