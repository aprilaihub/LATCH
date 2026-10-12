"""Paper palette, per 12_FPL_Timing_Agent_Writeup/Writing_Paper/colour_scheme_guide.md.

One shared module, as the guide asks, so every paper figure updates from one place.
Layered on top of figstyle.py: typography, `clean`, `halo`, `title_block`, `save` and
the corpus stamp are unchanged; only the colours and the redundant-encoding maps are
overridden here.

Guide compliance notes
----------------------
* Qualitative palette is exactly the four hexes in the guide, used for nothing but
  ablation rungs.
* The guide warns that pastels collapse in greyscale, and it is right: these four sit
  between grey 142 and 172 out of 255, and S1 vs S3 differ by only 5 levels. Colour
  therefore carries NO information on its own anywhere in these figures. Every level
  also carries a marker shape (point plots), a hatch (bar plots), a fixed position
  within its group, and a printed value.
* Phase categories are NOT levels, so they must not borrow the level palette --
  a reader would otherwise read a stacked segment as a rung. They use the guide's
  diverging teal/cream/brick family instead, which the guide notes greyscales
  properly, with "failed" in brick so waste is the darkest thing on the chart.
* Worth/risk likewise use teal (good) and brick (bad) rather than a fifth and sixth
  qualitative hue.
"""
import matplotlib.pyplot as plt

from figstyle import (setup as _base_setup, clean, save, halo, title_block, stamp,
                      INK, INK2, INK3, GRID, SURFACE, LEVELS, LEVELN, LEVELN_SHORT,
                      MODELM, MODELN, KNOWN, V2REIMPL, OUTFAM, UNKNOWN, SHORT, CORPUS)

# ----------------------------------------------------------------- qualitative
LEVELC = {"S0": "#E8A34D",     # pastel orange   -- heuristic
          "S1": "#6C93C7",     # pastel blue     -- LLM
          "S2": "#7FAE7F",     # pastel green    -- + tree search
          "S3": "#A08CC0"}     # pastel purple   -- + knowledge base

# Redundant encodings. The existing marker convention (S0 diamond, S1 circle,
# S2 square, S3 triangle) is kept rather than the guide's illustrative "e.g."
# ordering, so these figures stay consistent with the drafts already circulated;
# the guide's actual requirement -- a distinct, consistently applied shape per
# level -- is met either way.
LEVELM = {"S0": "D", "S1": "o", "S2": "s", "S3": "^"}
LEVELLS = {"S0": "-", "S1": (0, (4, 2)), "S2": (0, (5, 1.5, 1, 1.5)),
           "S3": (0, (1.4, 1.4))}
# S3 is "xxx" rather than a backslash hatch: on a desaturated render "///" and
# "\\\" read as the same texture at print size. Verified, not assumed.
LEVELH = {"S0": "", "S1": "///", "S2": "...", "S3": "xxx"}   # bars

# --------------------------------------------------- diverging / scale family
TEAL, CREAM, BRICK = "#4F9C93", "#F0EDE5", "#B85C4A"
DIVERGING = [TEAL, CREAM, BRICK]

# Phase composition: an ordered ramp inside the scale family, never the level
# palette. Deliberately spans a wide lightness range so the stack survives
# desaturation, with the wasteful category darkest.
PHASEC = {"analysis_s": TEAL,
          "diagnostic_s": "#93BFB8",
          "exec_productive_s": "#E3DFD2",
          "exec_failed_s": BRICK}
PHASEH = {"analysis_s": "", "diagnostic_s": "..",
          "exec_productive_s": "", "exec_failed_s": "xx"}
PHASEN = {"analysis_s": "language-model analysis",
          "diagnostic_s": "Vivado diagnostics",
          "exec_productive_s": "flow execution (productive)",
          "exec_failed_s": "flow execution (failed)"}
PHASE_ORDER = ["analysis_s", "diagnostic_s", "exec_productive_s", "exec_failed_s"]

WORTHC, RISKC = TEAL, BRICK          # good / bad, not a fifth qualitative hue


def setup():
    _base_setup()
    plt.rcParams.update({"hatch.linewidth": 0.6})


def grey(hexc):
    """Rec.709 luminance of a hex colour, 0-255. Used to audit greyscale safety."""
    h = hexc.lstrip("#")
    r, g, b = (int(h[i:i + 2], 16) for i in (0, 2, 4))
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def greyscale_report(name, colours):
    """Print the greyscale separation of a colour set. The guide requires this be
    checked rather than assumed."""
    vals = {k: grey(v) for k, v in colours.items()}
    order = sorted(vals.items(), key=lambda kv: kv[1])
    worst = min(abs(order[i + 1][1] - order[i][1]) for i in range(len(order) - 1))
    print(f"  {name}:")
    for k, v in order:
        print(f"     {k:22s} {colours[k]}  grey {v:5.1f}")
    print(f"     closest adjacent pair differs by {worst:.1f} / 255"
          f"  -> {'OK' if worst >= 25 else 'TOO CLOSE, needs shape/hatch'}")
    return worst
