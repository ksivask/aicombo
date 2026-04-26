"""mcp/mutable — test MCP server with dynamically mutable tool list.

Sibling to mcp/weather, mcp/news, etc. Test infrastructure for E20
(tools/list snapshot correlation): exposes admin endpoints under
/_admin/* that let the harness mutate the tool list at trial time.

Standard MCP protocol on the regular MCP path; admin endpoints under
/_admin/ (impossible to confuse with MCP method names since none start
with `_`).

In-memory KV store backs the four INITIAL_TOOLS so the initial tool
set is functionally usable in an LLM-driven trial. Resets on container
restart.
"""
from __future__ import annotations

import os
from typing import Any

import uvicorn
from fastmcp import FastMCP
import fastmcp
from fastmcp.tools import FunctionTool
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.cors import CORSMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse
from fastmcp.utilities.logging import configure_logging

# MCP metadata for x-mcp-tag header
MCP_TAG = (
    f"framework=fastmcp;version={fastmcp.__version__};"
    f"server=mutable;api=in-memory-kv"
)

LOGLVL = os.environ.get("LOGLVL", "INFO")
configure_logging(level=LOGLVL)

mcp = FastMCP("mutable-mcp-server")

# ── Initial tool spec (mirrored to MCP via add_tool below) ──
#
# Realistic 4-tool scaffold modelled on a key-value store. Trials can
# exercise tool-calling without contrived setup; admin endpoints can
# replace this set at any time to drive E20 snapshot-mutation tests.
INITIAL_TOOLS: list[dict[str, Any]] = [
    {
        "name": "mutable_get",
        "description": "Get a value by key",
        "inputSchema": {
            "type": "object",
            "properties": {"key": {"type": "string"}},
            "required": ["key"],
        },
    },
    {
        "name": "mutable_set",
        "description": "Set a value by key",
        "inputSchema": {
            "type": "object",
            "properties": {
                "key": {"type": "string"},
                "value": {"type": "string"},
            },
            "required": ["key", "value"],
        },
    },
    {
        "name": "mutable_list",
        "description": "List all keys",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "mutable_delete",
        "description": "Delete a key",
        "inputSchema": {
            "type": "object",
            "properties": {"key": {"type": "string"}},
            "required": ["key"],
        },
    },
]

# ── Mutable runtime state ──
#
# version_counter bumps on every set_tools call. Surfaces in /_admin/state
# as an audit-debugging signal ("test mutated tools at this point").
# Reset endpoint zeroes it back to 0.
_state = {
    "version_counter": 0,
    "kv": {},  # in-memory KV backing the INITIAL_TOOLS
    # Track which tool names are currently registered so we can remove
    # them all on reset / set_tools without iterating the FastMCP
    # internal registry.
    "registered_tool_names": set(),
}


def _make_stub_tool(spec: dict[str, Any]) -> FunctionTool:
    """Build a FunctionTool from a {name, description, inputSchema} dict.

    FastMCP rejects functions with **kwargs as tools, so the stub takes a
    single optional `payload: dict` argument. We then OVERRIDE the
    derived parameters with the caller-supplied JSON Schema via
    `model_copy` so the wire-level tools/list response carries the exact
    shape the test asked for.

    The body is intentionally trivial (returns the args echoed back)
    because the test surface for E20 is the LIST shape, not call
    semantics. Trials that need real tool behavior should use
    INITIAL_TOOLS or compose mcp_admin with their own real-tool MCP.
    """
    name = spec["name"]
    description = spec.get("description", "") or ""
    input_schema = spec.get("inputSchema") or {
        "type": "object", "properties": {},
    }

    async def _stub(payload: dict | None = None) -> dict:
        return {"ok": True, "tool": name, "payload": payload or {}}

    tool = FunctionTool.from_function(
        _stub, name=name, description=description,
    )
    # Override the auto-derived parameters schema with the spec's
    # inputSchema so wire-level tools/list matches what the admin caller
    # asked for.
    return tool.model_copy(update={"parameters": input_schema})


# ── Real tools backing INITIAL_TOOLS (KV operations) ──
#
# Defined as inner closures so they share `_state["kv"]`. Registered via
# `_register_initial_tools()` below (NOT @mcp.tool decorator) so admin
# reset can re-register cleanly after a set_tools mutation.

async def _kv_get(key: str) -> dict:
    """Get a value by key."""
    return {"key": key, "value": _state["kv"].get(key), "found": key in _state["kv"]}

async def _kv_set(key: str, value: str) -> dict:
    """Set a value by key."""
    _state["kv"][key] = value
    return {"key": key, "value": value, "ok": True}

async def _kv_list() -> dict:
    """List all keys."""
    return {"keys": sorted(_state["kv"].keys()), "count": len(_state["kv"])}

async def _kv_delete(key: str) -> dict:
    """Delete a key."""
    existed = key in _state["kv"]
    _state["kv"].pop(key, None)
    return {"key": key, "deleted": existed}


# Wire INITIAL_TOOLS to their backing functions by name. New tools added
# via /_admin/set_tools use _make_stub_tool instead; only tools listed
# here get the real KV implementation.
_INITIAL_TOOL_IMPLS = {
    "mutable_get": _kv_get,
    "mutable_set": _kv_set,
    "mutable_list": _kv_list,
    "mutable_delete": _kv_delete,
}


