#!/usr/bin/env python3
"""Generate claim-neutral candidate figures and addressable source CSVs."""

from __future__ import annotations
import argparse,csv,json,statistics
from collections import Counter,defaultdict
from pathlib import Path
from typing import Any
import matplotlib.pyplot as plt

def read(path):
    with open(path,newline="",encoding="utf-8") as f:return list(csv.DictReader(f))
def write(path,rows):
    fields=list(dict.fromkeys(k for r in rows for k in r)); path.parent.mkdir(parents=True,exist_ok=True)
    with open(path,"w",newline="",encoding="utf-8") as f:
        w=csv.DictWriter(f,fieldnames=fields);w.writeheader();w.writerows(rows)
def save(fig,path):
    fig.tight_layout();fig.savefig(path,dpi=160,bbox_inches="tight");plt.close(fig)
def source(ids):return "|".join(ids)
def group(rows,keys):
    d=defaultdict(list)
    for r in rows:d[tuple(r[k] for k in keys)].append(r)
    return d
def rate(rows,pred):return sum(pred(r) for r in rows)/len(rows) if rows else 0
def f(v):return None if v=="" else float(v)

def bars(stem,rows,x,label,cap,value,plotdir,srcdir):
    write(srcdir/f"{stem}.csv",rows)
    fig,ax=plt.subplots(figsize=(max(7,len(rows)*.42),4.4));xs=range(len(rows));ax.bar(xs,[float(r[value]) for r in rows],color="#4c78a8")
    ax.set_xticks(list(xs),[r[x] for r in rows],rotation=45,ha="right",fontsize=8);ax.set_ylabel(label);ax.set_title(cap);ax.axhline(0,color="black",lw=.7);save(fig,plotdir/f"{stem}.png")

