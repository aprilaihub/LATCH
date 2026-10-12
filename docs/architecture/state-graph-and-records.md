# State Graph and Canonical Facts

One managed run has one append-only `run.db`. A root, fixed stage, frontier
stage, intervention episode, and certification step are execution scopes in
that same database.

An attempted change is represented as:

```text
Proposal -> Action -> Attempt -> Outcome
                         |          |
                         |          +-> Artifact -> DesignState
                         +-> lifecycle and budget events
DesignState -> Validation -> Eligibility -> Selection
```

An invalid produced DCP is recorded with its validation result and is ineligible
as a search parent or selected result. Intervention policy can allow further
work from that state. An attempt that produces no DCP has attempt-level
validation without a new design state.

Selection is append-only. The latest selection determines result export; invalid
states can never be selected. Attempt, certification, and formal validation are
separate levels, so expensive functional certification is only run when policy
requires it.

Resume reconstructs nodes, lineage, incumbent selection, and budget usage from
database facts. Frontier scopes write directly to the same store, preserving
state identities and lineage across the run.
