"""Direct-MCP adapter — drive MCP tool calls deterministically (no LLM).

Plan A v1: simple keyword-based routing over the tools returned from
`tools/list`. Plan B will expand this (richer rules, auth2v config compat).

Same pedagogy as adapters/langchain/framework_bridge.py: captures actual
wire bytes via httpx event hooks so the cidgar drawer can show exactly
what went over the wire.
"""
from __future__ import annotations

import json
import os
import re
from typing import Any

import httpx

# fastmcp is imported lazily inside Trial.__init__ so that the pure helper
# functions (route, pick_mcp_base_url) can be unit-tested without fastmcp
# installed (useful for the harness test suite running outside the adapter
# container).


# ── URL helpers (shared shape with langchain adapter) ──

def pick_mcp_base_url(routing: str, mcp: str) -> str:
    if mcp == "NONE":
        raise ValueError("direct-mcp adapter requires a concrete MCP (mcp=NONE invalid)")
    prefix = "AGW_MCP_" if routing == "via_agw" else "DIRECT_MCP_"
    var = f"{prefix}{mcp.upper()}"
    url = os.environ.get(var)
    if not url:
        raise ValueError(f"env var {var} not set")
    return url


# ── Deterministic routing (Plan A) ──

def route(user_msg: str, tools: list[dict]) -> tuple[str, dict]:
    """Pick a tool + extract args deterministically, no LLM."""
    msg_lower = user_msg.lower()

    # 1. Match tools by keyword presence in name
    for tool in tools:
        name_parts = tool["name"].replace("_", " ").replace("-", " ").split()
        for part in name_parts:
            if len(part) >= 3 and part.lower() in msg_lower:
                args = _extract_args_from_msg(user_msg, tool.get("inputSchema", {}))
                return tool["name"], args

    # 2. Fallback: description keyword match
    for tool in tools:
        desc_words = (tool.get("description") or "").lower().split()
        for word in desc_words:
            if len(word) >= 4 and word in msg_lower:
                args = _extract_args_from_msg(user_msg, tool.get("inputSchema", {}))
                return tool["name"], args

    # 3. Last resort: first tool — still try to extract args so the
    # tool has a fighting chance (e.g. follow-up "What about London?"
    # has no tool-keyword but the city regex still hits).
    if tools:
        args = _extract_args_from_msg(user_msg, tools[0].get("inputSchema", {}))
        return tools[0]["name"], args
    raise ValueError("no tools available")


def _extract_args_from_msg(user_msg: str, input_schema: dict) -> dict:
    """Extract args from the user message based on tool's inputSchema.

    v1 strategy (Plan A): for each string-typed property, look for a
    capitalized word or a phrase after 'in/for/about/at'. For integer-typed
    property, grab the first number. Miss → leave unset, MCP may reject —
    that's fine for v1.
    """
    args: dict[str, Any] = {}
    props = {}
    if isinstance(input_schema, dict):
        props = input_schema.get("properties") or {}
    for prop, spec in props.items():
        if prop.startswith("_ib_"):
            continue  # cidgar-injected fields — skip
        t = spec.get("type") if isinstance(spec, dict) else None
        if t == "string":
            # Match "in Paris", "for London", or just a capitalized word
            m = re.search(r'(?:in|for|about|at)\s+([A-Z][a-zA-Z]+)', user_msg)
            if m:
                args[prop] = m.group(1)
                continue
            # Fallback: URL (for fetch-like tools)
            m_url = re.search(r'(https?://\S+)', user_msg)
            if m_url:
                args[prop] = m_url.group(1)
                continue
            # Fallback: last capitalized word
            cap_words = re.findall(r'\b[A-Z][a-zA-Z]{2,}\b', user_msg)
            if cap_words:
                args[prop] = cap_words[-1]
        elif t in ("integer", "number"):
            m = re.search(r'\b(\d+)\b', user_msg)
            if m:
                val = int(m.group(1))
                args[prop] = val if t == "integer" else float(val)
    return args


# ── Capture helpers ──

def _safe_json(raw: bytes) -> Any:
    """Try JSON, fall back to string, else length marker."""
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        pass
    try:
        return raw.decode("utf-8", errors="replace")
    except Exception:
        return f"<{len(raw)} bytes non-utf8>"


def _tool_to_dict(t: Any) -> dict:
    """Normalize a fastmcp Tool object (pydantic) into a plain dict."""
    if isinstance(t, dict):
        return t
    d: dict = {}
    for attr in ("name", "description"):
        if hasattr(t, attr):
            d[attr] = getattr(t, attr)
    # fastmcp uses either inputSchema or input_schema depending on version
    for attr in ("inputSchema", "input_schema"):
        if hasattr(t, attr):
            d["inputSchema"] = getattr(t, attr)
            break
    return d


