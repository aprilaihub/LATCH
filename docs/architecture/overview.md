# Architecture Overview

LATCH is a reusable FPGA/EDA experiment runtime with Xilinx Vivado and
RapidWright as its first backend.

```text
apps -> run orchestration -> capabilities -> execution contracts -> backend
                  |                 |
                  +-------> core domain/store <-------+

integrations/llm -> provider-neutral capability injection
experiments/dof or ablation -> shared capabilities and provider injection
```

`core` has no app, backend, provider, prompt, model, or experiment dependency.
Applications own policy and result export. Analysis gathers evidence without
changing the DCP; search and intervention follow their configured execution
permissions. Backend code owns tool lifecycle and flow implementation. Every
managed run writes immutable facts to one SQLite database and exposes derived
views through query functions.

Research packages own conditions, prompts/knowledge and validation timing;
the shared runtime does not import them. `experiments/llm` contains compatibility
forwarders and historical support; new provider code belongs in
`integrations/llm`. See [the current module map](../latch-organization.md) for
extension points.

See [State Graph and Canonical Facts](state-graph-and-records.md) and
[Outputs and Artifacts](../outputs-and-artifacts.md).
