# Troubleshooting

Use [explicit tool configuration](../latch-tools.md) and the same isolated
interpreter for CLI, MCP and validation. Missing `mcp` or a `NoneType` near
`StdioServerParameters` can indicate a wrong interpreter/missing extra; check
the configured environment rather than substituting system Python.

An MCP connection and a later Vivado implementation/device-license failure are
different results. Check tool startup and operation results separately. Licenses
and installation permissions are managed outside LATCH; do not expose license
settings in logs or install/upgrade system tools automatically.

Missing material, capture/query context, model SDK or explicit configuration
leaves the affected operation failed or unexecuted. Supply the missing item
before starting a new run.
For a started run, [resume constraints](../latch-harness.md#resume-without-silently-changing-a-run)
protect input/configuration and prevent uncertain candidate replay. Preserve
failed records. After an API error, check provider usage and run records before
considering a retry; uncertain or already-submitted candidates must not replay.

Use [output records](../outputs-and-artifacts.md) to distinguish execution,
physical validity, scoring and functional validation. No final output, skipped
checks and negative gain require separate interpretation; exit code alone is
not complete validation. Review data rights before sharing design-derived context.
