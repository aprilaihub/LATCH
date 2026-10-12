#!/usr/bin/env python3
"""Task 2/3: cost semantics, mechanism, trajectory, anomaly follow-ups."""
import glob
import json
import os
import numpy as np
import pandas as pd

_PA_ROOT = os.environ.get("PAPER_ANALYSIS_ROOT")
RAW_ROOT = os.environ.get("ABLATION_RAW_ROOT")
if not _PA_ROOT or not RAW_ROOT:
    raise SystemExit("PAPER_ANALYSIS_ROOT and ABLATION_RAW_ROOT must be set by the processing entrypoint")

pd.set_option("display.width", 240)
pd.set_option("display.max_columns", 80)
pd.set_option("display.max_rows", 500)
D = os.path.expanduser(_PA_ROOT + "/data")
runs = pd.read_csv(D + "/runs_enriched.csv")
nodes = pd.read_csv(D + "/nodes.csv")
diag = pd.read_csv(D + "/diagnoses.csv")
cand = pd.read_csv(D + "/polcand.csv")
H = lambda t: print("\n" + "=" * 104 + f"\n{t}\n" + "=" * 104)

H("A. WHAT IS 'MEASURED' COST? per-analysis-session rate constancy (MT runs only)")
rows = []
for p in glob.glob(os.path.join(RAW_ROOT, "*/*/*/repeat_[0-9]/analysis/analysis_events.jsonl")):
    parts = os.path.relpath(p, RAW_ROOT).split(os.sep)
    bench, lev, mod, rep = parts[0], parts[1], parts[2], parts[3]
    prev = 0
    for line in open(p):
        try:
            e = json.loads(line)
        except Exception:
            continue
        tok = e.get("tokens")
        cost = e.get("cost_usd")
        if tok is None or cost is None:
            continue
        rows.append(dict(benchmark=bench, level=lev, model=mod, repeat=rep,
                         session=e.get("session_id"), tokens=tok, cost=cost,
                         d_tok=tok - prev, llm_calls=e.get("llm_calls")))
        prev = tok
S = pd.DataFrame(rows)
print("analysis sessions with cost:", len(S))
S["rate"] = 1e6 * S.cost / S.tokens
print("\nper-session $/1M (cumulative tokens basis) — by model x level:")
print(S.groupby(["model", "level"]).rate.agg(["count", "mean", "std", "min", "max"]).round(4).to_string())
print("""
NOTE: 'tokens' in analysis_events is CUMULATIVE, and 'cost_usd' is the session's own cost.
Recompute the incremental rate properly:""")
S["inc_rate"] = 1e6 * S.cost / S.d_tok.replace(0, np.nan)
print(S.groupby(["model", "level"]).inc_rate.agg(["count", "mean", "std", "min", "max"]).round(4).to_string())
print("\nIf inc_rate is constant within a model, 'measured' cost is itself rate x tokens.")
for m, g in S.groupby("model"):
    cv = g.inc_rate.std() / g.inc_rate.mean()
    print(f"  {m:14s} mean {g.inc_rate.mean():.4f}  sd {g.inc_rate.std():.4f}  CV {cv:.4f}  "
          f"min {g.inc_rate.min():.4f} max {g.inc_rate.max():.4f}")
print("\ninc_rate by level, to see if S3 shifts:")
print(S.groupby(["model", "level"]).inc_rate.median().unstack().round(4).to_string())

H("B. WHY DOES S1 STOP EARLY?  (NO_VALID_CANDIDATE)")
s1 = runs[runs.level == "S1"].copy()
s1["early"] = s1.stop_reason == "NO_VALID_CANDIDATE"
print("early-stop rate by model:")
print(s1.groupby("model").early.agg(["sum", "count", "mean"]).round(3).to_string())
print("\nearly-stop rate by benchmark:")
print(s1.groupby(["study", "benchmark"]).early.agg(["sum", "count", "mean"])
      .sort_values("mean", ascending=False).round(3).to_string())
n1 = nodes[(nodes.level == "S1") & (nodes.flow != "root")]
print("\nvalidation status of the LAST node in early-stopping S1 chains:")
last = n1.sort_values("node_id").groupby(["benchmark", "model", "repeat"]).tail(1)
last = last.merge(s1[["benchmark", "model", "repeat", "early"]],
                  on=["benchmark", "model", "repeat"], how="left")
