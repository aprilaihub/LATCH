"""Lightweight test-only stubs for environments without the MCP package.

Physical-tool environments require the real dependencies. This only keeps unit tests for
configuration/table/frontier-expansion logic importable in minimal local environments.
"""

from __future__ import annotations

import sys
import types

if "mcp" not in sys.modules:
    mcp = types.ModuleType("mcp")

    class ClientSession:  # pragma: no cover - test import shim only
        pass

    class StdioServerParameters:  # pragma: no cover - test import shim only
        def __init__(self, *args, **kwargs):
            self.args = args
            self.kwargs = kwargs

    async def stdio_client(*args, **kwargs):  # pragma: no cover
        raise RuntimeError("MCP test stub cannot open stdio clients")

    mcp.ClientSession = ClientSession
    mcp.StdioServerParameters = StdioServerParameters
    client = types.ModuleType("mcp.client")
    stdio = types.ModuleType("mcp.client.stdio")
    stdio.stdio_client = stdio_client
    stdio.StdioServerParameters = StdioServerParameters
    sys.modules["mcp"] = mcp
    sys.modules["mcp.client"] = client
    sys.modules["mcp.client.stdio"] = stdio
