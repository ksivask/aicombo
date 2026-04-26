"""autogen-agentchat-specific adapter logic.

autogen-agentchat (microsoft/autogen 0.7+) is a typed agent framework built
around `AssistantAgent(name, model_client, tools=[...])`. The `model_client`
is a `ChatCompletionClient` subclass; autogen-ext ships:

  - `OpenAIChatCompletionClient`     — chat completions (openai-compat)
  - `AnthropicChatCompletionClient`  — anthropic messages

Both accept `http_client=httpx.AsyncClient` (they pass it straight to the
underlying `openai.AsyncOpenAI` / `anthropic.AsyncAnthropic`), so a single
hooked httpx.AsyncClient captures all LLM wire bytes — no monkey-patching.

autogen-ext does NOT currently ship an OpenAI Responses client, so for
api=responses / api=responses+conv we **bypass the AssistantAgent** and call
`openai.AsyncOpenAI(http_client=our_httpx).responses.create(...)` directly.
This is both simpler (no fighting autogen's abstractions for an API it
doesn't model) and gives us full control over `previous_response_id` for
the state-mode turn chain that unlocks verdict (e) in T11.

MCP integration:
  autogen-ext's `mcp_server_tools(StreamableHttpServerParams)` uses the
  `mcp` SDK's `streamablehttp_client` under the hood — which does NOT
  accept a custom httpx client, so we can't capture MCP wire bytes
  through it. Same as adapters/crewai, we wrap `fastmcp.Client` manually
  via a BaseTool / FunctionTool whose httpx_client_factory funnels into
  our hooked client.

Supported (api, llm) combos:
  - api=chat,           llm in {ollama, chatgpt, gemini, mock}  (OpenAIChatCompletionClient)
  - api=messages,       llm=claude                              (AnthropicChatCompletionClient)
  - api=responses,      llm=chatgpt                             (openai SDK bypass)
  - api=responses+conv, llm=chatgpt                             (openai SDK bypass + previous_response_id chain)

Capture model:
  Mirrors pydantic-ai / crewai / langgraph adapters: mark the HTTP exchange
  list at turn start, run the agent (or direct responses.create), then
  classify every exchange captured during the run as `llm_hop_N` or `mcp_*`
  by URL + JSON-RPC method.
"""
from __future__ import annotations

import asyncio
import copy
import json
import os
from typing import Any

import httpx


# ── LLM base URL resolution (mirrors pydantic-ai / crewai) ──

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
            return os.environ.get("DEFAULT_OPENAI_MODEL", "gpt-4o-mini")
        if llm == "gemini":
            return os.environ.get("DEFAULT_GEMINI_MODEL", "gemini-2.0-flash")
        if llm == "mock":
            return os.environ.get("DEFAULT_OPENAI_MODEL", "gpt-4o-mini")
    if api == "messages":
        if llm == "claude":
            return os.environ.get("DEFAULT_CLAUDE_MODEL", "claude-haiku-4-5")
    if api in ("responses", "responses+conv"):
        if llm == "chatgpt":
            return os.environ.get("DEFAULT_OPENAI_MODEL", "gpt-4o-mini")
    raise ValueError(f"unsupported (api, llm) for autogen: ({api}, {llm})")


# ── Model-info used for non-OpenAI models (ollama/mock/gemini via OpenAI-compat).
#   autogen-ext wants a ModelInfo dict when it can't lookup the model name
#   against its built-in list of OpenAI models. We pass a permissive one.
_DEFAULT_MODEL_INFO: dict = {
    "vision": False,
    "function_calling": True,
    "json_output": False,
    "family": "unknown",
    "structured_output": False,
}


