#!/usr/bin/env python3
"""Falsification tests for the two load-bearing mechanism claims."""
import glob
import json
import os
import numpy as np
import pandas as pd

_PA_ROOT = os.environ.get("PAPER_ANALYSIS_ROOT")
RAW_ROOT = os.environ.get("ABLATION_RAW_ROOT")
try:
    PARTICIPANT_ROOTS = json.loads(os.environ.get("ABLATION_PARTICIPANT_ROOTS_JSON", "{}"))
except json.JSONDecodeError as exc:
    raise SystemExit(f"invalid ABLATION_PARTICIPANT_ROOTS_JSON: {exc}") from exc
PARTICIPANT_ORDER = ("cristian", "matt", "shu")
if (
    not _PA_ROOT
    or not RAW_ROOT
    or not isinstance(PARTICIPANT_ROOTS, dict)
    or PARTICIPANT_ROOTS.get("matt") != RAW_ROOT
):
    raise SystemExit(
        "PAPER_ANALYSIS_ROOT, Matt ABLATION_RAW_ROOT, and ABLATION_PARTICIPANT_ROOTS_JSON "
        "must be set by the processing entrypoint"
    )

pd.set_option("display.width", 240)
pd.set_option("display.max_columns", 80)
pd.set_option("display.max_rows", 400)
D = os.path.expanduser(_PA_ROOT + "/data")
runs = pd.read_csv(D + "/runs_enriched.csv")
nodes = pd.read_csv(D + "/nodes.csv")
cand = pd.read_csv(D + "/polcand.csv")
diag = pd.read_csv(D + "/diagnoses.csv")
H = lambda t: print("\n" + "=" * 104 + f"\n{t}\n" + "=" * 104)

H("V1. IS `analysis_priority_delta` AN INERT INSTRUMENTATION FIELD?")
print("clipped_priority distribution (should vary if the policy ranks candidates):")
print(cand.groupby("level").clipped_priority.describe().round(4).to_string())
print("\nunique clipped_priority values:", sorted(cand.clipped_priority.dropna().unique())[:20])
print("unique base_priority_no_analysis:", sorted(cand.base_priority_no_analysis.dropna().unique())[:20])
print("\nanalysis_session_id populated on candidate rows?",
      cand.get("analysis_session_id", pd.Series(dtype=object)).notna().sum() if "analysis_session_id" in cand else "col absent")
print("candidate_rank distribution:")
print(cand.groupby("level").candidate_rank.describe().round(2).to_string())
print("""
=> If clipped_priority is a single constant and rank is always 0, the policy-candidate
   priority fields are INERT INSTRUMENTATION, not evidence that analysis had no effect.
   The real test is whether the EXECUTED flow tracks the analysis's top-ranked flow.""")

H("V2. DOES THE EXECUTED FLOW TRACK THE ANALYSIS'S TOP-RANKED FLOW?")
# Rebuild: for each analysis session, the ranked candidate list; compare to what ran next.
rows = []
v2_participants = [name for name in PARTICIPANT_ORDER if name in PARTICIPANT_ROOTS]
print("V2 follow-through participants (source order):", ", ".join(v2_participants))
for participant in v2_participants:
    root = PARTICIPANT_ROOTS[participant]
    for p in glob.glob(os.path.join(root, "*/*/*/repeat_[0-9]/analysis/node_diagnoses.jsonl")):
        parts = os.path.relpath(p, root).split(os.sep)
        bench, lev, mod, rep = parts[0], parts[1], parts[2], parts[3]
        for line in open(p):
            try:
                e = json.loads(line)
            except Exception:
                continue
            cps = e.get("candidate_priorities") or []
            if not cps:
                continue
            src = cps[0].get("analysis_session_id") or ""
            ordered = [c.get("flow") for c in cps]
            scored = sorted(cps, key=lambda c: -(c.get("priority_score") or 0))
            rows.append(dict(benchmark=bench, level=lev, model=mod, repeat=int(rep.split("_")[1]),
                             session=src, node_id=e.get("node_id") or cps[0].get("node_id"),
                             top_listed=ordered[0], top_scored=scored[0].get("flow"),
                             n=len(cps),
                             n_distinct_scores=len({round(c.get("priority_score") or 0, 6) for c in cps}),
                             top_score=scored[0].get("priority_score"),
                             min_score=scored[-1].get("priority_score"),
                             evidence=scored[0].get("evidence_basis"),
                             ranked_flows="|".join(ordered)))
