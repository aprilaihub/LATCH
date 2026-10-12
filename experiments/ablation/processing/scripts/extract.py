#!/usr/bin/env python3
"""
Ablation study data extraction.

Walks the explicitly configured participant ablation trees plus an S0 sweep,
and emits flat tables:

  runs.csv        one row per completed run (cell)
  nodes.csv       one row per node within every run
  loopdec.csv     one row per LLM analysis decision (token accounting)
  diagnoses.csv   one row per analysis session (bottleneck class, evidence)
  setaside.csv    inventory of partial/truncated run dirs that were NOT used

Stdlib only; no third-party imports. Run through experiments.ablation.processing
so all input and output roots are explicit.
"""
import csv
import glob
import json
import os
import re
import sys

_PA_ROOT = os.environ.get("PAPER_ANALYSIS_ROOT")
if not _PA_ROOT:
    raise SystemExit("PAPER_ANALYSIS_ROOT must be set by the processing entrypoint")

OUT = os.path.expanduser(_PA_ROOT + "/data")
os.makedirs(OUT, exist_ok=True)

RAW_ROOT = os.environ.get("ABLATION_RAW_ROOT")
S0_SWEEP = os.environ.get("ABLATION_S0_SWEEP_ROOT")
try:
    PARTICIPANT_ROOTS = json.loads(os.environ.get("ABLATION_PARTICIPANT_ROOTS_JSON", "{}"))
except json.JSONDecodeError as exc:
    raise SystemExit(f"invalid ABLATION_PARTICIPANT_ROOTS_JSON: {exc}") from exc
if not isinstance(PARTICIPANT_ROOTS, dict) or "matt" not in PARTICIPANT_ROOTS or not RAW_ROOT or not S0_SWEEP:
    raise SystemExit(
        "set participant_roots including matt (for Matt-only mechanism and V3 cost scans), ABLATION_RAW_ROOT, "
        "and ABLATION_S0_SWEEP_ROOT via the processing entrypoint"
    )
if os.path.abspath(RAW_ROOT) != os.path.abspath(PARTICIPANT_ROOTS["matt"]):
    raise SystemExit("ABLATION_RAW_ROOT must match participant_roots.matt for Matt-only mechanism and V3 cost scans")
try:
    S0_EXTRA = json.loads(os.environ.get("ABLATION_S0_EXTRA_ROOTS_JSON", "{}"))
except json.JSONDecodeError as exc:
    raise SystemExit(f"invalid ABLATION_S0_EXTRA_ROOTS_JSON: {exc}") from exc

# --- benchmark metadata, from the vault's definitive 15-benchmark table -------
# study: known | unknown ; family: seen (<parent>) | out-of-family | -
BENCH = {
    "vexriscv_re-place":         dict(study="known",   ver="v1", family="vexriscv",      mb=1.8,   luts=2000,   owner="cristian"),
    "amd_mini_isp":              dict(study="known",   ver="v1", family="amd_mini_isp",  mb=3.9,   luts=3000,   owner="cristian"),
    "logicnets_jscl":            dict(study="known",   ver="v1", family="logicnets",     mb=13.5,  luts=31000,  owner="shu"),
    "finn_radioml":              dict(study="known",   ver="v1", family="finn",          mb=51.8,  luts=74000,  owner="shu"),
    "rosetta_digit-recognition": dict(study="known",   ver="v1", family="rosetta_digit", mb=17.9,  luts=23000,  owner="shu"),
    "boom_soc":                  dict(study="known",   ver="v1", family="boom_soc",      mb=144.8, luts=227000, owner="matt"),
    "rosetta_optical-flow":      dict(study="known",   ver="v1", family="rosetta_of",    mb=26.4,  luts=34000,  owner="shu"),
    "corescore_500_mod":         dict(study="known",   ver="v1", family="corescore",     mb=66.4,  luts=100000, owner="matt"),
    "amd_mini-isp_v2":           dict(study="unknown", ver="v2", family="amd_mini_isp",  mb=3.9,   luts=3000,   owner="cristian"),
    "boom_soc_v2":               dict(study="unknown", ver="v2", family="boom_soc",      mb=134.9, luts=227000, owner="matt"),
    "vexriscv_re-place_v2":      dict(study="unknown", ver="v2", family="vexriscv",      mb=2.0,   luts=2000,   owner="cristian"),
    "fir_symmetric_systolic":    dict(study="unknown", ver="new", family="OUT-OF-FAMILY", mb=24.7, luts=None,   owner="matt"),
    "fir_systolic_transposed":   dict(study="unknown", ver="new", family="OUT-OF-FAMILY", mb=15.2, luts=None,   owner="matt"),
    "rosetta_3d-rendering_v2":   dict(study="unknown", ver="v2", family="rosetta_3d",    mb=7.8,   luts=14000,  owner="cristian"),
    "vtr_mcml_v2":               dict(study="unknown", ver="v2", family="vtr_mcml",      mb=29.6,  luts=43000,  owner="shu"),
    # 16th benchmark, added 2026-08-27. The only non-AMD design in the set (RapidWright
    # PicoBlazeArray), genuinely out-of-family, and the only one whose S0 run sits outside
    # the auto_top8 sweep directory. luts = true CLB LUTs (75,900 = 60,060 logic + 15,840 memory).
    "picoblazearray":            dict(study="unknown", ver="new", family="OUT-OF-FAMILY", mb=None, luts=75900, owner="matt"),
}


