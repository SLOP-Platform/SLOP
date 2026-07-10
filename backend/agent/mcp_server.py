#!/usr/bin/env python3
"""Thin stdio MCP server wrapping the agent action registry seam.

Exposes two tools — list_agent_actions and invoke_agent_action — delegating
directly to mcp_adapter.tool_definitions() and mcp_adapter.dispatch().

Transport: stdio (standard MCP integration).  Fail-closed: unknown tools return
an isError envelope.

Usage:
    # Standalone (stdio, for MCP host):
    python3 -m backend.agent.mcp_server

    # Import check (no network/side-effects):
    python3 -c "from backend.agent.mcp_server import *"
"""

from __future__ import annotations

import asyncio
import json

from backend.core.logging import get_logger
from backend.agent.mcp_adapter import dispatch, tool_definitions

try:
    from mcp.server import Server
    from mcp.server.stdio import stdio_server
    from mcp.types import TextContent, Tool
except Exception as _mcp_import_err:
    # mcp 1.23.x ships broken Windows-only type annotations that raise
    # TypeError on Linux during import. Degrade gracefully so the module
    # remains importable; the MCP server is inoperative until the upstream
    # fix lands.
    get_logger(__name__).warning(
        "MCP import failed — server unavailable: %s", _mcp_import_err
    )
    Server = None  # type: ignore[misc,assignment]
    stdio_server = None  # type: ignore[misc,assignment]
    TextContent = None  # type: ignore[misc,assignment]
    Tool = None  # type: ignore[misc,assignment]

# ---------------------------------------------------------------------------
# MCP server definition
# ---------------------------------------------------------------------------

if Server is not None:
    app = Server("agent-actions")

    @app.list_tools()
    async def list_tools() -> list[Tool]:
        definitions = tool_definitions()
        return [
            Tool(
                name=d["name"],
                description=d["description"],
                inputSchema=d["inputSchema"],
            )
            for d in definitions
        ]

    @app.call_tool()
    async def call_tool(name: str, arguments: dict) -> list[TextContent]:
        result = dispatch(name, arguments)
        text_block = result.get("content", [])
        if not text_block:
            text_block = [{"type": "text", "text": json.dumps(result, sort_keys=True)}]

        return [TextContent(type=b["type"], text=b["text"]) for b in text_block]
else:
    app = None  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


async def main() -> None:
    if app is None or stdio_server is None:
        import logging as _logging
        _logging.getLogger(__name__).error(
            "MCP server unavailable — mcp library import failed (see above). "
            "Check upstream issue or upgrade mcp>=1.24 when available."
        )
        raise RuntimeError("MCP server unavailable due to import failure")
    async with stdio_server() as (read_stream, write_stream):
        await app.run(
            read_stream,
            write_stream,
            app.create_initialization_options(),
        )


if __name__ == "__main__":
    asyncio.run(main())
