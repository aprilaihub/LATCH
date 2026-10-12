#!/usr/bin/env python3
"""Task 3 step 1: restore even coverage over slices the first pass under-examined."""
import os
import numpy as np
import pandas as pd
from itertools import combinations

_PA_ROOT = os.environ.get("PAPER_ANALYSIS_ROOT")
if not _PA_ROOT:
    raise SystemExit("PAPER_ANALYSIS_ROOT must be set by the processing entrypoint")

pd.set_option("display.width", 240)
pd.set_option("display.max_columns", 80)
pd.set_option("display.max_rows", 400)
D = os.path.expanduser(_PA_ROOT + "/data")
runs = pd.read_csv(D + "/runs_enriched.csv")
nodes = pd.read_csv(D + "/nodes.csv")
H = lambda t: print("\n" + "=" * 104 + f"\n{t}\n" + "=" * 104)

# richer benchmark taxonomy
GRP = {"amd_mini-isp_v2": "v2-seen", "boom_soc_v2": "v2-seen", "vexriscv_re-place_v2": "v2-seen",
       "rosetta_3d-rendering_v2": "v2-seen", "vtr_mcml_v2": "v2-seen",
       "fir_symmetric_systolic": "out-of-family", "fir_systolic_transposed": "out-of-family"}
runs["ugroup"] = runs.benchmark.map(GRP).fillna("known")

H("C1. UNKNOWN SET, SPLIT BY KIND — v2 re-implementation vs genuinely new design")
print(runs.groupby(["level", "ugroup"]).agg(
    n=("pct_gain", "size"), mean_pct=("pct_gain", "mean"), median_pct=("pct_gain", "median"),
    sd=("pct_gain", "std"), mean_mhz=("delta_mhz", "mean")).round(2).to_string())
print("\nper-benchmark within the unknown set:")
u = runs[runs.ugroup != "known"]
print(u.pivot_table(index=["ugroup", "benchmark"], columns="level", values="pct_gain",
                    aggfunc="mean").round(2).to_string())

H("C2. S0 vs EACH LEVEL, PAIRED PER BENCHMARK (the only valid way to include S0)")
piv = runs.pivot_table(index="benchmark", columns="level", values="pct_gain", aggfunc="mean")
piv["study"] = runs.groupby("benchmark").study.first()
piv = piv[["study", "S0", "S1", "S2", "S3"]]
print(piv.round(2).sort_values(["study", "S0"], ascending=[True, False]).to_string())
print("\npaired sign test vs S0 (benchmark-level means, n=15):")
for lv in ["S1", "S2", "S3"]:
    d = piv[lv] - piv["S0"]
    print(f"  {lv} - S0 : better {int((d>1e-9).sum()):2d}  worse {int((d<-1e-9).sum()):2d}  "
          f"tied {int((d.abs()<=1e-9).sum()):2d}   mean diff {d.mean():+6.2f} pp   median {d.median():+6.2f} pp")
print("\nsplit known / unknown:")
for st in ["known", "unknown"]:
    p = piv[piv.study == st]
    print(f"  --- {st} (n={len(p)}) ---")
    for lv in ["S1", "S2", "S3"]:
        d = p[lv] - p["S0"]
        print(f"    {lv} - S0 : better {int((d>1e-9).sum())}  worse {int((d<-1e-9).sum())}  "
              f"tied {int((d.abs()<=1e-9).sum())}   mean {d.mean():+6.2f} pp")
print("\npaired ladder tests S1->S2->S3 (per benchmark x model x repeat, n=75 each):")
key = ["benchmark", "model", "repeat"]
w = runs[runs.level != "S0"].pivot_table(index=key, columns="level", values="pct_gain")
for a, b in [("S1", "S2"), ("S2", "S3"), ("S1", "S3")]:
    d = (w[b] - w[a]).dropna()
    from scipy import stats
    try:
        st_ = stats.wilcoxon(d[d != 0]) if (d != 0).sum() > 0 else None
        pv = f"{st_.pvalue:.2e}" if st_ else "n/a"
    except Exception:
        pv = "n/a"
    print(f"  {b} > {a}: {int((d>1e-9).sum()):2d} wins, {int((d<-1e-9).sum()):2d} losses, "
          f"{int((d.abs()<=1e-9).sum()):2d} ties | mean {d.mean():+6.2f} pp | median {d.median():+6.2f} pp "
          f"| wilcoxon p={pv}")