def jload(p, default=None):
    try:
        with open(p) as f:
            return json.load(f)
    except Exception:
        return default


def jlines(p):
    out = []
    try:
        with open(p) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except Exception:
                    pass
    except Exception:
        pass
    return out


def g(d, *path, default=None):
    """Nested get."""
    cur = d
    for k in path:
        if not isinstance(cur, dict) or k not in cur:
            return default
        cur = cur[k]
    return cur


def extract_run(run_dir, ident):
    """ident: dict with source/participant/benchmark/level/model/repeat"""
    ps = jload(os.path.join(run_dir, "pipeline_summary.json"))
    if ps is None:
        return None, []

    cfg = ps.get("config", {}) or {}
    rm = ps.get("root_metrics", {}) or {}
    bq = ps.get("best_pure_qor", {}) or {}
    comp = bq.get("components", {}) or {}
    phase = ps.get("phase_runtime_breakdown", {}) or {}
    st = ps.get("state", {}) or {}
    eq = st.get("equivalence_gate", {}) or {}
    an = ps.get("analysis", {}) or {}
    sysc = ps.get("system_conditions", {}) or {}
    vc = ps.get("validation_counts", {}) or {}
    gs = jload(os.path.join(run_dir, "graph_stats.json"), {}) or {}
    repo = ps.get("repo_revision", {}) or {}
    risk = ps.get("risk_budget", {}) or {}

    bench = ident["benchmark"]
    meta = BENCH.get(bench, {})

    # ---- nodes -------------------------------------------------------------
    rows_nodes = []
    nodes = jload(os.path.join(run_dir, "results_summary.json"), []) or []
    key = (ident["source"], bench, ident["level"], ident["model"], ident["repeat"])
    best_attempted = None
    root_fmax = rm.get("initial_fmax_mhz")
    n_valid_nodes = 0
    n_invalid_nodes = 0
    for nd in nodes:
        fm = nd.get("final_fmax_mhz")
        vs = nd.get("validation_status")
        if nd.get("flow") != "root":
            if nd.get("validation_valid") is True:
                n_valid_nodes += 1
            elif nd.get("validation_valid") is False:
                n_invalid_nodes += 1
        if fm is not None and root_fmax:
            d = fm - root_fmax
            if best_attempted is None or d > best_attempted:
                best_attempted = d
        rows_nodes.append(dict(
            source=key[0], benchmark=key[1], level=key[2], model=key[3], repeat=key[4],
            study=meta.get("study"),
            node_id=nd.get("node_id"), depth=nd.get("depth"), flow=nd.get("flow"),
            flow_sequence=nd.get("flow_sequence"), parent_id=nd.get("parent_id"),
            fmax_mhz=fm, wns_ns=nd.get("final_wns_ns"), tns_ns=nd.get("final_tns_ns"),
            root_fmax_mhz=root_fmax,
            delta_mhz=(fm - root_fmax) if (fm is not None and root_fmax) else None,
            node_runtime_s=nd.get("node_runtime_seconds"),
            path_runtime_s=nd.get("path_runtime_seconds"),
            validation_status=vs, validation_valid=nd.get("validation_valid"),
            validation_errors=nd.get("validation_error_count"),
            validation_warnings=nd.get("validation_warning_count"),
            pruned=nd.get("pruned"), prune_reason=nd.get("prune_reason"),
            status=nd.get("status"), qor_status=nd.get("qor_status"),
            qor_score=nd.get("pure_qor_score"),
        ))

    # ---- per-call token accounting ----------------------------------------
    # NOTE ON SEMANTICS (verified 2026-08-24): in loop_decisions.jsonl,
    #   estimated_prompt_tokens = tokens in THIS call's prompt (per-call)
    #   estimated_total_tokens  = RUNNING CUMULATIVE total for the whole run
    # so run total = max(estimated_total_tokens) == pipeline_summary.analysis_tokens,
    # input  = sum(estimated_prompt_tokens),
    # output = run total - input.
    lds = jlines(os.path.join(run_dir, "analysis", "loop_decisions.jsonl"))
    prompt_tok = sum(x.get("estimated_prompt_tokens") or 0 for x in lds)
    max_cum_tok = max((x.get("estimated_total_tokens") or 0) for x in lds) if lds else None
    total_tok_est = max_cum_tok
    completion_tok = (max_cum_tok - prompt_tok) if max_cum_tok is not None else None
    budget_hard = lds[0].get("analysis_token_budget_hard") if lds else None
    budget_soft = lds[0].get("analysis_token_budget_soft") if lds else None

    # ---- run row -----------------------------------------------------------
    best_seq = ps.get("best_flow_sequence") or []
    row = dict(
        source=key[0], participant=ident["participant"], benchmark=bench,
        level=ident["level"], model=ident["model"], repeat=ident["repeat"],
        study=meta.get("study"), ver=meta.get("ver"), family=meta.get("family"),
        size_mb=meta.get("mb"), luts=meta.get("luts"), owner=meta.get("owner"),
        run_dir=run_dir,
        # baseline
        root_fmax_mhz=root_fmax, root_wns_ns=rm.get("initial_wns_ns"),
        root_tns_ns=rm.get("initial_tns_ns"),
        failing_endpoints=rm.get("failing_endpoints"),
        clock_period_ns=rm.get("clock_period_ns"), target_clock=rm.get("target_clock"),
        # outcome
        best_node_id=ps.get("best_node_id"),
        best_depth=gs.get("best_depth"),
        best_flow_sequence=" -> ".join(best_seq),
        best_flow_len=len(best_seq),
        best_fmax_mhz=comp.get("node_fmax_mhz"),
        delta_mhz=comp.get("fmax_delta_mhz"),
        improvement_ratio=comp.get("fmax_improvement_ratio"),
        best_wns_ns=comp.get("node_wns_ns"),
        wns_improvement_ns=comp.get("wns_improvement_ns"),
        best_qor_score=ps.get("best_pure_qor_score"),
        best_qor_status=bq.get("status"),
        best_validation_status=ps.get("best_validation_status"),
        best_attempted_delta_mhz=best_attempted,
        stop_reason=st.get("stop_reason"), state_status=st.get("status"),
        success=ps.get("success"),
        # graph
        nodes_total=ps.get("nodes_total"), eda_runs=ps.get("eda_runs"),
        scheduled_runs=ps.get("scheduled_runs"),
        nodes_completed=ps.get("nodes_completed"), nodes_failed=ps.get("nodes_failed"),
        nodes_pruned=ps.get("nodes_pruned"),
        max_depth_reached=gs.get("max_depth_reached"),
        max_children_per_node=gs.get("max_children_per_node"),
        n_valid_nodes=n_valid_nodes, n_invalid_nodes=n_invalid_nodes,
        val_clean=vc.get("CLEAN", 0), val_warning=vc.get("WARNING", 0),
        val_invalid=vc.get("INVALID", 0),
        invalid_rate=risk.get("invalid_rate_so_far"),
        warning_rate=risk.get("warning_rate_so_far"),
        fallback_rate=risk.get("fallback_rate_so_far"),
        # equivalence gate
        eq_status=eq.get("status"), eq_passed=eq.get("passed"),
        eq_phase1=eq.get("phase1_passed"), eq_phase2=eq.get("phase2_passed"),
        eq_phase2_skipped=eq.get("phase2_skipped"),
        eq_infra_failure=eq.get("infrastructure_failure"),
        # timing
        wall_s=ps.get("total_wall_runtime_seconds"),
        analysis_s=phase.get("analysis_seconds"),
        diagnostic_s=phase.get("diagnostic_seconds"),
        diagnostic_calls=phase.get("diagnostic_calls"),
        exec_productive_s=phase.get("execution_productive_seconds"),
        exec_failed_s=phase.get("execution_failed_seconds"),
        verification_s=phase.get("verification_seconds"),
        partition_valid=phase.get("partition_valid"),
        phases_concurrent=phase.get("phases_run_concurrently"),
        # tokens & cost
        analysis_tokens=ps.get("analysis_tokens"),
        analysis_llm_calls=ps.get("analysis_llm_calls"),
        analysis_sessions=an.get("sessions"),
        analysis_tool_calls=an.get("tool_calls"),
        analysis_plan_count=an.get("analysis_plan_count"),
        diagnosis_count=an.get("diagnosis_count"),
        feedback_count=an.get("feedback_count"),
        selected_feedback_count=an.get("selected_feedback_count"),
        fast_skip_suggestion_count=an.get("fast_skip_suggestion_count"),
        analysis_changed_ranking_count=an.get("analysis_changed_ranking_count"),
        cost_usd=ps.get("analysis_total_cost_usd"),
        cost_source=ps.get("cost_source"),
        n_loop_decisions=len(lds),
        prompt_tokens=prompt_tok, completion_tokens=completion_tok,
        est_total_tokens=total_tok_est,
        max_cum_tokens=max_cum_tok,
        token_budget_hard=budget_hard, token_budget_soft=budget_soft,
        # system
        peak_load_per_core=sysc.get("peak_load_per_core"),
        any_sample_contended=sysc.get("any_sample_contended"),
        load_start=g(sysc, "samples", default=[{}])[0].get("load_per_core") if sysc.get("samples") else None,
        load_end=g(sysc, "end", "load_per_core"),
        t_start=g(sysc, "samples", default=[{}])[0].get("captured_at") if sysc.get("samples") else None,
        t_end=g(sysc, "end", "captured_at"),
        n_load_samples=len(sysc.get("samples") or []),
        pinned_cores=g(sysc, "end", "pinned_cores"),
        pinned_core_count=g(sysc, "end", "pinned_core_count"),
        vivado_threads=g(sysc, "end", "vivado_threads", "general_max_threads"),
        # config
        knowledge_mode=cfg.get("knowledge_mode"),
        knowledge_base_path=cfg.get("knowledge_base_path"),
        cfg_max_depth=cfg.get("max_depth"), cfg_max_total_runs=cfg.get("max_total_runs"),
        cfg_branching=cfg.get("branching_factor"), cfg_beam=cfg.get("beam_width"),
        cfg_batch=cfg.get("batch_size"), cfg_parallel=cfg.get("max_parallel_jobs"),
        cfg_policy=cfg.get("policy_type"), cfg_model=cfg.get("model"),
        cfg_temperature=cfg.get("temperature"),
        cfg_analysis_level=cfg.get("analysis_level"),
        cfg_context_mode=cfg.get("llm_context_mode"),
        cfg_prune_invalid=cfg.get("prune_invalid"),
        cfg_prune_severe=cfg.get("prune_severe_regression"),
        cfg_no_repeat=cfg.get("disallow_consecutive_repeat"),
        cfg_allowed_flows=len(cfg.get("allowed_flows") or []),
        repo_branch=repo.get("branch"), repo_commit=(repo.get("commit") or "")[:10],
    )
    return row, rows_nodes


