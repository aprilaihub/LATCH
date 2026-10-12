# Source credits

Certain source files in this repository carry notices identifying them as
Copyright © 2026 Advanced Micro Devices, Inc., under the Apache License,
Version 2.0. Those notices also state that portions of the files are AI-generated.
See the individual file headers and [LICENSE-APACHE-2.0.txt](LICENSE-APACHE-2.0.txt).

File headers retain original declarations and modification notices.

## AMD-origin code and retained declarations

The following paths distinguish upstream-derived code from a retained header
whose external source has not yet been established. An upstream-derived file
may contain later LATCH changes; this table does not attribute those changes to AMD.

| Current source | Upstream source / evidence | Role in LATCH |
| --- | --- | --- |
| `VivadoMCP/vivado_mcp_server.py` | Same path in the contest upstream revision `d21b17d`; AMD notice retained | Vivado tool server, with LATCH toolchain and process-cleanup adaptations |
| `VivadoMCP/test_vivado_mcp.py` | Same upstream path; AMD notice retained | Vivado server tests |
| `RapidWrightMCP/server.py` | Same upstream path; AMD notice retained | RapidWright tool server |
| `RapidWrightMCP/rapidwright_tools.py` | Same upstream path; AMD notice retained | RapidWright operation wrappers |
| `RapidWrightMCP/test_server.py` | Same upstream path; AMD notice retained | RapidWright server tests |
| `RapidWrightMCP/setup.sh` | Same upstream path; AMD notice retained | Dependency setup helper; inspect before running |
| `optimizer_pipeline/backends/xilinx/legacy.py` | Derived from upstream `dcp_optimizer.py`; AMD notice retained | Backend lifecycle and flow implementation adapted into the shared runtime |
| `validate_dcps.py` | Same upstream path; AMD notice and LATCH modification notice retained | Structural checks and sampled functional validation |
| `mcp_stdio_asyncio.py` | Locally introduced adapter; message-pump structure follows MCP Python SDK stdio (MIT); original AMD declaration retained, not independently established for this file | Asyncio pipe I/O adaptation; see the provenance detail below and LICENSE-MCP-MIT.txt |

## Other code, tools and materials

The shared Harness, study protocols, prompts and analysis code include LATCH
contributions beyond these upstream tools. Benchmark DCPs and experiment data
have their own provenance and distribution terms in the accompanying data package.

Original notices are retained. LATCH modifications do not replace upstream
copyright or license terms. The shared execution code also incorporates the
WP015 dispatcher, identified in its module header.

`validate_dcps.py` now carries a prominent LATCH modification notice for an
optional caller-owned workspace directory and owned child working directories.
Validation logic remains unchanged; MCP tool children run inside their own
validation workspace rather than inheriting a shared caller directory.

RapidWright is a separate upstream dependency. Its existing submodule gitlink
is retained at `2fc8b6aec859c20c04218573f9dda09db0a72e3f`; its own license and
notices remain applicable. Model SDKs similarly retain their upstream terms.
The pinned RapidWright `LICENSE.TXT` lists the API library's Apache/Xilinx-EULA
components and bundled third-party licenses, including LGPL/EPL families.
Redistributed JARs and tools are subject to those dependency-specific terms.

The identified upstream repository is
[Xilinx/fpl26_optimization_contest](https://github.com/Xilinx/fpl26_optimization_contest),
which publishes [the Apache text](https://github.com/Xilinx/fpl26_optimization_contest/blob/main/LICENSE-APACHE-2.0.txt)
and the original validator/MCP source. Provenance refers to its initial
`aaf26cb` and later `d21b17d` revisions; these Git objects need not be present
in a source-only checkout. The current upstream main tree is not a
replacement for that historical source revision or the pinned RapidWright.
The legacy backend derives from upstream `dcp_optimizer.py`; retain notices
and distinguish modifications from upstream source.
`mcp_stdio_asyncio.py` was added locally at `c585ae6` on 2026-04-21 to address
stdio initialization hangs; it is absent from the selected AMD revision.
Its stream/message pumps, exception handling and task-group structure closely
follow the [MCP Python SDK stdio transport](https://github.com/modelcontextprotocol/python-sdk/blob/v1.9.4/src/mcp/server/stdio.py).
That source carries the [MIT license](https://github.com/modelcontextprotocol/python-sdk/blob/v1.9.4/LICENSE),
Copyright (c) 2024 Anthropic, PBC; the text is retained in
[LICENSE-MCP-MIT.txt](LICENSE-MCP-MIT.txt) for the SDK-derived elements.
The local adaptation replaces AnyIO file wrappers with asyncio pipes.
The original dependency was only `mcp>=1.0.0`: v1.9.4 is a comparison reference,
not a claim that the exact version used during authorship has been established.
The pre-existing AMD/Apache header is retained; its attribution for this local
adapter has not been independently established. See [Licensing](LICENSING.md)
for the scope of the included licenses and the separate terms for research materials.
