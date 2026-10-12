# CLI Reference

This reference lists the contest-compatible command-line options and Makefile
variables. Their implementations are `optimizer_pipeline/apps/fpl_contest/args.py`
and `Makefile`; the shared harness has its own [CLI and API guide](../latch-harness.md).

## Main Command Shape

```bash
python dcp_optimizer.py INPUT.dcp [options]
```

Independent application entries:

```bash
"$PYTHON" -m optimizer_pipeline.apps.direct_action INPUT.dcp --action phys_opt
"$PYTHON" -m optimizer_pipeline.apps.experiment INPUT.dcp --custom-pipeline package.module:run
```

Set `PYTHON` to the explicitly configured isolated interpreter. Recommended
Harness/study entries use tool JSON; legacy scripts may consume externally
supplied environment values. No checkout-local `.env` is required.

## Main Options

| Option | Values | Default | Meaning |
|---|---|---|---|
| `INPUT.dcp` | path | required | Input design checkpoint. |
| `--output`, `-o` | path | `<input>_optimized-<timestamp>.dcp` | Output optimized checkpoint. |
| `--run-dir` | path | `dcp_optimizer_run-<timestamp>` | Run artifact directory. |
| `--config` | path | `configs/frontier_expansion_default.json` | Optimization config JSON. |
| `--mock` | flag | false | Use mock analysis and execution without Vivado. |
| `--no-dotenv` | flag | false | Do not read repo or cwd `.env` files automatically. |
| `--run-mode` | `contest-strict`, `research-dev` | `contest-strict` | Network and research-mode policy. |
| `--pipeline` | `contest_default`, `fixed`, `linear`, `frontier`, `analysis_single_path`, `analysis_frontier`, `analysis_single_path_frontier`, `custom` | `contest_default` | Built-in workflow profile. |
| `--policy` | `analysis_priority`, `deterministic`, `reliability`, `retrieval` | `deterministic` | Candidate selection policy. |
| `--analysis` | `none`, `static`, `fast_llm`, `deep_llm` | `none` | Optional analysis before expansion. |
| `--llm-provider` | `none`, `openrouter`, `openai`, `local` | `none` | Provider label for LLM-backed modes. |
| `--plan` | comma-separated flows | none | Required for `--pipeline fixed`. |
| `--budget-runs` | integer | `6` | Maximum transition executions. |
| `--single-path-depth` | integer | `3` | Depth for `--pipeline linear`. |
| `--parallelism` | integer | `1` | Concurrent transition executions for frontier phases. |
| `--custom-pipeline` | `module:function` | none | Required for `--pipeline custom`. |
| `--rescue-mode` | `off`, `deterministic`, `llm`, `synthesis`, `synthesis_loop`, `manual_loop` | `off` | Optional no-improvement rescue branch after the main profile. |
| `--rescue-trigger` | `no_improvement`, `always` | `no_improvement` | Gate for whether rescue may run. |
| `--rescue-max-runs` | integer | `1` | Maximum rescue transitions after the main profile. |
| `--rescue-max-episodes` | integer | `1` | Maximum manual-loop episodes. |
| `--rescue-min-main-delta-mhz` | float | `0.1` | Main-profile delta threshold for `no_improvement`. |
| `--rescue-synthesis-max-steps` | integer | `8` | Maximum primitive steps for synthesized rescue recipes. |
| `--rescue-synthesis-fallback` | `deterministic`, `fail` | `deterministic` | Fallback behavior when synthesis returns no valid recipe. |
| `--rescue-loop-policy` | `fixed_attempts`, `stop_after_first_improvement`, `external` | `fixed_attempts` | Attempt-consumption policy for `synthesis_loop`; benchmark ROI remains outside the rescue module. |
| `--rescue-parent-strategy` | `current_best`, `root`, `round_robin_safe_parents` | `current_best` | Parent selection strategy for `synthesis_loop` attempts. |
| `--rescue-manual-parent-selector` | `last_scratch`, `current_best`, `root` | `last_scratch` | Checkpoint parent selector inside `manual_loop`. |
| `--rescue-manual-min-publish-delta-mhz` | float | `0.01` | Minimum clean manual-loop gain before replacing the incumbent. |
| `--rescue-manual-allow-nonclean-publish`, `--no-rescue-manual-allow-nonclean-publish` | flag | false | Permit WARNING/FALLBACK_USED manual checkpoints to publish. |
| `--rescue-evidence-mode` | `compact`, `planner` | `compact` | Evidence context used by rescue synthesis. |
| `--rescue-history-scope` | `in_run`, `known_public` | `in_run` | History visible to rescue synthesis. |
| `--rescue-enable-act-probes` | flag | false | Enable limited act-probe evidence tools. |
| `--rescue-max-act-probes` | integer | `0` | Maximum act-probe calls. |
| `--rescue-act-probe-wall-sec` | seconds | `300.0` | Wall-time cap for act probes. |
| `--rescue-allow-invalid-scratch`, `--no-rescue-allow-invalid-scratch` | flag | true | Legacy manual-loop invalid-scratch continuation switch; invalid output remains nonpublishable. |
| `--api-key` | string | environment | Provider API key override. |
| `--model` | provider model label | provider/config default | Model label for LLM policy or analysis loops. |
| `--llm-cost-budget-usd` | float | none | Optional provider-cost cap for the run. |
| `--knowledge-mode` | `off`, `reference_manual` | config/default `off` | Inject static optimizer flow cards into analysis LLM prompts. |
| `--knowledge-base-path` | JSON path | bundled manual | Optional override for the reference manual JSON. |
| `--debug` | flag | false | Enable verbose artifacts. |
| `--max-nets` | integer | `5` | Max high-fanout nets for deterministic test flows. |