def _tool_result_to_string(result: Any) -> str:
    """Render a fastmcp CallToolResult as a readable string."""
    # fastmcp returns a CallToolResult-ish object with `.content` (list of blocks)
    content = getattr(result, "content", None)
    if content is None and isinstance(result, dict):
        content = result.get("content")
    if not content:
        # Last resort — just stringify
        try:
            return json.dumps(result, default=str)
        except Exception:
            return str(result)
    chunks: list[str] = []
    for block in content:
        if isinstance(block, dict):
            txt = block.get("text")
            if txt is not None:
                chunks.append(txt)
                continue
            chunks.append(json.dumps(block, default=str))
        else:
            txt = getattr(block, "text", None)
            if txt is not None:
                chunks.append(txt)
            else:
                try:
                    chunks.append(json.dumps(block, default=str))
                except Exception:
                    chunks.append(str(block))
    return "\n".join(chunks)


class Trial:
    """Per-trial direct-MCP state.

    Captures real wire bytes for the underlying HTTP MCP calls via httpx
    event hooks, same pattern as the langchain adapter.
    """

    def __init__(self, trial_id: str, config: dict):
        self.trial_id = trial_id
        self.config = config

        # Per-turn capture slots. Populated by httpx event hooks.
        self._last_request: dict | None = None
        self._last_response: dict | None = None
        # Secondary capture (for list_tools vs call_tool split).
        self._all_requests: list[dict] = []
        self._all_responses: list[dict] = []

        self.base_url = pick_mcp_base_url(routing=config["routing"], mcp=config["mcp"])

        # Paired capture: link responses to their originating request via
        # httpx.Response.request identity. fastmcp streamable-HTTP does
        # async reads (SSE), sends notifications that may or may not get
        # response events, so positional pairing is unreliable.
        self._pairs: list[dict] = []  # list of {"request": {...}, "response": {...}}
        self._req_by_id: dict[int, dict] = {}  # id(httpx.Request) → pair dict

        async def log_req(request: httpx.Request) -> None:
            body_bytes = request.content or b""
            req_dict = {
                "method": request.method,
                "url": str(request.url),
                "headers": {k: v for k, v in request.headers.items()},
                "body": _safe_json(body_bytes),
                "body_bytes_len": len(body_bytes),
            }
            pair = {"request": req_dict, "response": None}
            self._pairs.append(pair)
            self._req_by_id[id(request)] = pair
            self._last_request = req_dict
            self._all_requests.append(req_dict)

        async def log_resp(response: httpx.Response) -> None:
            await response.aread()
            body_bytes = response.content or b""
            resp_dict = {
                "status": response.status_code,
                "headers": {k: v for k, v in response.headers.items()},
                "body": _safe_json(body_bytes),
                "body_bytes_len": len(body_bytes),
                "elapsed_ms": int(response.elapsed.total_seconds() * 1000),
            }
            # Link back to the originating request so we get correct pairing.
            pair = self._req_by_id.get(id(response.request))
            if pair is not None:
                pair["response"] = resp_dict
            self._last_response = resp_dict
            self._all_responses.append(resp_dict)

        self._default_headers: dict[str, str] = {
            "X-Harness-Trial-ID": trial_id,
        }

        # Placeholder httpx client we aclose() on shutdown for symmetry with
        # the langchain adapter (fastmcp builds its own internal clients).
        self._http_client = httpx.AsyncClient(
            event_hooks={"request": [log_req], "response": [log_resp]},
            timeout=httpx.Timeout(120.0),
            headers=self._default_headers,
        )

        # httpx client factory for fastmcp — installs our event hooks into
        # every httpx client fastmcp creates, so we capture actual wire bytes.
        # fastmcp passes headers/auth/follow_redirects/timeout kwargs.
        self._log_req = log_req
        self._log_resp = log_resp

        def _httpx_factory(**kwargs):
            existing_hooks = kwargs.pop("event_hooks", {}) or {}
            req_hooks = list(existing_hooks.get("request", [])) + [log_req]
            resp_hooks = list(existing_hooks.get("response", [])) + [log_resp]
            kwargs["event_hooks"] = {"request": req_hooks, "response": resp_hooks}
            return httpx.AsyncClient(**kwargs)

        self._httpx_factory = _httpx_factory

        # fastmcp Client — pass a StreamableHttpTransport built with our
        # factory + per-trial headers. We rebuild the transport per turn
        # so we can swap in the turn-specific X-Harness-Turn-ID header.
        from fastmcp import Client as FastMCPClient  # lazy import
        from fastmcp.client.transports import StreamableHttpTransport
        self._FastMCPClient = FastMCPClient
        self._StreamableHttpTransport = StreamableHttpTransport
        self.mcp_client = self._build_mcp_client(self._default_headers)

    def _build_mcp_client(self, headers: dict[str, str]):
        transport = self._StreamableHttpTransport(
            self.base_url,
            headers=headers,
            httpx_client_factory=self._httpx_factory,
        )
        return self._FastMCPClient(transport)

    def _set_turn_headers(self, turn_id: str) -> None:
        self._default_headers = {
            "X-Harness-Trial-ID": self.trial_id,
            "X-Harness-Turn-ID": turn_id,
        }
        self._http_client.headers.update(self._default_headers)
        # Rebuild fastmcp client with the new turn's headers so the
        # X-Harness-Turn-ID header lands on every HTTP request this turn.
        self.mcp_client = self._build_mcp_client(self._default_headers)

    def _find_rpc_pair(self, rpc_method: str) -> tuple[dict | None, dict | None]:
        """Find the request/response pair whose JSON-RPC body matches.

        fastmcp's streamable-HTTP transport issues POSTs, GETs (for SSE
        re-open), notifications (no response), and a DELETE. Pairs are
        linked at hook time via httpx.Response.request identity (see
        log_req/log_resp), so we just walk the pair list.
        """
        for pair in self._pairs:
            req = pair.get("request") or {}
            body = req.get("body")
            if isinstance(body, dict) and body.get("method") == rpc_method:
                return req, pair.get("response")
        return None, None

    async def turn(self, turn_id: str, user_msg: str) -> dict:
        """One turn: list tools → route → call tool. Captures wire bytes."""
        # Reset per-turn capture slots
        self._last_request = None
        self._last_response = None
        self._all_requests = []
        self._all_responses = []
        self._pairs = []
        self._req_by_id = {}

        self._set_turn_headers(turn_id)

        framework_events: list[dict] = []

        # 1. tools/list + 2. tools/call inside a single session.
        try:
            async with self.mcp_client:
                tools_raw = await self.mcp_client.list_tools()
                tools = [_tool_to_dict(t) for t in tools_raw]

                # Route deterministically
                try:
                    tool_name, args = route(user_msg, tools)
                except Exception as e:
                    list_req, list_resp = self._find_rpc_pair("tools/list")
                    return {
                        "turn_id": turn_id,
                        "assistant_msg": f"[direct-mcp] routing failed: {e}",
                        "tool_calls": [],
                        "request_captured": list_req or {},
                        "response_captured": list_resp or {},
                        "framework_events": [{
                            "kind": "tools_list",
                            "tool_count": len(tools),
                            "tools": [{"name": t.get("name"),
                                       "description": (t.get("description") or "")[:80]}
                                      for t in tools],
                            "request": list_req,
                            "response": list_resp,
                        }],
                    }

                call_result = await self.mcp_client.call_tool(tool_name, args)
        except Exception as e:
            list_req, list_resp = self._find_rpc_pair("tools/list")
            call_req, call_resp = self._find_rpc_pair("tools/call")
            return {
                "turn_id": turn_id,
                "assistant_msg": f"[direct-mcp] mcp session failed: {e}",
                "tool_calls": [],
                "request_captured": call_req or list_req or {"error": str(e)},
                "response_captured": call_resp or list_resp or {"error": str(e)},
                "framework_events": [{"kind": "mcp_error", "error": str(e)}],
            }

        # Pick canonical request/response pairs by JSON-RPC method.
        list_req, list_resp = self._find_rpc_pair("tools/list")
        call_req, call_resp = self._find_rpc_pair("tools/call")

        framework_events.append({
            "kind": "tools_list",
            "tool_count": len(tools),
            "tools": [{"name": t.get("name"),
                       "description": (t.get("description") or "")[:80]}
                      for t in tools],
            "request": list_req,
            "response": list_resp,
        })

        assistant_msg = _tool_result_to_string(call_result)
        tool_calls = [{"name": tool_name, "args": args, "id": turn_id}]

        request_captured = call_req or {
            "method": "POST",
            "url": self.base_url,
            "headers": self._default_headers,
            "body": {"note": "no tools/call request found in captures"},
        }
        response_captured = call_resp or {
            "status": 0,
            "headers": {},
            "body": {"note": "no tools/call response found in captures"},
        }

        return {
            "turn_id": turn_id,
            "assistant_msg": assistant_msg,
            "tool_calls": tool_calls,
            "request_captured": request_captured,
            "response_captured": response_captured,
            "framework_events": framework_events,
        }

    async def aclose(self) -> None:
        """Release httpx client connections."""
        try:
            await self._http_client.aclose()
        except Exception:
            pass
