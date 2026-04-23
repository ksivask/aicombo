"""Langchain-specific adapter logic.

Two modes per trial:

* **chat-only** (`mcp == "NONE"`): single LLM round-trip per turn. Existing
  behavior, kept byte-compatible with the original Plan-A bridge.
* **MCP agent loop** (`mcp != "NONE"`): on first turn (lazy), the adapter
  calls the MCP server's `tools/list` (cidgar f1 injects `_ib_cid`+`_ib_gar`
  into each tool schema), converts the resulting MCP tools into LangChain
  `StructuredTool`s via `langchain-mcp-adapters`, and binds them to the
  `ChatOpenAI` instance. Each turn then runs an iterative agent loop
  (max 3 LLM hops): LLM may emit `tool_calls` → adapter dispatches each
  call back through MCP (cidgar f4 strips CID, f5 appends Channel-3 marker
  to tool_result) → tool result fed back to LLM → ... → final text response
  (cidgar f3 PATH B injects C2 marker into content).

Capture model:
  - `_last_request` / `_last_response` are overwritten on every HTTP call
    (kept for legacy chat-only path + back-compat with the runner).
  - `_events` is a per-turn list. Each event snapshots the `_last_request`
    + `_last_response` at the moment that event was logged, so the UI's
    multi-step drawer can render the full agent-loop flow.
"""
from __future__ import annotations

import copy
import json
import os
from typing import Any

import httpx
from langchain_openai import ChatOpenAI


def pick_llm_base_url(routing: str, llm: str) -> str:
    env_map_via_agw = {
        "ollama": "AGW_LLM_BASE_URL_OLLAMA",
        "mock":   "AGW_LLM_BASE_URL_MOCK",
    }
    env_map_direct = {
        "ollama": "DIRECT_LLM_BASE_URL_OLLAMA",
        "mock":   "DIRECT_LLM_BASE_URL_MOCK",
    }
    env_map = env_map_via_agw if routing == "via_agw" else env_map_direct
    var = env_map.get(llm)
    if not var:
        raise ValueError(f"no LLM base URL mapping for llm={llm} routing={routing}")
    url = os.environ.get(var)
    if not url:
        raise ValueError(f"env var {var} not set")
    return url


def pick_mcp_base_url(routing: str, mcp: str) -> str:
    if mcp == "NONE":
        return ""
    prefix = "AGW_MCP_" if routing == "via_agw" else "DIRECT_MCP_"
    var = f"{prefix}{mcp.upper()}"
    url = os.environ.get(var)
    if not url:
        raise ValueError(f"env var {var} not set")
    return url


def _safe_json(raw: bytes) -> Any:
    """Try to JSON-decode; fall back to a decoded string; else a length marker."""
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


# ── Hop limit (avoid runaway loops). 3 covers list+call+followup comfortably.
MAX_LLM_HOPS = 3


