#!/usr/bin/env python3
"""Task 2: first-pass DESCRIPTIVE analysis. No conclusions, just distributions."""
import os
import numpy as np
import pandas as pd

_PA_ROOT = os.environ.get("PAPER_ANALYSIS_ROOT")
if not _PA_ROOT:
    raise SystemExit("PAPER_ANALYSIS_ROOT must be set by the processing entrypoint")

pd.set_option("display.width", 240)
pd.set_option("display.max_columns", 80)
pd.set_option("display.max_rows", 500)
D = os.path.expanduser(_PA_ROOT + "/data")
runs = pd.read_csv(D + "/runs.csv")
nodes = pd.read_csv(D + "/nodes.csv")

runs["pct_gain"] = 100.0 * runs.delta_mhz / runs.root_fmax_mhz
runs["clean_frac"] = runs.val_clean / (runs.val_clean + runs.val_warning + runs.val_invalid)
runs["invalid_frac"] = runs.val_invalid / (runs.val_clean + runs.val_warning + runs.val_invalid)
runs["eq_ok"] = runs.eq_status.eq("passed")
runs["improved"] = runs.delta_mhz > 1e-6
runs["wall_min"] = runs.wall_s / 60.0
LEV = ["S0", "S1", "S2", "S3"]
sx = runs[runs.level != "S0"].copy()


def desc(df, by, col):
    g = df.groupby(by)[col]
    out = g.agg(n="count", mean="mean", sd="std", min="min",
                q25=lambda s: s.quantile(.25), median="median",
                q75=lambda s: s.quantile(.75), max="max")
    return out.round(2)


H = lambda t: print("\n" + "=" * 104 + f"\n{t}\n" + "=" * 104)

H("2.1a  PRIMARY OUTCOME — Fmax improvement (MHz), by level")
print(desc(runs, "level", "delta_mhz").to_string())
H("2.1b  PRIMARY OUTCOME — Fmax improvement (% of baseline), by level")
print(desc(runs, "level", "pct_gain").to_string())

H("2.1c  by level x model  (MHz)")
print(desc(runs, ["level", "model"], "delta_mhz").to_string())
H("2.1d  by level x model  (% gain)")
print(desc(runs, ["level", "model"], "pct_gain").to_string())

H("2.1e  by level x known/unknown  (MHz then % gain)")
print(desc(runs, ["level", "study"], "delta_mhz").to_string())
print()
print(desc(runs, ["level", "study"], "pct_gain").to_string())

H("2.1f  PER-BENCHMARK MATRIX — mean Fmax gain (MHz) by level")
piv = runs.pivot_table(index=["study", "benchmark"], columns="level",
                       values="delta_mhz", aggfunc="mean").reindex(columns=LEV)
piv["root_MHz"] = runs.groupby("benchmark").root_fmax_mhz.first().reindex(
    piv.index.get_level_values(1)).values
print(piv.round(2).to_string())

H("2.1g  PER-BENCHMARK MATRIX — spread of Fmax gain across the 5 runs in each level (sd MHz)")
piv2 = runs[runs.level != "S0"].pivot_table(index=["study", "benchmark"], columns="level",
                                            values="delta_mhz", aggfunc="std")
print(piv2.round(2).to_string())

H("2.1h  WITHIN-MODEL VARIANCE — luna 3 repeats only (sd across repeats)")
lu = runs[(runs.model == "luna")]
rv = lu.groupby(["benchmark", "level"]).delta_mhz.agg(["mean", "std", "min", "max"])
rv["range"] = rv["max"] - rv["min"]
print(rv.round(2).to_string())
print("\nsummary of within-luna repeat sd by level:")
print(rv.reset_index().groupby("level")["std"].describe().round(2).to_string())
print("\nsummary of within-luna repeat RANGE by level:")
print(rv.reset_index().groupby("level")["range"].describe().round(2).to_string())

