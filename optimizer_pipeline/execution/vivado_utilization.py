"""Vivado utilization report parsing for execution metrics.

This module is intentionally in the execution layer: it translates Vivado
report text into normalized resource facts.  Durable records and scoring code
should consume the normalized dictionaries rather than know Vivado's text
format.
"""

from __future__ import annotations

import json
import math
import re
from typing import Any, Dict, Optional


RESOURCE_KEYS = ("luts", "ffs", "dsps", "brams", "urams")


def int_count_or_none(value: Any, *, ceil_fraction: bool = True) -> Optional[int]:
    """Convert a Vivado count-like value to an integer resource count."""
    if value is None:
        return None
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float)):
        number = float(value)
    else:
        text = str(value).strip().replace(",", "")
        if not text:
            return None
        match = re.search(r"-?\d+(?:\.\d+)?", text)
        if not match:
            return None
        try:
            number = float(match.group(0))
        except ValueError:
            return None
    if number <= 0:
        return 0
    return int(math.ceil(number) if ceil_fraction else round(number))


def empty_utilization() -> Dict[str, Optional[int]]:
    data: Dict[str, Optional[int]] = {}
    for key in RESOURCE_KEYS:
        data[key] = None
        data[f"{key}_1_5x"] = None
    return data


def parse_pblock_utilization_text(report: str) -> Dict[str, Optional[int]]:
    """Parse Vivado pblock-utilization text or JSON into stable lower-case keys."""
    parsed = empty_utilization()
    if not report:
        return parsed

    try:
        payload = json.loads(report)
        if isinstance(payload, dict):
            aliases = {
                "luts": ["luts", "lut", "LUT", "LUTs"],
                "ffs": ["ffs", "ff", "FF", "FFs"],
                "dsps": ["dsps", "dsp", "DSP", "DSPs"],
                "brams": ["brams", "bram", "BRAM", "BRAMs"],
                "urams": ["urams", "uram", "URAM", "URAMs"],
                "luts_1_5x": ["luts_1_5x", "lut_1_5x"],
                "ffs_1_5x": ["ffs_1_5x", "ff_1_5x"],
                "dsps_1_5x": ["dsps_1_5x", "dsp_1_5x"],
                "brams_1_5x": ["brams_1_5x", "bram_1_5x"],
                "urams_1_5x": ["urams_1_5x", "uram_1_5x"],
            }
            for key, candidates in aliases.items():
                for candidate in candidates:
                    if candidate in payload:
                        parsed[key] = int_count_or_none(payload.get(candidate))
                        break
    except (json.JSONDecodeError, TypeError):
        pass

    lower_report = report.lower()
    split_index = lower_report.find("1.5x")
    base_block = report[:split_index] if split_index >= 0 else report
    scaled_block = report[split_index:] if split_index >= 0 else ""
    base_values = _parse_resource_block(base_block)
    scaled_values = _parse_resource_block(scaled_block)

    for key in RESOURCE_KEYS:
        if parsed[key] is None:
            parsed[key] = base_values.get(key)
        scaled_key = f"{key}_1_5x"
        if parsed[scaled_key] is None:
            parsed[scaled_key] = scaled_values.get(key)
        if parsed[scaled_key] is None and parsed[key] is not None:
            parsed[scaled_key] = int(round(parsed[key] * 1.5))
    return parsed


def utilization_delta(
    initial_utilization: Dict[str, Optional[int]],
    final_utilization: Dict[str, Optional[int]],
) -> Dict[str, Optional[int]]:
    """Return final-minus-initial resource deltas for tracked utilization."""
    deltas: Dict[str, Optional[int]] = {}
    for key in RESOURCE_KEYS:
        initial = initial_utilization.get(key) if initial_utilization else None
        final = final_utilization.get(key) if final_utilization else None
        deltas[key] = final - initial if isinstance(initial, int) and isinstance(final, int) else None
    return deltas


def format_pblock_utilization(parsed: Dict[str, Optional[int]]) -> str:
    """Format normalized utilization using the existing Vivado MCP text contract."""
    values = {key: int(parsed.get(key) or 0) for key in RESOURCE_KEYS}
    scaled = {
        key: int(parsed.get(f"{key}_1_5x") if parsed.get(f"{key}_1_5x") is not None else values[key] * 1.5)
        for key in RESOURCE_KEYS
    }
    result_lines = [
        "=== Design Resource Utilization ===",
        "",
        f"LUTs:  {values['luts']:8,}",
        f"FFs:   {values['ffs']:8,}",
        f"DSPs:  {values['dsps']:8,}",
        f"BRAMs: {values['brams']:8,}",
        f"URAMs: {values['urams']:8,}",
        "",
        "=== 1.5x Multiplier (for pblock sizing) ===",
        "",
        f"LUTs:  {scaled['luts']:8,}",
        f"FFs:   {scaled['ffs']:8,}",
        f"DSPs:  {scaled['dsps']:8,}",
        f"BRAMs: {scaled['brams']:8,}",
        f"URAMs: {scaled['urams']:8,}",
    ]
    return "\n".join(result_lines)


def _parse_resource_block(block: str) -> Dict[str, Optional[int]]:
    values: Dict[str, Optional[int]] = {key: None for key in RESOURCE_KEYS}
    patterns = {
        "luts": (r"\|\s*Slice LUTs\s*\|([^|\n]+)", r"\|\s*LUT as Logic\s*\|([^|\n]+)", r"LUTs\s*:\s*([0-9,]+(?:\.\d+)?)"),
        "ffs": (
            r"\|\s*Register as Flip Flop\s*\|([^|\n]+)",
            r"\|\s*Slice Registers\s*\|([^|\n]+)",
            r"\|\s*CLB Registers\s*\|([^|\n]+)",
            r"FFs\s*:\s*([0-9,]+(?:\.\d+)?)",
        ),
        "dsps": (r"\|\s*DSPs\s*\|([^|\n]+)", r"DSPs\s*:\s*([0-9,]+(?:\.\d+)?)"),
        "brams": (r"\|\s*Block RAM Tile\s*\|([^|\n]+)", r"BRAMs\s*:\s*([0-9,]+(?:\.\d+)?)"),
        "urams": (r"\|\s*URAM\s*\|([^|\n]+)", r"URAMs\s*:\s*([0-9,]+(?:\.\d+)?)"),
    }
    for key, candidates in patterns.items():
        for pattern in candidates:
            match = re.search(pattern, block, re.IGNORECASE)
            if match:
                values[key] = int_count_or_none(match.group(1))
                break
    return values
