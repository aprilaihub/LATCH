"""Design-signal helpers — derivation rules shared by the offline extractor and
the runtime retrieval path.

These rules are deliberately NOT buried in the extraction script: the SAME
``complexity_class`` (and friends) must be applied to a LIVE design at runtime.
Keep them here so both the history extractor and the runtime path import one
definition.
"""

from __future__ import annotations

from typing import Any, Dict, Optional, Tuple

# xcvu3p-ffvc1517-2-e device capacity. Source: docs/contest/benchmarks.md
# ("|LUTs|FFs|DSPs|BRAMs|  |394k|788k|2280|720|"). Used only for utilisation_pct.
DEVICE_LUT_CAPACITY = 394_000

# complexity_class cut-points on absolute LUT count. Chosen to span the full
# contest range (~2k vexriscv .. ~289k boom_soc): small | medium | large.
COMPLEXITY_SMALL_MAX = 10_000     # luts < this  -> "small"
COMPLEXITY_MEDIUM_MAX = 100_000   # luts < this  -> "medium", else "large"


def complexity_class(lut_count: Optional[int]) -> Optional[str]:
    """Deterministic design-size bucket from LUT count. Returns None if unknown.

    >>> complexity_class(1866), complexity_class(41762), complexity_class(289000)
    ('small', 'medium', 'large')
    """
    if not lut_count or lut_count <= 0:
        return None  # 0/None = missing capture, NOT a real small design
    if lut_count < COMPLEXITY_SMALL_MAX:
        return "small"
    if lut_count < COMPLEXITY_MEDIUM_MAX:
        return "medium"
    return "large"


def utilisation_pct(lut_count: Optional[int],
                    device_lut_capacity: int = DEVICE_LUT_CAPACITY) -> Optional[float]:
    """LUT utilisation as a percentage of device capacity (xcvu3p by default)."""
    if not lut_count or not device_lut_capacity:
        return None
    return round(100.0 * lut_count / device_lut_capacity, 2)


def total_hardblocks(dsp_count: Optional[int], bram_count: Optional[int]) -> Optional[int]:
    if dsp_count is None or bram_count is None:
        return None
    return int(dsp_count) + int(bram_count)


def ff_gt_lut(ff_count: Optional[int], lut_count: Optional[int]) -> Optional[bool]:
    if ff_count is None or lut_count is None:
        return None
    return bool(ff_count > lut_count)


# --- Complexity fallback when live LUT count is unavailable -----------------
#
# The real (non-mock) contest entry path builds DeterministicAnalyzer() with no
# analysis_provider (optimizer_pipeline/apps/fpl_contest/entry.py), so utilization_summary
# is never populated for the ROOT node -- complexity_class(live_luts) is None at
# exactly the moment (the first flow decision) retrieval matters most. The
# benchmark's input DCP filename IS always known, though (path.stem fallback in
# build_analysis_context_v2 never returns None). This table lets retrieval
# resolve a complexity bucket from the name alone when live utilization is
# missing. Source: docs/contest/benchmarks.md benchmark table (LUTs column).
# Approximate (rounded "Xk" figures from the docs), used ONLY as a fallback --
# never overrides a real live LUT count.
_BENCHMARK_LUT_HINTS: Dict[str, int] = {
    "amd_mini-isp": 3_000,
    "boom_soc": 227_000,
    "corescore_500_mod": 100_000,
    "finn_radioml": 74_000,
    "ispd16_example2": 289_000,
    "logicnets_jscl": 31_000,
    "rosetta_3d-rendering": 14_000,
    "rosetta_digit-recognition": 23_000,
    "rosetta_optical-flow": 34_000,
    "rosetta_spam-filter": 5_000,
    "vexriscv_re-place_v2": 2_000,  # checked before the shorter prefix below
    "vexriscv_re-place": 2_000,
    "vtr_mcml": 43_000,
}
# Longest key first so e.g. "vexriscv_re-place_v2_..." doesn't get short-circuited
# by the "vexriscv_re-place" prefix match.
_BENCHMARK_LUT_HINT_KEYS = sorted(_BENCHMARK_LUT_HINTS, key=len, reverse=True)


def lut_hint_from_benchmark_name(benchmark_name: Optional[str]) -> Optional[int]:
    """Approximate LUT count from the static name table; None if unrecognized."""
    if not benchmark_name:
        return None
    name = str(benchmark_name).lower()
    for key in _BENCHMARK_LUT_HINT_KEYS:
        if key in name:
            return _BENCHMARK_LUT_HINTS[key]
    return None


def resolve_complexity_class(
    live_design_context: Optional[Dict[str, Any]],
) -> Tuple[Optional[str], str]:
    """Resolve a complexity_class for the LIVE design being optimised now.

    Tries, in order: (1) live LUT count from the current run's analysis context,
    (2) a static benchmark-name -> LUT-count lookup (docs table) when live data
    is missing, (3) gives up and reports "unknown" -- callers must handle this
    gracefully (never crash, never fabricate a class).

    Returns ``(complexity_class_or_None, source)`` where source is one of
    "live_lut_count", "benchmark_name_table", "unknown".
    """
    ctx = live_design_context or {}
    live_luts = ctx.get("lut_count")
    cc = complexity_class(live_luts)
    if cc is not None:
        return cc, "live_lut_count"
    hint = lut_hint_from_benchmark_name(ctx.get("benchmark_name"))
    cc = complexity_class(hint)
    if cc is not None:
        return cc, "benchmark_name_table"
    return None, "unknown"
