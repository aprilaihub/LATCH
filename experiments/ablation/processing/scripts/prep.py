#!/usr/bin/env python3
"""Fix two derived metrics before plotting.

1. clean_frac / invalid_frac in runs.csv came from pipeline_summary.validation_counts,
   which COUNTS THE ROOT NODE. The root is always CLEAN and is not an optimisation
   attempt, so this inflates every level and inflates S0 most (its runs have 2-4 nodes,
   so the root is 25-50% of them). Recompute from non-root nodes only.

2. trajectory.csv's per-step mean is over SURVIVORS, which biases S0 and S1 upward at
   later steps (only successful runs get that far). Recompute with the terminated runs
   carried forward at their final value, so every step averages the same set of runs.
"""
import os
import numpy as np
import pandas as pd

_PA_ROOT = os.environ.get("PAPER_ANALYSIS_ROOT")
if not _PA_ROOT:
    raise SystemExit("PAPER_ANALYSIS_ROOT must be set by the processing entrypoint")

D = os.path.expanduser(_PA_ROOT + "/data/")
runs = pd.read_csv(D + "runs_enriched.csv")
nodes = pd.read_csv(D + "nodes.csv")
ld = pd.read_csv(D + "loopdec.csv")
KEY = ["level", "benchmark", "model", "repeat"]

# ---- 1. attempt-level validation, root excluded --------------------------------
nr = nodes[nodes.flow != "root"].copy()
agg = nr.groupby(KEY).agg(
    attempts=("node_id", "size"),
    n_clean=("validation_status", lambda s: (s == "CLEAN").sum()),
    n_warn=("validation_status", lambda s: (s == "WARNING").sum()),
    n_invalid=("validation_status", lambda s: (s == "INVALID").sum()),
).reset_index()
agg["clean_frac_att"] = agg.n_clean / agg.attempts
agg["invalid_frac_att"] = agg.n_invalid / agg.attempts
runs = runs.drop(columns=[c for c in ["attempts", "n_clean", "n_warn", "n_invalid",
                                      "clean_frac_att", "invalid_frac_att"]
                          if c in runs.columns])
runs = runs.merge(agg, on=KEY, how="left")

print("clean fraction, root INCLUDED (old, wrong) vs root EXCLUDED (new):")
print(runs.groupby("level")[["clean_frac", "clean_frac_att"]].mean().round(4).to_string())
print("\ninvalid fraction, old vs new:")
print(runs.groupby("level")[["invalid_frac", "invalid_frac_att"]].mean().round(4).to_string())
print("\nattempts per run, by level:")
print(runs.groupby("level").attempts.agg(["mean", "min", "max"]).round(2).to_string())
runs.to_csv(D + "runs_enriched.csv", index=False)

# ---- 2. trajectory with carry-forward ------------------------------------------
nn = nodes[nodes.flow != "root"].copy()
nn["order"] = nn.node_id.str.extract(r"node_(\d+)").astype(int)
MAXSTEP = 11
recs = []
for k, grp in nn.groupby(KEY):
    grp = grp.sort_values("order")
    root = grp.root_fmax_mhz.iloc[0]
    best = 0.0
    curve = []
    for _, r in grp.iterrows():
        d = r.delta_mhz if (r.validation_valid == True and pd.notna(r.delta_mhz)) else -1e9
        best = max(best, d, 0.0)
        curve.append(best)
    # carry the final value forward to the full horizon
    budget = 3 if k[0] == "S0" else MAXSTEP
    while len(curve) < budget:
        curve.append(curve[-1] if curve else 0.0)
    for i, v in enumerate(curve[:budget], start=1):
        recs.append(dict(level=k[0], benchmark=k[1], model=k[2], repeat=k[3],
                         step=i, best_delta=v, best_pct=100 * v / root,
                         still_running=int(i <= len(grp))))
T = pd.DataFrame(recs)
T.to_csv(D + "trajectory_cf.csv", index=False)
print("\ncarry-forward trajectory — mean best %% gain by step (n is now constant per level):")
pv = T.pivot_table(index="step", columns="level", values="best_pct", aggfunc="mean").round(2)
cn = T.pivot_table(index="step", columns="level", values="best_pct", aggfunc="size")
print(pd.concat([pv, cn.add_suffix("_n")], axis=1).to_string())
print("\nshare of runs still actively searching at each step:")
print(T.pivot_table(index="step", columns="level", values="still_running",
                    aggfunc="mean").round(3).to_string())

# ---- 3. monotone cumulative tokens by node index --------------------------------
ld = ld.assign(order=ld.node_id.str.extract(r"node_(\d+)")[0].astype(float))
rows = []
for k, g in ld.groupby(["level", "benchmark", "model", "repeat"]):
    g = g.sort_values(["order", "idx"])
    running = 0
    per = {}
    for _, r in g.iterrows():
        if pd.isna(r.order):
            continue
        running = max(running, r.total_tokens or 0)
        per[int(r.order)] = running
    last = 0
    for s in range(1, MAXSTEP + 1):
        last = per.get(s, last)
        rows.append(dict(level=k[0], benchmark=k[1], model=k[2], repeat=k[3],
                         step=s, cum_tokens=last))
TK = pd.DataFrame(rows)
TK.to_csv(D + "tokens_cf.csv", index=False)
print("\nmonotone cumulative tokens by step (thousands):")
print((TK.pivot_table(index="step", columns="level", values="cum_tokens",
                      aggfunc="mean") / 1000).round(1).to_string())
print("\n[wrote runs_enriched.csv, trajectory_cf.csv, tokens_cf.csv]")

# ---- 4. POOLED validation shares per cell (the right basis for a heatmap) --------
# A per-run mean over-weights S0's 1-3-attempt runs. "Share of attempts" means
# total clean attempts / total attempts within the cell.
cell = nr.groupby(["benchmark", "level"]).agg(
    attempts=("node_id", "size"),
    clean=("validation_status", lambda s: (s == "CLEAN").sum()),
    warn=("validation_status", lambda s: (s == "WARNING").sum()),
    invalid=("validation_status", lambda s: (s == "INVALID").sum())).reset_index()
cell["clean_pct"] = 100 * cell.clean / cell.attempts
cell["invalid_pct"] = 100 * cell.invalid / cell.attempts
cell.to_csv(D + "validation_cells.csv", index=False)
lev = nr.groupby("level").agg(
    attempts=("node_id", "size"),
    clean=("validation_status", lambda s: (s == "CLEAN").sum()),
    warn=("validation_status", lambda s: (s == "WARNING").sum()),
    invalid=("validation_status", lambda s: (s == "INVALID").sum()))
lev["clean_pct"] = (100 * lev.clean / lev.attempts).round(2)
lev["invalid_pct"] = (100 * lev.invalid / lev.attempts).round(2)
lev["warn_pct"] = (100 * lev.warn / lev.attempts).round(2)
lev.to_csv(D + "validation_levels.csv")
print("\nPOOLED attempt-level validation by level (root excluded) — the correct basis:")
print(lev.to_string())
print("\nper-run-mean vs pooled, invalid share:")
print(pd.concat([runs.groupby('level').invalid_frac_att.mean().mul(100).round(2).rename('per_run_mean'),
                 lev.invalid_pct.rename('pooled')], axis=1).to_string())