def _build_model_client(api: str, llm: str, model_name: str, base_url: str,
                        api_key: str, http_client: httpx.AsyncClient) -> Any:
    """Construct the autogen-ext ChatCompletionClient for (api, llm).

    For chat → OpenAIChatCompletionClient (with http_client=)
    For messages → AnthropicChatCompletionClient (with http_client=)

    Caller should NOT invoke this for api=responses / api=responses+conv —
    those paths bypass the autogen client entirely.
    """
    if api == "chat":
        from autogen_ext.models.openai import OpenAIChatCompletionClient
        kwargs: dict[str, Any] = {
            "model": model_name,
            "base_url": base_url,
            "api_key": api_key,
            "http_client": http_client,
        }
        # autogen's built-in catalog only covers recognized OpenAI model
        # names. For ollama/mock/gemini-in-compat-mode we must supply
        # model_info so it doesn't raise "model not found in registry".
        if llm in ("ollama", "mock", "gemini"):
            kwargs["model_info"] = dict(_DEFAULT_MODEL_INFO)
        return OpenAIChatCompletionClient(**kwargs)
    if api == "messages":
        if llm != "claude":
            raise ValueError(f"api=messages is claude-only; got llm={llm}")
        from autogen_ext.models.anthropic import AnthropicChatCompletionClient
        return AnthropicChatCompletionClient(
            model=model_name,
            base_url=base_url,
            api_key=api_key,
            http_client=http_client,
        )
    raise ValueError(
        f"_build_model_client: api={api} uses direct openai SDK bypass, "
        f"not an autogen ChatCompletionClient"
    )


# ── Trial ───────────────────────────────────────────────────────────────

