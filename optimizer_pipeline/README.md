# optimizer_pipeline architecture

The managed optimizer is a DCP-state experiment runtime.

- `core/` owns immutable facts, validation contracts, SQLite storage, and query views.
- `run/` composes one managed run and one `run.db`.
- `capabilities/analysis/` gathers provider-neutral evidence without mutating DCPs.
- `capabilities/search/` schedules fixed, linear, and frontier transitions.
- `capabilities/intervention/` executes recipe/manual actions under intervention policy.
- `execution/` runs and validates one DCP transition.
- `backends/xilinx/` owns Vivado/RapidWright execution and probing.
- `apps/` owns contest, direct-action, and experiment entry points.

Provider clients, prompts, pricing, and model-specific loops are external under
`integrations/llm`. Selected `experiments/llm` modules forward for compatibility;
other historical experiment material remains separate.

```python
ctx = await bootstrap_run(...)
await ctx.analysis.run_once()
await ctx.expand.linear(depth=3, policy="deterministic")
await ctx.expand.frontier_if_budget(policy="deterministic", parallelism=1)
ctx.finalize()
ctx.close()  # put resource cleanup in finally in real code
```

`run.db` stores the run facts. `result_manifest.json` summarizes the selected
result for callers; DCPs, backend logs, reports, and journals hold the associated
artifacts and diagnostics.

Use [the complete general CLI/API guide](../docs/latch-harness.md) and
[directory/dependency map](../docs/latch-organization.md). The general custom
entry uses its own parser and configuration. `finalize()` exports a result;
functional certification is a separate operation. `resume_run()` requires an
existing database, checks root identity and recorded configuration/limits, and
rejects orphaned attempts.