class Trial:
    """Holds per-trial framework state for langchain.

    Uses httpx event hooks to capture the ACTUAL HTTP request + response
    bytes going over the wire — essential for cidgar pedagogy (diff what
    the framework sent vs. what cidgar returned).
    """

    def __init__(self, trial_id: str, config: dict):
        self.trial_id = trial_id
        self.config = config
        self.messages: list[Any] = []  # langchain message objects or dicts

        # Per-turn capture slots. Populated by httpx event hooks.
        # Note: overwritten on every HTTP call. Use `_events` for per-step.
        self._last_request: dict | None = None
        self._last_response: dict | None = None

        # Per-turn multi-step events (LLM hops + MCP tool calls).
        self._events: list[dict] = []

        base_url = pick_llm_base_url(routing=config["routing"], llm=config["llm"])
        model = config.get("model") or os.environ.get("DEFAULT_OLLAMA_MODEL", "qwen2.5:7b")

        # httpx client with event hooks that capture real wire bytes.
        async def log_req(request: httpx.Request) -> None:
            body_bytes = request.content or b""
            self._last_request = {
                "method": request.method,
                "url": str(request.url),
                "headers": {k: v for k, v in request.headers.items()},
                "body": _safe_json(body_bytes),
                "body_bytes_len": len(body_bytes),
            }

        async def log_resp(response: httpx.Response) -> None:
            # Read the body now so it's captured before returning
            await response.aread()
            body_bytes = response.content or b""
            self._last_response = {
                "status": response.status_code,
                "headers": {k: v for k, v in response.headers.items()},
                "body": _safe_json(body_bytes),
                "body_bytes_len": len(body_bytes),
                "elapsed_ms": int(response.elapsed.total_seconds() * 1000),
            }

        self._log_req = log_req
        self._log_resp = log_resp

        self._http_client = httpx.AsyncClient(
            event_hooks={"request": [log_req], "response": [log_resp]},
            timeout=httpx.Timeout(120.0),
        )

        self.llm = ChatOpenAI(
            base_url=base_url,
            api_key="ollama",  # placeholder; Ollama doesn't validate
            model=model,
            http_async_client=self._http_client,
            default_headers={},  # populated per-turn in drive_turn
            temperature=0.3,
        )

        # MCP wiring (lazy — fetched on first turn that needs it).
        self.mcp_url = (
            pick_mcp_base_url(config["routing"], config["mcp"])
            if config.get("mcp") and config["mcp"] != "NONE"
            else ""
        )
        self._mcp_tools: list | None = None  # populated by _setup_mcp_tools
        self._llm_with_tools: Any | None = None

    # ── httpx factory for langchain-mcp-adapters ──
    #
    # langchain-mcp-adapters' StreamableHttpConnection accepts an
    # `httpx_client_factory` callable. The MCP SDK calls it for every
    # session (and we use one session per `tools/list` + one per `call_tool`
    # because we pass `connection=` not `session=`). This installs OUR
    # event hooks on each created client, so wire bytes for every MCP
    # request flow into `_last_request`/`_last_response`.
    def _httpx_factory(self, **kwargs):
        existing_hooks = kwargs.pop("event_hooks", {}) or {}
        req_hooks = list(existing_hooks.get("request", [])) + [self._log_req]
        resp_hooks = list(existing_hooks.get("response", [])) + [self._log_resp]
        kwargs["event_hooks"] = {"request": req_hooks, "response": resp_hooks}
        return httpx.AsyncClient(**kwargs)

    def _mcp_connection(self, headers: dict[str, str]) -> dict:
        """Build a StreamableHttpConnection dict for langchain-mcp-adapters.

        Passing this as `connection=` (without a `session=`) makes the lib
        create a fresh MCP session per call, each one running through our
        httpx factory so the cidgar pipeline sees the wire bytes.
        """
        return {
            "transport": "streamable_http",
            "url": self.mcp_url,
            "headers": headers,
            "httpx_client_factory": self._httpx_factory,
        }

    async def _setup_mcp_tools(self, headers: dict[str, str]) -> None:
        """Lazy-load MCP tools (calls /tools/list once per trial).

        Caches the resulting LangChain tools + a `bind_tools()`-ed LLM.
        The cached tools' tool-call execution path uses `connection=` so
        each invocation re-opens an MCP session and our event hooks fire.
        """
        if not self.mcp_url:
            return
        if self._mcp_tools is not None:
            return

        # Use langchain-mcp-adapters as the canonical conversion path.
        # Falls back to a manual StructuredTool wrapping if the lib's
        # convert function ever changes shape — keeps the adapter robust.
        try:
            from langchain_mcp_adapters.tools import load_mcp_tools
        except ImportError as e:
            raise RuntimeError(
                "langchain-mcp-adapters not installed; required for mcp != NONE"
            ) from e

        connection = self._mcp_connection(headers)
        # Pass session=None + connection=conn so each tool execution opens
        # its own session (our httpx factory wires hooks on every one).
        tools = await load_mcp_tools(session=None, connection=connection)
        self._mcp_tools = tools
        self._llm_with_tools = self.llm.bind_tools(tools)

    def _snapshot_event(self, kind: str, **extra) -> dict:
        """Snapshot the most recent request/response into an event dict."""
        ev = {
            "t": kind,
            "request": copy.deepcopy(self._last_request) if self._last_request else None,
            "response": copy.deepcopy(self._last_response) if self._last_response else None,
        }
        ev.update(extra)
        return ev

    @staticmethod
    def _extract_tool_calls(resp: Any) -> list[dict]:
        """Normalize tool_calls off a langchain AIMessage (best-effort)."""
        tcs = getattr(resp, "tool_calls", None) or []
        out = []
        for tc in tcs:
            if isinstance(tc, dict):
                out.append({
                    "name": tc.get("name"),
                    "args": tc.get("args", {}) or {},
                    "id": tc.get("id"),
                })
            else:  # pydantic ToolCall
                out.append({
                    "name": getattr(tc, "name", None),
                    "args": getattr(tc, "args", {}) or {},
                    "id": getattr(tc, "id", None),
                })
        return out

    async def turn(self, turn_id: str, user_msg: str) -> dict:
        """One turn — chat-only or MCP agent loop, depending on config.

        Both paths propagate X-Harness-* headers and capture wire bytes.
        The MCP-agent path additionally captures per-step framework events.
        """
        # Lazy import; only the langchain message classes — keeps unit
        # tests that don't exercise turn() lighter.
        from langchain_core.messages import (
            HumanMessage, AIMessage, ToolMessage, SystemMessage,
        )

        headers = {
            "X-Harness-Trial-ID": self.trial_id,
            "X-Harness-Turn-ID": turn_id,
        }
        # Replace default_headers so each turn's X-Harness-Turn-ID updates.
        # ChatOpenAI is built with this http_async_client; default_headers
        # are sent by the OpenAI SDK on every call.
        self.llm.default_headers = headers

        # Reset per-turn capture
        self._last_request = None
        self._last_response = None
        self._events = []

        # Set up MCP tools once if needed (captures the tools/list event).
        await self._setup_mcp_tools(headers)
        if self._mcp_tools is not None and self._last_request is not None:
            # The most recent request was the tools/list call (only fires once
            # per trial — subsequent turns skip _setup_mcp_tools).
            self._events.append(self._snapshot_event(
                "tools_list",
                tool_count=len(self._mcp_tools),
                tool_names=[t.name for t in self._mcp_tools],
            ))

        self.messages.append(HumanMessage(content=user_msg))

        llm_to_use = self._llm_with_tools if self._llm_with_tools is not None else self.llm
        first_request = None
        final_resp: Any | None = None
        last_assistant_content = ""
        last_tool_calls: list[dict] = []

        # Iterative agent loop, max MAX_LLM_HOPS LLM hops.
        for hop in range(MAX_LLM_HOPS):
            resp = await llm_to_use.ainvoke(self.messages)
            # Record this LLM hop
            if first_request is None:
                first_request = copy.deepcopy(self._last_request)
            self._events.append(self._snapshot_event(f"llm_hop_{hop}"))

            asst_content = getattr(resp, "content", "") or ""
            tool_calls = self._extract_tool_calls(resp)
            last_assistant_content = asst_content if isinstance(asst_content, str) else str(asst_content)
            last_tool_calls = tool_calls

            # Append assistant message back into history. We use the
            # AIMessage from the response directly so tool_calls metadata
            # is preserved correctly for the next-hop wire format.
            self.messages.append(resp)

            if not tool_calls:
                final_resp = resp
                break

            # Execute each tool call against MCP.
            for tc in tool_calls:
                tool = next((t for t in (self._mcp_tools or []) if t.name == tc["name"]), None)
                if tool is None:
                    tool_result_text = f"unknown tool: {tc['name']}"
                    err = "unknown_tool"
                else:
                    try:
                        # `ainvoke(args)` runs the StructuredTool's coroutine,
                        # which (since we passed connection= not session=)
                        # opens a fresh MCP session via our httpx factory.
                        raw_result = await tool.ainvoke(tc.get("args", {}) or {})
                        # langchain_mcp_adapters returns either str / list /
                        # ToolMessage / Command depending on content shape.
                        tool_result_text = _stringify_tool_result(raw_result)
                        err = None
                    except Exception as e:  # noqa: BLE001
                        tool_result_text = f"tool error: {e}"
                        err = str(e)
                # Snapshot the MCP exchange. _last_request/_last_response
                # were just overwritten by the tool's MCP call.
                ev_extra = {
                    "tool_name": tc["name"],
                    "args": tc.get("args", {}),
                    "result_summary": tool_result_text[:500],
                }
                if err:
                    ev_extra["error"] = err
                self._events.append(self._snapshot_event("mcp_tool_call", **ev_extra))

                self.messages.append(ToolMessage(
                    content=tool_result_text,
                    tool_call_id=tc.get("id") or "",
                ))

            final_resp = resp  # in case we hit hop limit before LLM concludes
        else:
            # Hit hop limit without natural termination
            self._events.append({"t": "hop_limit_reached", "max_hops": MAX_LLM_HOPS})

        final_content = getattr(final_resp, "content", None) if final_resp is not None else None
        if not final_content:
            final_content = last_assistant_content or "(no response)"

        request_captured = first_request or self._last_request or {
            "method": "POST",
            "url": getattr(self.llm, "openai_api_base", "") or "",
            "headers": headers,
            "body": {"note": "event hook didn't fire — check httpx version"},
        }
        response_captured = self._last_response or {
            "status": 0,
            "headers": {},
            "body": {"note": "event hook didn't fire"},
        }

        return {
            "turn_id": turn_id,
            "assistant_msg": final_content,
            "tool_calls": last_tool_calls,
            "request_captured": request_captured,
            "response_captured": response_captured,
            "framework_events": self._events,
        }

    async def aclose(self) -> None:
        """Release httpx client connections."""
        try:
            await self._http_client.aclose()
        except Exception:
            pass