H("2.2a  VALIDATION SUCCESS")
print("run-level equivalence gate:")
print(pd.crosstab(runs.level, runs.eq_status).to_string())
print("\nnode-level clean fraction, by level:")
print(desc(runs, "level", "clean_frac").to_string())
print("\nnode-level invalid fraction, by level:")
print(desc(runs, "level", "invalid_frac").to_string())
print("\nnode-level validation status by level x model (share of non-root nodes):")
nr = nodes[nodes.flow != "root"]
print(pd.crosstab([nr.level, nr.model], nr.validation_status, normalize="index").round(3).to_string())
print("\nruns achieving ANY improvement (delta>0):")
print(runs.groupby("level").improved.agg(["sum", "count", "mean"]).round(3).to_string())
print("\nby level x study:")
print(runs.groupby(["level", "study"]).improved.agg(["sum", "count", "mean"]).round(3).to_string())

H("2.2b  WALL-CLOCK (minutes)")
print(desc(runs, "level", "wall_min").to_string())
print()
print(desc(runs, ["level", "model"], "wall_min").to_string())
print("\nwall time is dominated by design size — per-benchmark medians (min):")
wp = runs.pivot_table(index=["benchmark"], columns="level", values="wall_min",
                      aggfunc="median").reindex(columns=LEV)
wp["size_MB"] = runs.groupby("benchmark").size_mb.first()
print(wp.round(1).sort_values("size_MB").to_string())

H("2.2c  TOKENS")
print(desc(sx, "level", "analysis_tokens").to_string())
print()
print(desc(sx, ["level", "model"], "analysis_tokens").to_string())
print("\nLLM calls:")
print(desc(sx, ["level", "model"], "analysis_llm_calls").to_string())
print("\ntokens per LLM call:")
sx["tok_per_call"] = sx.analysis_tokens / sx.analysis_llm_calls
print(desc(sx, ["level", "model"], "tok_per_call").to_string())

H("2.3  S1 CHAIN LENGTH — the early-termination issue")
s1 = runs[runs.level == "S1"].copy()
print("S1 nodes_total distribution:")
print(s1.nodes_total.value_counts().sort_index().to_string())
print("\nS1 chain length by model:")
print(desc(s1, "model", "nodes_total").to_string())
print("\nS1 stop_reason x chain length:")
print(pd.crosstab(s1.nodes_total, s1.stop_reason).to_string())
print("\nS1 outcome vs chain length (correlation):")
print("  pearson r(nodes_total, delta_mhz) =",
      round(s1[["nodes_total", "delta_mhz"]].corr().iloc[0, 1], 3))
print("  pearson r(nodes_total, pct_gain)  =",
      round(s1[["nodes_total", "pct_gain"]].corr().iloc[0, 1], 3))
print("  spearman r(nodes_total, pct_gain) =",
      round(s1[["nodes_total", "pct_gain"]].corr(method="spearman").iloc[0, 1], 3))
print("\nS1 grouped by chain length:")
print(s1.assign(bin=pd.cut(s1.nodes_total, [0, 4, 8, 12], labels=["3-4", "5-8", "9-12"]))
      .groupby("bin", observed=True)[["delta_mhz", "pct_gain", "wall_s", "analysis_tokens"]]
      .agg(["count", "mean"]).round(2).to_string())

print("\n--- NORMALISING FOR WORK DONE: gain per EDA run ---")
runs["gain_per_eda"] = runs.delta_mhz / runs.eda_runs
runs["pctgain_per_eda"] = runs.pct_gain / runs.eda_runs
print(desc(runs, "level", "gain_per_eda").to_string())
print()
print(desc(runs, "level", "pctgain_per_eda").to_string())

print("\n--- MATCHED-BUDGET COMPARISON: restrict S2/S3 to their first k nodes, k = the "
      "paired S1 chain length ---")
