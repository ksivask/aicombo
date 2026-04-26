"""E22 — tests for mcp/mutable test MCP server.

Drives the FastMCP Starlette app via httpx.AsyncClient with ASGITransport
so no docker / network required. Covers the admin endpoints (state,
set_tools, reset) and verifies that the dynamic tool registry shows up
correctly via the standard MCP `tools/list` method.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import httpx
import pytest


# Path to the mutable MCP server module. We load it as a private module
# (`_mutable_mcp_main`) via importlib so we don't collide with
# `harness/main.py` which other tests in this suite import as `main`.
_MUTABLE_MAIN = (
    Path(__file__).resolve().parent.parent / "mcp" / "mutable" / "main.py"
)
_MUTABLE_MOD_NAME = "_mutable_mcp_main"


@pytest.fixture
def mutable_app():
    """Fresh load of mcp/mutable/main.py per test so the in-memory state
    (registered tools, version_counter, KV) is reset between tests.

    Uses importlib.util.spec_from_file_location to bypass sys.path so
    there's no collision with `harness/main.py` (loaded by other tests
    as `main`). The module gets a private name and is popped from
    sys.modules afterwards.
    """
    sys.modules.pop(_MUTABLE_MOD_NAME, None)
    spec = importlib.util.spec_from_file_location(
        _MUTABLE_MOD_NAME, _MUTABLE_MAIN,
    )
    main = importlib.util.module_from_spec(spec)
    sys.modules[_MUTABLE_MOD_NAME] = main
    spec.loader.exec_module(main)
    yield main.mcp_app, main
    sys.modules.pop(_MUTABLE_MOD_NAME, None)


@pytest.mark.asyncio
async def test_initial_state_returns_initial_tools(mutable_app):
    """GET /_admin/state returns INITIAL_TOOLS and version_counter=0."""
    app, main = mutable_app
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver",
    ) as client:
        resp = await client.get("/_admin/state")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["version_counter"] == 0
    names = sorted(t["name"] for t in body["tools"])
    expected = sorted(t["name"] for t in main.INITIAL_TOOLS)
    assert names == expected
    # Each tool has the three fields we promised in the admin contract.
    for t in body["tools"]:
        assert "name" in t and "description" in t and "inputSchema" in t


@pytest.mark.asyncio
async def test_set_tools_replaces_and_bumps_counter(mutable_app):
    """POST /_admin/set_tools replaces the tool list and bumps the counter."""
    app, _ = mutable_app
    new_tools = [
        {
            "name": "alpha",
            "description": "first new tool",
            "inputSchema": {"type": "object", "properties": {}},
        },
        {
            "name": "beta",
            "description": "second new tool",
            "inputSchema": {
                "type": "object",
                "properties": {"x": {"type": "string"}},
                "required": ["x"],
            },
        },
    ]
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver",
    ) as client:
        post = await client.post(
            "/_admin/set_tools", json={"tools": new_tools},
        )
        assert post.status_code == 200, post.text
        post_body = post.json()
        assert post_body["ok"] is True
        assert post_body["version_counter"] == 1
        assert post_body["tool_count"] == 2

        state = await client.get("/_admin/state")
    body = state.json()
    assert body["version_counter"] == 1
    names = sorted(t["name"] for t in body["tools"])
    assert names == ["alpha", "beta"]
    # Schema for beta should match what we POSTed (verifying our
    # parameters override actually took effect).
    beta = next(t for t in body["tools"] if t["name"] == "beta")
    assert beta["inputSchema"].get("required") == ["x"]


@pytest.mark.asyncio
async def test_reset_reverts_to_initial(mutable_app):
    """After set_tools then reset, state matches initial."""
    app, main = mutable_app
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver",
    ) as client:
        await client.post(
            "/_admin/set_tools",
            json={"tools": [
                {"name": "x", "description": "x", "inputSchema": {}},
            ]},
        )
        # Bump to 2 to verify counter actually zeros (not just stays at 0).
        await client.post(
            "/_admin/set_tools",
            json={"tools": [
                {"name": "y", "description": "y", "inputSchema": {}},
            ]},
        )
        reset = await client.post("/_admin/reset", json={})
        assert reset.status_code == 200, reset.text
        state = await client.get("/_admin/state")
    body = state.json()
    assert body["version_counter"] == 0
    names = sorted(t["name"] for t in body["tools"])
    expected = sorted(t["name"] for t in main.INITIAL_TOOLS)
    assert names == expected


@pytest.mark.asyncio
async def test_tools_list_returns_current_tools(mutable_app):
    """MCP `tools/list` returns the current (post-mutation) tool set.

    Calls FastMCP's `mcp.list_tools()` directly — equivalent to what
    the JSON-RPC `tools/list` method would return, but without needing
    the HTTP/SSE/session-id lifecycle for unit testing. The full HTTP
    wire path is exercised by integration tests that hit the running
    container under docker-compose.
    """
    app, main = mutable_app
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver",
    ) as client:
        # Mutate via the admin endpoint — this is the path that drives
        # the registry change we're verifying.
        post = await client.post(
            "/_admin/set_tools",
            json={"tools": [
                {
                    "name": "only_one",
                    "description": "single tool",
                    "inputSchema": {"type": "object", "properties": {}},
                },
            ]},
        )
        assert post.status_code == 200, post.text

    # Call list_tools through FastMCP's internal API. This is exactly
    # what the JSON-RPC tools/list method dispatches to.
    tools = await main.mcp.list_tools()
    names = sorted(t.name for t in tools)
    assert names == ["only_one"], names
    assert tools[0].description == "single tool"


@pytest.mark.asyncio
async def test_admin_does_not_appear_in_tools_list(mutable_app):
    """Admin endpoints (/_admin/*) MUST NOT appear as MCP tools.

    Sanity check that custom_route HTTP handlers are NOT visible to MCP
    clients — they're orthogonal surfaces. Otherwise an LLM could
    accidentally call set_tools as a tool.
    """
    app, main = mutable_app
    # The most reliable way to assert this is to check the FastMCP
    # tool registry directly: it must contain ONLY INITIAL_TOOLS and
    # nothing prefixed with `_admin`.
    registered = main._state["registered_tool_names"]
    assert all(not n.startswith("_admin") for n in registered), registered
    expected = {t["name"] for t in main.INITIAL_TOOLS}
    assert registered == expected
