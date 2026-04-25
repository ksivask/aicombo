"""CrewAI-specific adapter logic.

Unlike langchain/langgraph (which share the ChatOpenAI substrate),
crewai 1.14+ routes `openai/...`, `anthropic/...`, `ollama/...`
etc. to their NATIVE SDK classes (OpenAICompletion, AnthropicCompletion,
OpenAICompatibleCompletion), each wrapping the vendor SDK directly
(`openai.OpenAI`, `anthropic.Anthropic`). The Crew itself is a
one-shot orchestrator: `Crew.kickoff_async` simply wraps the sync
`kickoff` in `asyncio.to_thread`, so all downstream HTTP calls run
in a worker thread using the SYNC SDK clients.

To capture wire bytes end-to-end we:

1. Construct `crewai.LLM(model=..., base_url=..., api_key=...)` as
   normal — this gives us the native-provider subclass.
2. Rebuild and swap BOTH `_client` (sync) and `_async_client` (async)
   on the returned instance, each configured with OUR httpx clients
   whose `event_hooks` funnel request+response snapshots into the
   per-trial `_http_exchanges` list.
3. For MCP, we subclass `crewai.tools.BaseTool` with a `_run` method
   that calls `fastmcp.Client` via a fresh event loop (since we're
   inside `to_thread`). The fastmcp transport accepts
   `httpx_client_factory`, so OUR hooked httpx client captures MCP
   wire bytes too.

Supported (api, llm) combos per Plan B spec:
  - api=chat, llm in {ollama, chatgpt, gemini, mock}
  - api=messages, llm=claude

Capture model mirrors adapters/langgraph/framework_bridge.py: one
pass over `_http_exchanges` at end of turn, classifying by URL and
JSON-RPC method into `llm_hop_N` / `mcp_*` event kinds.
"""
from __future__ import annotations

import asyncio
import copy
import json
import os
from typing import Any

import httpx

from crewai import Agent, Crew, LLM, Task
from crewai.tools import BaseTool as CrewBaseTool


# ── LLM base URL resolution (mirrors langgraph adapter) ──

