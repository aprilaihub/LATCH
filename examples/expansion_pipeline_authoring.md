# Expansion Pipeline Authoring

Custom pipelines compose analysis and search operations over a managed run
context. The context handles budget accounting, records and result selection.

```python
async def my_pipeline(ctx):
    await ctx.analysis.run_once(mode="static_plan")
    await ctx.expand.fixed(["phys_opt"])
    await ctx.expand.linear(depth=2, policy="deterministic")
    await ctx.expand.frontier_if_budget(
        policy="deterministic",
        max_transitions=ctx.budget.remaining_eda_runs,
        parallelism=2,
    )
```

Run it through the application entry:

```bash
export LATCH_PYTHON=/absolute/configured/venv/bin/python
"$LATCH_PYTHON" -m optimizer_pipeline.apps.experiment /absolute/input.dcp \
  --run-dir /absolute/new/run \
  --tool-config /absolute/tool-config.json \
  --custom-pipeline your_module:my_pipeline \
  --budget-runs 6
```

`--tool-config` identifies the installed Python, Vivado, RapidWright and Java
paths. For a fixture run, replace it with `--mock` and use an existing fixture
input. See [the Harness guide](../docs/latch-harness.md) for configuration and
the supported toolchain.

Composition guidelines:

- Analysis is evidence-only and must not create DCP states.
- Use `ctx.expand` for ordinary search and `ctx.intervention` only for explicit
  intervention policy.
- Respect `ctx.budget`; do not launch backend execution directly.
- Let the context record run facts in `run.db` and export
  `result_manifest.json`.
- Query results with `load_run_view(ctx.run_dir)`.
- Provider-backed policies belong under `integrations/llm` and are injected by
  experiment/application entries.
