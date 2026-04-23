"""llamaindex-specific adapter logic.

LlamaIndex (`llama-index-core` + `llama-index-llms-openai`) is the last of
the five Plan B framework adapters. Its core LLM interface is the
`llama_index.core.llms.LLM` abstract class; the OpenAI-compat concrete
implementation is `llama_index.llms.openai.OpenAI`, and there is a separate
Responses-API client, `llama_index.llms.openai.OpenAIResponses`.

Both classes accept `async_http_client=httpx.AsyncClient` (forwarded to the
underlying `openai.AsyncOpenAI`), so a single event-hook'd
httpx.AsyncClient captures all LLM wire bytes without monkey-patching —
same pattern as the pydantic-ai adapter.

For api=chat we DO use `OpenAI(..., async_http_client=our_httpx).achat(...)`
directly so llama_index owns the agent-shaped loop.

For api=responses / api=responses+conv we BYPASS `OpenAIResponses` and
call `openai.AsyncOpenAI(http_client=our_httpx).responses.create(...)`
directly. Rationale (mirrors adapters/autogen): we need full control over
`previous_response_id` chaining + the `force_state_ref` override path
for verdict (e) testing in T11, and the direct openai SDK is strictly
simpler than navigating llamaindex's `track_previous_responses` /
`previous_response_id` state-tracking abstraction.

MCP integration: we don't use `llama-index-tools-mcp` — its
`McpToolSpec` builds an mcp-sdk `streamablehttp_client` internally
which does NOT accept a custom httpx client. Instead (same as
adapters/crewai and adapters/autogen) we wrap `fastmcp.Client`
manually with a `FunctionTool` whose `httpx_client_factory` is our
hooked client.

Supported (api, llm) combos:
  - api=chat,           llm in {ollama, chatgpt, gemini, mock}  (llama_index.OpenAI)
  - api=responses,      llm=chatgpt                              (openai SDK bypass)
  - api=responses+conv, llm=chatgpt                              (openai SDK bypass + previous_response_id chain)

Note: llamaindex does not have a native Anthropic Messages API wrapper on
the LLM catalog equivalent to `OpenAIChatCompletionClient`'s Anthropic
sibling. api=messages is deliberately NOT supported here — crewai /
autogen / pydantic-ai already cover that combo.

Capture model mirrors the other adapters: mark the HTTP exchange list
at turn start, run the agent loop, then classify every exchange captured
during the run as `llm_hop_N` or `mcp_*` by URL + JSON-RPC method.
"""
from __future__ import annotations

import asyncio
import copy
import json
import os
from typing import Any

import httpx


# ── LLM base URL resolution (mirrors autogen / pydantic-ai) ──

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


def _default_model_name(api: str, llm: str, requested: str | None) -> str:
    """Return the concrete model identifier for (api, llm, requested)."""
    if requested:
        return requested
    if api == "chat":
        if llm == "ollama":
            return os.environ.get("DEFAULT_OLLAMA_MODEL", "qwen2.5:7b")
        if llm == "chatgpt":
            return "gpt-4o-mini"
        if llm == "gemini":
            return "gemini-1.5-flash"
        if llm == "mock":
            return "gpt-4o-mini"
    if api in ("responses", "responses+conv"):
        if llm == "chatgpt":
            return "gpt-4o-mini"
    raise ValueError(f"unsupported (api, llm) for llamaindex: ({api}, {llm})")


