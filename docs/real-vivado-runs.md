# Real Vivado runs

Use a complete source installation, isolated Python and explicit four-path
configuration. [Tool setup](latch-tools.md) describes Python, Vivado,
RapidWright, Java and externally managed licenses. Recommended entries do not
read or require a checkout-local `.env`.

```bash
/absolute/venv/bin/python -m optimizer_pipeline.apps.experiment /external/input.dcp \
  --tool-config /external/tools.json --run-dir /external/new/harness \
  --custom-pipeline optimizer_pipeline.apps.experiment.pipelines:one_physical_transition
```

This executes an operation; functional validation is a separate step. For fixed
proposals, scoring and seeds use [DoF](../experiments/dof/README.md) or
[acceptance utilities](../experiments/acceptance/README.md). Input, golden,
constraints and validation material must match. Use new external output.
Model analysis may transmit design-derived context to the configured provider
and incur charges. MCP connection success differs from a later Vivado license
error. Do not replace versions or reduce validation to work around failure.