class Trial:
    """Per-trial state for the autogen adapter.

    State modes:
      - stateless (api in chat/messages/responses without state): no chaining
      - responses_previous_id (api=responses+conv, or api=responses+state=T):
          `_last_response_id` is chained across turns as `previous_response_id`.
          `_response_history` lets `force_state_ref(turn_idx)` override the
          next turn's previous_response_id → referential test path for verdict
          (e) in T11.
    """

    def __init__(self, trial_id: str, config: dict):
        self.trial_id = trial_id
        self.config = config

        # ── Conversation state ──
        # For chat/messages: we accumulate autogen BaseChatMessage objects and
        # pass them as `task=` each turn so AssistantAgent "remembers" history
        # without relying on its internal _chat_messages recipient tracking.
        self._agentchat_messages: list[Any] = []
        # For api=responses + state=T (E13a): chained response id(s).
        self._last_response_id: str | None = None
        self._response_history: list[str] = []
        self._forced_prev_id: str | None = None

        # E13b: Conversations API container ID for api=responses+conv.
        # Lazy-minted on first +conv turn via _ensure_conversation_id()
        # → POST /v1/conversations through the hooked httpx client.
        self._conversation_id: str | None = None

        # Per-turn capture
        self._last_request: dict | None = None
        self._last_response: dict | None = None
        self._events: list[dict] = []

        # Full HTTP exchange log; per-turn window marked by _exchange_mark.
        self._http_exchanges: list[dict] = []
        self._exchange_mark: int = 0

        # Mutable shared headers dict. The httpx.AsyncClient is built once
        # with headers=this_dict; mutating it per turn is picked up by
        # httpx on each outgoing request (same trick as pydantic-ai adapter).
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

        # Single hooked AsyncClient — used by the autogen model client, the
        # openai responses bypass client, AND (via _httpx_factory) fastmcp.
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
        self._mode: str  # 'agent' | 'responses_direct'
        if api in ("chat", "messages"):
            self._mode = "agent"
            self._model_client = _build_model_client(
                api=api, llm=config["llm"], model_name=self._model_name,
                base_url=self._base_url, api_key=self._api_key,
                http_client=self._http_client,
            )
            # Agent is built lazily on first turn (needs mcp tools resolved).
            self._agent: Any = None
        elif api in ("responses", "responses+conv"):
            # Direct openai SDK bypass — simpler than fighting autogen
            # for an API it doesn't natively expose.
            self._mode = "responses_direct"
            from openai import AsyncOpenAI
            self._openai_client = AsyncOpenAI(
                api_key=self._api_key,
                base_url=self._base_url,
                http_client=self._http_client,
            )
        else:
            raise ValueError(f"unsupported api for autogen: {api}")

        # MCP wiring (lazy: first turn triggers list + tool wrap).
        self.mcp_url = (
            pick_mcp_base_url(config["routing"], config["mcp"])
            if config.get("mcp") and config["mcp"] != "NONE"
            else ""
        )
        self._mcp_tools: list[Any] | None = None

    async def _ensure_conversation_id(self) -> str:
        """E13b: mint or return the cached OpenAI conversation_id for this trial.

        POSTs /v1/conversations through the hooked httpx client (so setup
        wire bytes get captured for cidgar pedagogy). Requires AGW's
        llm-chatgpt route to map /v1/conversations to passthrough — see
        agw/config.yaml.
        """
        if self._conversation_id is not None:
            return self._conversation_id
        r = await self._http_client.post(
            f"{self._base_url.rstrip('/')}/conversations",
            json={},
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
                "X-Harness-Trial-ID": self.trial_id,
                "X-Harness-Turn-ID": "conv-setup",
            },
        )
        r.raise_for_status()
        self._conversation_id = r.json()["id"]
        return self._conversation_id

    # ── httpx factory for fastmcp ──

    def _httpx_factory(self, **kwargs) -> httpx.AsyncClient:
        existing_hooks = kwargs.pop("event_hooks", {}) or {}
        req_hooks = list(existing_hooks.get("request", [])) + [self._async_log_req]
        resp_hooks = list(existing_hooks.get("response", [])) + [self._async_log_resp]
        kwargs["event_hooks"] = {"request": req_hooks, "response": resp_hooks}
        return httpx.AsyncClient(**kwargs)

    # ── exchange classification (shared with pydantic-ai/crewai adapters) ──

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

    # ── MCP tool bootstrap (fastmcp path, shared with crewai) ──

    async def _setup_mcp_tools(self) -> None:
        """Fetch MCP tool list via fastmcp + wrap each as an autogen FunctionTool."""
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
            _make_autogen_mcp_tool(t, self.mcp_url, self._headers, self)
            for t in raw_tools
        ]

    # ── Agent construction (lazy, first-turn, after MCP tools resolved) ──

    def _ensure_agent(self) -> None:
        if self._agent is not None:
            return
        from autogen_agentchat.agents import AssistantAgent
        # max_tool_iterations > 1 so the AssistantAgent can do a tool-call +
        # follow-up LLM hop within a single .run() — otherwise it returns
        # after the first tool-call message without a final text reply.
        self._agent = AssistantAgent(
            name="aiplay_assistant",
            model_client=self._model_client,
            tools=list(self._mcp_tools or []) or None,
            system_message=(
                "You are a concise, helpful assistant. "
                "Use tools when relevant; otherwise answer directly."
            ),
            max_tool_iterations=3,
        )

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

        if self._mode == "agent":
            return await self._turn_agent(turn_id, user_msg)
        if self._mode == "responses_direct":
            return await self._turn_responses_direct(turn_id, user_msg)
        raise RuntimeError(f"unknown mode: {self._mode}")

    async def _turn_agent(self, turn_id: str, user_msg: str) -> dict:
        """Turn via AssistantAgent.run() — used for api=chat / api=messages."""
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

        self._ensure_agent()

        # Build task sequence: history + new user message. AssistantAgent's
        # .run(task=[...]) appends these to its internal model_context, so
        # each turn only needs the NEW user message (the agent holds the
        # model_context from prior runs).
        from autogen_agentchat.messages import TextMessage

        self._mark_exchange_start()
        try:
            result = await self._agent.run(
                task=TextMessage(content=user_msg, source="user"),
            )
        except Exception as e:  # noqa: BLE001
            result = None
            final_text = f"(autogen error: {e.__class__.__name__}: {e})"
        else:
            final_text = _extract_final_text(result)

        if not final_text:
            final_text = "(no response)"

        # Classify HTTP exchanges during .run()
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
        """Turn via direct openai.responses.create() — bypasses autogen entirely.

        Handles three sub-modes:
          * stateless (api=responses, state=F): no chain, no container.
          * state_chain (api=responses, state=T): chain `previous_response_id`
            from the prior turn's resp_xxx (E13a). force_state_ref overrides.
          * conv_container (api=responses+conv): bind `conversation:{id}` to
            the lazy-minted conv_xxx for this trial (E13b). The container
            handles continuity server-side; previous_response_id is NOT
            threaded here.
        """
        api = self.config.get("api")
        state_chain = api == "responses" and bool(self.config.get("state"))
        conv_container = api == "responses+conv"

        # MCP tools (if any) become OpenAI Responses-API tools. Since we're
        # bypassing the AssistantAgent, we call them ourselves via fastmcp
        # when the model emits a function_call output. For Plan B T5 smoke
        # we focus on the MCP=NONE path; MCP+responses is a bonus.
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

        # E13a: figure out previous_response_id (chain mode only).
        if state_chain:
            effective_prev = self._forced_prev_id or self._last_response_id
        else:
            effective_prev = None
        self._forced_prev_id = None  # consumed

        # E13b: mint (or fetch cached) the conv_xxx container before the
        # /v1/responses POST so the setup call shows up first in the
        # exchange log on turn 1.
        conv_id_for_turn: str | None = None
        if conv_container:
            conv_id_for_turn = await self._ensure_conversation_id()

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
            if conv_id_for_turn is not None:
                # SDK accepts either "conv_xxx" or {"id": "conv_xxx"}.
                # We pass the dict form for explicitness.
                kwargs["conversation"] = {"id": conv_id_for_turn}
            if tools_param:
                kwargs["tools"] = tools_param
            resp_obj = await self._openai_client.responses.create(**kwargs)
        except Exception as e:  # noqa: BLE001
            final_text = f"(autogen/responses error: {e.__class__.__name__}: {e})"
        else:
            # resp_obj may carry convenient .output_text; otherwise extract.
            final_text = getattr(resp_obj, "output_text", "") or _extract_text_from_responses_obj(resp_obj)
            rid = getattr(resp_obj, "id", None)
            # E13a: only track per-response chain in state_chain mode.
            # E13b: +conv tracks via the conversation container instead;
            # appending here would mislead downstream consumers.
            if rid and state_chain:
                self._response_history.append(rid)
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

        envelope = {
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
        # E13b: also expose the Conversations API container id when in
        # +conv mode so the inspector can correlate turns by container.
        if self.config.get("api") == "responses+conv":
            envelope["_conversation_id"] = self._conversation_id
        return envelope

    async def compact(self, strategy: str) -> dict:
        """Plan B T10 — mutate the framework's internal conversation history.

        autogen has two separate state models depending on mode:

          * `agent` mode (api=chat / api=messages) — AssistantAgent keeps a
            `_model_context` with a private `_messages` list. There's no
            public trim API in autogen 0.7; we poke the private attribute,
            which is the same path autogen's own example code uses in the
            absence of a first-class "compact" method. If the agent isn't
            built yet (happens when compact fires before any turn), the
            op is a no-op (nothing to drop).
          * `responses_direct` mode — there's no per-message history; only
            a chain of response ids. compact = clear the older half of
            `_response_history` so subsequent turns start a fresh chain,
            which approximates "forget earlier context".

        Strategies are honored where the shape permits:
          * drop_half — keep oldest 50% of system + newest 50% of rest
          * drop_tool_calls — drop FunctionExecutionResultMessage /
            messages whose `content` is a list of FunctionCall objects
          * summarize — drop_half + inject a SystemMessage summary
        """
        if self._mode == "agent":
            return await self._compact_agent(strategy)
        if self._mode == "responses_direct":
            return self._compact_responses(strategy)
        raise RuntimeError(f"unknown mode: {self._mode}")

    async def _compact_agent(self, strategy: str) -> dict:
        """Compact the AssistantAgent's model_context message list."""
        agent = self._agent
        if agent is None:
            # Agent hasn't been lazily built yet — there's no history to trim.
            return {
                "strategy": strategy,
                "history_len_before": 0,
                "history_len_after": 0,
                "note": "agent not yet constructed; nothing to compact",
            }
        ctx = getattr(agent, "_model_context", None) or getattr(
            agent, "model_context", None,
        )
        if ctx is None:
            return {
                "strategy": strategy,
                "history_len_before": 0,
                "history_len_after": 0,
                "note": "no model_context on agent; nothing to compact",
            }

        # autogen 0.7 exposes `_messages` on BufferedChatCompletionContext /
        # UnboundedChatCompletionContext. Some builds provide `get_messages()`
        # async; we only need the concrete list to rewrite it.
        msgs = getattr(ctx, "_messages", None)
        if msgs is None:
            # Best-effort via public get_messages() coroutine, but we still
            # can't write back; treat as "nothing compacted".
            return {
                "strategy": strategy,
                "history_len_before": 0,
                "history_len_after": 0,
                "note": "autogen model_context has no writable _messages; no-op",
            }

        before = len(msgs)
        note: str | None = None

        if strategy == "drop_half":
            sys_msgs, rest = _split_autogen_system(msgs)
            keep = rest[len(rest) // 2:]
            ctx._messages = sys_msgs + keep
        elif strategy == "drop_tool_calls":
            ctx._messages = [m for m in msgs if not _is_autogen_tool_msg(m)]
        elif strategy == "summarize":
            sys_msgs, rest = _split_autogen_system(msgs)
            keep = rest[len(rest) // 2:]
            dropped = len(rest) - len(keep)
            summary = _make_autogen_system_msg(
                f"[summarized {dropped} earlier messages]"
            )
            if summary is None:
                # Couldn't synthesize a SystemMessage — fall back to drop_half.
                ctx._messages = sys_msgs + keep
                note = "autogen SystemMessage ctor unavailable; fell back to drop_half"
            else:
                ctx._messages = sys_msgs + [summary] + keep
        else:
            raise ValueError(f"unknown strategy: {strategy}")

        out: dict = {
            "strategy": strategy,
            "history_len_before": before,
            "history_len_after": len(ctx._messages),
        }
        if note:
            out["note"] = note
        return out

    def _compact_responses(self, strategy: str) -> dict:
        """Compact the responses_direct response-id history.

        No per-message list exists on this path; the only continuity
        artifact is `_last_response_id` + `_response_history`. "drop_half"
        drops older half; "drop_tool_calls" / "summarize" also drop half
        since there are no tool-call messages or text to summarize at this
        layer.
        """
        before = len(self._response_history)
        if strategy in ("drop_half", "drop_tool_calls", "summarize"):
            half = before // 2
            self._response_history = self._response_history[half:]
            # Keep _last_response_id consistent: if we dropped it, reset.
            if self._last_response_id not in self._response_history:
                self._last_response_id = (
                    self._response_history[-1] if self._response_history else None
                )
        else:
            raise ValueError(f"unknown strategy: {strategy}")
        note = (
            "responses_direct mode has no per-message history; "
            "compacted _response_history chain instead"
        )
        return {
            "strategy": strategy,
            "history_len_before": before,
            "history_len_after": len(self._response_history),
            "note": note,
        }

    async def aclose(self) -> None:
        # Close autogen model client first (releases its own resources).
        client = getattr(self, "_model_client", None)
        if client is not None:
            try:
                close = getattr(client, "close", None)
                if close is not None:
                    r = close()
                    if asyncio.iscoroutine(r):
                        await r
            except Exception:
                pass
        # Then the shared httpx client.
        try:
            await self._http_client.aclose()
        except Exception:
            pass


# ── Helpers ─────────────────────────────────────────────────────────────

def _extract_final_text(task_result: Any) -> str:
    """Pull the assistant's final text reply from an autogen TaskResult."""
    messages = getattr(task_result, "messages", None) or []
    # Walk backwards; the first message authored by the assistant with
    # text content is our final reply. TextMessage has .content as str;
    # older builds may yield ToolCallSummaryMessage — skip those unless
    # they're the only thing available.
    best_text = ""
    for msg in reversed(messages):
        # Non-user messages only (skip the input echo).
        src = getattr(msg, "source", "")
        if src in ("user", "system"):
            continue
        content = getattr(msg, "content", None)
        if isinstance(content, str) and content.strip():
            return content
        # List-of-parts (some message types): pick any string parts.
        if isinstance(content, list):
            parts = [p for p in content if isinstance(p, str)]
            if parts and not best_text:
                best_text = "\n".join(parts)
    return best_text


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


def _split_autogen_system(msgs: list[Any]) -> tuple[list[Any], list[Any]]:
    """Split an autogen model_context message list by system-role.

    autogen-core's LLMMessage class hierarchy uses SystemMessage subclass;
    we detect by class name for version resilience (the class moves between
    autogen_core and autogen_agentchat across 0.4 → 0.7). Everything that
    isn't a SystemMessage ends up in the "rest" list.
    """
    sys_msgs: list[Any] = []
    rest: list[Any] = []
    for m in msgs:
        if m.__class__.__name__ == "SystemMessage":
            sys_msgs.append(m)
        else:
            rest.append(m)
    return sys_msgs, rest


def _is_autogen_tool_msg(msg: Any) -> bool:
    """True if `msg` is a tool-use artifact in an autogen message context.

    FunctionExecutionResultMessage holds tool_result payloads. AssistantMessage
    with a content list full of FunctionCall objects is the tool-call emission.
    Both should go when the user asks for `drop_tool_calls`.
    """
    cls = msg.__class__.__name__
    if cls == "FunctionExecutionResultMessage":
        return True
    if cls == "AssistantMessage":
        content = getattr(msg, "content", None)
        if isinstance(content, list) and content and all(
            c.__class__.__name__ == "FunctionCall" for c in content
        ):
            return True
    return False


def _make_autogen_system_msg(text: str) -> Any:
    """Construct an autogen SystemMessage with `text` content, or None.

    Returns None if the expected SystemMessage class can't be imported;
    callers fall back to drop_half behavior in that case.
    """
    try:
        from autogen_core.models import SystemMessage  # autogen 0.7+
        return SystemMessage(content=text)
    except Exception:
        try:
            from autogen_agentchat.messages import SystemMessage  # fallback
            return SystemMessage(content=text)
        except Exception:
            return None


def _extract_tool_calls_from_response(response_snap: dict) -> list[dict]:
    """Parse tool_calls off a captured LLM response body.

    Handles OpenAI chat.completions, OpenAI Responses API, Anthropic messages.
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
    # Anthropic messages
    for block in body.get("content", []) or []:
        if isinstance(block, dict) and block.get("type") == "tool_use":
            out.append({
                "name": block.get("name"),
                "args": block.get("input") or {},
                "id": block.get("id"),
            })
    return out


# ── MCP tool adapter: wraps a fastmcp tool as an autogen FunctionTool ──

def _make_autogen_mcp_tool(mcp_tool: Any, mcp_url: str, headers: dict,
                           trial: "Trial") -> Any:
    """Return an autogen_core.tools.FunctionTool that calls MCP via fastmcp.

    autogen's AssistantAgent accepts either a `BaseTool` (autogen_core.tools)
    or a plain async callable. We give it an async callable wrapped as a
    FunctionTool with a permissive schema — the LLM sees name/description
    and calls it with kwargs. We forward those kwargs through fastmcp.
    """
    from autogen_core.tools import FunctionTool
    name = mcp_tool.name
    description = (mcp_tool.description or f"MCP tool: {name}")[:1024]

    # Build a closure that does the MCP call via fastmcp using our hooked
    # httpx factory (captures tools/call wire bytes).
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

    # Ensure the callable presents the tool name (autogen FunctionTool uses
    # fn.__name__ unless `name=` override is accepted).
    _call.__name__ = name
    _call.__doc__ = description

    try:
        return FunctionTool(_call, description=description, name=name)
    except TypeError:
        # Older FunctionTool versions may not accept name=; fall back.
        return FunctionTool(_call, description=description)
