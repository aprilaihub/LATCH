#!/usr/bin/env python3
# Copyright (C) 2026, Advanced Micro Devices, Inc. All rights reserved.
# Portions of this file consist of AI-generated content.
# SPDX-License-Identifier: Apache 2.0
# Original repository declaration retained above; it does not describe all
# provenance of this local adapter. Its message-pump structure follows the MCP
# Python SDK stdio transport (Copyright (c) 2024 Anthropic, PBC, MIT).
# See LICENSE-MCP-MIT.txt and SOURCE_CREDITS.md for scope and source references.
# Local adaptation: asyncio pipe I/O replaces the SDK's AnyIO file wrappers.

"""
Asyncio-backed stdio transport for MCP servers.

This replaces mcp.server.stdio.stdio_server() in environments where AnyIO's
stdin wrapper does not consume subprocess pipe input correctly.
"""

from __future__ import annotations

import asyncio
import sys
from contextlib import asynccontextmanager

import anyio
import anyio.lowlevel
import mcp.types as types
from anyio.streams.memory import MemoryObjectReceiveStream, MemoryObjectSendStream

from mcp.shared.message import SessionMessage


@asynccontextmanager
async def stdio_server() -> tuple[
    MemoryObjectReceiveStream[SessionMessage | Exception],
    MemoryObjectSendStream[SessionMessage],
]:
    """
    Server transport for stdio using asyncio pipes instead of AnyIO-wrapped files.

    The repo's MCP servers are launched as subprocesses. On the APRIL host,
    mcp.server.stdio.stdio_server() starts successfully but never reads the
    client's initialize request from stdin. This helper keeps the MCP session
    model intact while swapping in a pipe implementation that does.
    """

    read_stream_writer, read_stream = anyio.create_memory_object_stream[SessionMessage | Exception](0)
    write_stream, write_stream_reader = anyio.create_memory_object_stream[SessionMessage](0)

    loop = asyncio.get_running_loop()

    stdin_reader = asyncio.StreamReader()
    stdin_protocol = asyncio.StreamReaderProtocol(stdin_reader)
    stdin_transport, _ = await loop.connect_read_pipe(lambda: stdin_protocol, sys.stdin)

    stdout_transport, stdout_protocol = await loop.connect_write_pipe(
        asyncio.streams.FlowControlMixin,
        sys.stdout,
    )
    stdout_writer = asyncio.StreamWriter(stdout_transport, stdout_protocol, None, loop)

    async def stdin_pump():
        try:
            async with read_stream_writer:
                while True:
                    line = await stdin_reader.readline()
                    if not line:
                        break

                    try:
                        message = types.JSONRPCMessage.model_validate_json(line.decode("utf-8", "replace"))
                    except Exception as exc:  # pragma: no cover - mirrors upstream behavior
                        await read_stream_writer.send(exc)
                        continue

                    await read_stream_writer.send(SessionMessage(message=message))
        except anyio.ClosedResourceError:  # pragma: no cover
            await anyio.lowlevel.checkpoint()

    async def stdout_pump():
        try:
            async with write_stream_reader:
                async for session_message in write_stream_reader:
                    payload = session_message.message.model_dump_json(by_alias=True, exclude_none=True)
                    stdout_writer.write((payload + "\n").encode("utf-8"))
                    await stdout_writer.drain()
        except anyio.ClosedResourceError:  # pragma: no cover
            await anyio.lowlevel.checkpoint()

    try:
        async with anyio.create_task_group() as tg:
            tg.start_soon(stdin_pump)
            tg.start_soon(stdout_pump)
            yield read_stream, write_stream
    finally:
        stdin_transport.close()
        stdout_transport.close()
