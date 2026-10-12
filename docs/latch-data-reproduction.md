# Data packages and reproduction

Source installation excludes datasets; their redistribution rights are separate.
Read inputs only; write derived tables and figures to new directories. Selected
DoF data, historical diagnostics and S0–S3 processed/raw data are separate routes.
New smoke output never substitutes for paper data.

## Selected DoF dataset

`experiments/dof/configs/manuscript-20260930.json` selects 16 designs, seven
models and three conditions with the `R1_LEAN_PRIMARY` repeat selection.
The matching `draft_scoped_trials.csv` has 336 rows, 112 per condition.
This identifies a data-package version, not the final manuscript's complete
input population. Mapping it to a paper revision requires that revision's
selection, captions and analysis definitions, not just matching row counts.

```bash
python -m experiments.dof.analysis.curated --trials /external/draft_scoped_trials.csv \
  --selection /checkout/experiments/dof/configs/manuscript-20260930.json \
  --output /external/new/curated
```

Missing/duplicate cells stop processing. Cumulative stages are proposal,
execution, physical, functional and improved. Denominators include all selected
trials: 16 per model, 112 pooled. The direct/constructed matrix uses functionally
passing measurements and the same design baseline. Preserve missing values,
zero and negative gains. The matching package has 70 per-model stage values and
224 matrix cells (193 numeric, 31 missing). SVGs are diagnostic redraws, not
manuscript-asset replacements. Processed checks do not establish raw reproduction.

## Historical WP015 diagnostic corpus

```bash
python -m experiments.dof.analysis --scientific /external/scientific.json \
  --bundle /external/bundle.json --development /external/development.json \
  --references /external/references.json --output /external/new/wp015-diagnostics
python -m experiments.dof.analysis.plot --input /external/new/wp015-diagnostics \
  --output /external/new/wp015-figures
```

Requires matching aggregates, bundle bindings and reference materials, including
140 development cells and 110 post-agent cases. Missing/mismatched material
stops the route. Historical collectors
also require version-bound protocol documents and bundles as external
reproduction materials in their expected layout. Recommended current entries
do not depend on those archived documents. WP016 is separate history, not WP015
physical evidence; the 336-row subset cannot replace this corpus.

Retained history entry points require explicit external locations. Set
`LATCH_HISTORICAL_WORKSPACE` to the root of the matching historical material
layout for WP016 input protection and optional evidence-dependent tests.
The protected subdirectories must exist; missing configuration stops live
history entries before dispatch. WP013 auditing accepts `--runs` or
`--historical-workspace`; the M10 reference-panel builder requires
`LATCH_WP013_PANEL_PATH`. WP013B plan generation requires
`LATCH_WP013B_LOGICNETS_DCP`, and WP012 static analysis requires
`LATCH_WP012_HOLDOUT_AUDIT`. These location settings do not replace the
retained content, version, checksum, admission or scientific checks.

Version-bound records retain their original workflow/schema labels for reader
compatibility. Historical packaging resources belong to the matching external
materials. The historical WP015 core freezer additionally requires the complete original
source tree, including the original packaging resource named in its component
list. Use that historical tree to reproduce its component identities; the
current source copy does not recreate it. Missing components stop the freezer
before it writes a result.

## S0–S3 processed package and raw processing

The package selected at source revision `a722b1e` contains
`data/runs_enriched.csv` (256 runs) and `data/nodes.csv` (2677 nodes including
roots). Conditions: S0 16; S1/S2/S3 80 each. Repeats are not balanced.
Execution success differs from functional checking: 242 checks passed, 8 failed,
6 skipped. Measured cost covers 90 rows; reconstructed cost is not an invoice.
These describe that package, not an independently verified final-paper population.

```bash
python -m experiments.ablation.processing --config /external/processing.json \
  --output /external/new/processed
python -m experiments.ablation.analysis --input-root /external/processed-package \
  --output /external/new/figures
```

Processing writes extract → firstpass → mech → verify → coverage → prep in
order; plotting reads processed tables only. This prefix is not all historical
checks, targeted analyses or physical probes. See the [schema/material requirements](../experiments/ablation/README.md#bounded-participant-processing-entry).
Partial inputs can produce intermediate tables, but later stages require the
original schema's groups. Statistics from partial inputs describe only that
population.

Complete raw reconstruction has not been established for all supplied tables.
Original physical-probe DCPs are not supplied and are required to regenerate
those probes. Missing raw materials mean **processed redraw only**, not complete
reproduction. Preserve failures, missing/unexecuted checks, denominators and
aggregation rules. New single-design checks have independent results; see
[check utilities](../experiments/acceptance/README.md) for result semantics.
