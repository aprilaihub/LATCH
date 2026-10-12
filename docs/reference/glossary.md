# Glossary

| Term | Plain meaning |
|---|---|
| Run | One managed optimization experiment, including its scopes, states, attempts, budgets, validations, and selected result. |
| Execution Scope | A named local execution context inside one Run, such as root, fixed, frontier, intervention, or certification. It is not a child Run. |
| Design State | A registered DCP state with artifact identity, lineage, validation status, and independent parent/selection eligibility. |
| Action Specification | The explicit typed optimization action and parameters requested from a backend. |
| Attempt | One scheduled execution of an Action Specification from one parent Design State. |
| Execution Outcome | The backend result of an Attempt, recorded separately from validation and result selection. |
| Validation Record | An immutable Attempt, Certification, or Formal validation fact. Formal may be `NOT_RUN`. |
| Artifact Reference | Identity and location metadata for an external artifact; large payloads are not embedded in the canonical store. |
| Budget Event | An immutable reservation, completion, failure, or release fact used for accounting. |
| Result Selection | The recorded decision that chooses a publishable Design State and explains why. |
| DCP | Vivado design checkpoint, the `.dcp` file used as optimizer input and output. |
| Mock run | A fixture run using supplied mock services without launching Vivado or calling external LLM providers. |
| Real Vivado run | A run that uses Vivado/RapidWright and may need license access. |
| Flow | One named optimization action, such as `phys_opt` or `retime`. |
| Analysis | A read-only stage that inspects a DCP and writes evidence. |
| Expansion | A stage that tries one or more flows and creates candidate DCPs. |
| Finalize | A stage that writes a summary of the selected state and run status; functional certification is separate. |
| Record of what changed | Immutable canonical facts stored in `run.db`. |
| State graph | A map of the input DCP and candidate DCPs created during a run. |
| Policy | The rule or model that chooses which candidate flow to try next. |
| Published best | The output DCP selected as the best valid result known to the run. |

The capitalized terms name the stored run concepts. Code and compatibility
interfaces also use transition, node, expansion, and Rescue. Use the documented
API names when writing pipelines; these older names do not change the meaning
of the recorded facts.