def _clear_registered_tools() -> None:
    """Remove every tool we previously registered. Idempotent."""
    # FastMCP 3.x deprecated the top-level remove_tool in favour of the
    # local_provider attribute; use the new API to avoid noisy warnings
    # on every test run.
    for name in list(_state["registered_tool_names"]):
        try:
            mcp.local_provider.remove_tool(name)
        except Exception:
            # remove_tool raises NotFoundError if the tool isn't
            # registered — safe to swallow during cleanup.
            pass
    _state["registered_tool_names"].clear()


def _register_initial_tools() -> None:
    """Register all INITIAL_TOOLS — the four KV operations."""
    for spec in INITIAL_TOOLS:
        impl = _INITIAL_TOOL_IMPLS[spec["name"]]
        tool = FunctionTool.from_function(
            impl, name=spec["name"], description=spec["description"],
        )
        # Override parameters with the spec'd inputSchema so MCP clients
        # see the documented shape rather than the impl's signature
        # (which is the same here, but keeps the contract explicit).
        tool = tool.model_copy(update={"parameters": spec["inputSchema"]})
        mcp.add_tool(tool)
        _state["registered_tool_names"].add(spec["name"])


def _set_tools_from_specs(tool_specs: list[dict[str, Any]]) -> None:
    """Replace the entire tool registry with stub-backed tools per spec."""
    _clear_registered_tools()
    for spec in tool_specs:
        if not isinstance(spec, dict) or "name" not in spec:
            raise ValueError(
                f"each tool must be a dict with 'name'; got: {spec!r}"
            )
        tool = _make_stub_tool(spec)
        mcp.add_tool(tool)
        _state["registered_tool_names"].add(spec["name"])


# Register the initial set at import time.
_register_initial_tools()


# ── Admin endpoints (NO governance — test infra only) ──
#
# Path prefix /_admin/ guards against MCP method-name collisions (no MCP
# method starts with `_`). AGW config.yaml routes /_admin/* to a
# governance-disabled route; the matching path prefix means these calls
# never hit the cidgar pipeline.

@mcp.custom_route("/_admin/state", methods=["GET"])
async def admin_state(request: Request):
    """Inspect current state: tools list + version_counter."""
    tools_out = []
    for name in sorted(_state["registered_tool_names"]):
        try:
            tool = await mcp.get_tool(name)
        except Exception:
            continue
        tools_out.append({
            "name": tool.name,
            "description": tool.description or "",
            "inputSchema": tool.parameters or {
                "type": "object", "properties": {},
            },
        })
    return JSONResponse({
        "tools": tools_out,
        "version_counter": _state["version_counter"],
    })


@mcp.custom_route("/_admin/set_tools", methods=["POST"])
async def admin_set_tools(request: Request):
    """Replace the current tool list. Bumps version_counter.

    Body shape:
      {"tools": [{"name": "...", "description": "...", "inputSchema": {...}}, ...]}
    """
    try:
        body = await request.json()
    except Exception as e:
        return JSONResponse(
            {"error": f"invalid JSON body: {e}"}, status_code=400,
        )
    tools_spec = body.get("tools")
    if not isinstance(tools_spec, list):
        return JSONResponse(
            {"error": "body must contain 'tools' as a list"},
            status_code=400,
        )
    try:
        _set_tools_from_specs(tools_spec)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    _state["version_counter"] += 1
    return JSONResponse({
        "ok": True,
        "version_counter": _state["version_counter"],
        "tool_count": len(tools_spec),
    })


@mcp.custom_route("/_admin/reset", methods=["POST"])
async def admin_reset(request: Request):
    """Revert to INITIAL_TOOLS; reset version_counter to 0.

    Body is ignored (accept anything). KV store is also cleared so the
    initial tools see the same blank slate as a fresh container.
    """
    _clear_registered_tools()
    _register_initial_tools()
    _state["version_counter"] = 0
    _state["kv"] = {}
    return JSONResponse({
        "ok": True,
        "version_counter": _state["version_counter"],
    })


@mcp.custom_route("/health", methods=["GET"])
async def health_check(request):
    return JSONResponse({
        "status": "healthy",
        "service": "mutable-mcp-server",
        "version": "1.0.0",
    })


class MCPTagMiddleware(BaseHTTPMiddleware):
    """Middleware to add x-mcp-tag header to all responses."""

    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        response.headers["x-mcp-tag"] = MCP_TAG
        return response


mcp_app = mcp.http_app()

mcp_app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS", "DELETE"],
    allow_headers=[
        "Content-Type", "Mcp-Session-Id", "Authorization",
        "mcp-protocol-version", "x-agent-tag", "x-pxgw-tag",
    ],
    expose_headers=["Mcp-Session-Id", "x-mcp-tag", "x-pxgw-tag"],
)
mcp_app.add_middleware(MCPTagMiddleware)


if __name__ == "__main__":
    uvicorn.run(mcp_app, host="0.0.0.0", port=8000, log_level="debug")
