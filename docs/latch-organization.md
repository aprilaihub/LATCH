# LATCH reading map and module organization

```text
README.md -> docs/latch-harness.md -> selected study / extension guide
optimizer_pipeline/
  core/{domain,store,validation}/  immutable facts and canonical SQLite
  run/                           bootstrap/resume, configuration, lifecycle
  capabilities/                 analysis, search, intervention composition
  execution/                    one transition and output validation
  backends/xilinx/               tool execution, probing, owned processes
  apps/experiment/               general custom CLI and installed examples
  apps/fpl_contest/              retained contest application policy
  dof/                          retained transport/version-bound history
integrations/llm/                clients, profiles, model usage/pricing
experiments/
  dof/{protocol,runtime,analysis}/ WP015 study and curated/diagnostic analyses
  ablation/{runtime,processing,analysis}/ S0–S3 and ordered processing prefix
  acceptance/                   opt-in single-design evidence/budget orchestration
  llm/                          compatibility adapters AND retained historical material
tests/{architecture,core,latch}/ boundary, persistence, protocol and safe regression
docs/                           user guides, architecture and data interfaces
tools/source_inventory.py       read-only file-level source/notice inventory
```

```text
apps and studies -> run/capabilities -> execution/backends/core
apps and studies -> integrations/llm -> provider SDKs (loaded when needed)
experiments/acceptance -> existing DoF/Ablation entries, shared accounting
shared optimizer_pipeline -X-> experiments/dof or experiments/ablation
```

Dependencies on retained `optimizer_pipeline.dof` worker/permission transports and
script-backed macro bridges are deliberate current compatibility boundaries.
These paths support the current runtime. For historical WP015 collection and
data, see [data reproduction](latch-data-reproduction.md).
`experiments.llm.runtime` forwarding modules are compatibility paths, while
remaining old prompts/tool-loop support are historical material. New model
changes belong under `integrations/llm`.

Read general CLI/API first, then a study README, then its protocol/config and
runtime/analysis implementation. For paper evidence read
[data reproduction](latch-data-reproduction.md) and [sources](../SOURCE_CREDITS.md).
Keep generated outputs in independent directories outside the source tree so
logs, databases and checkpoints remain separate from code and historical inputs.

Root `validate_dcps.py` and `mcp_stdio_asyncio.py` are installed compatibility
modules used by the source-backed backend. MCP servers and script resources
remain relative to the installed editable checkout, not the caller's working
directory. Physical runs require the full source checkout and an editable
installation.