print(pd.crosstab(last.early, last.validation_status).to_string())
print("\nnode validation profile in early vs full S1 chains:")
nm = n1.merge(s1[["benchmark", "model", "repeat", "early"]], on=["benchmark", "model", "repeat"])
print(pd.crosstab(nm.early, nm.validation_status, normalize="index").round(3).to_string())
print("\nS1 chains: is the run stopped because ALL 9 flows were tried and failed, or fewer?")
nflows = n1.groupby(["benchmark", "model", "repeat"]).flow.nunique().rename("uniq_flows")
nn = n1.groupby(["benchmark", "model", "repeat"]).size().rename("n_nodes")
tmp = pd.concat([nflows, nn], axis=1).reset_index().merge(
    s1[["benchmark", "model", "repeat", "early"]], on=["benchmark", "model", "repeat"])
print(tmp.groupby("early")[["uniq_flows", "n_nodes"]].agg(["mean", "min", "max"]).round(2).to_string())

H("C. FLOW SELECTION — what does each level actually choose?")
nr = nodes[nodes.flow != "root"]
print("share of applied flows, by level:")
print(pd.crosstab(nr.level, nr.flow, normalize="index").round(3).to_string())
print("\ndistinct flows used per run, by level:")
print(nr.groupby(["level", "benchmark", "model", "repeat"]).flow.nunique()
      .reset_index().groupby("level").flow.describe().round(2).to_string())
print("\nshare of applied flows, by level x model:")
print(pd.crosstab([nr.level, nr.model], nr.flow, normalize="index").round(3).to_string())
print("\nper-flow OUTCOME: mean delta vs root, and clean rate (all levels pooled, non-root nodes)")
fo = nr.groupby("flow").agg(n=("flow", "size"), mean_delta=("delta_mhz", "mean"),
                            median_delta=("delta_mhz", "median"),
                            pct_invalid=("validation_status", lambda s: (s == "INVALID").mean()),
                            pct_clean=("validation_status", lambda s: (s == "CLEAN").mean()),
                            mean_runtime=("node_runtime_s", "mean"))
print(fo.sort_values("mean_delta", ascending=False).round(3).to_string())
print("\nper-flow invalid rate by level (does S3 avoid the risky flows?):")
print(nr.pivot_table(index="flow", columns="level",
                     values="validation_status",
                     aggfunc=lambda s: (s == "INVALID").mean()).round(3).to_string())

H("D. DIAGNOSTIC EVIDENCE — bottleneck classes and whether analysis changed the ranking")
print("bottleneck class distribution by level:")
print(pd.crosstab(diag.level, diag.bottleneck_class, normalize="index").round(3).to_string())
print("\nbottleneck confidence by level:")
print(diag.groupby("level").bottleneck_confidence.describe().round(3).to_string())
print("\nevidence basis of the TOP-ranked candidate, by level:")
print(pd.crosstab(diag.level, diag.top_evidence_basis, normalize="index").round(3).to_string())
print("\nby level x model:")
print(pd.crosstab([diag.level, diag.model], diag.top_evidence_basis, normalize="index").round(3).to_string())
print("\ncandidates carrying explicit contraindications (S3's manual should raise this):")
print(diag.groupby(["level", "model"])[["n_contraindicated", "n_candidates"]].mean().round(2).to_string())
print("\nanalysis_changed_ranking_count by level (run-level):")
print(runs.groupby("level").analysis_changed_ranking_count.agg(["mean", "max", "count"]).round(3).to_string())

H("E. DID ANALYSIS MOVE THE CANDIDATE PRIORITIES?  (policy candidates)")
cand["prio_shift"] = cand.clipped_priority - cand.base_priority_no_analysis
print("analysis_prior_applied rate by level:")
print(cand.groupby("level").analysis_prior_applied.mean().round(3).to_string())
print("\nmean |priority shift| attributable to analysis, by level x model:")
print(cand.groupby(["level", "model"]).agg(
    n=("prio_shift", "size"),
    mean_shift=("prio_shift", "mean"),
    mean_abs_shift=("prio_shift", lambda s: s.abs().mean()),
    mean_prio_delta=("analysis_priority_delta", "mean"),
    mean_risk_delta=("analysis_risk_delta", "mean")).round(4).to_string())
print("\ncandidate outcome by level:")
print(pd.crosstab(cand.level, cand.outcome_qor_status, normalize="index").round(3).to_string())

