"""Shared publication figure style + validated palette.

Palette validated with the dataviz skill's validate_palette.js (light mode, --pairs all):
  levels  #eb6834,#2a78d6,#1baf7a,#4a3aa7  -> ALL CHECKS PASS
  models  #eda100,#e87ba4,#008300          -> ALL CHECKS PASS
Both carry a contrast WARN vs the light surface, so the relief rule applies:
every figure ships visible direct value labels and/or a companion table in the
results document.
"""
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.patheffects as pe

SURFACE = "#fcfcfb"
INK = "#0b0b0b"
INK2 = "#52514e"
INK3 = "#8a8880"
GRID = "#e4e3de"

LEVELC = {"S0": "#eb6834", "S1": "#2a78d6", "S2": "#1baf7a", "S3": "#4a3aa7"}
MODELC = {"luna": "#eda100", "sonnet5": "#e87ba4", "gemini36flash": "#008300"}
MODELM = {"luna": "o", "sonnet5": "s", "gemini36flash": "^"}          # secondary encoding
MODELN = {"luna": "GPT-5.6 Luna", "sonnet5": "Claude Sonnet 5",
          "gemini36flash": "Gemini 3.6 Flash"}
# Paper-facing level names, finalised 2026-09-03. Bare S0-S3 codes are kept as a
# secondary prefix (not dropped) so dense figures can still cross-reference the
# ladder compactly; LEVELN_SHORT is the bare descriptive name with no code prefix,
# for contexts too narrow for the full "S0  " + name form.
LEVELN = {"S0": "S0  Fitted Heuristic", "S1": "S1  Serial Reasoning",
          "S2": "S2  Branching Exploration", "S3": "S3  KB-Guided Exploration"}
LEVELN_SHORT = {"S0": "Fitted Heuristic", "S1": "Serial Reasoning",
                "S2": "Branching Exploration", "S3": "KB-Guided Exploration"}
LEVELS = ["S0", "S1", "S2", "S3"]

# single-hue sequential ramp for magnitude (blue, light -> dark)
SEQ = ["#eef4fc", "#cbdff6", "#9dc3ec", "#6aa5e5", "#3987e5", "#2a78d6", "#1c5aa3", "#123c6d"]

# 16 designs. picoblazearray added 2026-08-27: the only non-AMD design, out-of-family.
KNOWN = ["vexriscv_re-place", "amd_mini_isp", "logicnets_jscl", "finn_radioml",
         "rosetta_digit-recognition", "boom_soc", "rosetta_optical-flow",
         "corescore_500_mod"]
V2REIMPL = ["amd_mini-isp_v2", "boom_soc_v2", "vexriscv_re-place_v2",
            "rosetta_3d-rendering_v2", "vtr_mcml_v2"]
OUTFAM = ["fir_symmetric_systolic", "fir_systolic_transposed", "picoblazearray"]
UNKNOWN = V2REIMPL + OUTFAM
GRP = {**{b: "known" for b in KNOWN}, **{b: "v2-reimpl" for b in V2REIMPL},
       **{b: "out-of-family" for b in OUTFAM}}
GRPN = {"known": "KNOWN", "v2-reimpl": "UNKNOWN — v2 re-implementation",
        "out-of-family": "UNKNOWN — out of family"}
SHORT = {
    "vexriscv_re-place": "vexriscv", "amd_mini_isp": "amd_mini_isp",
    "logicnets_jscl": "logicnets", "finn_radioml": "finn_radioml",
    "rosetta_digit-recognition": "rosetta_digit", "boom_soc": "boom_soc",
    "rosetta_optical-flow": "rosetta_optflow", "corescore_500_mod": "corescore",
    "amd_mini-isp_v2": "amd_mini_isp v2", "boom_soc_v2": "boom_soc v2",
    "vexriscv_re-place_v2": "vexriscv v2", "fir_symmetric_systolic": "fir_symmetric",
    "fir_systolic_transposed": "fir_transposed", "rosetta_3d-rendering_v2": "rosetta_3d v2",
    "vtr_mcml_v2": "vtr_mcml v2", "picoblazearray": "picoblazearray",
}
# Stamped on every figure so the corpus is never ambiguous.
CORPUS = "n = 16 designs · 256 runs · 2,421 attempts · S0 budget-matched at 11"


def setup():
    plt.rcParams.update({
        "figure.facecolor": SURFACE, "axes.facecolor": SURFACE,
        "savefig.facecolor": SURFACE,
        "font.family": "DejaVu Sans", "font.size": 8.5,
        "axes.edgecolor": GRID, "axes.linewidth": 0.8,
        "axes.labelcolor": INK2, "axes.titlecolor": INK,
        "axes.titlesize": 9.5, "axes.titleweight": "bold", "axes.labelsize": 8.5,
        "xtick.color": INK2, "ytick.color": INK2,
        "xtick.labelsize": 8, "ytick.labelsize": 8,
        "xtick.major.width": 0.8, "ytick.major.width": 0.8,
        "xtick.major.size": 3, "ytick.major.size": 3,
        "grid.color": GRID, "grid.linewidth": 0.7, "grid.linestyle": "-",
        "legend.frameon": False, "legend.fontsize": 8,
        "lines.linewidth": 1.6, "lines.solid_capstyle": "round",
        "figure.dpi": 130, "savefig.dpi": 300,
    })


def clean(ax, grid="y", left=True, bottom=True):
    for s in ("top", "right"):
        ax.spines[s].set_visible(False)
    ax.spines["left"].set_visible(left)
    ax.spines["bottom"].set_visible(bottom)
    if grid:
        ax.set_axisbelow(True)
        ax.grid(axis=grid, color=GRID, lw=0.7)
        if grid == "y":
            ax.grid(axis="x", visible=False)
        else:
            ax.grid(axis="y", visible=False)
    return ax


def halo(txt, w=2.2, c=SURFACE):
    txt.set_path_effects([pe.withStroke(linewidth=w, foreground=c)])
    return txt


def title_block(fig, title, subtitle, y=0.985, sy=0.945, x=0.5):
    """Title + subtitle at fixed figure coords. Explicit margins, no tight bbox,
    so positions are predictable and never collide."""
    fig.text(x, y, title, ha="center", va="top", fontsize=13.2,
             fontweight="bold", color=INK)
    fig.text(x, sy, subtitle, ha="center", va="top", fontsize=8.2, color=INK2,
             linespacing=1.5)


def stamp(fig, extra=""):
    """Corpus provenance, bottom-left of every figure, so n is never ambiguous."""
    fig.text(0.006, 0.006, CORPUS + (("  ·  " + extra) if extra else ""),
             ha="left", va="bottom", fontsize=6.6, color=INK3)


def save(fig, name, outdir="figures"):
    import os
    stamp(fig)
    os.makedirs(outdir, exist_ok=True)
    for ext in ("png", "pdf"):
        fig.savefig(f"{outdir}/{name}.{ext}")
    plt.close(fig)
    print(f"  wrote {outdir}/{name}.png / .pdf")