A = pd.DataFrame(rows)
print("analysis sessions with a ranked candidate list:", len(A))
print("\nare candidate priority scores actually differentiated within a session?")
print(A.groupby("level").n_distinct_scores.describe().round(2).to_string())
print("\ntop score vs min score, by level:")
print(A.groupby("level")[["top_score", "min_score"]].describe().round(3).to_string())

# what flow actually ran, per (run, parent node)? use nodes' parent_id
nn = nodes[nodes.flow != "root"].copy()
nn["order"] = nn.node_id.str.extract(r"node_(\d+)").astype(int)
nn["key"] = list(zip(nn.benchmark, nn.level, nn.model, nn.repeat, nn.parent_id))
# for each analysis session on node X, the children of X that were subsequently created
A["key"] = list(zip(A.benchmark, A.level, A.model, A.repeat, A.node_id))
kids = nn.groupby("key").flow.apply(list).rename("children").reset_index()
M = A.merge(kids, on="key", how="left")
M["children"] = M.children.apply(lambda v: v if isinstance(v, list) else [])
M["hit_top1"] = [t in c[:1] for t, c in zip(M.top_scored, M.children)]
M["hit_any"] = [t in c for t, c in zip(M.top_scored, M.children)]
M["n_kids"] = M.children.apply(len)
M = M[M.n_kids > 0]
print(f"\nsessions whose parent node subsequently produced children: {len(M)}")
print("\nDoes the analysis's top-scored flow appear among the children actually executed?")
print(M.groupby("level")[["hit_top1", "hit_any", "n_kids"]].mean().round(3).to_string())
print("\nbaseline if children were chosen at random from 9 flows:")
print("  P(top-scored flow among k children) = k/9 :")
print(M.groupby("level").n_kids.mean().div(9).round(3).to_string())
print("\nby level x model:")
print(M.groupby(["level", "model"])[["hit_any", "n_kids"]].agg(["mean", "size"]).round(3).to_string())

H("V3. COST — incremental rate, differencing BOTH cost and tokens")
rows = []
print("V3 cost participant: matt")
for p in glob.glob(os.path.join(RAW_ROOT, "*/*/*/repeat_[0-9]/analysis/analysis_events.jsonl")):
    parts = os.path.relpath(p, RAW_ROOT).split(os.sep)
    bench, lev, mod, rep = parts[0], parts[1], parts[2], parts[3]
    pt = pc = 0.0
    for line in open(p):
        try:
            e = json.loads(line)
        except Exception:
            continue
        t, c = e.get("tokens"), e.get("cost_usd")
        if t is None or c is None:
            continue
        rows.append(dict(benchmark=bench, level=lev, model=mod, repeat=rep,
                         cum_tok=t, cum_cost=c, d_tok=t - pt, d_cost=c - pc))
        pt, pc = t, c
S = pd.DataFrame(rows)
print("is cost_usd cumulative? (monotone non-decreasing within a run)")
print("  negative d_cost rows:", (S.d_cost < -1e-12).sum(), "/", len(S))
print("  negative d_tok  rows:", (S.d_tok < 0).sum(), "/", len(S))
S2 = S[(S.d_tok > 0) & (S.d_cost > 0)].copy()
S2["inc_rate"] = 1e6 * S2.d_cost / S2.d_tok
print(f"\nincremental $/1M tokens per analysis session (n={len(S2)}):")
print(S2.groupby(["model", "level"]).inc_rate.agg(["count", "mean", "std", "min", "max"]).round(4).to_string())
print("\ncoefficient of variation of the incremental rate, by model:")
for m, g in S2.groupby("model"):
    print(f"  {m:14s} mean {g.inc_rate.mean():8.4f}  sd {g.inc_rate.std():7.4f}  CV {g.inc_rate.std()/g.inc_rate.mean():.4f}")
print("""
=> A near-constant incremental rate means the harness's 'measured' cost is itself a
   blended rate x tokens computation, NOT a provider-reported charge. Rate-scaling
   would then reproduce it by construction, and cannot be treated as independent
   validation of a real dollar figure.""")