H("F. WHERE DOES IMPROVEMENT ARRIVE?  (trajectory by node order)")
nn = nodes[nodes.flow != "root"].copy()
nn["order"] = nn.node_id.str.extract(r"node_(\d+)").astype(int)
recs = []
for k, grp in nn.groupby(["level", "benchmark", "model", "repeat"]):
    grp = grp.sort_values("order")
    best = 0.0
    root = grp.root_fmax_mhz.iloc[0]
    for i, (_, r) in enumerate(grp.iterrows(), start=1):
        d = r.delta_mhz if (r.validation_valid == True and pd.notna(r.delta_mhz)) else -1e9
        best = max(best, d, 0.0)
        recs.append(dict(level=k[0], benchmark=k[1], model=k[2], repeat=k[3],
                         step=i, best_delta=best, best_pct=100 * best / root))
TR = pd.DataFrame(recs)
TR.to_csv(D + "/trajectory.csv", index=False)
print("mean best-so-far % gain by step, per level:")
pv = TR.pivot_table(index="step", columns="level", values="best_pct", aggfunc="mean")
cnt = TR.pivot_table(index="step", columns="level", values="best_pct", aggfunc="size")
out = pv.round(2).astype(str) + " (n=" + cnt.fillna(0).astype(int).astype(str) + ")"
print(out.to_string())
print("\nfraction of each level's FINAL gain already achieved by step k (runs reaching that step):")
fin = TR.groupby(["level", "benchmark", "model", "repeat"]).best_pct.max().rename("final")
T2 = TR.merge(fin, on=["level", "benchmark", "model", "repeat"])
T2["frac"] = np.where(T2.final > 0, T2.best_pct / T2.final, np.nan)
print(T2.pivot_table(index="step", columns="level", values="frac", aggfunc="mean").round(3).to_string())
print("\nnode order at which the winning node was found (best_depth is depth, this is order):")
win = nn.merge(runs[["level", "benchmark", "model", "repeat", "best_node_id"]],
               on=["level", "benchmark", "model", "repeat"])
win = win[win.node_id == win.best_node_id]
print(win.groupby("level").order.describe().round(2).to_string())

H("G. ANOMALIES FOLLOW-UP")
print("G1. Benchmarks where S1 gives IDENTICAL results across all 5 model-repeats:")
g = runs[runs.level == "S1"].groupby("benchmark").delta_mhz.agg(["std", "mean", "count"])
print(g[g["std"] < 1e-9].round(4).to_string())
print("\n  what flow sequences did those runs use?")
ident = g[g["std"] < 1e-9].index
print(runs[(runs.level == "S1") & (runs.benchmark.isin(ident))]
      [["benchmark", "model", "repeat", "nodes_total", "delta_mhz", "best_flow_sequence"]]
      .sort_values(["benchmark", "model"]).to_string(index=False))

print("\nG2. logicnets_jscl S3 instability (sd 50 MHz):")
print(runs[(runs.benchmark == "logicnets_jscl")]
      [["level", "model", "repeat", "delta_mhz", "best_flow_sequence", "eq_status",
        "val_invalid", "val_warning"]].sort_values(["level", "model", "repeat"]).to_string(index=False))

print("\nG3. equivalence-gate failures — are they clustered by design or by level?")
f = runs[runs.eq_status == "failed"]
print("  by benchmark:", f.benchmark.value_counts().to_dict())
print("  by participant:", f.participant.value_counts().to_dict())
print("  by model:", f.model.value_counts().to_dict())
print("  by level:", f.level.value_counts().to_dict())
print("  total runs on those two benchmarks:",
      len(runs[runs.benchmark.isin(f.benchmark.unique())]))
print("\n  failure rate on finn_radioml + logicnets_jscl, by model:")
sub = runs[runs.benchmark.isin(["finn_radioml", "logicnets_jscl"])]
print(pd.crosstab(sub.model, sub.eq_status).to_string())

print("\nG4. MEANINGFUL improvement thresholds (delta>1 MHz, and >1% of baseline):")
runs["imp_1mhz"] = runs.delta_mhz > 1.0
runs["imp_1pct"] = runs.pct_gain > 1.0
runs["imp_5pct"] = runs.pct_gain > 5.0
print(runs.groupby("level")[["imp_1mhz", "imp_1pct", "imp_5pct"]].mean().round(3).to_string())
print("\n  counts:")
print(runs.groupby("level")[["imp_1mhz", "imp_1pct", "imp_5pct"]].agg(["sum", "count"]).to_string())
print("\n  by level x study (>1% of baseline):")
print(runs.groupby(["level", "study"]).imp_1pct.agg(["sum", "count", "mean"]).round(3).to_string())

runs.to_csv(D + "/runs_enriched.csv", index=False)
S.to_csv(D + "/sessions_mt.csv", index=False)
print("\n[wrote trajectory.csv, sessions_mt.csv, runs_enriched.csv]")