def main():
    ap=argparse.ArgumentParser();ap.add_argument("--capsule",type=Path,required=True);a=ap.parse_args();c=a.capsule
    plotdir=c/"figures/candidate";srcdir=c/"figures/source_csv";jobs=read(c/"canonical/wp015_jobs.csv");refs=read(c/"canonical/wp015_reference_cells.csv");cells=read(c/"canonical/wp016b_cells.csv");actions=[]
    conditions=["L2","L3","L4","M_ONLY","P_BOUNDARY","G_STRICT_BOUNDARY"]
    # 1 functional composition
    rec=[]
    for (cond,),g in group(jobs,["condition"]).items():
        for status,n in Counter(r["functional_status_class"] for r in g).items():rec.append({"condition":cond,"status":status,"n":n,"fraction":n/len(g),"source_ids":source(r["job_id"] for r in g if r["functional_status_class"]==status)})
    write(srcdir/"01_assigned_functional_composition.csv",rec);fig,ax=plt.subplots(figsize=(8,4.5));bottom=[0]*len(conditions)
    for status,color in [("PASS","#59a14f"),("MISMATCH","#e15759"),("INFRASTRUCTURE_UNKNOWN","#f28e2b"),("NOT_ATTEMPTED","#bab0ac")]:
        vals=[next((r["fraction"] for r in rec if r["condition"]==x and r["status"]==status),0) for x in conditions];ax.bar(conditions,vals,bottom=bottom,label=status,color=color);bottom=[a+b for a,b in zip(bottom,vals)]
    ax.set_ylabel("fraction of assigned jobs");ax.legend(fontsize=7);ax.set_title("Assigned-system functional outcomes");save(fig,plotdir/"01_assigned_functional_composition.png")
    # 2 signed distributions
    rec=[]
    for (cond,),g in group(jobs,["condition"]).items():
        vals=[float(r["functional_scientific_delta_mhz"]) for r in g if r["functional_scientific_delta_mhz"]!=""]
        rec.append({"condition":cond,"n_pass":len(vals),"mean_signed_mhz":statistics.fmean(vals) if vals else 0,"median_signed_mhz":statistics.median(vals) if vals else 0,"source_ids":source(r["job_id"] for r in g if r["functional_scientific_delta_mhz"]!="")})
    bars("02_signed_fmax_by_assigned_system",rec,"condition","MHz","Signed FMax among functional passes","mean_signed_mhz",plotdir,srcdir)
    # 3 reliability/upside
    rec=[]
    for cond in conditions:
        g=[r for r in jobs if r["condition"]==cond];vals=[float(r["functional_scientific_delta_mhz"]) for r in g if r["functional_scientific_delta_mhz"]!=""]
        rec.append({"condition":cond,"functional_pass_rate":len(vals)/len(g),"conditional_signed_mean_mhz":statistics.fmean(vals) if vals else 0,"assigned_lower_mean_mhz":statistics.fmean(float(r["assigned_utility_lower_bound_mhz"]) for r in g),"source_ids":source(r["job_id"] for r in g)})
    write(srcdir/"03_reliability_vs_conditional_upside.csv",rec);fig,ax=plt.subplots(figsize=(6,5));
    for r in rec:ax.scatter(float(r["functional_pass_rate"]),float(r["conditional_signed_mean_mhz"]),s=65);ax.text(float(r["functional_pass_rate"]),float(r["conditional_signed_mean_mhz"]),r["condition"],fontsize=8)
    ax.set_xlabel("functional-pass probability");ax.set_ylabel("conditional signed mean ΔFmax (MHz)");ax.set_title("Reliability versus conditional upside");save(fig,plotdir/"03_reliability_vs_conditional_upside.png")
    # 4 strict subsets
    subsets={"assigned_P":[r for r in jobs if r["condition"]=="P_BOUNDARY"],"strict_P":[r for r in jobs if r["condition"]=="P_BOUNDARY" and r["P_exclusive_realisation"]=="true"],"assigned_G":[r for r in jobs if r["condition"]=="G_STRICT_BOUNDARY"],"causal_G":[r for r in jobs if r["G_CAUSAL_RUNTIME_EXECUTED"]=="true"],"named_G_v4":[r for r in jobs if r["G_NAMED_FAMILY_PROVEN_V4"]=="true"]}
    rec=[]
    for name,g in subsets.items():rec.append({"subset":name,"n":len(g),"functional_pass":sum(r["functional_status_class"]=="PASS" for r in g),"beneficial":sum(r["beneficial_functional_tristate"]=="true" for r in g),"source_ids":source(r["job_id"] for r in g)})
    bars("04_strict_subsets",rec,"subset","functional pass count","Strict-realisation subsets","functional_pass",plotdir,srcdir)
    # 5 root x control pass
    rec=[]
    for (root,cond),g in group(jobs,["root_id","condition"]).items():rec.append({"root_id":root,"condition":cond,"pass_rate":rate(g,lambda r:r["functional_status_class"]=="PASS"),"n":len(g),"source_ids":source(r["job_id"] for r in g)})
    write(srcdir/"05_root_control_functional_success.csv",rec);heat("05_root_control_functional_success",rec,"root_id","condition","pass_rate","Functional-pass rate",plotdir)
    # 6 root x control delta
    rec=[]
    for (root,cond),g in group(jobs,["root_id","condition"]).items():
        vals=[float(r["functional_scientific_delta_mhz"]) for r in g if r["functional_scientific_delta_mhz"]!=""]
        rec.append({"root_id":root,"condition":cond,"mean_signed_mhz":statistics.fmean(vals) if vals else 0,"n_pass":len(vals),"source_ids":source(r["job_id"] for r in g)})
    write(srcdir/"06_root_control_signed_fmax.csv",rec);heat("06_root_control_signed_fmax",rec,"root_id","condition","mean_signed_mhz","Mean signed ΔFmax (MHz)",plotdir,cmap="coolwarm")
    # 7 model/control selection-realisation
    rec=[]
    for (model,cond),g in group(jobs,["model_key","condition"]).items():rec.append({"model":model,"condition":cond,"pass_rate":rate(g,lambda r:r["functional_status_class"]=="PASS"),"p_selected_rate":rate(g,lambda r:r["selected_language"]=="P"),"p_realised_rate":rate(g,lambda r:r["P_exclusive_realisation"]=="true"),"g_selected_rate":rate(g,lambda r:r["selected_language"]=="G"),"g_realised_rate":rate(g,lambda r:r["G_CAUSAL_RUNTIME_EXECUTED"]=="true"),"source_ids":source(r["job_id"] for r in g)})
    write(srcdir/"07_model_control_selection_realisation.csv",rec);heat("07_model_control_selection_realisation",rec,"model","condition","pass_rate","Functional-pass rate",plotdir)
    # 8 track required use
    rec=[]
    for (track,cond),g in group(jobs,["track","condition"]).items():rec.append({"track_condition":track+" / "+cond,"n":len(g),"p_realised":sum(r["P_exclusive_realisation"]=="true" for r in g),"g_realised":sum(r["G_CAUSAL_RUNTIME_EXECUTED"]=="true" for r in g),"source_ids":source(r["job_id"] for r in g)})
    bars("08_track_required_use",rec,"track_condition","realised cells","Natural, stress, and required-use capability","p_realised",plotdir,srcdir)
    # 9 functional exceptions
    rec=[]
    for status in ("MISMATCH","INFRASTRUCTURE_UNKNOWN"):
        g=[r for r in jobs if r["functional_status_class"]==status];rec.append({"status":status,"n":len(g),"source_ids":source(r["job_id"] for r in g)})
    bars("09_functional_unknown_mismatch",rec,"status","jobs","Functional mismatch and infrastructure unknown","n",plotdir,srcdir)
    # 10 reference action feasibility vs agent use (source module already addressable)
    rec=read(c/"modules/reference_action_feasibility/action_feasibility_vs_agent_use.csv");write(srcdir/"10_action_feasibility_vs_agent_use.csv",rec)
    fig,ax=plt.subplots(figsize=(7,5));
    for r in rec:ax.scatter(float(r["reference_functional_pass"]),float(r["agent_count"]),alpha=.7)
    ax.set_xlabel("reference functional-pass cells");ax.set_ylabel("agent declared/compiled/executed count");ax.set_title("Action feasibility versus agent use");save(fig,plotdir/"10_action_feasibility_vs_agent_use.png")
    # 11 WP016 heatmap
    rec=[]
    for (arm,cap),g in group(cells,["arm","capability"]).items():rec.append({"arm":arm,"capability":cap,"success_rate":rate(g,lambda r:r["primary_success"]=="1"),"n":len(g),"source_ids":source(r["cell_id"] for r in g)})
    write(srcdir/"11_wp016b_primary_endpoint_heatmap.csv",rec);heat("11_wp016b_primary_endpoint_heatmap",rec,"arm","capability","success_rate","Proposal certificate success",plotdir)
    # 12 paired contrasts
    rec=read(c/"canonical/wp016b_paired_contrasts.csv");
    for r in rec:r["label"]=r["capability"]+" / "+r["contrast"];r["source_ids"]=source(x["cell_id"] for x in cells if x["capability"]==r["capability"])
    bars("12_wp016b_paired_contrasts",rec,"label","mean paired difference","WP-016B paired contrasts","mean_difference",plotdir,srcdir)
    # 13 terminal composition
    rec=[]
    for (arm,terminal),g in group(cells,["arm","terminal"]).items():rec.append({"arm_terminal":arm+" / "+terminal,"arm":arm,"terminal":terminal,"n":len(g),"source_ids":source(r["cell_id"] for r in g)})
    bars("13_wp016b_terminal_composition",rec,"arm_terminal","cells","WP-016B typed terminal composition","n",plotdir,srcdir)
    # 14 nested denominators
    stages=[("assigned",jobs),("valid executable",[r for r in jobs if r["valid_executable_submission"]=="true"]),("physical valid",[r for r in jobs if r["physical_validity"]=="true"]),("functional pass",[r for r in jobs if r["functional_status_class"]=="PASS"]),("beneficial",[r for r in jobs if r["beneficial_functional_tristate"]=="true"])]
    rec=[{"stage":name,"n":len(g),"source_ids":source(r["job_id"] for r in g)} for name,g in stages]
    bars("14_evidence_stage_nested_denominators",rec,"stage","jobs","Nested evidence denominators","n",plotdir,srcdir)
    return 0

def heat(stem,rows,ykey,xkey,vkey,title,plotdir,cmap="viridis"):
    ys=sorted({r[ykey] for r in rows});xs=sorted({r[xkey] for r in rows});lookup={(r[ykey],r[xkey]):float(r[vkey]) for r in rows};matrix=[[lookup.get((y,x),float("nan")) for x in xs] for y in ys]
    fig,ax=plt.subplots(figsize=(max(6,len(xs)*1.1),max(4,len(ys)*.28)));im=ax.imshow(matrix,aspect="auto",cmap=cmap);ax.set_xticks(range(len(xs)),xs,rotation=40,ha="right",fontsize=8);ax.set_yticks(range(len(ys)),ys,fontsize=7);ax.set_title(title);fig.colorbar(im,ax=ax,shrink=.75);save(fig,plotdir/f"{stem}.png")

if __name__=="__main__":raise SystemExit(main())