def _stringify_tool_result(raw: Any) -> str:
    """Render a langchain-mcp-adapters tool result as text for ToolMessage.

    Per langchain_mcp_adapters.tools, a tool's coroutine returns either:
      - a string (plain text content)
      - a list of LangChain content blocks (dicts with `text` / `image` keys)
      - a ToolMessage (langgraph path)
      - a (content, artifact) tuple from response_format="content_and_artifact"
    Normalize all of these into a flat string for our message history —
    cidgar's Channel-3 marker arrives as a `<!-- ib:cid=... -->` HTML
    comment inside tool_result text, which round-trips fine through str.
    """
    if raw is None:
        return ""
    if isinstance(raw, str):
        return raw
    if isinstance(raw, tuple) and len(raw) >= 1:
        return _stringify_tool_result(raw[0])
    if isinstance(raw, list):
        chunks = []
        for block in raw:
            if isinstance(block, dict):
                t = block.get("text") or block.get("data") or ""
                if t:
                    chunks.append(str(t))
                else:
                    chunks.append(json.dumps(block, default=str))
            else:
                txt = getattr(block, "text", None)
                chunks.append(str(txt) if txt is not None else str(block))
        return "\n".join(chunks)
    # ToolMessage / Command / unknown — fall back to its content/text/str.
    for attr in ("content", "text"):
        v = getattr(raw, attr, None)
        if v is not None:
            return _stringify_tool_result(v)
    try:
        return json.dumps(raw, default=str)
    except Exception:
        return str(raw)
