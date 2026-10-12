# Intervention Mental Model

Intervention is an application-policy branch for targeted recipe or manual
actions. It may run after a trigger such as no improvement and has its own
scope and bounded budget.

The intervention strategy checks the configured permissions. The capability
then validates a recipe/manual action, records Proposal/Action/Attempt facts,
and executes through the same transition executor as search. Invalid produced
DCPs are retained with their validation results and cannot be selected;
intervention policy can allow further work from such a scratch state.

Provider recipe generation lives under `integrations/llm`. The managed
capability owns allowlists, schema validation, state eligibility, and selection
safety. All intervention evidence and decisions are queryable from the run's
`run.db`.
