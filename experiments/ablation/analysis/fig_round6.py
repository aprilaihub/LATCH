#!/usr/bin/env python3
"""Round 6 -- paper-insertion edits to the three round-5 ablation figures.

LATCH migration modification: corpus metadata now describes the input tables;
unverified hardcoded historical cost counts/accuracy/time ratios are not printed.
The underlying calculations and plotted numeric values remain unchanged.

Outputs to figures_refined_5/. figures_refined_4/ (round 5) is left intact.
Data, layout and encodings are unchanged from fig_round5.py; only these edits:

  1. Plain white background (figure, axes, text halos, bar edges, box fill),
     so the figures sit cleanly on the page.
  2. No S0-S3 codes anywhere -- legends, titles, subtitles, axis labels and the
     corpus stamp use the level names only.
  3. Figure 7: left x-axis label -> "Share of Ablation Level's Attempts, %".
  4. Figure 8: cost axis relabelled; the timing panel's x labels are the level
     names on two lines; its key is split into a titled "Phase time" group and a
     separate wall-time entry.
"""
import os
import sys
import numpy as np
import pandas as pd
import matplotlib.pyplot as plt
from matplotlib.lines import Line2D
from matplotlib.patches import Patch
from matplotlib.ticker import FixedLocator, FixedFormatter

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from figstyle_paper import (setup, clean, halo, title_block,
                            LEVELC, LEVELM, LEVELH, LEVELS,
                            PHASEC, PHASEH, PHASEN, PHASE_ORDER,
                            WORTHC, RISKC, MODELN,
                            KNOWN, V2REIMPL, OUTFAM, SHORT,
                            INK, INK2, INK3, GRID)
from figstyle import LEVELN_SHORT as LEVELN

_PA_ROOT = os.environ.get("PAPER_ANALYSIS_ROOT") or os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))

WHITE = "#ffffff"
setup()
plt.rcParams.update({"figure.facecolor": WHITE, "axes.facecolor": WHITE,
                     "savefig.facecolor": WHITE})
CORPUS = ""  # Populated from the supplied tables, never an assumed historical corpus.


def halo_w(txt):
    return halo(txt, c=WHITE)


def save(fig, name, outdir):
    fig.text(0.006, 0.006, CORPUS, ha="left", va="bottom", fontsize=6.6, color=INK3)
    os.makedirs(outdir, exist_ok=True)
    for ext in ("png", "pdf"):
        fig.savefig(f"{outdir}/{name}.{ext}", facecolor=WHITE)
    plt.close(fig)
    print(f"  wrote {outdir}/{name}.png / .pdf")


A = os.path.expanduser(_PA_ROOT + "")
D = A + "/data/"
OUT = os.environ["LATCH_FIGURE_OUTPUT"]
runs = pd.read_csv(D + "runs_enriched.csv")
nodes = pd.read_csv(D + "nodes.csv")
nr = nodes[nodes.flow != "root"].copy()
CORPUS = (f"Input tables: {runs.benchmark.nunique()} designs · {len(runs):,} runs · "
          f"{len(nr):,} non-root attempts; corpus completeness not certified")

piv = runs.pivot_table(index="benchmark", columns="level", values="pct_gain",
                       aggfunc="mean")
YLD = nr.groupby("flow").agg(
    n=("flow", "size"),
    mean_delta=("delta_mhz", "mean"),
    pct_invalid=("validation_status", lambda s: 100 * (s == "INVALID").mean()),
).sort_values("mean_delta", ascending=False)
FLOWS = list(YLD.index)

NARROW = 0.85


