# LATCH contributor instructions

Read README.md and the relevant Harness/study guide. Shared runtime owns
capabilities and facts; studies own conditions, prompts, budgets, validation
timing and interpretation. Preserve legacy imports and data labels unless a
separately reviewed change permits removal.

Use explicit isolated Python and tool configuration. Match Vivado to the input
and retain the RapidWright gitlink version. Do not silently initialize/upgrade
tools, discover `.env`, replace models or weaken checks. External licenses
belong to the operator's environment. Obtain authorization for real tools and
paid calls; a documentation/implementation task alone does not grant it.

Offline unit/mock tests must not launch EDA tools or model calls. Write new
outputs outside source, separate from historical inputs. Preserve failed,
unexecuted and uncertain results; never repair/replay submitted candidates to
promote outcomes. Execution, physical validity, scoring and functional results
are distinct. Retain process ownership/cleanup and input protection.

Do not commit credentials, license endpoints, site paths, capture contexts,
raw logs, databases or DCPs. Internal development/acceptance records belong
outside the source worktree; user-facing methods and license declarations
remain in source. Retain original notices; attribution is not redistribution
clearance. Inspect scope before commit; publishing needs separate authorization.