def _build_chat_llm(llm: str, model_name: str, base_url: str,
                    api_key: str, http_client: httpx.AsyncClient) -> Any:
    """Construct a llamaindex chat LLM with our hooked async_http_client.

    For `chatgpt` (real OpenAI catalog names like gpt-4o-mini) we use
    `llama_index.llms.openai.OpenAI`, whose `metadata` property does a
    catalog lookup to pick `is_chat_model` / context_window.

    For `ollama` / `gemini` / `mock` we instead use
    `llama_index.llms.openai_like.OpenAILike`. The base `OpenAI` wrapper's
    `metadata` hits `openai_modelname_to_contextsize` which raises
    "Unknown model 'qwen2.5:7b'" for anything outside the OpenAI catalog.
    `OpenAILike` takes the same openai-compat wire protocol but lets the
    caller declare `is_chat_model=True` + `context_window=...` explicitly,
    bypassing the catalog. Both classes accept `async_http_client=` which
    flows straight into `openai.AsyncOpenAI(http_client=...)` — so our
    httpx event hooks still capture every LLM wire byte.
    """
    if llm == "chatgpt":
        from llama_index.llms.openai import OpenAI
        return OpenAI(
            model=model_name,
            api_base=base_url,
            api_key=api_key,
            async_http_client=http_client,
            # reuse_client=True (default) so llamaindex keeps a single
            # AsyncOpenAI wrapping our hooked http_client. With
            # reuse_client=False it builds a fresh AsyncOpenAI per call
            # and can close our shared httpx client out from under us on
            # retries / __del__ (observed as APIConnectionError on turn 2+).
            reuse_client=True,
        )
    # ollama / gemini / mock / anything non-catalog
    from llama_index.llms.openai_like import OpenAILike
    return OpenAILike(
        model=model_name,
        api_base=base_url,
        api_key=api_key,
        async_http_client=http_client,
        is_chat_model=True,
        context_window=8192,
        reuse_client=True,
    )


# ── Trial ───────────────────────────────────────────────────────────────