# ================================================================ FIGURE 6
def fig1_generalisation():
    fig = plt.figure(figsize=(13.2 * NARROW, 6.2))
    gs = fig.add_gridspec(2, 2, width_ratios=[1.30, 1], height_ratios=[5, 3],
                          wspace=0.30, hspace=0.82)
    axK = fig.add_subplot(gs[:, 0])
    axV = fig.add_subplot(gs[0, 1])
    axO = fig.add_subplot(gs[1, 1])
    DODGE = {"S0": -0.21, "S1": -0.07, "S2": 0.07, "S3": 0.21}

    def draw(ax, order):
        clean(ax, grid="x")
        xmax = float(piv.loc[order, LEVELS].max().max())
        for yi, b in enumerate(order):
            v = piv.loc[b]
            ax.plot([v[l] for l in LEVELS], [yi + DODGE[l] for l in LEVELS], "-",
                    color=GRID, lw=1.3, zorder=1)
            for lv in LEVELS:
                ax.plot(v[lv], yi + DODGE[lv], LEVELM[lv],
                        ms=7.6 if lv == "S0" else 5.8, color=LEVELC[lv],
                        mec=INK2, mew=0.7, zorder=4)
            d = v["S3"] - v["S0"]
            halo_w(ax.text(float(v[LEVELS].max()) + xmax * 0.035, yi, f"{d:+.1f}",
                           va="center", ha="left", fontsize=7.6, fontweight="bold",
                           color=LEVELC["S3"] if d > 0 else LEVELC["S0"]))
        ax.set_yticks(range(len(order)))
        ax.set_yticklabels([SHORT[b] for b in order], fontsize=8.4)
        ax.set_ylim(len(order) - 0.45, -0.55)
        ax.set_xlim(-xmax * 0.03, xmax * 1.25)
        step = 10 ** int(np.floor(np.log10(max(xmax, 1)))) / (2 if xmax < 50 else 1)
        ticks = np.arange(0, xmax * 1.03, step)
        ax.set_xticks(ticks)
        ax.set_xticklabels([f"{t:.0f}" for t in ticks])
        ax.axvline(0, color=INK3, lw=0.9, zorder=2)

    draw(axK, sorted(KNOWN, key=lambda b: -piv.loc[b, "S0"]))
    draw(axV, sorted(V2REIMPL, key=lambda b: -piv.loc[b, "S0"]))
    draw(axO, sorted(OUTFAM, key=lambda b: -piv.loc[b, "S0"]))

    axK.set_title("KNOWN — the 8 designs the heuristic was fitted on",
                  fontsize=9.6, pad=8)
    axV.set_title("UNKNOWN — 5 v2 re-implementations", fontsize=9.4, pad=8)
    axO.set_title("UNKNOWN — 3 out of family", fontsize=9.4, pad=8)
    for ax in (axK, axV, axO):
        ax.set_xlabel("Fmax gain (% of baseline), mean over runs", fontsize=8.2)

    handles = [Line2D([], [], marker=LEVELM[l], ls="none", ms=7 if l == "S0" else 6,
                      color=LEVELC[l], mec=INK2, mew=0.7, label=LEVELN[l])
               for l in LEVELS]
    fig.legend(handles=handles, loc="upper center", bbox_to_anchor=(0.5, 0.900),
               ncol=len(handles), columnspacing=1.8)
    title_block(fig, "The heuristic's advantage does not survive outside the designs "
                     "it was fitted on",
                "Each group is drawn on its own x-scale — read the axis of the panel you "
                "are looking at, not across panels.\nThe number beside each design is "
                "KB-Guided Exploration minus Fitted Heuristic in percentage points, "
                "coloured for whichever of the two wins.",
                y=0.985, sy=0.940)
    fig.subplots_adjust(top=0.745, bottom=0.115, left=0.125, right=0.982)
    save(fig, "fig6_generalisation", OUT)


