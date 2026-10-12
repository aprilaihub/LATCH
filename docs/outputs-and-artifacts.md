# Outputs and Artifacts

A managed run stores records and artifacts in its run directory. On successful
completion, the application can export the selected DCP; failed runs retain
their records for diagnosis.

| Path | Meaning |
|---|---|
| `run.db` | Canonical Run, Scope, Action, Proposal, Attempt, Outcome, State, Validation, Eligibility, Budget, and Selection facts. |
| `result_manifest.json` | Result summary: success, mode, input/final paths, budget, selected state, and database location. |
| `transition_*/output.dcp` | Candidate DCP produced by an attempt. |
| `analysis_runs/`, `transition_*/executor_run/` | Backend logs, reports, journals, and temporary workspaces. |
| requested output DCP | Export of an eligible selected state, which may be the input if no candidate improves it. |

Use `optimizer_pipeline.core.store.load_run_view(RUN_DIR)` for a stable Python
read model. Use `tools/capture_semantic_trace.py` for a compact review trace.
SQLite integrity and foreign-key checks are available through
`SQLiteRunStore.integrity_check()`.

Older run directories use JSON/JSONL records. Import one with
`tools/migrate_v1_run.py` to read it through the managed store; the importer
preserves its source.
