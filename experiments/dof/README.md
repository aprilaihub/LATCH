# DoF control-language study

This study compares recipe selection, direct operations and constructed
procedures over shared physical capabilities. It provides mock, fixed-response
`prepared-real`, and fresh-provider `real` trials using the WP015 study protocol.
Historical WP015 collection additionally requires its version-bound code and
external bundles. WP016B is a separate study of model proposals, with different
evidence from the physical trials described here.

## Runtime path

```text
fresh read-only capture or supplied root-bound observation / handles
  -> fixed messages or the configured provider/model
  -> study-owned WP015-derived protocol and strict-G checks
  -> one COMMIT, then terminal (no repair after COMMIT)
  -> shared compiled-operation executor
  -> shared read-only timing scoring and explicitly configured sampled validation
  -> separate SQLite execution, physical, functional and research records
```

The recipe / direct / constructed boundaries use `M_ONLY`, `P_BOUNDARY` and
`G_BOUNDARY`. The protocol allows 4 planning turns, 8 queries and 3 feedback
rounds. Compiler bodies and shared typed-dispatch bodies are checked against
the historical implementations. Workers enforce operation permissions,
attestation, budgets and root checkpoint protection.

Model-visible construction feedback uses the WP015 `32d852a` field projection,
including the digest of that projected compiled payload. Execution plans,
transcripts and stored result feedback retain the current richer lineage;
model presentation does not change construction or strict-G decisions.

The isolated worker/transport helpers remain under `optimizer_pipeline.dof`
for compatibility. Historical collector scripts retain their source-revision
checks and their original collection layout.

## Prepared physical dispatch

```bash
python -m experiments.dof --condition direct --mode prepared-real --input /external/root.dcp --context /external/captured-context.json --messages /external/fixed-messages.json --python /tools/worker/python --vivado /tools/vivado --output /chosen/new/prepared-trial
```

The context must contain the exact `root_sha256`, captured `observation`,
`trial_id`, `handle_secret_hex`, registered `objects` and the compiler/query
context needed by those saved responses. Keep this sensitive context in the
external run workspace. Missing query captures are marked `not_captured`.
The public summary omits the secret. Context must come from the checkpoint's
capture and registry; a screenshot or model-proposal record cannot supply it.

Messages are JSON objects in an array (`DRAFT`, `VALIDATE`, queries, `COMMIT` as
allowed by the retained protocol). This entrypoint makes **no model calls**.
An invalid proposal is an experimental terminal, not automatically a code
failure. Execution runs at most once, after a valid COMMIT.

## Fresh trial

```bash
python -m experiments.dof --condition direct --mode real --input /external/root.dcp --golden /external/golden.dcp --tool-config /external/tools.json --provider EXACT_PROFILE --model EXACT_MODEL --cost-config /external/cost.json --validation-config /external/validation.json --output /chosen/new/trial
```

Use a full editable checkout installed in both the CLI and worker
interpreters. `tools.json` explicitly supplies `python`, `vivado`,
`rapidwright` (checkout containing `jars/`) and `java_home` (containing
`bin/java`). Alternatively provide all four corresponding CLI flags. Real
mode uses the configured installations; physical execution is needed to check
their operation together. Use this configuration for prepared-real validation
too; its dispatch-only
interface is retained for compatibility with explicitly managed environments.
`--capture /external/capture` reuses a complete root/clock-bound
capture; otherwise a new sibling `<output>-capture` is created. Historical
runners retain their separate Git/bundle checks. `--context` alternatively
uses an explicitly supplied sensitive context. This entry requires neither a
`.research-flow` installation nor a historical pricing snapshot.

`validation.json` explicitly supplies `target_clock`, unique uint32 `seeds`,
integer `vectors`, integer `precheck_vectors`, `epsilon_fmax`, and positive
`prepare_timeout_seconds`, `seed_timeout_seconds`, `score_timeout_seconds`.
Custom seed/vector plans are supported and recorded in new results. The WP015
seed/vector profile uses seeds `[12648430, 1592594996, 464367618, 219540062,
168939093]`, 200 vectors per seed and a 100-vector precheck. Matching this
profile describes those parameters, not the whole protocol or formal
equivalence. The retained MemoryAwareSeedScheduler runs serially;
its default unknown/high memory estimate is 64 GiB, normal is 8 GiB, with a
20 percent memory reserve. Optional `seed_memory_class` and
`memory_reserve_fraction` expose those policies. Memory refusal is a recorded
unexecuted infrastructure result, not an unlimited wait or a reduced seed set.