# ================================================================ FIGURE 7
def fig2_flow_usage_value_risk():
    y = np.arange(len(FLOWS))
    share = (pd.crosstab(nr.level, nr.flow, normalize="index") * 100).reindex(
        index=LEVELS, columns=FLOWS).fillna(0)

    fig, (axL, axR) = plt.subplots(
        1, 2, figsize=(13.2 * NARROW, 6.2),
        gridspec_kw={"width_ratios": [1.22, 1], "wspace": 0.09})

    clean(axL, grid="x")
    h = 0.19
    for i, lv in enumerate(LEVELS):
        axL.barh(y + (1.5 - i) * h, share.loc[lv].values, height=h * 0.86,
                 color=LEVELC[lv], hatch=LEVELH[lv], ec=WHITE, lw=0.5,
                 label=LEVELN[lv], zorder=3)
        for j, f in enumerate(FLOWS):
            v = share.loc[lv, f]
            if v >= 1.0:
                halo_w(axL.text(v + 0.7, y[j] + (1.5 - i) * h, f"{v:.0f}",
                                va="center", fontsize=6.4, color=INK2,
                                fontweight="bold"))
    axL.set_yticks(y)
    axL.set_yticklabels(FLOWS, fontsize=9.0)
    axL.set_ylim(len(FLOWS) - 0.45, -0.55)
    axL.set_xlim(0, 52)
    axL.set_xlabel("Share of Ablation Level's Attempts, %")
    axL.set_title("Which flows each level chooses", fontsize=10, pad=36)
    axL.legend(loc="lower right", fontsize=7.8)

    clean(axR, grid="x")
    axT = axR.twiny()
    for s in ("right", "left"):
        axT.spines[s].set_visible(False)
    axT.spines["bottom"].set_visible(False)
    axT.spines["top"].set_visible(True)
    axT.spines["top"].set_color(RISKC)
    hb = 0.34
    axR.barh(y - hb / 2, YLD.mean_delta.values, height=hb * 0.88, color=WORTHC,
             ec=WHITE, lw=0.5, zorder=3)
    axT.barh(y + hb / 2, YLD.pct_invalid.values, height=hb * 0.88, color=RISKC,
             ec=WHITE, lw=0.5, zorder=3)
    for j in range(len(FLOWS)):
        halo_w(axR.text(YLD.mean_delta.iloc[j] + 0.8, y[j] - hb / 2,
                        f"{YLD.mean_delta.iloc[j]:.1f}", va="center", fontsize=7.2,
                        color=INK2, fontweight="bold"))
        halo_w(axT.text(YLD.pct_invalid.iloc[j] + 1.4, y[j] + hb / 2,
                        f"{YLD.pct_invalid.iloc[j]:.0f}", va="center", fontsize=7.2,
                        color=INK2, fontweight="bold"))
    for ax in (axR, axT):
        ax.set_ylim(len(FLOWS) - 0.45, -0.55)
    axR.set_yticks(y)
    axR.set_yticklabels([])
    axR.set_xlim(0, 48)
    axT.set_xlim(0, 72)
    axR.set_xlabel("Average Fmax Improvement, MHz", color=WORTHC, fontweight="bold")
    axT.set_xlabel("Attempts Failing Validation, %", color=RISKC, fontweight="bold")
    axT.tick_params(axis="x", colors=RISKC)
    axR.tick_params(axis="x", colors=WORTHC)
    axR.set_title("What each flow is worth, and what it risks", fontsize=10, pad=36)
    axR.legend(handles=[Patch(fc=WORTHC, ec=WHITE,
                              label="worth — mean Fmax gain (MHz, bottom axis)"),
                        Patch(fc=RISKC, ec=WHITE,
                              label="risk — attempts failing validation (%, top axis)")],
               loc="upper center", bbox_to_anchor=(0.5, -0.100), ncol=1, fontsize=7.6)

    title_block(
        fig, "The levels concentrate on flows that are not the most valuable ones",
        "Both panels share a row per flow, ordered by mean Fmax gain — read straight "
        "across from a flow's usage share to its value and its risk.\nThe two right-hand "
        "metrics are in different units (MHz and %), so they are drawn on a genuine dual "
        "axis rather than a shared one.",
        y=0.985, sy=0.938)
    fig.subplots_adjust(top=0.745, bottom=0.160, left=0.148, right=0.982)
    save(fig, "fig7_flow_usage_value_risk", OUT)