# For each benchmark/model/repeat, compare S1 full chain vs S2/S3 truncated to same #EDA runs
nn = nodes[nodes.flow != "root"].copy()
nn["run_key"] = list(zip(nn.benchmark, nn.level, nn.model, nn.repeat))
nn["order"] = nn.node_id.str.extract(r"node_(\d+)").astype(int)
best_at_k = {}
for k, grp in nn.groupby(["benchmark", "level", "model", "repeat"]):
    grp = grp.sort_values("order")
    valid = grp[(grp.validation_valid == True)]
    cum = []
    run = -np.inf
    for _, r in grp.iterrows():
        d = r.delta_mhz if (r.validation_valid == True and pd.notna(r.delta_mhz)) else -np.inf
        run = max(run, d)
        cum.append(max(run, 0.0))
    best_at_k[k] = cum
rows = []
for (b, lv, m, rp), cum in best_at_k.items():
    if lv == "S1":
        continue
    s1row = s1[(s1.benchmark == b) & (s1.model == m) & (s1.repeat == rp)]
    if not len(s1row):
        continue
    k = int(s1row.eda_runs.iloc[0])
    trunc = cum[min(k, len(cum)) - 1] if cum else 0.0
    rows.append(dict(benchmark=b, level=lv, model=m, repeat=rp, k_eda=k,
                     s1_delta=float(s1row.delta_mhz.iloc[0]),
                     trunc_delta=trunc, full_delta=cum[-1] if cum else 0.0,
                     root=float(s1row.root_fmax_mhz.iloc[0])))
mb = pd.DataFrame(rows)
mb["s1_pct"] = 100 * mb.s1_delta / mb.root
mb["trunc_pct"] = 100 * mb.trunc_delta / mb.root
mb["full_pct"] = 100 * mb.full_delta / mb.root
print(mb.groupby("level")[["k_eda", "s1_delta", "trunc_delta", "full_delta",
                           "s1_pct", "trunc_pct", "full_pct"]].mean().round(2).to_string())
print("\npaired wins (same benchmark/model/repeat), matched budget:")
for lv in ["S2", "S3"]:
    s = mb[mb.level == lv]
    print(f"  {lv} vs S1 @ matched budget: {lv} better in {(s.trunc_pct > s.s1_pct + 1e-9).sum()}/{len(s)}, "
          f"S1 better in {(s.s1_pct > s.trunc_pct + 1e-9).sum()}, tied {(abs(s.s1_pct - s.trunc_pct) < 1e-9).sum()}")
    print(f"  {lv} vs S1 @ full budget:    {lv} better in {(s.full_pct > s.s1_pct + 1e-9).sum()}/{len(s)}, "
          f"S1 better in {(s.s1_pct > s.full_pct + 1e-9).sum()}, tied {(abs(s.s1_pct - s.full_pct) < 1e-9).sum()}")
mb.to_csv(D + "/matched_budget.csv", index=False)

H("2.4  COST RECONSTRUCTION VALIDATION (measured = MT only)")
mt = runs[(runs.participant == "matt") & (runs.level != "S0")].copy()
mt["out_frac"] = mt.completion_tokens / mt.analysis_tokens
print("MT measured cost by level x model ($):")
print(desc(mt, ["level", "model"], "cost_usd").to_string())
print("\nimplied blended $ per 1M total tokens (measured cost / tokens):")
mt["rate_per_Mtok"] = 1e6 * mt.cost_usd / mt.analysis_tokens
print(desc(mt, ["level", "model"], "rate_per_Mtok").to_string())
print("\ninput/output token split (output share of total):")
print(desc(mt, ["level", "model"], "out_frac").to_string())

print("\n--- Method A: single blended rate per model, fitted on that model's S1+S2 runs, "
      "applied to all levels ---")
res = []
for m, g in mt.groupby("model"):
    fit = g[g.level.isin(["S1", "S2"])]
    rate = fit.cost_usd.sum() / fit.analysis_tokens.sum()      # $ per token
    g = g.assign(pred=g.analysis_tokens * rate)
    for lv, gg in g.groupby("level"):
        res.append(dict(model=m, level=lv, n=len(gg),
                        measured=gg.cost_usd.mean(), predicted=gg.pred.mean(),
                        err_pct=100 * (gg.pred.mean() - gg.cost_usd.mean()) / gg.cost_usd.mean(),
                        mape=100 * ((gg.pred - gg.cost_usd).abs() / gg.cost_usd).mean()))
