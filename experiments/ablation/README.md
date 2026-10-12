# S0–S3 study package

This package studies fitted-heuristic versus model-guided frontier search,
branching and fixed knowledge. It owns conditions, policies, analysis context and fixed
knowledge base. Physical execution, common model clients and SQLite are reused.
Study conditions and analysis materials are described below.

## Study conditions

| Condition | Selection | Attempts / depth | Branching / beam | Knowledge |
|---|---|---|---|---|
| S0 baseline | fitted heuristic / auto_top8 | 3 / 3 | 1 / 1 | off |
| S0 extension | fitted heuristic / auto_top8 | +8 / 8 | 1 / 1 | off |
| S1 | analysis_priority | 11 / 11 | 1 / 1 | off |
| S2 | analysis_priority | 11 / 6 | 3 / 2 | off |
| S3 | analysis_priority | 11 / 6 | 3 / 2 | reference_manual |

S0 extension starts from the recorded baseline output, not again from the
original root. A separate invocation retains that phase boundary:

```bash
python -m experiments.ablation --condition S0 --mode mock --s0-phase extension --prior-run /chosen/s0 --input /path/reported/as/final_output_dcp --output /chosen/new/s0-extension
```

`analysis_priority` does not apply a new beam filter. Heuristic ranking remains
hidden from the LLM study context. The nine-flow recipe range and fixed knowledge
content are fixed study inputs; `_rw` flows are not silently added. Changes to
selection or stopping rules constitute a different experimental configuration.

## Physical invocation and validation

Use `--mode real --input /absolute/design.dcp --output /absolute/new/run
--tool-config /external/tools.json`.
S1–S3 additionally require `--provider NAME --model EXACT_MODEL
--cost-budget-usd ACCOUNTED_BUDGET`. The selected provider's configured
environment variable must be present and its SDK installed. Missing models,
SDKs or toolchain versions stop the invocation; conditions are not dropped.
No secrets are read from `.env` by this entrypoint.

Tool JSON explicitly supplies `python`, `vivado`, `rapidwright`, and `java_home`,
using the same schema as DoF. Invoke this entry with that Python environment;
the existing MCP/validator implementations use its `sys.executable`.
Vivado environment and Java classpath are constructed from those paths. The
RapidWright Python installation is checked. This study requests 8 Vivado
threads through `LATCH_VIVADO_MAX_THREADS`; the generic backend does not impose
that experimental default on unrelated applications. CPU affinity is recorded,
not automatically pinned by this entrypoint.

Candidates retain the original lightweight output check. The selected final
output receives one `DCPValidator` check (200 vectors, 100 precheck vectors),
with its temporary workspace inside the new run's `final-validation/`.
Results retain phase results, skipped checks and infrastructure failure flags.
Do not substitute this schedule for DoF's post-COMMIT multi-seed study.

## Processing is separate from drawing

`python -m experiments.ablation.analysis` only reads externally supplied
`data/runs_enriched.csv` and `data/nodes.csv`, and writes three round-6 PNG/PDF
pairs to a separate new directory. The source plot calculations are retained.
The figures label counts from the supplied tables and do not print
unverified fixed historical cost-accuracy or concurrency ratios. The six-stage
processing prefix supplies the tables used by round 6; later targeted studies
are separate. Partial participant/S0 inputs may be structurally drawable, but
are not a complete paper reproduction. Check the processing scope report.
Redrawing a supplied processed bundle is not raw re-extraction or proof of
the latest manuscript's data/asset mapping.

The processing entry covers a prefix, not the complete historical analysis chain.
`paper_analysis/RUN_ORDER.md` at `a722b1e` remains its instruction source. That
chain reads raw run trees and rewrites derived CSVs in order; it must not be run
on the final input bundle merely to draw figures. Some physical probes cannot
be regenerated without their source checkpoints. See
[data materials](../../docs/latch-data-reproduction.md).

The legacy `runner_cli.py` exists for imported helper compatibility and tests;
its CLI is not the recommended portable interface.

## Bounded participant processing entry

`python -m experiments.ablation.processing --config /absolute/path/config.json
--output /absolute/path/new-output` runs the historical processing prefix in
the authored order: `extract.py`, `firstpass.py`, `mech.py`, `verify.py`,
`coverage.py`, then `prep.py`. The JSON config must explicitly provide
an absolute `participant_roots` map using the stable participant labels
`cristian`, `matt`, and `shu`; each configured root points to a
`benchmark/level/model/repeat_N/` tree. The Matt root is required for the
legacy `matt`-bucket mechanism analysis in `mech.py` and V3 cost analysis in
`verify.py`. These names are inherited schema keys, not inferred ownership.
V2 follow-through scans configured roots in stable key order
`cristian`, `matt`, `shu`. Its output keeps the historical
columns; it does not add participant fields.
The config also needs an absolute `s0_sweep_root` and may include an
`s0_extra_roots` map for separate S0 runs. For example:

```json
{
  "participant_roots": {
    "cristian": "/external/raw/group-a/ablation_study",
    "matt": "/external/raw/group-b/ablation_study",
    "shu": "/external/raw/group-c/ablation_study"
  },
  "s0_sweep_root": "/absolute/path/to/s0_sweep",
  "s0_extra_roots": {}
}
```

This is the selected mutating rebuild path from phases 1–2 of
`paper_analysis/RUN_ORDER.md`, not a complete execution of those phases: it
omits phase-1 `inventory.py` and `checks.py`, plus phase-2 `claimcheck.py` and
`claimcheck2.py`. It also does not run phases 3–7 (targeted studies,
investigations, figures, or independent audits).

For compatibility, `participant: "matt"` plus `raw_run_root` remains accepted
and is treated as a one-participant map. No participant or raw-data path is
inferred from the machine. Each configured root needs at least one completed
S1, S2, and S3 run; the combined extraction also needs at least one S0 run.
The output path must be new and must be outside the configured raw trees;
the input trees are read-only and all tables are written under its `data/`
directory.

Install the `analysis` optional dependencies and SciPy before running; the
historical `coverage.py` imports `scipy.stats`. `extract.py`
reads `loop_decisions.jsonl` and `policy_candidates.jsonl` when present;
`mech.py` uses the configured `matt` bucket's `analysis_events.jsonl` token/cost
fields. In `verify.py`, V2 follow-through uses `node_diagnoses.jsonl` from
every configured participant root, while V3 cost analysis uses that bucket's
`analysis_events.jsonl` only. These files may be absent for individual runs;
the report records missing artifact counts, and the original analysis scripts
determine whether their derived tables can be produced. The scope report
records the available inputs and derived outputs.

`processing_report.json` records each stage's output row counts and run totals
(`num_runs`, available cost columns, and final Fmax when present). An optional
`--reference-processed /absolute/path/to/data` compares matching tables by row
count, run/node keys, and shared numeric columns. It reports differences without
stopping processing. The report names configured and unconfigured
participants and states the processing scope; this remains a prefix, not a
reproduction of later targeted studies, figures, and independent audits.
Plot generation remains separate.

## Optional single-design checks

The [single-design utilities](../acceptance/README.md) run S0 baseline (3 attempts)
and a separate extension (8 attempts) from its recorded output, then reuses S0
for the model-driven phase. S1–S3 retain their 11 execution budgets,
selection/knowledge rules and existing internal physical scheduling.
`--acceptance-config` opts into shared request/cost accounting and owned smoke
diagnostics; no smoke-specific limit or failure policy becomes a global default.
Final smoke functional checks use the original input as golden. A check's
coverage is limited to its actual design/condition/tool execution, not a full
matrix. Early stopping without final output remains incomplete coverage.