# ================================================================ FIGURE 8
def fig3_cost_and_time_log():
    RATE = {"luna": 0.321e-6, "gemini36flash": 1.359e-6, "sonnet5": 3.955e-6}
    sx = runs[runs.level != "S0"].copy()
    sx["cost"] = [r.cost_usd if pd.notna(r.cost_usd)
                  else (r.analysis_tokens or 0) * RATE.get(r.model, np.nan)
                  for r in sx.itertuples()]

    fig, (a1, a2) = plt.subplots(1, 2, figsize=(13.0 * NARROW, 5.9),
                                 gridspec_kw={"width_ratios": [1.0, 1.0],
                                              "wspace": 0.30})
    clean(a1)
    for lv in ["S1", "S2", "S3"]:
        g = sx[sx.level == lv]
        a1.plot(g.cost, g.pct_gain, LEVELM[lv], ms=4.6, color=LEVELC[lv],
                mec=INK2, mew=0.45, ls="none", alpha=0.9)
    YROW = {"luna": -0.065, "gemini36flash": -0.155, "sonnet5": -0.065}
    for m in ["luna", "gemini36flash", "sonnet5"]:
        med = sx[sx.model == m].cost.median()
        a1.axvline(med, color=INK3, lw=0.8, ls=(0, (4, 3)), zorder=1)
        halo_w(a1.text(med, YROW[m], f"{MODELN[m]}  ·  median ${med:.2f}",
                       transform=a1.get_xaxis_transform(),
                       ha="center", fontsize=6.6, color=INK2, fontweight="bold",
                       va="top"))
    a1.set_xscale("log")
    a1.set_xlabel("language-model cost per run, US$ (log scale)", labelpad=44)
    a1.set_ylabel("Fmax gain (% of baseline)")
    a1.set_ylim(-8, 128)
    a1.set_title("Every model-driven run: what it cost, what it returned",
                 fontsize=9.6, pad=8)
    a1.legend(handles=[Line2D([], [], marker=LEVELM[l], ls="none", ms=5.6,
                              color=LEVELC[l], mec=INK2, mew=0.5, label=LEVELN[l])
                       for l in ["S1", "S2", "S3"]]
              + [Line2D([], [], ls=(0, (4, 3)), color=INK3, lw=0.8,
                        label="model median cost")],
              loc="upper left", ncol=1, fontsize=7.0)

    clean(a2)
    m = runs.groupby("level")[PHASE_ORDER].mean().reindex(LEVELS).fillna(0.0) / 60.0
    x = np.arange(4)
    bot = np.zeros(4)
    for c in PHASE_ORDER:
        a2.bar(x - 0.17, m[c].values, bottom=bot, width=0.30, color=PHASEC[c],
               label=PHASEN[c], hatch=PHASEH[c], ec=INK2, lw=0.5, zorder=3)
        bot = bot + m[c].values
    data = [runs[runs.level == lv].wall_min.dropna().values for lv in LEVELS]
    a2.boxplot(data, positions=x + 0.17, widths=0.24, whis=(0, 100), showfliers=False,
               patch_artist=True, zorder=5,
               medianprops=dict(color=INK, lw=1.4),
               boxprops=dict(facecolor=WHITE, ec=INK2, lw=0.9),
               whiskerprops=dict(color=INK2, lw=0.9),
               capprops=dict(color=INK2, lw=0.9))
    a2.set_yscale("log")
    ticks = [1, 2, 5, 10, 20, 50, 100, 200, 500]
    a2.yaxis.set_major_locator(FixedLocator(ticks))
    a2.yaxis.set_minor_locator(FixedLocator([]))
    a2.yaxis.set_major_formatter(FixedFormatter([str(t) for t in ticks]))
    a2.set_ylim(1.0, 700)
    a2.set_ylabel("minutes per run (log₁₀ scale, raw minutes)")
    a2.set_xticks(x)
    # Level names only, broken onto two lines: four single-line names collide
    # edge-to-edge in this panel width.
    a2.set_xticklabels([LEVELN[lv].replace(" ", "\n", 1) for lv in LEVELS],
                       fontsize=7.6, color=INK, linespacing=1.15)
    a2.tick_params(axis="x", length=0, pad=5)
    a2.set_xlim(-0.6, 3.7)
    a2.set_title("Bars: Share of Total Phase Time\n"
                 "Boxes: Elapsed Wall-Clock Time (not additive)",
                 fontsize=8.4, pad=10, linespacing=1.35)
    # Key in two groups: the four stacked categories are titled "Phase time";
    # the box-and-whisker entry sits beside them as its own group.
    leg_phase = a2.legend(
        handles=[Patch(fc=PHASEC[c], hatch=PHASEH[c], ec=INK2, label=PHASEN[c])
                 for c in PHASE_ORDER],
        title="Phase time (bars)", loc="upper left", bbox_to_anchor=(-0.02, -0.135),
        ncol=1, fontsize=6.8, title_fontproperties={"weight": "bold", "size": 7.0},
        alignment="left")
    a2.add_artist(leg_phase)
    a2.legend(
        handles=[Patch(fc=WHITE, ec=INK2,
                       label="wall time (box = IQR,\nwhiskers = min/max)")],
        title="Wall time (boxes)", loc="upper left", bbox_to_anchor=(0.50, -0.135),
        ncol=1, fontsize=6.8, title_fontproperties={"weight": "bold", "size": 7.0},
        alignment="left")

    title_block(
        fig, "Fmax gain against spend, and where the time actually goes",
        "Cost uses the supplied measured/reconstructed fields; historical reconstruction "
        "accuracy is not re-certified by this redraw.\nPhase time and wall time are NOT "
        "combined: concurrent attempts can make summed phase time exceed elapsed time.\n"
        "Right panel is log₁₀ — segment lengths are not proportional to share; read the "
        "printed axis values.",
        y=0.985, sy=0.928)
    fig.subplots_adjust(top=0.775, bottom=0.300, left=0.082, right=0.985)
    save(fig, "fig8_cost_and_time_log", OUT)


if __name__ == "__main__":
    os.makedirs(OUT, exist_ok=True)
    fig1_generalisation()
    fig2_flow_usage_value_risk()
    fig3_cost_and_time_log()
    print("\nround-6 figures written to", OUT)