def extract_aux(run_dir, ident):
    """loop decisions + diagnoses for mechanism analysis."""
    key = dict(source=ident["source"], benchmark=ident["benchmark"],
               level=ident["level"], model=ident["model"], repeat=ident["repeat"])
    lrows, drows = [], []
    for i, x in enumerate(jlines(os.path.join(run_dir, "analysis", "loop_decisions.jsonl"))):
        lrows.append(dict(key, idx=i,
                          decision_index=x.get("decision_index"),
                          node_id=x.get("node_id"), action=x.get("action"),
                          tool=x.get("next_tool_name"),
                          confidence=x.get("confidence"),
                          continue_analysis=x.get("continue_analysis"),
                          prompt_tokens=x.get("estimated_prompt_tokens"),
                          total_tokens=x.get("estimated_total_tokens"),
                          payload_chars=x.get("payload_chars"),
                          context_mode=x.get("analysis_context_mode"),
                          budget_hard=x.get("analysis_token_budget_hard"),
                          budget_soft=x.get("analysis_token_budget_soft"),
                          expected_information_gain=x.get("expected_information_gain")))
    for i, x in enumerate(jlines(os.path.join(run_dir, "analysis", "node_diagnoses.jsonl"))):
        cps = x.get("candidate_priorities") or []
        top = cps[0] if cps else {}
        drows.append(dict(key, idx=i,
                          bottleneck_class=x.get("bottleneck_class"),
                          bottleneck_confidence=x.get("bottleneck_confidence"),
                          analysis_type=x.get("analysis_type"),
                          n_candidates=len(cps),
                          top_flow=top.get("flow"),
                          top_priority=top.get("priority_score"),
                          top_confidence=top.get("confidence"),
                          top_evidence_basis=top.get("evidence_basis"),
                          top_risk=top.get("risk_score"),
                          n_contraindicated=sum(1 for c in cps if c.get("contraindications")),
                          n_llm_evidence=sum(1 for c in cps
                                             if c.get("evidence_basis")
                                             and c.get("evidence_basis") != "heuristic_prior"),
                          evidence_bases="|".join(sorted({str(c.get("evidence_basis")) for c in cps})),
                          ))
    prows = []
    for x in jlines(os.path.join(run_dir, "policy_candidates.jsonl")):
        prows.append(dict(key,
                          event_index=x.get("event_index"),
                          candidate_id=x.get("candidate_id"),
                          candidate_flow=x.get("candidate_flow"),
                          candidate_rank=x.get("candidate_rank"),
                          clipped_priority=x.get("clipped_priority"),
                          base_priority_no_analysis=x.get("approximate_base_priority_without_analysis"),
                          analysis_prior_applied=x.get("analysis_prior_applied"),
                          analysis_priority_delta=x.get("analysis_priority_delta"),
                          analysis_risk_delta=x.get("analysis_risk_delta"),
                          analysis_confidence=x.get("analysis_confidence"),
                          confidence=x.get("confidence"),
                          executed=x.get("executed"),
                          accepted=x.get("accepted_by_transition_executor"),
                          final_status=x.get("final_status"),
                          outer_rejection_reason=x.get("outer_rejection_reason"),
                          outcome_node_id=x.get("outcome_node_id"),
                          outcome_status=x.get("outcome_status"),
                          outcome_qor_status=x.get("outcome_qor_status"),
                          outcome_validation_status=x.get("outcome_validation_status"),
                          outcome_qor_score=x.get("outcome_pure_qor_score"),
                          decision_valid=x.get("decision_valid"),
                          ))
    return lrows, drows, prows