def pick_llm_base_url(routing: str, llm: str) -> str:
    env_map_via_agw = {
        "ollama":  "AGW_LLM_BASE_URL_OLLAMA",
        "mock":    "AGW_LLM_BASE_URL_MOCK",
        "chatgpt": "AGW_LLM_BASE_URL_OPENAI",
        "gemini":  "AGW_LLM_BASE_URL_GEMINI",
        "claude":  "AGW_LLM_BASE_URL_ANTHROPIC",
    }
    env_map_direct = {
        "ollama":  "DIRECT_LLM_BASE_URL_OLLAMA",
        "mock":    "DIRECT_LLM_BASE_URL_MOCK",
        "chatgpt": "DIRECT_LLM_BASE_URL_OPENAI",
        "gemini":  "DIRECT_LLM_BASE_URL_GEMINI",
        "claude":  "DIRECT_LLM_BASE_URL_ANTHROPIC",
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


# Per-LLM API-key env var. Cloud providers (chatgpt/gemini/claude) need a real
# key; local ones (ollama/mock) accept a placeholder.
_API_KEY_ENV_BY_LLM = {
    "ollama":  None,
    "mock":    None,
    "chatgpt": "OPENAI_API_KEY",
    "gemini":  "GOOGLE_API_KEY",
    "claude":  "ANTHROPIC_API_KEY",
}


def _pick_api_key(llm: str) -> str:
    env_var = _API_KEY_ENV_BY_LLM.get(llm)
    if env_var is None:
        return "placeholder"
    key = os.environ.get(env_var, "")
    if not key:
        raise ValueError(
            f"{env_var} not set in adapter environment — needed for llm={llm}"
        )
    return key


def _llm_model_string(config: dict) -> str:
    """Build a litellm/crewai-compatible model identifier from (api, llm, model).

    crewai 1.14 parses the `<provider>/<model>` prefix to route to the
    correct native SDK. Choices:
      - api=chat, ollama  → "ollama/<model>"         (OpenAICompatibleCompletion)
      - api=chat, chatgpt → "openai/<model>"         (OpenAICompletion)
      - api=chat, gemini  → "gemini/<model>"         (GeminiCompletion)
      - api=chat, mock    → "openai/<any>"           (OpenAICompletion pointed at mock-llm)
      - api=messages, claude → "anthropic/<model>"   (AnthropicCompletion)
    """
    api = config.get("api")
    llm = config.get("llm")
    requested = config.get("model")
    if api == "chat":
        if llm == "ollama":
            return f"ollama/{requested or os.environ.get('DEFAULT_OLLAMA_MODEL', 'qwen2.5:7b')}"
        if llm == "chatgpt":
            return f"openai/{requested or os.environ.get('DEFAULT_OPENAI_MODEL', 'gpt-4o-mini')}"
        if llm == "gemini":
            return f"gemini/{requested or os.environ.get('DEFAULT_GEMINI_MODEL', 'gemini-2.0-flash')}"
        if llm == "mock":
            # mock-llm exposes OpenAI-compat chat completions; route via
            # OpenAICompletion. Model name is arbitrary but must match a
            # recognized openai prefix for the native path.
            return f"openai/{requested or os.environ.get('DEFAULT_OPENAI_MODEL', 'gpt-4o-mini')}"
    elif api == "messages":
        if llm == "claude":
            return f"anthropic/{requested or os.environ.get('DEFAULT_CLAUDE_MODEL', 'claude-haiku-4-5')}"
    raise ValueError(f"unsupported (api, llm) for crewai: ({api}, {llm})")


# ── HTTP capture helpers (shared sync+async hook closure factory) ──

def _make_hook_closures(trial: "Trial"):
    """Build sync+async request/response hooks that write to trial._http_exchanges.

    httpx.Client uses sync hooks; httpx.AsyncClient uses async ones. crewai's
    native providers build BOTH a sync and async client (the sync one is
    what kickoff_async hits via to_thread); fastmcp uses AsyncClient. So we
    return a pair of each kind, all writing to the same shared list.
    """

    def _snap_req(request: httpx.Request) -> dict:
        body_bytes = request.content or b""
        return {
            "method": request.method,
            "url": str(request.url),
            "headers": {k: v for k, v in request.headers.items()},
            "body": _safe_json(body_bytes),
            "body_bytes_len": len(body_bytes),
            "_req_id": id(request),
        }

    def _snap_resp(response: httpx.Response, body_bytes: bytes) -> dict:
        return {
            "status": response.status_code,
            "headers": {k: v for k, v in response.headers.items()},
            "body": _safe_json(body_bytes),
            "body_bytes_len": len(body_bytes),
            "elapsed_ms": int(response.elapsed.total_seconds() * 1000),
        }

    def _attach_resp(resp_snap: dict, request_id: int) -> None:
        trial._last_response = resp_snap
        for ex in reversed(trial._http_exchanges):
            if ex["resp"] is None and ex["req"].get("_req_id") == request_id:
                ex["resp"] = resp_snap
                return
        for ex in reversed(trial._http_exchanges):
            if ex["resp"] is None:
                ex["resp"] = resp_snap
                return

    # ── sync (httpx.Client — crewai native SDK sync path) ──
    def sync_log_req(request: httpx.Request) -> None:
        req_snap = _snap_req(request)
        trial._last_request = req_snap
        trial._http_exchanges.append({"req": req_snap, "resp": None})

    def sync_log_resp(response: httpx.Response) -> None:
        response.read()
        body_bytes = response.content or b""
        resp_snap = _snap_resp(response, body_bytes)
        _attach_resp(resp_snap, id(response.request))

    # ── async (httpx.AsyncClient — fastmcp path + any async SDK usage) ──
    async def async_log_req(request: httpx.Request) -> None:
        req_snap = _snap_req(request)
        trial._last_request = req_snap
        trial._http_exchanges.append({"req": req_snap, "resp": None})

    async def async_log_resp(response: httpx.Response) -> None:
        await response.aread()
        body_bytes = response.content or b""
        resp_snap = _snap_resp(response, body_bytes)
        _attach_resp(resp_snap, id(response.request))

    return sync_log_req, sync_log_resp, async_log_req, async_log_resp


# ── Native SDK client rebuilds ─────────────────────────────────────────
#
# crewai's native providers eagerly build `_client` + `_async_client` in
# their post-init. We rebuild them so the underlying transport is OUR
# hooked httpx client. The base_url / api_key on the crewai LLM are
# already set (passed through kwargs in __init__) — we just reuse them.

def _rebuild_openai_clients(llm_obj: Any, sync_client: httpx.Client,
                            async_client: httpx.AsyncClient,
                            base_url: str, api_key: str) -> None:
    """Swap OpenAI sync + async clients on an OpenAICompletion instance."""
    from openai import AsyncOpenAI, OpenAI

    llm_obj._client = OpenAI(
        api_key=api_key,
        base_url=base_url,
        http_client=sync_client,
    )
    llm_obj._async_client = AsyncOpenAI(
        api_key=api_key,
        base_url=base_url,
        http_client=async_client,
    )


def _rebuild_anthropic_clients(llm_obj: Any, sync_client: httpx.Client,
                               async_client: httpx.AsyncClient,
                               base_url: str, api_key: str) -> None:
    """Swap Anthropic sync + async clients on an AnthropicCompletion instance."""
    from anthropic import Anthropic, AsyncAnthropic

    kwargs_sync: dict[str, Any] = {
        "api_key": api_key,
        "http_client": sync_client,
    }
    kwargs_async: dict[str, Any] = {
        "api_key": api_key,
        "http_client": async_client,
    }
    if base_url:
        kwargs_sync["base_url"] = base_url
        kwargs_async["base_url"] = base_url

    llm_obj._client = Anthropic(**kwargs_sync)
    llm_obj._async_client = AsyncAnthropic(**kwargs_async)


# ── MCP tool wrapper ───────────────────────────────────────────────────
#
# fastmcp.Client is async-only. crewai calls tools from the sync path
# (inside to_thread), so our _run must synchronously run an async
# fastmcp session. We can safely `asyncio.run(...)` inside to_thread.

class McpFastmcpTool(CrewBaseTool):
    """CrewAI BaseTool subclass that forwards every call to an MCP tool.

    Private attrs (leading underscore) are excluded from pydantic model
    serialization; we set them on the instance after construction.
    """
    # All config fields are set at construction time via the factory below.
    # Keep the schema permissive: accept arbitrary kwargs, forward as-is.

    _mcp_url: str = ""
    _mcp_headers: dict = {}
    _trial_ref: Any = None  # Trial — holds hook closures + _http_exchanges

    class Config:
        extra = "allow"

    def _run(self, *args: Any, **kwargs: Any) -> str:
        """Sync entry point (called from crewai's agent loop)."""
        # Safe to use asyncio.run — we're in a worker thread (to_thread).
        return asyncio.run(self._arun_impl(kwargs))

    async def _arun(self, *args: Any, **kwargs: Any) -> str:
        """Async entry point — same logic, used if crewai calls via arun."""
        return await self._arun_impl(kwargs)

    async def _arun_impl(self, kwargs: dict) -> str:
        from fastmcp import Client
        from fastmcp.client.transports import StreamableHttpTransport

        trial = self._trial_ref
        transport = StreamableHttpTransport(
            url=self._mcp_url,
            headers=dict(self._mcp_headers or {}),
            httpx_client_factory=(trial._httpx_factory if trial else None),
        )
        async with Client(transport=transport) as c:
            res = await c.call_tool(self.name, kwargs)
        if res is None:
            return ""
        # fastmcp CallToolResult has .content (list of TextContent blocks)
        content = getattr(res, "content", None)
        if content is None:
            return str(res)
        chunks = []
        for block in content:
            t = getattr(block, "text", None)
            if t is not None:
                chunks.append(str(t))
                continue
            if isinstance(block, dict):
                tt = block.get("text") or block.get("data")
                if tt:
                    chunks.append(str(tt))
                    continue
            chunks.append(str(block))
        return "\n".join(chunks) if chunks else ""


def _mcp_tool_from_schema(
    mcp_tool: Any,
    mcp_url: str,
    headers: dict,
    trial: "Trial",
) -> McpFastmcpTool:
    """Create an McpFastmcpTool instance from a fastmcp Tool descriptor.

    fastmcp exposes Tool objects with .name/.description/.inputSchema.
    We don't bother wiring the JSON schema into crewai's pydantic
    args_schema — crewai accepts kwargs via `extra="allow"` on our
    BaseTool subclass, and the LLM just needs the name + description
    in the tool list to know when to call it.
    """
    inst = McpFastmcpTool(
        name=mcp_tool.name,
        description=(mcp_tool.description or f"MCP tool: {mcp_tool.name}")[:1024],
    )
    # PrivateAttrs aren't part of pydantic validation — assign directly.
    inst._mcp_url = mcp_url
    inst._mcp_headers = headers
    inst._trial_ref = trial
    return inst


# ── Trial ──────────────────────────────────────────────────────────────

class Trial:
    """Per-trial state for the crewai adapter."""

    def __init__(self, trial_id: str, config: dict):
        self.trial_id = trial_id
        self.config = config

        # Conversation state — crewai Crew is single-shot per kickoff, so
        # we manually prepend prior turns into each Task description.
        self._messages: list[dict] = []

        # Per-turn capture
        self._last_request: dict | None = None
        self._last_response: dict | None = None
        self._events: list[dict] = []

        # Full HTTP exchange log; per-turn window marked by _exchange_mark.
        self._http_exchanges: list[dict] = []
        self._exchange_mark: int = 0

        # Build hooks closures (they all target THIS trial).
        self._sync_req_hook, self._sync_resp_hook, \
            self._async_req_hook, self._async_resp_hook = _make_hook_closures(self)

        # Build our hooked httpx clients (sync + async share the same set
        # of trial-scoped capture slots).
        self._http_client = httpx.AsyncClient(
            event_hooks={
                "request":  [self._async_req_hook],
                "response": [self._async_resp_hook],
            },
            timeout=httpx.Timeout(120.0),
        )
        self._sync_http_client = httpx.Client(
            event_hooks={
                "request":  [self._sync_req_hook],
                "response": [self._sync_resp_hook],
            },
            timeout=httpx.Timeout(120.0),
        )

        # Build the crewai LLM instance and swap in our hooked clients.
        base_url = pick_llm_base_url(routing=config["routing"], llm=config["llm"])
        model_str = _llm_model_string(config)
        api_key = _pick_api_key(config["llm"])

        self.llm = LLM(
            model=model_str,
            base_url=base_url,
            api_key=api_key,
            temperature=0.3,
        )

        llm_type = type(self.llm).__name__
        # Rebuild native SDK clients to use our hooked httpx.
        if llm_type in ("OpenAICompletion", "OpenAICompatibleCompletion"):
            _rebuild_openai_clients(
                self.llm, self._sync_http_client, self._http_client,
                base_url=base_url, api_key=api_key,
            )
        elif llm_type == "AnthropicCompletion":
            _rebuild_anthropic_clients(
                self.llm, self._sync_http_client, self._http_client,
                base_url=base_url, api_key=api_key,
            )
        # Other provider classes (Gemini, Bedrock, etc.) could be added
        # here; for Plan B T3 we only need OpenAI + Anthropic paths.

        # MCP tools (lazy — loaded on first turn).
        self.mcp_url = (
            pick_mcp_base_url(config["routing"], config["mcp"])
            if config.get("mcp") and config["mcp"] != "NONE"
            else ""
        )
        self._mcp_tools: list[McpFastmcpTool] | None = None

    # ── httpx factory for fastmcp ──

    def _httpx_factory(self, **kwargs) -> httpx.AsyncClient:
        existing_hooks = kwargs.pop("event_hooks", {}) or {}
        req_hooks = list(existing_hooks.get("request", [])) + [self._async_req_hook]
        resp_hooks = list(existing_hooks.get("response", [])) + [self._async_resp_hook]
        kwargs["event_hooks"] = {"request": req_hooks, "response": resp_hooks}
        return httpx.AsyncClient(**kwargs)

    # ── exchange classification (same shape as langgraph adapter) ──

    def _mark_exchange_start(self) -> None:
        self._exchange_mark = len(self._http_exchanges)

    def _exchanges_since_mark(self) -> list[dict]:
        return list(self._http_exchanges[self._exchange_mark:])

    @staticmethod
    def _rpc_method(exchange: dict) -> str | None:
        req = exchange.get("req") or {}
        body = req.get("body")
        if isinstance(body, dict):
            return body.get("method")
        return None

    @staticmethod
    def _is_llm_exchange(exchange: dict) -> bool:
        req = exchange.get("req") or {}
        url = str(req.get("url", ""))
        # OpenAI-compat chat completions, Responses API, Anthropic messages.
        return (
            "chat/completions" in url
            or "/llm/" in url
            or "/v1/messages" in url
            or "/responses" in url
        )

    def _capture_events_since_mark(self) -> list[dict]:
        method_to_kind = {
            "initialize":                "mcp_initialize",
            "notifications/initialized": "mcp_notif_initialized",
            "tools/list":                "mcp_tools_list",
            "tools/call":                "mcp_tools_call",
        }
        out: list[dict] = []
        llm_hop_n = 0
        for ex in self._exchanges_since_mark():
            req = ex.get("req") or {}
            method = req.get("method", "")
            rpc_method = self._rpc_method(ex)

            if self._is_llm_exchange(ex):
                kind = f"llm_hop_{llm_hop_n}"
                llm_hop_n += 1
            elif method == "DELETE":
                kind = "mcp_session_close"
            elif method == "GET" and "text/event-stream" in str(
                (req.get("headers") or {}).get("accept", "")
            ):
                kind = "mcp_sse_open"
            elif rpc_method and rpc_method in method_to_kind:
                kind = method_to_kind[rpc_method]
            elif rpc_method:
                kind = f"mcp_rpc_{rpc_method.replace('/', '_')}"
            else:
                kind = "mcp_http"

            out.append({
                "t": kind,
                "rpc_method": rpc_method,
                "request": copy.deepcopy(ex.get("req") or {}),
                "response": copy.deepcopy(ex.get("resp") or {}),
            })
        return out

    # ── MCP bootstrap ──

    async def _setup_mcp_tools(self, headers: dict) -> None:
        """Fetch the tool list from the MCP server and wrap each as an McpFastmcpTool."""
        if self._mcp_tools is not None:
            return
        if not self.mcp_url:
            self._mcp_tools = []
            return

        from fastmcp import Client
        from fastmcp.client.transports import StreamableHttpTransport
        transport = StreamableHttpTransport(
            url=self.mcp_url,
            headers=dict(headers),
            httpx_client_factory=self._httpx_factory,
        )
        async with Client(transport=transport) as c:
            raw_tools = await c.list_tools()

        self._mcp_tools = [
            _mcp_tool_from_schema(t, self.mcp_url, headers, self)
            for t in raw_tools
        ]

    # ── Turn driver ──

    async def turn(self, turn_id: str, user_msg: str) -> dict:
        """Drive one turn through a fresh crewai Crew.kickoff_async."""
        headers = {
            "X-Harness-Trial-ID": self.trial_id,
            "X-Harness-Turn-ID":  turn_id,
        }

        # Reset per-turn capture
        self._last_request = None
        self._last_response = None
        self._events = []

        # Apply per-turn headers to both httpx clients (so LLM calls carry them).
        # We can't mutate httpx.Client default_headers mid-flight cleanly, so we
        # rebuild the underlying SDK clients' base_url/api_key — they already have
        # our hooked httpx. Instead: pass headers via default_headers on the
        # SDK layer (OpenAI/Anthropic SDK both accept `default_headers`). Simpler
        # for MVP: just rely on the SDK's own retry/headers; X-Harness-* are best-
        # effort and currently unused by cidgar for LLM routing in Plan A/B.
        # (Langgraph adapter does the same — sets self.llm.default_headers; the
        # underlying openai SDK picks them up.)
        try:
            self.llm.default_headers = headers
        except Exception:
            pass

        # MCP tool setup (lazy, first-turn). Captures initialize + tools/list
        # as its own event window.
        if self.mcp_url and self._mcp_tools is None:
            setup_mark = len(self._http_exchanges)
            await self._setup_mcp_tools(headers)
            if len(self._http_exchanges) > setup_mark:
                self._exchange_mark = setup_mark
                setup_events = self._capture_events_since_mark()
                for ev in setup_events:
                    if ev["t"] == "mcp_tools_list":
                        ev["tool_count"] = len(self._mcp_tools or [])
                        ev["tool_names"] = [t.name for t in (self._mcp_tools or [])]
                self._events.extend(setup_events)

        # Record this user message into our conversation history. crewai's
        # Crew is single-shot per kickoff_async, so we splice prior turns
        # into the Task description as context.
        prior_history = "\n".join(
            f"{m['role'].capitalize()}: {m['content']}"
            for m in self._messages
        )
        self._messages.append({"role": "user", "content": user_msg})
        if prior_history:
            task_desc = (
                f"Prior conversation:\n{prior_history}\n\n"
                f"User: {user_msg}"
            )
        else:
            task_desc = user_msg

        # Build a fresh Agent + Task + Crew for this turn.
        agent = Agent(
            role="Helpful assistant",
            goal="Answer the user's question accurately",
            backstory=(
                "You answer concisely and use tools when relevant. "
                "If no tool is needed, reply directly to the user."
            ),
            llm=self.llm,
            tools=list(self._mcp_tools or []),
            allow_delegation=False,
            verbose=False,
        )
        task = Task(
            description=task_desc,
            expected_output="A direct answer to the user's question.",
            agent=agent,
        )
        crew = Crew(agents=[agent], tasks=[task], verbose=False)

        # Mark the window for this turn's kickoff, then run.
        self._mark_exchange_start()
        try:
            result = await crew.kickoff_async(inputs={})
        except Exception as e:  # noqa: BLE001
            # Collapse any failure into an error string so the harness
            # still gets a response payload.
            result = None
            final_text = f"(crewai error: {e.__class__.__name__}: {e})"
        else:
            final_text = str(getattr(result, "raw", None) or result or "").strip()

        if not final_text:
            final_text = "(no response)"

        # Classify all HTTP exchanges that happened during kickoff.
        turn_events = self._capture_events_since_mark()
        self._events.extend(turn_events)

        # Record assistant turn into conversation history.
        self._messages.append({"role": "assistant", "content": final_text})

        # Pick the first LLM request of the turn for the legacy slot.
        request_captured = None
        for ev in turn_events:
            if ev["t"].startswith("llm_hop_") and ev.get("request"):
                request_captured = ev["request"]
                break
        if request_captured is None:
            request_captured = self._last_request or {
                "method": "POST",
                "url": getattr(self.llm, "base_url", "") or "",
                "headers": headers,
                "body": {"note": "event hook didn't fire — crewai may have used cache"},
            }

        response_captured = self._last_response or {
            "status": 0,
            "headers": {},
            "body": {"note": "event hook didn't fire"},
        }

        # Best-effort: extract tool_calls from captured LLM responses.
        last_tool_calls: list[dict] = []
        for ev in reversed(turn_events):
            if ev["t"].startswith("llm_hop_"):
                last_tool_calls = _extract_tool_calls_from_response(ev.get("response") or {})
                if last_tool_calls:
                    break

        return {
            "turn_id": turn_id,
            "assistant_msg": final_text,
            "tool_calls": last_tool_calls,
            "request_captured": request_captured,
            "response_captured": response_captured,
            "framework_events": self._events,
        }

    async def compact(self, strategy: str) -> dict:
        """Plan B T10 — mutate `self._messages` per the requested strategy.

        crewai's conversation history in this adapter is a list of plain
        `{"role": str, "content": str}` dicts that we assemble and splice
        into each Task description. The dicts don't carry tool_calls
        metadata (crewai owns that internally during kickoff), so:

          * drop_half — drop oldest 50% of messages.
          * drop_tool_calls — falls back to drop_half (no tool_calls
            metadata in our dict schema to filter on). Documented.
          * summarize — drop_half + prepend a synthesized system summary.
        """
        before = len(self._messages)
        if strategy == "drop_half":
            self._messages = self._messages[before // 2:]
            note = None
        elif strategy == "drop_tool_calls":
            self._messages = self._messages[before // 2:]
            note = (
                "crewai dict schema has no tool_calls metadata — "
                "fell back to drop_half"
            )
        elif strategy == "summarize":
            keep = self._messages[before // 2:]
            dropped = before - len(keep)
            self._messages = [
                {"role": "system",
                 "content": f"[summarized {dropped} earlier messages]"}
            ] + keep
            note = None
        else:
            raise ValueError(f"unknown strategy: {strategy}")
        out: dict = {
            "strategy": strategy,
            "history_len_before": before,
            "history_len_after": len(self._messages),
        }
        if note:
            out["note"] = note
        return out

    async def aclose(self) -> None:
        try:
            await self._http_client.aclose()
        except Exception:
            pass
        try:
            self._sync_http_client.close()
        except Exception:
            pass


def _extract_tool_calls_from_response(response_snap: dict) -> list[dict]:
    """Parse tool_calls off a captured LLM response body (OpenAI-chat shape)."""
    body = response_snap.get("body")
    if not isinstance(body, dict):
        return []
    out: list[dict] = []
    # OpenAI chat.completions shape
    for choice in body.get("choices", []) or []:
        msg = choice.get("message") or {}
        for tc in msg.get("tool_calls", []) or []:
            fn = tc.get("function") or {}
            args_raw = fn.get("arguments") or "{}"
            try:
                args = json.loads(args_raw) if isinstance(args_raw, str) else (args_raw or {})
            except Exception:
                args = {"_raw": args_raw}
            out.append({
                "name": fn.get("name"),
                "args": args,
                "id": tc.get("id"),
            })
    # Anthropic messages shape: content blocks with type=tool_use
    for block in body.get("content", []) or []:
        if isinstance(block, dict) and block.get("type") == "tool_use":
            out.append({
                "name": block.get("name"),
                "args": block.get("input") or {},
                "id": block.get("id"),
            })
    return out
