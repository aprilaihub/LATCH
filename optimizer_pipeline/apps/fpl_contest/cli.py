"""Owned contest command-line entry."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence

from optimizer_pipeline.backends.xilinx import run_test_mode

from .args import build_contest_parser
from .entry import run_contest_entry


async def main(argv: Sequence[str] | None = None) -> int:
    args = build_contest_parser().parse_args(argv)
    return await run_contest_entry(args, run_test_mode=run_test_mode)


def main_sync(argv: Sequence[str] | None = None) -> int:
    return asyncio.run(main(argv))