H("C3. PARTICIPANT CONFOUND — is any level effect driven by who ran it?")
print("mean % gain by participant x level (each participant has a disjoint benchmark set):")
print(runs.pivot_table(index="participant", columns="level", values="pct_gain",
                       aggfunc=["mean", "size"]).round(2).to_string())
print("\nwithin-participant ladder (S3 - S1), paired by benchmark/model/repeat:")
for p in ["cristian", "matt", "shu"]:
    sub = runs[(runs.participant == p) & (runs.level != "S0")]
    ww = sub.pivot_table(index=key, columns="level", values="pct_gain")
    d = (ww["S3"] - ww["S1"]).dropna()
    print(f"  {p:9s} n={len(d):2d}  mean {d.mean():+6.2f} pp  wins {int((d>0).sum())}  losses {int((d<0).sum())}")

H("C4. SECONDARY OUTCOME — worst negative slack (WNS)")
runs["wns_gain"] = runs.best_wns_ns - runs.root_wns_ns
print(runs.groupby("level").wns_gain.describe().round(3).to_string())
print("\ncorrelation between WNS gain and Fmax gain:")
print("  pearson  =", round(runs[["wns_gain", "delta_mhz"]].corr().iloc[0, 1], 3))
print("  spearman =", round(runs[["wns_gain", "pct_gain"]].corr(method="spearman").iloc[0, 1], 3))
print("\nruns where WNS improved but Fmax did not, and vice versa:")
print(pd.crosstab(runs.level, [runs.wns_gain > 0.001, runs.pct_gain > 1.0]).to_string())

H("C5. TIME — phase composition and the partition problem")
for c in ["analysis_s", "diagnostic_s", "exec_productive_s", "exec_failed_s", "verification_s"]:
    runs[c + "_min"] = runs[c] / 60
cols = [c + "_min" for c in ["analysis_s", "diagnostic_s", "exec_productive_s",
                             "exec_failed_s", "verification_s"]]
print("mean minutes in each phase, by level (these do NOT partition wall time at S2/S3):")
t = runs.groupby("level")[cols + ["wall_min"]].mean().round(1)
t["phase_sum"] = t[cols[:-1]].sum(axis=1)
t["sum/wall"] = (t.phase_sum / t.wall_min).round(2)
print(t.to_string())
print("\nshare of PHASE TIME (not wall time) in each phase — safe to compare across levels:")
sh = runs.groupby("level")[cols[:-1]].mean()
print(sh.div(sh.sum(axis=1), axis=0).round(3).to_string())
print("\nLLM analysis time as a share of phase time, by level x model:")
sh2 = runs[runs.level != "S0"].groupby(["level", "model"])[cols[:-1]].mean()
print(sh2.div(sh2.sum(axis=1), axis=0).round(3).to_string())
print("\nEDA (Vivado) time dominates: exec_productive + diagnostic vs analysis:")
runs["eda_min"] = runs.exec_productive_s_min + runs.diagnostic_s_min
runs["llm_share"] = runs.analysis_s_min / (runs.eda_min + runs.analysis_s_min)
print(runs.groupby(["level"]).llm_share.describe().round(3).to_string())
print("\nverification (end-of-run equivalence gate) is reported separately:")
print(runs.groupby("level").verification_s_min.describe().round(2).to_string())

H("C6. COST-EFFECTIVENESS — reconstructed cost per benchmark-run and per MHz")
RATE = {"luna": 0.321e-6, "gemini36flash": 1.359e-6, "sonnet5": 3.955e-6}   # $/token, fitted on MT S1+S2
runs["cost_recon"] = runs.apply(
    lambda r: r.analysis_tokens * RATE.get(r.model, np.nan) if pd.notna(r.analysis_tokens) else np.nan, axis=1)