def main():
    runs, nodes, loopdec, diagnoses, setaside, polcand = [], [], [], [], [], []

    # ---- S1-S3 -------------------------------------------------------------
    for person, root in PARTICIPANT_ROOTS.items():
        for d in sorted(glob.glob(os.path.join(root, "*/*/*/repeat_*"))):
            if not os.path.isdir(d):
                continue
            rel = os.path.relpath(d, root)
            bench, level, model, rep = rel.split(os.sep)
            m = re.fullmatch(r"repeat_(\d+)", rep)
            if not m:
                # set-aside partial / truncated
                setaside.append(dict(participant=person, benchmark=bench, level=level,
                                     model=model, dirname=rep, path=d,
                                     kind=("partial" if ".partial" in rep else
                                           "truncated" if ".truncated" in rep else "other"),
                                     has_pipeline_summary=os.path.exists(
                                         os.path.join(d, "pipeline_summary.json"))))
                continue
            ident = dict(source=person, participant=person, benchmark=bench,
                         level=level, model=model, repeat=int(m.group(1)))
            row, nrows = extract_run(d, ident)
            if row is None:
                setaside.append(dict(participant=person, benchmark=bench, level=level,
                                     model=model, dirname=rep, path=d,
                                     kind="no_pipeline_summary", has_pipeline_summary=False))
                continue
            runs.append(row)
            nodes.extend(nrows)
            lr, dr, pr = extract_aux(d, ident)
            loopdec.extend(lr)
            diagnoses.extend(dr)
            polcand.extend(pr)

    # ---- S0 ----------------------------------------------------------------
    s0_dirs = [(os.path.basename(d.rstrip("/")), d.rstrip("/"))
               for d in sorted(glob.glob(os.path.join(S0_SWEEP, "*/")))]
    s0_dirs += sorted(S0_EXTRA.items())
    for bench, d in s0_dirs:
        if bench not in BENCH or not os.path.isdir(d):
            continue
        ident = dict(source="S0sweep", participant="sweep", benchmark=bench,
                     level="S0", model="none", repeat=1)
        row, nrows = extract_run(d, ident)
        if row is None:
            continue
        runs.append(row)
        nodes.extend(nrows)
        _, _, pr = extract_aux(d, ident)
        polcand.extend(pr)

    def write(name, rows):
        if not rows:
            print(f"  {name}: EMPTY")
            return
        keys = []
        for r in rows:
            for k in r:
                if k not in keys:
                    keys.append(k)
        p = os.path.join(OUT, name)
        with open(p, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=keys)
            w.writeheader()
            for r in rows:
                w.writerow(r)
        print(f"  {name}: {len(rows)} rows, {len(keys)} cols")

    print("Wrote to", OUT)
    write("runs.csv", runs)
    write("nodes.csv", nodes)
    write("loopdec.csv", loopdec)
    write("diagnoses.csv", diagnoses)
    write("setaside.csv", setaside)
    write("polcand.csv", polcand)


if __name__ == "__main__":
    main()
