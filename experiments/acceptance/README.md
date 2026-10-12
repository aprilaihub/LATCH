# Single-design check utilities

These optional checks compose the Harness, DoF and Ablation entries to exercise
execution, scoring and validation on one design. Configure external input/golden,
tools, validation material and a new output directory. Historical inputs and
results remain read-only.

## Checklist, tools and reports

```bash
python -m experiments.acceptance --config /external/smoke.json
python -m experiments.acceptance --config /external/smoke.json --phase report
python -m experiments.acceptance --config /external/smoke.json --phase tools --authorized-tools
```

The default checklist creates no model client or tool process. The tools phase
checks versions, prepares golden/precheck, captures real objects and runs fixed legal
proposals through execution, scoring and validation. Templates must fit the
captured registry and strict-G rules.

`smoke-april.example.json` retains its filename for compatibility; copy it
outside source and supply paths for the actual installation. Use isolated
Python 3.12 and a full editable checkout. See [tool setup](../../docs/latch-tools.md).
No `.env`, credential or personal directory is discovered. Configure exact tool
versions, design/clock and validation material explicitly.

DoF uses at most one COMMIT per condition. The WP015 seeds are `0xC0FFEE`,
`0x5EED1234`, `0x1BADB002`, `0x0D15EA5E`, `0x0A11CE55`: 100 precheck vectors,
200 per seed, serial seeds and 20% memory reserve. Example timeouts are 10800 s
preparation, 3600 s/seed, 900 s scoring and 7200 s execution, not global defaults.
Zero epsilon records raw deltas, not statistical significance. S0 runs a
three-attempt baseline, then a separate up-to-eight-attempt extension from its
recorded output; early stopping and validation schedules remain unchanged.

## Independent generic-harness smoke

```bash
python -m experiments.acceptance.harness --config /external/tools-and-validation.json --output /external/new/harness
python -m experiments.acceptance.harness --config /external/tools-and-validation.json --output /external/new/harness --phase run --authorized-tools
python -m experiments.acceptance.harness --config /external/tools-and-validation.json --output /external/new/harness --phase report
```

This no-model check executes one fixed `phys_opt` transition through the general
CLI, then shared scoring/five-seed validation of that candidate. It does not
modify a study/cost ledger. The general Harness owns no paper conditions.

## Independent fixed DoF check

```bash
python -m experiments.acceptance.prepared --config /external/tools-and-validation.json --output /external/new/dof
python -m experiments.acceptance.prepared --config /external/tools-and-validation.json --output /external/new/dof --phase run --authorized-tools
python -m experiments.acceptance.prepared --config /external/tools-and-validation.json --output /external/new/dof --phase report
```

Reuses `prepared-real` for three fixed physical interfaces without a model
client. Infrastructure failure stops and preserves partial coverage. It neither
executes S0–S3 nor changes an earlier result.

The report separates execution/scoring/five-seed coverage from the constructed
strict-G certificate. Finished conditions with absent evidence are reported as
`CONDITIONS_FINISHED_COVERAGE_INCOMPLETE`, not full capability coverage. A
negative measured gain is a research result, not missing execution coverage.

## Paid checks and continuation

Optional entries are `python -m experiments.acceptance.paid` and
`python -m experiments.acceptance.continuation`; use `--help` for explicit
config/prior/source-tools/supplement inputs. They use the configured exact
OpenAI model and shared durable SQLite request/cost reservations. Total and
per-condition limits differ; conditions are serial, S2/S3 retain internal
physical parallelism. The `paid` entry requires `--authorized-tools`,
`--authorized-paid` and `--accepted-limited-a`; the last flag acknowledges the
fixed-proposal and supplement reports' limited coverage. The `continuation`
entry requires `--authorized-conservative-continuation`. Review current pricing
and configured bounds before running.

The `paid` entry accepts only the specific stopped S0-extension source state
and matching passing supplement checked by `paid.accepted_evidence`; other
source states are rejected. For general model-driven trials, use the
[DoF](../dof/README.md) or [ablation](../ablation/README.md) study entry.

Accounted thresholds are not supplier-enforced billing caps. Unknown usage,
identity/authentication errors, uncertain requests or insufficient reservation
stop later calls. SDK/adapter retries, automatic probes and model fallback are
disabled. Do not replay started conditions, submitted candidates or uncertain
requests. Continuation carries prior costs and only starts eligible conditions
not previously started; it does not reset limits. This continuation entry supports
only a source batch stopped after an explained physical rejection in
`dof-recipe`, with version checks reached and that condition incomplete. Other
stopped states are rejected. Invalid proposals and normal early stopping remain
research outcomes.

## Explicit OpenRouter ZDR client

The study entries' `--acceptance-config` can also select the opt-in
OpenRouter client through `cost.provider="openrouter"`. The legacy `paid`
and `continuation` organizers above retain their original OpenAI configuration;
do not pass a router configuration to those organizers.

This bounded client supports `openai/gpt-5.6-luna` with medium reasoning.
Supply `ledger_path`, the existing request/token/timeout/price limits in `cost`,
and an explicit `cost.endpoint` containing `model_id`, `tag`, `provider_name`,
`supported_parameters` and `zdr_verified=true`. Record the exact allowed
`cost.response_models` identifiers and fresh endpoint/pricing evidence in
`price_source` and timezone-qualified `verified_at`; evidence older than 24 hours
is rejected. `zdr_verified` is an operator assertion, not an automatic audit:
check the model endpoint against OpenRouter's current ZDR catalog first.
Use conservative rates covering applicable context tiers, cache writes and
reasoning output. No credentials belong in the configuration; explicitly set
`OPENROUTER_API_KEY` in the invoking environment.

Requests enforce `provider.zdr=true`, one endpoint, no fallback and required
parameters. Transport performs one attempt and refuses redirects. Parallel
physical workers use the same SQLite ledger and serialize model exchanges;
an uncertain earlier exchange blocks later calls rather than being released.
The ledger records provider-reported cost separately and accounts the greater
of it and the conservative token estimate. Missing usage, unexpected identities
or reservation overruns stop subsequent calls. Retain the ledger for continuation
so earlier requests and costs remain accounted for.

## Supplement and result interpretation

`python -m experiments.acceptance.supplement` checks the unique recorded pruned
non-root S0-extension candidate specified by an existing source configuration.
It requires new output and does not resume search or promote an earlier result.
Missing/ambiguous material stops it. It uses Ablation's 100-vector precheck and
200-vector single seed `0xDEADBEEF`, not DoF's five-seed schedule.

Reports separate actual execution, physical validity, scoring, each functional
seed, termination and accounted usage. `--phase report` is read-only; exit code
zero is not full-chain coverage. Missing/skipped checks are not passes; negative
gains stay visible and sampled simulation is not formal equivalence. Input
protection and owned-process cleanup remain active. Raw logs and captured handle
contexts stay in restricted external output, not source/shareable summaries.