runs["cost_used"] = runs.cost_usd.where(runs.cost_usd.notna(), runs.cost_recon)
runs["cost_is_measured"] = runs.cost_usd.notna()
print("cost ($) by level x model — MEASURED (MT) vs RECONSTRUCTED (CR, SH):")
print(runs[runs.level != "S0"].groupby(["level", "model", "cost_is_measured"])
      .cost_used.agg(["count", "mean", "median", "min", "max"]).round(4).to_string())
print("\ntotal reconstructed spend of the whole S1-S3 campaign, by level x model ($):")
print(runs[runs.level != "S0"].pivot_table(index="level", columns="model",
                                           values="cost_used", aggfunc="sum").round(2).to_string())
print(f"\ngrand total (measured + reconstructed): ${runs.cost_used.sum():.2f}")
print("\ndollars per MHz gained (runs with >1 MHz gain only):")
g = runs[(runs.delta_mhz > 1) & runs.cost_used.notna()].copy()
g["usd_per_mhz"] = g.cost_used / g.delta_mhz
print(g.groupby(["level", "model"]).usd_per_mhz.agg(["count", "median", "mean"]).round(4).to_string())
print("\ndollars per percentage-point of Fmax gain:")
g["usd_per_pp"] = g.cost_used / g.pct_gain
print(g.groupby(["level", "model"]).usd_per_pp.agg(["count", "median"]).round(4).to_string())
print("\nS0 costs $0 in LLM terms; its compute cost is wall time only:")
print(runs[runs.level == "S0"].wall_min.describe().round(1).to_string())

H("C7. MODEL x LEVEL INTERACTION — is the ladder the same for every model?")
mm = runs[runs.level != "S0"].pivot_table(index="model", columns="level", values="pct_gain",
                                          aggfunc="mean").round(2)
mm["S2-S1"] = (mm.S2 - mm.S1).round(2)
mm["S3-S2"] = (mm.S3 - mm.S2).round(2)
mm["S3-S1"] = (mm.S3 - mm.S1).round(2)
print(mm.to_string())
print("\npaired within model (benchmark x repeat):")
for m in ["luna", "sonnet5", "gemini36flash"]:
    sub = runs[(runs.model == m)]
    ww = sub.pivot_table(index=["benchmark", "repeat"], columns="level", values="pct_gain")
    print(f"  --- {m} ---")
    for a, b in [("S1", "S2"), ("S2", "S3")]:
        d = (ww[b] - ww[a]).dropna()
        print(f"    {b}-{a}: n={len(d):2d} mean {d.mean():+6.2f} pp  wins {int((d>1e-9).sum()):2d} "
              f"losses {int((d<-1e-9).sum()):2d} ties {int((d.abs()<=1e-9).sum()):2d}")

H("C8. WHAT DESIGN CHARACTERISTICS PREDICT SUCCESS?")
b = runs[runs.level != "S0"].groupby("benchmark").agg(
    size_mb=("size_mb", "first"), luts=("luts", "first"),
    root_fmax=("root_fmax_mhz", "first"), root_wns=("root_wns_ns", "first"),
    fail_ep=("failing_endpoints", "first"), study=("study", "first"),
    mean_pct=("pct_gain", "mean"), mean_mhz=("delta_mhz", "mean"))
b["log_size"] = np.log10(b.size_mb)
print(b.round(2).sort_values("mean_pct", ascending=False).to_string())
print("\ncorrelations with mean % gain (n=15 benchmarks):")
for c in ["size_mb", "log_size", "luts", "root_fmax", "root_wns", "fail_ep"]:
    sub = b[[c, "mean_pct"]].dropna()
    print(f"  {c:12s} pearson {sub.corr().iloc[0,1]:+.3f}   spearman "
          f"{sub.corr(method='spearman').iloc[0,1]:+.3f}   (n={len(sub)})")
print("\nheadroom hypothesis: gain vs baseline Fmax (low-Fmax designs have more to gain)")
print(b[["root_fmax", "mean_pct", "mean_mhz"]].sort_values("root_fmax").round(2).to_string())

runs.to_csv(D + "/runs_enriched.csv", index=False)
print("\n[updated runs_enriched.csv with cost_used, ugroup, phase minutes]")
