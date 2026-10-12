# Shared LATCH harness: entry, API, and lifecycle

The general entry is `python -m optimizer_pipeline.apps.experiment`.
Applications and studies define contest eligibility, DoF conditions and S0–S3
budgets over its shared capabilities. The root `dcp_optimizer.py` retains the
contest-compatible interface.

## Install and run from another working directory

Use a full checkout and an editable Python 3.12 installation as described in
[the root README](../README.md). A wheel alone does not contain retained MCP
servers or script-backed physical helpers. The general mock entry
uses packaged fixtures/examples rather than checkout-only scripts.

```bash
/absolute/venv/bin/python -m optimizer_pipeline.apps.experiment --help
/absolute/venv/bin/python -m optimizer_pipeline.apps.experiment /absolute/mock-input.dcp \
  --mock --run-dir /absolute/new/mock-run \
  --custom-pipeline optimizer_pipeline.apps.experiment.pipelines:one_physical_transition
```

The fixture input must already exist; mock never invents a physical checkpoint.
The run directory must be absent or empty. The selected output defaults inside
that new run directory; `--output` may name a different **new** file. Input,
existing output, and old run records cannot be reused as output targets. No
`.env` file is discovered automatically. CLI fields override `--config` only
when explicitly supplied (`--budget-runs` and `--parallelism`).

For a real run, omit `--mock` and supply `--tool-config` with four paths:
`python`, `vivado`, `rapidwright`, `java_home`. Absolute paths are recommended;
relative paths resolve against the JSON file's directory. Tool and
license installation is external; load the site's license environment before
invocation. The CLI uses the configured installations. For model-backed custom
code, configure the provider, credentials and spending limits in that code;
the general CLI does not provide the study batch's shared cost accounting.

## Minimal Python API

```python
from pathlib import Path
from optimizer_pipeline import DeterministicAnalyzer, OutputValidator
from optimizer_pipeline.run import OptimizationConfig, bootstrap_run, resume_run
from optimizer_pipeline.apps.experiment.mock import MockExecutor, mock_analysis_provider

async def run(input_path, new_directory):
    config = OptimizationConfig(max_total_runs=1, max_parallel_jobs=1)
    ctx = await bootstrap_run(
        input_dcp=Path(input_path), run_dir=Path(new_directory), config=config,
        analyzer=DeterministicAnalyzer(analysis_provider=mock_analysis_provider),
        executor=MockExecutor(), output_validator=OutputValidator(),
    )
    try:
        await ctx.analysis.run_once(mode="static_plan", trigger_reason="example")
        await ctx.expand.fixed(["phys_opt"], source="example")
        return ctx.finalize()
    except Exception:
        from optimizer_pipeline.models import PipelineStatus
        ctx.finalize(success=False, status=PipelineStatus.FAILED)
        raise
    finally:
        ctx.close()
```

Replace the fixture analyzer/executor with the existing
`build_real_analysis_provider` and `DeterministicFlowExecutor(run_test_mode=...)`
over explicitly configured `backends.xilinx`. Reuse those transport, execution
and validation services. Custom callables receive `ctx` and can compose
`ctx.analysis`, `ctx.expand`, and `ctx.intervention`; intervention retains its
existing authorization/permission rules. They may be sync or async. See the
installed `optimizer_pipeline.apps.experiment.pipelines` for small examples.

`finalize()` exports selected state and run status. It does **not** automatically
run functional certification or close SQLite; call `close()` in `finally`.
Execution success and a selected file are not physical validity, functional pass,
or formal equivalence. Read `run.db` validation records and study-specific reports.
The CLI returns nonzero for a failed pipeline and closes its own context.
The supplied mock services make no tool/model calls. Arbitrary custom Python
code is not sandboxed by this CLI; its author remains responsible for any
external side effects. For a single-design no-model physical check, see
[independent acceptance](../experiments/acceptance/README.md#independent-generic-harness-smoke).

## Resume without silently changing a run

Use `resume_run()` with the same root input, recorded configuration, limits, and
optional run id. It requires an existing database, verifies the root path and
already-recorded artifact digest, and rejects orphaned attempts. Resume requires
existing run records and does not automatically replay a candidate.
New runs record configuration/limits in an existing run-detail event. Historical
runs without that event can verify input and orphans but cannot prove original
configuration fidelity; check their external configuration before resuming.
Existing run records and artifacts provide the resume evidence.

The CLI starts a **new** run; resume is an explicit API operation. Existing run
directories are protected from overwrite, and resume restores recorded budget
usage.