## Pipeline Profiles

| Profile | Meaning |
|---|---|
| `contest_default` | Deterministic single-path warmup, then deterministic frontier with remaining budget. |
| `fixed` | Run exactly the flows listed in `--plan`. |
| `linear` | Run one single chain of candidate changes. |
| `frontier` | Explore candidate changes from the current frontier. |
| `analysis_single_path` | Run analysis, then `linear`. |
| `analysis_frontier` | Run analysis, then `frontier`. |
| `analysis_single_path_frontier` | Run analysis, then `linear`, then `frontier` with remaining budget. |
| `custom` | Load `--custom-pipeline module:function`. |

## Rescue Options

Rescue is an opt-in tail branch that can run after any main profile and before
finalization. It is documented in
[`../rescue-approach-mental-model.md`](../rescue-approach-mental-model.md).

Examples:

```bash
"$PYTHON" dcp_optimizer.py INPUT.dcp \
  --run-mode research-dev \
  --pipeline linear \
  --budget-runs 2 \
  --rescue-mode deterministic \
  --rescue-trigger no_improvement
```

```bash
"$PYTHON" dcp_optimizer.py INPUT.dcp \
  --run-mode research-dev \
  --pipeline fixed \
  --plan phys_opt \
  --budget-runs 2 \
  --llm-provider openai \
  --rescue-mode synthesis \
  --rescue-trigger always
```

```bash
"$PYTHON" dcp_optimizer.py INPUT.dcp \
  --run-mode research-dev \
  --pipeline linear \
  --single-path-depth 0 \
  --budget-runs 3 \
  --llm-provider openai \
  --rescue-mode synthesis_loop \
  --rescue-trigger always \
  --rescue-max-runs 2 \
  --rescue-loop-policy fixed_attempts \
  --rescue-parent-strategy round_robin_safe_parents
```

## Direct Action Values

The direct-action app requires `--action`; it never infers an action from the
input filename. `--parameters-json` accepts a JSON object of action parameters.

```text
fanout, pblock, vexriscv, phys_opt, auto_pblock, lut_cone, retime,
net_swap, memory_path_opt, hardblock_relocate, congestion_spread,
memory_path_opt_rw, hardblock_relocate_rw, congestion_spread_rw, auto_top3,
rescue_place_reset_retime, synthesized_recipe
```

## Makefile Targets

| Target | Meaning |
|---|---|
| `make setup` | Install dependencies, build or prepare RapidWright, check tools, download benchmarks. |
| `make run_optimizer DCP=<file>` | Run the profile shell on one DCP. |
| `make run_optimizer_all` | Run profile shell on benchmark DCPs. |
| `make run_action DCP=<file> FLOW=<action>` | Run one explicitly named backend action. |
| `make run_experiment DCP=<file> CUSTOM_PIPELINE=<module:function>` | Run a generic managed custom experiment. |
| `make validate GOLDEN=<dcp> REVISED=<dcp>` | Run structural checks and sampled functional validation. |
| `make validate_demo` | Run validation self-check. |
| `make clean` | Remove generated run/log/Vivado output directories. |
| `make veryclean` | Deep clean generated files and benchmark downloads. |

## Makefile Variables

The contest parser owns all defaults. An unset Make variable does not add a
flag, so `make run_optimizer DCP=<file>` has exactly the same defaults as the
main CLI. Setting a variable is an explicit override.

| Variable | Values |
|---|---|
| `PYTHON` | Explicit isolated Python executable for tool-backed runs. |
| `VIVADO_EXEC` | Path to Vivado executable. |
| `DCP` | Input DCP for Makefile run targets. |
| `RUN_MODE` | `contest-strict` or `research-dev`. |
| `PIPELINE` | Same values as `--pipeline`. |
| `POLICY` | Same values as `--policy`. |
| `ANALYSIS` | `none`, `static`, `fast_llm`, or `deep_llm`. |
| `LLM_PROVIDER` | `none`, `openrouter`, `openai`, or `local`. |
| `BUDGET_RUNS` | Maximum transition executions. |
| `SINGLE_PATH_DEPTH` | Depth for `PIPELINE=linear`. |
| `PARALLELISM` | Concurrent transition executions. |
| `PLAN` | Comma-separated flow list for `PIPELINE=fixed`. |
| `CUSTOM_PIPELINE` | `module:function` for `PIPELINE=custom`. |
| `FLOW` | Required explicit action for `run_action`. |
| `MOCK` | `1`, `true`, or `yes` to add `--mock`. |

LLM-backed modes may send DCP-derived summaries or reports to the provider. See
[Real Vivado Runs](../real-vivado-runs.md) before using them.
