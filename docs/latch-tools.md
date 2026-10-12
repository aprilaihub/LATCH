# Tools, configuration and physical inputs

Physical execution uses a complete editable source checkout. Keep the root
validator, MCP servers, script-backed execution helpers and package data;
a wheel alone is not the supported physical deployment.

## External installations

Physical runs have been tested with Python 3.12.12, Vivado 2025.1, Java 21.0.1
and RapidWright `2fc8b6aec859c20c04218573f9dda09db0a72e3f`.
Match Vivado to the checkpoint and record actual versions. Check compatibility
separately when using other versions.
RapidWright includes external data/jars; follow its setup instructions and
licensing at the fixed revision. A gitlink alone is not an installation.

Install `.[tools]` into the selected environment. The exercised Python tool
bindings are `mcp==1.28.0`, `rapidwright==2026.1.0` and `pexpect==4.9.0`.
The RapidWright **Python binding version is not the Java checkout revision**:
keep the explicitly configured external Java/data/jars at the fixed revision
above; installing a Python binding does not update that checkout.

To install the tested Python dependencies in a fresh environment:

```bash
/absolute/venv/bin/python -m pip install -e '.[tools]' \
  'mcp==1.28.0' 'rapidwright==2026.1.0' 'pexpect==4.9.0'
/absolute/venv/bin/python -m pip check
```

Load the site's external license environment, if required, before physical commands.
There is no required checkout-local `.env`; recommended entries do not read one.
Supply API credentials through the chosen provider's environment; keep them out
of tool JSON and command-line arguments.

## One configuration for all recommended entries

Copy [the non-secret template](../examples/latch/tools.example.json) outside the
source tree and fill four paths: `python`, `vivado`, `rapidwright`, `java_home`.
Absolute paths are recommended; relative paths are resolved against the JSON
file's directory, not the caller's working directory. Additional acceptance
fields are not tool arguments. All four tool paths are required.

Use the configured Python to invoke the command; this matters for MCP and
validation, not just parsing. Interpreter symlinks retain virtual-environment
identity. Configuration builds Java classpath and executable search paths but
does not start tools, discover credentials, initialize a submodule or certify
a license.

```bash
/absolute/venv/bin/python -m optimizer_pipeline.apps.experiment /external/root.dcp \
  --tool-config /external/tools.json --run-dir /external/new/harness \
  --custom-pipeline optimizer_pipeline.apps.experiment.pipelines:one_physical_transition
```

This executes an operation but does not itself certify functional equivalence.
For fixed proposals plus explicit scoring/seeds follow [DoF prepared-real](../experiments/dof/README.md).
The `experiments.acceptance.harness` no-model check needs tool configuration
and a physical-run scope, with no model prices or API key. Model-backed batches
also need pricing and cost records. Templates require real captured objects and
strict-G validation; choose a template applicable to the input.

## Materials and interpretation

Prepare an initial routed DCP, matching constraints/clock, golden checkpoint,
functional material and an independent output location. Do not export DCPs,
capture/handle context, logs, databases or credentials with a source bundle.
DoF context is sensitive and root-bound; missing captures are not complete
empty observations.

RapidWright mutations also require a readable logical EDIF netlist for the
exact input checkpoint. Where it is not available inside the DCP, supply a
matching readable `.edf` sidecar with the same basename in the input directory.
For example, `root.dcp` and `root.edf` must describe the same design; an EDIF
from a different checkpoint is not a substitute. A separate capture that used
an explicit EDIF path does not make that path available to the mutation reader.
Keep input material preparation separate from mutation, and retain the original
checkpoint unchanged.

The fixed RapidWright fanout optimiser supports FDRE/FDSE/FDCE/FDPE and
LUT1–LUT6 drivers, not every high-fanout `WIRE` net. The research census and
selector can include nets that do not support replication. Preserve
unsupported-target results and the submitted target in research runs. A fixed
engineering check may choose a known supported target; report it separately
from the research result.

[The validation example](../examples/latch/wp015-validation.example.json) is the
example single-design validation policy: WP015 five seeds, 100 precheck / 200 vectors,
serial seeds, 20% memory reserve and original timeouts. It is not a global
default or statistical-improvement claim (`epsilon_fmax=0`). Study schedules
remain separate: DoF post-COMMIT multi-seed versus Ablation lightweight
candidates and selected-final-output validation.

Missing SDK/tool/material, incompatible environment or license failure stops
the affected path explicitly. MCP connection success and a later Vivado
implementation-license warning are different observations. Do not weaken
validation or silently select another installation to avoid a failure.

Read [the harness guide](latch-harness.md) for lifecycle, input protection and
resume. Inspect execution, validity, scoring, individual seeds and termination
separately; they are not a single `success` flag.
