"""A real stdio round trip against the server process.

Every other test calls ``_dispatch`` or the handler coroutines directly, which
means they pass whether or not the MCP protocol layer is wired up correctly.
That gap is exactly what the mcp 2.x port could have broken silently: the tool
surface stays fine and the server answers nothing.

So this spawns the actual server over stdio and speaks the protocol to it.
No network — the scratch token store is empty, and the tools exercised here
answer without ever reaching Gmail.

The session is an inline ``async with`` rather than a fixture on purpose.
anyio cancel scopes must be entered and exited in the same task, and an async
generator fixture puts setup and teardown in different ones.
"""

from __future__ import annotations

import os
import sys
from contextlib import asynccontextmanager

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client


@asynccontextmanager
async def serving(tmp_path):
    """Spawn the server over stdio and hand back an initialized client."""
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "gmail_mcp.server"],
        env={**os.environ, "GMAIL_MCP_DB": str(tmp_path / "tokens.db")},
    )
    async with (
        stdio_client(params) as (read, write),
        ClientSession(read, write) as client,
    ):
        await client.initialize()
        yield client


async def test_server_initializes_and_lists_its_tools(tmp_path):
    async with serving(tmp_path) as client:
        assert client.initialize_result.server_info.name == "gmail-mcp"
        names = {t.name for t in (await client.list_tools()).tools}
        # Spot-check across the surface rather than pinning the whole list.
        assert {"list_accounts", "search_messages", "read_message", "create_draft"} <= names


async def test_hand_written_schemas_survive_the_protocol(tmp_path):
    # The port kept explicit inputSchema dicts rather than letting 2.x derive
    # them from function signatures. If that regresses, these properties vanish.
    async with serving(tmp_path) as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}
        schema = tools["create_draft"].input_schema
        assert {"reply_to_message_id", "reply_all", "from_addr"} <= set(schema["properties"])
        assert schema["required"] == ["account", "body"]


async def test_calls_and_failures_both_come_back_as_content(tmp_path):
    async with serving(tmp_path) as client:
        ok = await client.call_tool("list_accounts", {})
        assert ok.is_error is False
        assert "No accounts authorized yet" in ok.content[0].text

        unknown = await client.call_tool("bogus_tool", {})
        assert "Unknown tool" in unknown.content[0].text

        # The error branches in call_tool report through content, not by
        # raising. Preserved deliberately across the port; see _on_call_tool.
        failed = await client.call_tool(
            "read_message", {"account": "nope@example.com", "message_id": "1"}
        )
        assert "No authorized account" in failed.content[0].text