print("\nregress cost on (input, output) tokens with an intercept, per model, ALL levels:")
mt = runs[(runs.participant == "matt") & (runs.level != "S0")]
for m, g in mt.groupby("model"):
    X = np.column_stack([g.prompt_tokens.values, g.completion_tokens.values,
                         np.ones(len(g))])
    y = g.cost_usd.values
    coef, res, rank, sv = np.linalg.lstsq(X, y, rcond=None)
    pred = X @ coef
    r2 = 1 - ((y - pred) ** 2).sum() / ((y - y.mean()) ** 2).sum()
    # collinearity between input and output tokens
    cc = np.corrcoef(g.prompt_tokens, g.completion_tokens)[0, 1]
    print(f"  {m:14s} in=${1e6*coef[0]:7.3f}/M  out=${1e6*coef[1]:7.3f}/M  "
          f"intercept=${coef[2]:.4f}  R2={r2:.4f}  corr(in,out)={cc:.3f}")

print("\nblended-rate reconstruction leave-one-level-out (fit on 2 levels, predict the 3rd):")
res = []
for m, g in mt.groupby("model"):
    for lv in ["S1", "S2", "S3"]:
        fit = g[g.level != lv]
        tgt = g[g.level == lv]
        rate = fit.cost_usd.sum() / fit.analysis_tokens.sum()
        pred = tgt.analysis_tokens * rate
        res.append(dict(model=m, held_out=lv, n=len(tgt),
                        measured_mean=tgt.cost_usd.mean(), pred_mean=pred.mean(),
                        bias_pct=100 * (pred.mean() - tgt.cost_usd.mean()) / tgt.cost_usd.mean(),
                        mape=100 * ((pred - tgt.cost_usd).abs() / tgt.cost_usd).mean()))
print(pd.DataFrame(res).round(3).to_string(index=False))

H("V4. FMAX OUTCOME QUANTISATION")
nr = nodes[(nodes.flow != "root") & nodes.fmax_mhz.notna()]
q = nr.groupby("benchmark").agg(n_nodes=("fmax_mhz", "size"),
                                n_distinct=("fmax_mhz", "nunique"))
q["distinct_per_node"] = (q.n_distinct / q.n_nodes).round(3)
print("distinct Fmax values observed per benchmark across ALL nodes:")
print(q.sort_values("distinct_per_node").to_string())
print("\ntop repeated Fmax values on a representative benchmark (logicnets_jscl):")
print(nr[nr.benchmark == "logicnets_jscl"].fmax_mhz.round(3).value_counts().head(12).to_string())

H("V5. S1 PEAK-AT-STEP-1: falsification test")
nn2 = nodes[nodes.flow != "root"].copy()
nn2["order"] = nn2.node_id.str.extract(r"node_(\d+)").astype(int)
win = nn2.merge(runs[["level", "benchmark", "model", "repeat", "best_node_id", "nodes_total"]],
                on=["level", "benchmark", "model", "repeat"])
win = win[win.node_id == win.best_node_id]
print("share of runs whose winning node is the FIRST node, by level:")
print(win.assign(isfirst=win.order == 1).groupby("level")["isfirst"].agg(["sum", "count", "mean"]).round(3).to_string())
print("\nrestricted to S1 runs that reached the full 12 nodes (no early-stop confound):")
w12 = win[(win.level == "S1") & (win.nodes_total == 12)]
print(f"  n={len(w12)}, winning node is first in {(w12.order==1).sum()} ({(w12.order==1).mean():.3f})")
print("  winning-node order distribution:", w12.order.value_counts().sort_index().to_dict())
print("\nsame for S2/S3 (all reached 12):")
for lv in ["S2", "S3"]:
    w = win[win.level == lv]
    print(f"  {lv}: n={len(w)}, first in {(w.order==1).sum()} ({(w.order==1).mean():.3f}), "
          f"median order {w.order.median()}")

A.to_csv(D + "/analysis_sessions.csv", index=False)
M.to_csv(D + "/session_followthrough.csv", index=False)
print("\n[wrote analysis_sessions.csv, session_followthrough.csv]")