`cost.json` explicitly supplies `provider`, `model`, `price_source`,
`verified_at`, `verified_current: true`, `rates_cover_all_billed_tokens: true`,
positive `input_usd_per_million`, `output_usd_per_million`,
`accounted_budget_usd`, `timeout_seconds`, and integer `max_input_bytes`,
`max_output_tokens`, `max_requests` (at most four). The flags record reviewed
price evidence; the CLI cannot independently certify a price source. Unknown
prices or usage stop requests. Reservations are conservative accounted bounds,
**not a provider-enforced hard bill cap**. SDK retries are explicitly disabled;
there is no provider/model fallback or post-COMMIT repair.

To validate a known-legal fixed proposal, use `prepared-real` with
`--validation-config` and `--golden` to exercise scoring and all seeds without
making model calls. Without these flags prepared-real remains a dispatch-only
compatibility probe, with functional validation `NOT_RUN`.

## Result interpretation and remaining limits

Offline fixtures test the software path; single-design checks cover only the
conditions, design and provider actually executed. Historical collector
reproduction requires the corresponding external materials. Invalid model
proposals remain research results.

In real mode a scientific gain requires a COMMIT, physical validity, measured
scoring and the configured functional pass;
the measured delta can still be zero or negative. Mock physical validity stays
unknown. A dispatch-only prepared probe does not establish functional validity.

New `summary.json` and `post-commit.json` include a nonsecret `validation_plan`
(seeds, vectors per seed and requested precheck vectors) and a
`validation_assessment`. Read `display_status`, `wp015_profile_match` and
`execution_complete` together: a complete custom plan is `CUSTOM_SAMPLED_PASS`;
an observed complete passing WP015 seed/vector profile is
`WP015_PROFILE_SAMPLED_PASS`. Matching configuration alone is not a pass.
Absent precheck/count evidence is unverified, not assumed complete. A plan
provided for an uncommitted or physically failed trial remains `NOT_RUN`.
Legacy `MULTISEED_FUNCTIONAL_PASS`, `FUNCTIONALLY_CERTIFIED_MEASURED` and
`passed` machine fields are retained for compatibility and describe the
configured validation, not independent WP015 or formal-equivalence
certification. Older results without a recorded plan have unknown profile
scope; they are not relabelled or rewritten.

Record the code/configuration and prompt version for new experiments. Physical
tool coverage alone does not establish historical planning-request equality.

## Explicit manuscript subset

```bash
python -m experiments.dof.analysis.curated --trials /external/draft_scoped_trials.csv --selection /checkout/experiments/dof/configs/manuscript-20260930.json --output /chosen/new/curated
```

The supplied selection requires exactly one trial for each of 16 designs,
seven models, three conditions and the named primary repeat: 336 rows. Missing
or duplicate cells stop processing. Stage rates use all 16 selected trials per
model as denominator. Matrix improvements use the same design baseline and
only functionally passing measurements, preserving negative/zero deltas and
missing values separately. Inputs are read-only and output must be new.

The matching selected package has 70 stage cells and 224 matrix cells
(193 numeric/31 missing). SVGs contain native text/geometry. This is a specified
data package, not a claim of final-manuscript scope or raw WP015 reproduction.

## Historical analysis and plots

`python -m experiments.dof.analysis` takes explicit scientific, bundle,
development-reference and post-agent-reference JSONs. It retains the original
requirements: 1134 physical jobs, 140 development cells, 110 post-agent
references, and matching bundle bindings. Missing/incomplete inputs stop it.
The analysis retains its source hashes and version checks.

The original analysis writes canonical rows/tables and diagnostic figures to a
new directory. `python -m experiments.dof.analysis.plot --input ANALYSIS_DIR
--output NEW_FIGURE_DIR` redraws only those diagnostic figures without recomputing
or rewriting input tables. Both are source-derived interfaces; the complete
historical corpus requires the matching external materials described in
[data reproduction](../../docs/latch-data-reproduction.md).

## Optional single-design checks

The [single-design utilities](../acceptance/README.md) reuse this entrypoint and its
WP015 protocol, scoring and serial five-seed validation. `--acceptance-config`
opts into a shared durable request ledger and bounded client (OpenAI direct or
explicit OpenRouter ZDR configuration; see the single-design guide). Study
defaults remain unchanged. Fixed-proposal and model-driven phases are configured
separately. Physical coverage comes from executed tool checks.