class Trial:
    """Per-trial state for the llamaindex adapter.

    State modes:
      - stateless: chat + responses without state. For chat, we accumulate
        `ChatMessage` history and pass it to `llm.achat(messages=[...])`.
      - responses_previous_id: api=responses+conv (or api=responses+state=T):
        `_last_response_id` chained across turns as `previous_response_id`.
        `_response_history` lets `force_state_ref(turn_idx)` override the
        next turn's previous_response_id — verdict (e) test path in T11.
    """

    def __init__(self, trial_id: str, config: dict):
        self.trial_id = trial_id
        self.config = config

        # ── Conversation state ──
        # For chat: ChatMessage list, passed to llm.achat() each turn.
        self._messages: list[Any] = []
        # For responses/responses+conv: chained response id(s).
        self._last_response_id: str | None = None
        self._response_history: list[str] = []
        self._forced_prev_id: str | None = None

        # Per-turn capture
        self._last_request: dict | None = None
        self._last_response: dict | None = None
        self._events: list[dict] = []

        # Full HTTP exchange log; per-turn window marked by _exchange_mark.
        self._http_exchanges: list[dict] = []
        self._exchange_mark: int = 0

        # Mutable shared headers dict. The httpx.AsyncClient is built once
        # with headers=this_dict; mutating it per turn is picked up by
        # httpx on each outgoing request (same trick as pydantic-ai/autogen).
        self._headers: dict[str, str] = {
            "X-Harness-Trial-ID": trial_id,
            "X-Harness-Turn-ID":  "",
        }

        # httpx event hooks that capture wire bytes into _http_exchanges.
        async def log_req(request: httpx.Request) -> None:
            body_bytes = request.content or b""
            req_snap = {
                "method": request.method,
                "url": str(request.url),
                "headers": {k: v for k, v in request.headers.items()},
                "body": _safe_json(body_bytes),
                "body_bytes_len": len(body_bytes),
                "_req_id": id(request),
            }
            self._last_request = req_snap
            self._http_exchanges.append({"req": req_snap, "resp": None})

        async def log_resp(response: httpx.Response) -> None:
            await response.aread()
            body_bytes = response.content or b""
            resp_snap = {
                "status": response.status_code,
                "headers": {k: v for k, v in response.headers.items()},
                "body": _safe_json(body_bytes),
                "body_bytes_len": len(body_bytes),
                "elapsed_ms": int(response.elapsed.total_seconds() * 1000),
            }
            self._last_response = resp_snap
            req_id = id(response.request)
            for ex in reversed(self._http_exchanges):
                if ex["resp"] is None and ex["req"].get("_req_id") == req_id:
                    ex["resp"] = resp_snap
                    return
            for ex in reversed(self._http_exchanges):
                if ex["resp"] is None:
                    ex["resp"] = resp_snap
                    return

        self._async_log_req = log_req
        self._async_log_resp = log_resp

        # Single hooked AsyncClient — used by the llama_index OpenAI LLM,
        # the openai-responses-bypass client, AND (via _httpx_factory)
        # fastmcp.
        self._http_client = httpx.AsyncClient(
            event_hooks={"request": [log_req], "response": [log_resp]},
            headers=self._headers,
            timeout=httpx.Timeout(120.0),
        )

        # ── LLM endpoint + model resolution ──
        self._base_url = pick_llm_base_url(routing=config["routing"], llm=config["llm"])
        self._api_key = _pick_api_key(config["llm"])
        self._model_name = _default_model_name(
            config["api"], config["llm"], config.get("model"),
        )

        # ── Build the per-mode client(s) ──
        api = config["api"]
        self._mode: str  # 'chat' | 'responses_direct'
        if api == "chat":
            self._mode = "chat"
            self._llm = _build_chat_llm(
                llm=config["llm"], model_name=self._model_name,
                base_url=self._base_url, api_key=self._api_key,
                http_client=self._http_client,
            )
        elif api in ("responses", "responses+conv"):
            # Direct openai SDK bypass — consistent with adapters/autogen;
            # simpler than llamaindex's OpenAIResponses state abstraction.
            self._mode = "responses_direct"
            from openai import AsyncOpenAI
            self._openai_client = AsyncOpenAI(
                api_key=self._api_key,
                base_url=self._base_url,
                http_client=self._http_client,
            )
        else:
            raise ValueError(f"unsupported api for llamaindex: {api}")

        # MCP wiring (lazy: first turn triggers list + tool wrap).
        self.mcp_url = (
            pick_mcp_base_url(config["routing"], config["mcp"])
            if config.get("mcp") and config["mcp"] != "NONE"
            else ""
        )
        self._mcp_tools: list[Any] | None = None

    # ── httpx factory for fastmcp ──

    def _httpx_factory(self, **kwargs) -> httpx.AsyncClient:
        existing_hooks = kwargs.pop("event_hooks", {}) or {}
        req_hooks = list(existing_hooks.get("request", [])) + [self._async_log_req]
        resp_hooks = list(existing_hooks.get("response", [])) + [self._async_log_resp]
        kwargs["event_hooks"] = {"request": req_hooks, "response": resp_hooks}
        return httpx.AsyncClient(**kwargs)

    # ── exchange classification (shared shape with pydantic-ai/crewai/autogen) ──

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

    # ── MCP tool bootstrap (fastmcp path, shared with crewai/autogen) ──

    async def _setup_mcp_tools(self) -> None:
        """Fetch MCP tool list via fastmcp + wrap each as a FunctionTool."""
        if self._mcp_tools is not None:
            return
        if not self.mcp_url:
            self._mcp_tools = []
            return

        from fastmcp import Client
        from fastmcp.client.transports import StreamableHttpTransport

        transport = StreamableHttpTransport(
            url=self.mcp_url,
            headers=dict(self._headers),
            httpx_client_factory=self._httpx_factory,
        )
        async with Client(transport=transport) as c:
            raw_tools = await c.list_tools()

        self._mcp_tools = [
            _make_llamaindex_mcp_tool(t, self.mcp_url, self._headers, self)
            for t in raw_tools
        ]

    # ── Turn drivers (dispatch by mode) ──

    async def turn(self, turn_id: str, user_msg: str) -> dict:
        """Drive one turn, capturing all HTTP exchanges.

        Mutates `self._headers` in place so the next HTTP request carries
        the updated turn-id (httpx reads headers= fresh on each request).
        """
        # Update shared headers dict.
        self._headers["X-Harness-Trial-ID"] = self.trial_id
        self._headers["X-Harness-Turn-ID"] = turn_id

        # Reset per-turn capture
        self._last_request = None
        self._last_response = None
        self._events = []

        if self._mode == "chat":
            return await self._turn_chat(turn_id, user_msg)
        if self._mode == "responses_direct":
            return await self._turn_responses_direct(turn_id, user_msg)
        raise RuntimeError(f"unknown mode: {self._mode}")

    async def _turn_chat(self, turn_id: str, user_msg: str) -> dict:
        """Turn via llama_index OpenAI.achat() + manual tool-call loop.

        llama_index doesn't ship a "ReAct agent" wired for OpenAI tool calls
        in the same single-line way langgraph does. The simplest reliable
        path for Plan B's chat-mode smoke (ollama + NONE) is `achat` with
        accumulated history. MCP tool-call support falls into the same
        manual driver: if the model emits tool_calls in its response, we
        execute them via fastmcp and loop.
        """
        from llama_index.core.base.llms.types import ChatMessage, MessageRole

        # MCP tool setup (lazy). Captures initialize + tools/list as its own
        # window before we mark for the run.
        if self.mcp_url and self._mcp_tools is None:
            setup_mark = len(self._http_exchanges)
            await self._setup_mcp_tools()
            if len(self._http_exchanges) > setup_mark:
                self._exchange_mark = setup_mark
                setup_events = self._capture_events_since_mark()
                for ev in setup_events:
                    if ev["t"] == "mcp_tools_list":
                        ev["tool_count"] = len(self._mcp_tools or [])
                        ev["tool_names"] = [getattr(t, "name", "?") for t in (self._mcp_tools or [])]
                self._events.extend(setup_events)

        # Append the new user message to the running history.
        self._messages.append(ChatMessage(role=MessageRole.USER, content=user_msg))

        # Build tools kwarg if any MCP tools are bound. llamaindex's
        # `OpenAI.achat(tools=[...])` accepts its own tool-spec objects
        # (see convert_chat_messages / to_openai_tool). Our
        # `_make_llamaindex_mcp_tool` returns a `FunctionTool` with a
        # permissive schema — llamaindex converts it to the openai shape.
        tools_param = self._mcp_tools or None

        self._mark_exchange_start()
        final_text = ""
        try:
            # Simple tool-call loop: up to 3 iterations (matches autogen).
            max_iter = 3
            for _ in range(max_iter):
                if tools_param:
                    # achat_with_tools returns a ChatResponse with
                    # additional_kwargs possibly carrying tool_calls.
                    result = await self._llm.achat_with_tools(
                        tools=tools_param,
                        user_msg=None,
                        chat_history=list(self._messages),
                    )
                else:
                    result = await self._llm.achat(messages=list(self._messages))
                msg = getattr(result, "message", None)
                content = getattr(msg, "content", "") or ""
                # Detect tool_calls on the assistant message.
                tool_calls_sel: list[Any] = []
                if tools_param:
                    try:
                        tool_calls_sel = self._llm.get_tool_calls_from_response(
                            result, error_on_no_tool_call=False,
                        ) or []
                    except Exception:
                        tool_calls_sel = []
                if msg is not None:
                    # Persist assistant turn (even if it only contains
                    # tool_calls and no text) so subsequent LLM hops see it.
                    self._messages.append(msg)
                if not tool_calls_sel:
                    final_text = content
                    break
                # Execute tool calls and feed the results back as TOOL messages.
                from llama_index.core.tools.types import ToolOutput  # noqa: F401
                for ts in tool_calls_sel:
                    t_name = ts.tool_name
                    t_args = ts.tool_kwargs or {}
                    # Find the matching FunctionTool from our mcp_tools list.
                    tool_obj = None
                    for tt in (self._mcp_tools or []):
                        if getattr(tt, "metadata", None) and tt.metadata.name == t_name:
                            tool_obj = tt
                            break
                    if tool_obj is None:
                        tool_output = f"(tool {t_name} not found)"
                    else:
                        try:
                            res = await tool_obj.acall(**t_args)
                            tool_output = getattr(res, "content", None) or str(res)
                        except Exception as e:  # noqa: BLE001
                            tool_output = f"(tool {t_name} error: {e})"
                    self._messages.append(ChatMessage(
                        role=MessageRole.TOOL,
                        content=str(tool_output),
                        additional_kwargs={
                            "tool_call_id": getattr(ts, "tool_id", None) or t_name,
                            "name": t_name,
                        },
                    ))
            if not final_text:
                # Exhausted iterations without a text reply.
                final_text = "(no response)"
        except Exception as e:  # noqa: BLE001
            final_text = f"(llamaindex error: {e.__class__.__name__}: {e})"

        if not final_text:
            final_text = "(no response)"

        # Classify HTTP exchanges during the chat driver.
        turn_events = self._capture_events_since_mark()
        self._events.extend(turn_events)

        # Annotate mcp_tools_list events with tool metadata.
        for ev in turn_events:
            if ev["t"] == "mcp_tools_list":
                tools = []
                resp = ev.get("response") or {}
                body = resp.get("body")
                if isinstance(body, dict):
                    tools = ((body.get("result") or {}).get("tools")) or []
                elif isinstance(body, str) and body.startswith("event:"):
                    for line in body.splitlines():
                        if line.startswith("data:"):
                            try:
                                parsed = json.loads(line[len("data:"):].strip())
                                tools = ((parsed.get("result") or {}).get("tools")) or []
                                break
                            except Exception:
                                pass
                ev["tool_count"] = len(tools)
                ev["tool_names"] = [t.get("name") for t in tools if isinstance(t, dict)]

        return self._build_turn_response(turn_id, final_text, turn_events)

    async def _turn_responses_direct(self, turn_id: str, user_msg: str) -> dict:
        """Turn via direct openai.responses.create() — bypasses llamaindex entirely.

        Handles both stateless (api=responses, state=F) and stateful
        (api=responses+conv, or api=responses+state=T): in stateful mode,
        we chain `previous_response_id`. `force_state_ref()` can override
        the next turn's prev-id to point at an earlier response (verdict-e
        test in T11).
        """
        state_mode = bool(self.config.get("state")) or (self.config.get("api") == "responses+conv")

        # MCP tool setup is not wired on the responses path in this Plan B
        # pass — mirrors adapters/autogen. MCP+responses is a bonus that
        # can be added once T7 / T11 are in.
        if self.mcp_url and self._mcp_tools is None:
            setup_mark = len(self._http_exchanges)
            await self._setup_mcp_tools()
            if len(self._http_exchanges) > setup_mark:
                self._exchange_mark = setup_mark
                setup_events = self._capture_events_since_mark()
                for ev in setup_events:
                    if ev["t"] == "mcp_tools_list":
                        ev["tool_count"] = len(self._mcp_tools or [])
                        ev["tool_names"] = [getattr(t, "name", "?") for t in (self._mcp_tools or [])]
                self._events.extend(setup_events)

        # Figure out previous_response_id for this turn.
        if state_mode:
            effective_prev = self._forced_prev_id or self._last_response_id
        else:
            effective_prev = None
        self._forced_prev_id = None  # consumed

        tools_param = None  # For MCP+responses, would build FunctionToolParam list here.

        self._mark_exchange_start()
        resp_obj = None
        final_text = ""
        try:
            kwargs: dict[str, Any] = {
                "model": self._model_name,
                "input": user_msg,
            }
            if effective_prev is not None:
                kwargs["previous_response_id"] = effective_prev
            if tools_param:
                kwargs["tools"] = tools_param
            resp_obj = await self._openai_client.responses.create(**kwargs)
        except Exception as e:  # noqa: BLE001
            final_text = f"(llamaindex/responses error: {e.__class__.__name__}: {e})"
        else:
            # resp_obj may carry convenient .output_text; otherwise extract.
            final_text = getattr(resp_obj, "output_text", "") or _extract_text_from_responses_obj(resp_obj)
            rid = getattr(resp_obj, "id", None)
            if rid:
                self._response_history.append(rid)
                if state_mode:
                    self._last_response_id = rid

        if not final_text:
            final_text = "(no response)"

        turn_events = self._capture_events_since_mark()
        self._events.extend(turn_events)

        return self._build_turn_response(turn_id, final_text, turn_events)

    # ── State-ref override (T11 verdict-e test path) ──

    def force_state_ref(self, ref_to_turn: int) -> dict:
        """Override the next responses+conv turn's previous_response_id.

        ref_to_turn is an index into self._response_history (0-based,
        chronological). If in range, the next turn() will pass that
        response's id instead of _last_response_id.
        """
        if 0 <= ref_to_turn < len(self._response_history):
            self._forced_prev_id = self._response_history[ref_to_turn]
            return {
                "ok": True,
                "forced_prev_id": self._forced_prev_id,
                "ref_to_turn": ref_to_turn,
            }
        return {
            "ok": False,
            "reason": f"ref_to_turn={ref_to_turn} out of range (history len={len(self._response_history)})",
        }

    # ── Response formatter (shared across modes) ──

    def _build_turn_response(self, turn_id: str, final_text: str,
                             turn_events: list[dict]) -> dict:
        # Pick the first LLM request of the turn for the legacy slot.
        request_captured = None
        for ev in turn_events:
            if ev["t"].startswith("llm_hop_") and ev.get("request"):
                request_captured = ev["request"]
                break
        if request_captured is None:
            request_captured = self._last_request or {
                "method": "POST",
                "url": "",
                "headers": dict(self._headers),
                "body": {"note": "event hook didn't fire"},
            }

        response_captured = self._last_response or {
            "status": 0,
            "headers": {},
            "body": {"note": "event hook didn't fire"},
        }

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
            # T11 — expose the Responses-API response id at the envelope top
            # level so the runner can pick a target_response_id for a
            # subsequent force_state_ref turn. None for non-responses modes.
            "_response_id": (
                self._response_history[-1]
                if self._response_history else None
            ),
        }

    async def compact(self, strategy: str) -> dict:
        """Plan B T10 — mutate `self._messages` per the requested strategy.

        For api=chat: `_messages` is a list of `ChatMessage` objects
        (llama_index.core.base.llms.types). MessageRole enum carries SYSTEM /
        USER / ASSISTANT / TOOL. Strategies:
          * drop_half — keep SYSTEM, drop oldest 50% of rest
          * drop_tool_calls — drop role=TOOL messages
          * summarize — drop_half + prepend a SYSTEM summary marker

        For api=responses / responses+conv: `_messages` is unused; compact
        trims the `_response_history` chain instead (same approach as the
        autogen responses_direct path).
        """
        if self._mode == "chat":
            return self._compact_chat(strategy)
        if self._mode == "responses_direct":
            return self._compact_responses(strategy)
        raise RuntimeError(f"unknown mode: {self._mode}")

    def _compact_chat(self, strategy: str) -> dict:
        """Compact the ChatMessage list used by the llama_index chat path."""
        from llama_index.core.base.llms.types import ChatMessage, MessageRole

        before = len(self._messages)
        if strategy == "drop_half":
            sys_msgs = [m for m in self._messages if m.role == MessageRole.SYSTEM]
            rest = [m for m in self._messages if m.role != MessageRole.SYSTEM]
            keep = rest[len(rest) // 2:]
            self._messages = sys_msgs + keep
        elif strategy == "drop_tool_calls":
            self._messages = [
                m for m in self._messages if m.role != MessageRole.TOOL
            ]
        elif strategy == "summarize":
            sys_msgs = [m for m in self._messages if m.role == MessageRole.SYSTEM]
            rest = [m for m in self._messages if m.role != MessageRole.SYSTEM]
            keep = rest[len(rest) // 2:]
            dropped = len(rest) - len(keep)
            summary = ChatMessage(
                role=MessageRole.SYSTEM,
                content=f"[summarized {dropped} earlier messages]",
            )
            self._messages = sys_msgs + [summary] + keep
        else:
            raise ValueError(f"unknown strategy: {strategy}")
        return {
            "strategy": strategy,
            "history_len_before": before,
            "history_len_after": len(self._messages),
        }

    def _compact_responses(self, strategy: str) -> dict:
        """Compact `_response_history` (responses_direct mode)."""
        before = len(self._response_history)
        if strategy in ("drop_half", "drop_tool_calls", "summarize"):
            half = before // 2
            self._response_history = self._response_history[half:]
            if self._last_response_id not in self._response_history:
                self._last_response_id = (
                    self._response_history[-1] if self._response_history else None
                )
        else:
            raise ValueError(f"unknown strategy: {strategy}")
        return {
            "strategy": strategy,
            "history_len_before": before,
            "history_len_after": len(self._response_history),
            "note": (
                "responses_direct mode has no per-message history; "
                "compacted _response_history chain instead"
            ),
        }

    async def aclose(self) -> None:
        # Close the shared httpx client.
        try:
            await self._http_client.aclose()
        except Exception:
            pass
        # Best-effort: let llamaindex release its own wrapper if present.
        llm = getattr(self, "_llm", None)
        if llm is not None:
            close = getattr(llm, "aclose", None)
            if close is not None:
                try:
                    r = close()
                    if asyncio.iscoroutine(r):
                        await r
                except Exception:
                    pass


# ── Helpers ─────────────────────────────────────────────────────────────

def _extract_text_from_responses_obj(resp: Any) -> str:
    """Extract final text from an openai Responses API result object.

    Fallback if resp.output_text isn't populated. resp.output is a list of
    items; text items have type='output_text' (or 'message' with content list).
    """
    if resp is None:
        return ""
    output = getattr(resp, "output", None) or []
    chunks: list[str] = []
    for item in output:
        it = item if isinstance(item, dict) else getattr(item, "model_dump", lambda: {})()
        if not isinstance(it, dict):
            continue
        t = it.get("type")
        if t == "output_text":
            v = it.get("text")
            if v:
                chunks.append(str(v))
        elif t == "message":
            for c in it.get("content", []) or []:
                if isinstance(c, dict) and c.get("type") in ("output_text", "text"):
                    v = c.get("text")
                    if v:
                        chunks.append(str(v))
    return "\n".join(chunks)


def _extract_tool_calls_from_response(response_snap: dict) -> list[dict]:
    """Parse tool_calls off a captured LLM response body.

    Handles OpenAI chat.completions + OpenAI Responses API (messages/Anthropic
    not supported by this adapter since api=messages is excluded, but we
    keep the block for parity with sibling adapters).
    """
    body = response_snap.get("body")
    if not isinstance(body, dict):
        return []
    out: list[dict] = []
    # OpenAI chat.completions
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
    # OpenAI Responses API
    for item in body.get("output", []) or []:
        if isinstance(item, dict) and item.get("type") == "function_call":
            args_raw = item.get("arguments") or "{}"
            try:
                args = json.loads(args_raw) if isinstance(args_raw, str) else (args_raw or {})
            except Exception:
                args = {"_raw": args_raw}
            out.append({
                "name": item.get("name"),
                "args": args,
                "id": item.get("call_id") or item.get("id"),
            })
    return out


# ── MCP tool adapter: wraps a fastmcp tool as a llamaindex FunctionTool ──

def _make_llamaindex_mcp_tool(mcp_tool: Any, mcp_url: str, headers: dict,
                              trial: "Trial") -> Any:
    """Return a `llama_index.core.tools.FunctionTool` that calls MCP via fastmcp.

    llamaindex's FunctionTool accepts an async callable + metadata.
    We provide a permissive schema and let the LLM drive kwargs.
    """
    from llama_index.core.tools import FunctionTool

    name = mcp_tool.name
    description = (mcp_tool.description or f"MCP tool: {name}")[:1024]

    async def _call(**kwargs) -> str:
        from fastmcp import Client
        from fastmcp.client.transports import StreamableHttpTransport
        transport = StreamableHttpTransport(
            url=mcp_url,
            headers=dict(headers),
            httpx_client_factory=trial._httpx_factory,
        )
        async with Client(transport=transport) as c:
            res = await c.call_tool(name, kwargs)
        if res is None:
            return ""
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

    _call.__name__ = name
    _call.__doc__ = description

    return FunctionTool.from_defaults(
        async_fn=_call,
        name=name,
        description=description,
    )