A = pd.DataFrame(res)
print(A.round(3).to_string(index=False))

print("\n--- Method B: separate input/output rates per model, fitted by least squares on "
      "S1+S2, applied to all levels ---")
res = []
fitted_rates = {}
for m, g in mt.groupby("model"):
    fit = g[g.level.isin(["S1", "S2"])]
    X = np.column_stack([fit.prompt_tokens.values, fit.completion_tokens.values])
    y = fit.cost_usd.values
    coef, *_ = np.linalg.lstsq(X, y, rcond=None)
    fitted_rates[m] = coef
    pred = g.prompt_tokens.values * coef[0] + g.completion_tokens.values * coef[1]
    g = g.assign(pred=pred)
    for lv, gg in g.groupby("level"):
        res.append(dict(model=m, level=lv, n=len(gg),
                        measured=gg.cost_usd.mean(), predicted=gg.pred.mean(),
                        err_pct=100 * (gg.pred.mean() - gg.cost_usd.mean()) / gg.cost_usd.mean(),
                        mape=100 * ((gg.pred - gg.cost_usd).abs() / gg.cost_usd).mean()))
B = pd.DataFrame(res)
print(B.round(3).to_string(index=False))
print("\nfitted $/1M tokens (input, output) per model:")
for m, c in fitted_rates.items():
    print(f"  {m:14s} in={1e6*c[0]:8.3f}  out={1e6*c[1]:8.3f}  ratio out/in={c[1]/c[0]:.2f}")

print("\n--- The S3 divergence, isolated ---")
for m, g in mt.groupby("model"):
    s12 = g[g.level.isin(["S1", "S2"])]
    s3 = g[g.level == "S3"]
    r12 = 1e6 * s12.cost_usd.sum() / s12.analysis_tokens.sum()
    r3 = 1e6 * s3.cost_usd.sum() / s3.analysis_tokens.sum()
    print(f"  {m:14s} blended $/1M tok: S1+S2 = {r12:7.3f}   S3 = {r3:7.3f}   "
          f"S3 is {100*(r3/r12-1):+6.1f}%   out-share S1+S2={s12.completion_tokens.sum()/s12.analysis_tokens.sum():.3f} "
          f"S3={s3.completion_tokens.sum()/s3.analysis_tokens.sum():.3f}")

print("\n--- Prompt size per call: the reference-manual overhead ---")
sx["prompt_per_call"] = sx.prompt_tokens / sx.n_loop_decisions
print(desc(sx, ["model", "level"], "prompt_per_call").to_string())

H("2.5  ANOMALY SCAN")
print("runs with delta == 0 (no improvement published):")
z = runs[runs.delta_mhz.abs() < 1e-6]
print(pd.crosstab(z.level, z.benchmark).to_string() if len(z) else "  none")
print("\nbenchmarks by mean pct_gain across S1-S3 (identifying outliers):")
bm = sx.groupby(["study", "benchmark"]).pct_gain.agg(["count", "mean", "std", "min", "max"])
print(bm.round(2).sort_values("mean").to_string())
print("\nequivalence-gate FAILURES (final published DCP failed the full check):")
f = runs[runs.eq_status == "failed"]
print(f[["participant", "benchmark", "level", "model", "repeat", "delta_mhz",
         "eq_phase1", "eq_phase2", "eq_infra_failure", "best_flow_sequence"]].to_string(index=False))
print("\nruns where load_per_core peaked above the contention threshold (0.7):")
c = runs[runs.any_sample_contended == True]
print(pd.crosstab(c.participant, c.level).to_string())
print("\ndoes contention associate with longer wall time? (within benchmark z-score)")
runs["wall_z"] = runs.groupby("benchmark").wall_min.transform(lambda s: (s - s.mean()) / (s.std() or 1))
print(runs.groupby(runs.any_sample_contended)["wall_z"].agg(["count", "mean", "std"]).round(3).to_string())

runs.to_csv(D + "/runs_enriched.csv", index=False)
print("\n[wrote runs_enriched.csv and matched_budget.csv]")
