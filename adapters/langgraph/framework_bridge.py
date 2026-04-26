"""Langgraph-specific adapter logic.

Mirrors `adapters/langchain/framework_bridge.py` almost verbatim, but
replaces langchain's iterative `bind_tools` + manual hop loop with
langgraph's `create_react_agent` prebuilt. A single `graph.ainvoke` call
drives the full ReAct-style loop (LLM hop → tool call → LLM hop → …)
inside the StateGraph; all internal LLM + MCP HTTP calls still flow
through our httpx client, so the event hooks continue to capture wire
bytes for cidgar pedagogy.

Two modes per trial:

* **chat-only** (`mcp == "NONE"`): `create_react_agent(llm, [])` builds a
  graph with no tools — the agent loop terminates after one LLM hop.
* **MCP agent loop** (`mcp != "NONE"`): on first turn (lazy), the adapter
  calls `tools/list`, converts MCP tools via `langchain-mcp-adapters`,
  and rebuilds the graph with those tools. Subsequent turns reuse the
  same graph — langgraph handles the hop/tool-dispatch loop internally.

Capture model:
  - `_last_request` / `_last_response` are overwritten on every HTTP
    call (kept for legacy chat-only path + back-compat with the runner).
  - `_events` is a per-turn list; each event snapshots one HTTP exchange
    captured by the httpx hooks (LLM chat-completions POSTs + MCP
    initialize/tools_list/tools_call/sse_open/session_close).
  - Because langgraph drives the whole loop inside one ainvoke, we
    demux the captured HTTP exchanges AFTERWARDS by URL + JSON-RPC
    method, labeling each as llm_hop_N / mcp_* accordingly.
"""
from __future__ import annotations

import copy
import json
import os
from typing import Any

import httpx
from langgraph.prebuilt import create_react_agent


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


# Per-LLM API-key env var. Cloud providers (chatgpt/gemini) need a real
# key — we surface this clearly at Trial construction so a misconfigured
# adapter fails loudly instead of silently sending "placeholder".
_API_KEY_ENV_BY_LLM = {
    "ollama":  None,      # local; any placeholder is fine
    "mock":    None,      # local mock-llm; no validation
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


def _default_model(llm: str) -> str:
    """Pick a sensible default model name when row config doesn't specify one.

    Env-var overrides let operators pin a specific model without code changes.
    """
    if llm == "ollama":
        return os.environ.get("DEFAULT_OLLAMA_MODEL", "qwen2.5:7b")
    if llm == "chatgpt":
        return os.environ.get("DEFAULT_OPENAI_MODEL", "gpt-4o-mini")
    if llm == "gemini":
        return os.environ.get("DEFAULT_GEMINI_MODEL", "gemini-2.0-flash")
    if llm == "claude":
        return os.environ.get("DEFAULT_CLAUDE_MODEL", "claude-haiku-4-5")
    if llm == "mock":
        return "mock"
    return "unknown"


class Trial:
    """Holds per-trial framework state for langgraph.

    Uses httpx event hooks to capture the ACTUAL HTTP request + response
    bytes going over the wire — essential for cidgar pedagogy (diff what
    the framework sent vs. what cidgar returned). Same hooks fire for
    LLM chat-completions POSTs AND MCP session traffic, because langgraph's
    prebuilt `create_react_agent` wraps the same langchain-core primitives
    we already instrument.
    """

    def __init__(self, trial_id: str, config: dict):
        self.trial_id = trial_id
        self.config = config

        # Conversation state as a list of langchain-core messages, fed
        # into graph.ainvoke on every turn. (Graph itself is stateless
        # w.r.t. prior turns — we own the history.)
        self._messages: list[Any] = []

        # Per-turn capture slots. Populated by httpx event hooks.
        # Note: overwritten on every HTTP call. Use `_events` for per-step.
        self._last_request: dict | None = None
        self._last_response: dict | None = None

        # Per-turn multi-step events (LLM hops + MCP tool calls).
        self._events: list[dict] = []

        # Full log of HTTP exchanges captured by the event hooks — used
        # to reconstruct per-step event snapshots after graph.ainvoke.
        # Unlike the langchain adapter (which marks each MCP op window
        # explicitly), langgraph invokes all LLM hops + tool calls inside
        # one ainvoke, so we mark ONCE at the top of turn() and classify
        # every captured exchange afterwards by URL / JSON-RPC method.
        self._http_exchanges: list[dict] = []
        self._exchange_mark: int = 0

        # httpx client with event hooks that capture real wire bytes.
        # Writes to BOTH _last_* (back-compat) AND the _http_exchanges list
        # (used to demux LLM vs MCP calls after graph.ainvoke returns).
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
                    break
            else:
                for ex in reversed(self._http_exchanges):
                    if ex["resp"] is None:
                        ex["resp"] = resp_snap
                        break

        self._log_req = log_req
        self._log_resp = log_resp

        self._http_client = httpx.AsyncClient(
            event_hooks={"request": [log_req], "response": [log_resp]},
            timeout=httpx.Timeout(120.0),
        )

        # Build the wrapped chat model for the configured api. langgraph's
        # create_react_agent is LLM-agnostic — it accepts any langchain-
        # wrapped chat model with .bind_tools(), so api switching just
        # swaps the self.llm slot; the graph shape is unchanged.
        api = config.get("api", "chat")
        self.llm = self._build_llm(api, config)

        # Responses+state=T state chain (E13a). _last_response_id is
        # threaded as previous_response_id; _forced_prev_id is the T11
        # force_state_ref override (one-shot, consumed on use).
        self._last_response_id: str | None = None
        self._response_history: list[str] = []
        self._forced_prev_id: str | None = None

        # E13b: Conversations API container ID for api=responses+conv.
        # Lazy-minted on first +conv turn via _ensure_conversation_id()
        # → POST /v1/conversations through the hooked httpx client (so
        # setup wire bytes get captured for cidgar pedagogy). Cached for
        # the trial lifetime.
        self._conversation_id: str | None = None

        # MCP wiring (lazy — fetched on first turn that needs it).
        self.mcp_url = (
            pick_mcp_base_url(config["routing"], config["mcp"])
            if config.get("mcp") and config["mcp"] != "NONE"
            else ""
        )
        self._mcp_tools: list | None = None  # populated by _setup_mcp_tools
        self._graph: Any | None = None       # compiled langgraph agent

    def _build_llm(self, api: str, config: dict) -> Any:
        """Construct the langchain chat model for the configured api.

        Since langgraph's create_react_agent(llm, tools) takes any langchain
        chat model, switching api is just a matter of selecting the right
        wrapper class:

          * api=chat            → ChatOpenAI (openai-compat; works for ollama,
                                  mock, chatgpt, gemini via AGW's provider
                                  routing)
          * api=responses       → ChatOpenAI(use_responses_api=True) — requires
                                  llm=chatgpt
          * api=responses+conv  → same as responses; previous_response_id is
                                  threaded per-turn via graph.ainvoke's
                                  config={"configurable": ...}
          * api=messages        → ChatAnthropic — requires llm=claude
        """
        base_url = pick_llm_base_url(routing=config["routing"], llm=config["llm"])
        api_key = _pick_api_key(config["llm"])
        model = config.get("model") or _default_model(config["llm"])

        if api == "chat":
            from langchain_openai import ChatOpenAI
            return ChatOpenAI(
                base_url=base_url,
                api_key=api_key,
                model=model,
                http_async_client=self._http_client,
                default_headers={},  # populated per-turn
                temperature=0.3,
            )

        if api in ("responses", "responses+conv"):
            if config["llm"] != "chatgpt":
                raise ValueError(
                    f"api={api} requires llm=chatgpt; got {config['llm']}"
                )
            from langchain_openai import ChatOpenAI
            return ChatOpenAI(
                base_url=base_url,
                api_key=api_key,
                model=model,
                http_async_client=self._http_client,
                default_headers={},  # populated per-turn
                temperature=0.3,
                use_responses_api=True,
            )

        if api == "messages":
            if config["llm"] != "claude":
                raise ValueError(
                    f"api=messages requires llm=claude; got {config['llm']}"
                )
            from langchain_anthropic import ChatAnthropic
            # langchain-anthropic 1.4.x uses `anthropic_api_url` + its
            # alias `base_url`; same for `anthropic_api_key` / `api_key`.
            # Temperature surfaced the same as ChatOpenAI. `default_headers`
            # passthrough is populated per-turn.
            inst = ChatAnthropic(
                anthropic_api_url=base_url,
                anthropic_api_key=api_key,
                model=model,
                default_headers={},
                temperature=0.3,
            )
            # ChatAnthropic 1.4.x does NOT expose http_client / http_async_client
            # as model fields — the SDK clients are created lazily via
            # `@cached_property` methods named `_client` / `_async_client`.
            # We override both with instances whose `http_client=` is our
            # hooked httpx client, before either property is read. This
            # is the same pattern crewai's adapter uses for the native
            # anthropic SDK client swap.
            self._install_anthropic_hooked_clients(
                inst, base_url=base_url, api_key=api_key,
            )
            return inst

        raise ValueError(f"unsupported api: {api}")

    # Copied from adapters/langchain/framework_bridge.py (E5a). Keep in sync.
    def _install_anthropic_hooked_clients(
        self,
        chat_anthropic: Any,
        base_url: str,
        api_key: str,
    ) -> None:
        """Swap `_client` + `_async_client` on a ChatAnthropic so both use
        our hooked httpx clients. cached_property reads `inst.__dict__`
        first, so setting the attributes directly wins over the descriptor.
        """
        import anthropic  # provided by langchain-anthropic's deps

        # Sync httpx.Client with the same hook closures as our async one.
        # ChatAnthropic.ainvoke() only hits the async path, but if any
        # codepath (e.g. tests) happens to call the sync .invoke(), the
        # sync client will still work — just without hooks.
        sync_http_client = httpx.Client(timeout=httpx.Timeout(120.0))

        chat_anthropic._client = anthropic.Client(
            api_key=api_key,
            base_url=base_url,
            http_client=sync_http_client,
        )
        chat_anthropic._async_client = anthropic.AsyncClient(
            api_key=api_key,
            base_url=base_url,
            http_client=self._http_client,
        )
        # Stash the sync client on the Trial so aclose() can release it.
        self._anthropic_sync_http = sync_http_client

    async def _ensure_conversation_id(self) -> str:
        """E13b: mint or return the cached OpenAI conversation_id for this trial.

        POSTs /v1/conversations through the hooked httpx client (so setup
        wire bytes get captured for cidgar pedagogy). Requires AGW's
        llm-chatgpt route to map /v1/conversations to passthrough — see
        agw/config.yaml.
        """
        # Single-trial-at-a-time runner invariant — no asyncio.Lock needed.
        # If E18 (concurrent trials) ever lands, this needs protection: two
        # concurrent +conv turns on the same Trial would race the if-None
        # check and BOTH issue POST /v1/conversations, leaking one container.
        if self._conversation_id is not None:
            return self._conversation_id
        base_url = pick_llm_base_url(
            routing=self.config["routing"], llm=self.config["llm"],
        )
        api_key = _pick_api_key(self.config["llm"])
        r = await self._http_client.post(
            f"{base_url.rstrip('/')}/conversations",
            json={},
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                "X-Harness-Trial-ID": self.trial_id,
                "X-Harness-Turn-ID": "conv-setup",
            },
        )
        r.raise_for_status()
        self._conversation_id = r.json()["id"]
        return self._conversation_id

    # ── httpx factory for langchain-mcp-adapters ──
    #
    # langchain-mcp-adapters' StreamableHttpConnection accepts an
    # `httpx_client_factory` callable. The MCP SDK calls it for every
    # session. This installs OUR event hooks on each created client, so
    # wire bytes for every MCP request flow into the same capture slots
    # as the LLM calls.
    def _httpx_factory(self, **kwargs):
        existing_hooks = kwargs.pop("event_hooks", {}) or {}
        req_hooks = list(existing_hooks.get("request", [])) + [self._log_req]
        resp_hooks = list(existing_hooks.get("response", [])) + [self._log_resp]
        kwargs["event_hooks"] = {"request": req_hooks, "response": resp_hooks}
        return httpx.AsyncClient(**kwargs)

    def _mcp_connection(self, headers: dict[str, str]) -> dict:
        """Build a StreamableHttpConnection dict for langchain-mcp-adapters."""
        return {
            "transport": "streamable_http",
            "url": self.mcp_url,
            "headers": headers,
            "httpx_client_factory": self._httpx_factory,
        }

    async def _setup_mcp_tools(self, headers: dict[str, str]) -> None:
        """Lazy-load MCP tools once per trial, then build the langgraph agent.

        After this completes, `self._graph` is a compiled StateGraph ready
        for `graph.ainvoke({"messages": [...]})`. If MCP is NONE or tools
        load fails, falls back to a graph with an empty tool list (the
        ReAct agent still works — it just never emits tool_calls).
        """
        if self._graph is not None:
            return

        tools: list = []
        if self.mcp_url:
            try:
                from langchain_mcp_adapters.tools import load_mcp_tools
            except ImportError as e:
                raise RuntimeError(
                    "langchain-mcp-adapters not installed; required for mcp != NONE"
                ) from e

            connection = self._mcp_connection(headers)
            # Pass session=None + connection=conn so each tool execution
            # opens its own session (our httpx factory wires hooks on each).
            tools = await load_mcp_tools(session=None, connection=connection)
            self._mcp_tools = tools

        # Build the ReAct agent. Empty tools list is supported and yields
        # a one-hop graph (LLM → END) which is exactly what chat-only
        # trials want.
        self._graph = create_react_agent(self.llm, tools)

    def _mark_exchange_start(self) -> None:
        """Remember where in _http_exchanges the next operation starts."""
        self._exchange_mark = len(self._http_exchanges)

    def _exchanges_since_mark(self) -> list[dict]:
        return list(self._http_exchanges[self._exchange_mark:])

    @staticmethod
    def _rpc_method(exchange: dict) -> str | None:
        """Extract JSON-RPC method from an HTTP exchange's request body."""
        req = exchange.get("req") or {}
        body = req.get("body")
        if isinstance(body, dict):
            return body.get("method")
        return None

    @staticmethod
    def _is_llm_exchange(exchange: dict) -> bool:
        """Classify an HTTP exchange as an LLM call (vs MCP call) by URL."""
        req = exchange.get("req") or {}
        url = str(req.get("url", ""))
        # OpenAI-compatible chat/completions endpoint — covers ollama,
        # mock-llm, chatgpt-openai, gemini-openai-compat. All AGW /llm/*
        # routes end in `/v1/chat/completions` (or `/v1beta/.../chat/completions`
        # for gemini).
        return "chat/completions" in url or "/llm/" in url

    def _capture_events_since_mark(self) -> list[dict]:
        """Turn every HTTP exchange since the mark into a labeled event.

        LLM calls get `llm_hop_N` (N = 0-indexed count of LLM hops in
        this turn). MCP calls get the usual `mcp_initialize` /
        `mcp_notif_initialized` / `mcp_sse_open` / `mcp_tools_list` /
        `mcp_tools_call` / `mcp_session_close` labels.
        """
        method_to_kind = {
            "initialize":               "mcp_initialize",
            "notifications/initialized":"mcp_notif_initialized",
            "tools/list":               "mcp_tools_list",
            "tools/call":               "mcp_tools_call",
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

    @staticmethod
    def _extract_tool_calls(msg: Any) -> list[dict]:
        """Normalize tool_calls off a langchain AIMessage (best-effort)."""
        tcs = getattr(msg, "tool_calls", None) or []
        out = []
        for tc in tcs:
            if isinstance(tc, dict):
                out.append({
                    "name": tc.get("name"),
                    "args": tc.get("args", {}) or {},
                    "id": tc.get("id"),
                })
            else:
                out.append({
                    "name": getattr(tc, "name", None),
                    "args": getattr(tc, "args", {}) or {},
                    "id": getattr(tc, "id", None),
                })
        return out

    async def turn(self, turn_id: str, user_msg: str) -> dict:
        """One turn — always drives the langgraph ReAct agent once.

        The graph handles the LLM↔tool hop loop internally. We capture
        every HTTP exchange (LLM + MCP) that happens during ainvoke and
        label each as either `llm_hop_N` or `mcp_*`.

        For api=responses+conv, the effective previous_response_id (either
        the chained _last_response_id or a T11 force_state_ref override)
        is threaded through the graph's `config={"configurable": {...}}`,
        which langchain's ChatOpenAI reads when use_responses_api=True.
        """
        from langchain_core.messages import HumanMessage

        api = self.config.get("api", "chat")

        headers = {
            "X-Harness-Trial-ID": self.trial_id,
            "X-Harness-Turn-ID":  turn_id,
        }
        # B5 fix: assigning self.llm.default_headers post-init does NOT
        # propagate to outbound requests. The openai SDK was built once at
        # ChatOpenAI construction with the ORIGINAL default_headers dict
        # ({}) and snapshotted that into its `_custom_headers`. The
        # ChatOpenAI pydantic field reassignment is silently dropped on
        # the wire path — same shape as the B4 bug pattern. The reliable
        # place to inject per-turn headers is `self._http_client.headers`
        # (the live httpx Headers store), which httpx merges into every
        # outgoing request via `_merge_headers` in `build_request`. The
        # openai SDK routes its requests through this exact client, so
        # the per-turn mutation reaches the wire on every LLM call.
        for k, v in headers.items():
            self._http_client.headers[k] = v
        # ChatAnthropic + ChatOpenAI both accept default_headers as a
        # pydantic field; the kept-for-cosmetics assignment lets any code
        # that reads back `self.llm.default_headers` see current values.
        self.llm.default_headers = headers
        # ChatAnthropic's _async_client was overridden in __init__ to a
        # hand-built anthropic.AsyncClient with our hooked httpx — the
        # anthropic SDK reads `_custom_headers` live on each request, so
        # mutating it reaches the wire reliably for api=messages.
        if api == "messages":
            async_client = getattr(self.llm, "_async_client", None)
            if async_client is not None and hasattr(async_client, "_custom_headers"):
                async_client._custom_headers = dict(headers)

        # Reset per-turn capture
        self._last_request = None
        self._last_response = None
        self._events = []

        # Build the graph on first turn. If MCP is configured, this also
        # loads tools via load_mcp_tools (which fires the httpx hooks for
        # the initialize / SSE / tools_list session — those exchanges are
        # captured inside _http_exchanges as part of graph construction).
        setup_mark = len(self._http_exchanges)
        await self._setup_mcp_tools(headers)
        # Emit events for any MCP session traffic that occurred during
        # setup (only fires once, on first turn with MCP).
        if len(self._http_exchanges) > setup_mark:
            self._exchange_mark = setup_mark
            setup_events = self._capture_events_since_mark()
            # Annotate the tools_list event with tool metadata (mirrors
            # langchain adapter's `_capture_mcp_op_events("tools_list", …)`).
            for ev in setup_events:
                if ev["t"] == "mcp_tools_list":
                    ev["tool_count"] = len(self._mcp_tools or [])
                    ev["tool_names"] = [t.name for t in (self._mcp_tools or [])]
            self._events.extend(setup_events)

        self._messages.append(HumanMessage(content=user_msg))

        # E13a/E13b: split previously-conflated modes.
        #   state_chain (api=responses + state=True)
        #     → previous_response_id chaining (server-side response link).
        #   conv_container (api=responses+conv)
        #     → Conversations API container reference; mints conv_xxx once
        #       per trial and binds it on every turn via the openai SDK
        #       `conversation` kwarg. NOT chained via previous_response_id
        #       (the conversation tracks history server-side).
        state_chain = api == "responses" and bool(self.config.get("state"))
        conv_container = api == "responses+conv"

        # For state-chain modes, thread the effective previous_response_id
        # to the outbound OpenAI Responses API request. _forced_prev_id
        # (set by force_state_ref) wins over _last_response_id when
        # present; it's consumed below.
        #
        # I1 fix: previously this went through
        # `config={"configurable": {"previous_response_id": X}}` on
        # graph.ainvoke — but ChatOpenAI doesn't declare
        # previous_response_id as a configurable field, so that value
        # was silently dropped. The fix rebuilds the graph for this
        # single turn with a bound LLM carrying the prev id as a default
        # kwarg (survives into the outbound payload), AND strips
        # response_metadata.id from the message history (shallow-copied)
        # so langchain-openai's auto-compute can't interfere.
        #
        # E13b: conv_container mirrors the same per-turn graph rebuild
        # pattern but binds `conversation={"id": conv_xxx}` instead.
        # Setup happens BEFORE the graph rebuild so the POST
        # /v1/conversations call shows up in this turn's HTTP exchange
        # log (first turn only — subsequent turns reuse the cached id).
        graph_to_use = self._graph
        messages_for_invoke = self._messages
        effective_prev: str | None = None
        if conv_container:
            conv_id = await self._ensure_conversation_id()
            bound_llm = self.llm.bind(conversation={"id": conv_id})
            graph_to_use = create_react_agent(
                bound_llm, self._mcp_tools or [],
            )
        if state_chain:
            effective_prev = self._forced_prev_id or self._last_response_id
            if effective_prev:
                bound_llm = self.llm.bind(previous_response_id=effective_prev)
                graph_to_use = create_react_agent(
                    bound_llm, self._mcp_tools or [],
                )
                messages_for_invoke = [copy.copy(m) for m in self._messages]
                for m in messages_for_invoke:
                    md = getattr(m, "response_metadata", None)
                    if isinstance(md, dict) and "id" in md:
                        m.response_metadata = {
                            k: v for k, v in md.items() if k != "id"
                        }

        # Mark the window for this turn's graph.ainvoke, then invoke.
        self._mark_exchange_start()
        first_request_before = copy.deepcopy(self._last_request)  # may be None
        result = await graph_to_use.ainvoke({"messages": messages_for_invoke})

        # Update conversation history from graph output.
        new_messages = result["messages"][len(self._messages):]
        self._messages = list(result["messages"])

        # Classify all HTTP exchanges that happened during graph.ainvoke.
        turn_events = self._capture_events_since_mark()
        # Annotate mcp_tools_call events with the tool name/args/result
        # we can recover from the corresponding ToolMessage in new_messages.
        # (Best-effort: langgraph emits ToolMessage objects in the message
        # list in the same order tools were called.)
        _annotate_tool_calls(turn_events, new_messages)
        self._events.extend(turn_events)

        # Find the last AIMessage for final content + tool_calls.
        final_ai = None
        for m in reversed(new_messages):
            # AIMessage has .tool_calls attr (may be empty list). Identify
            # by class name to avoid extra imports.
            if m.__class__.__name__ == "AIMessage":
                final_ai = m
                break
        if final_ai is None and new_messages:
            final_ai = new_messages[-1]

        final_content = ""
        last_tool_calls: list[dict] = []
        if final_ai is not None:
            c = getattr(final_ai, "content", "")
            final_content = c if isinstance(c, str) else str(c)
            last_tool_calls = self._extract_tool_calls(final_ai)
        if not final_content:
            final_content = "(no response)"

        request_captured = (
            first_request_before
            or self._last_request
            or {
                "method": "POST",
                "url": getattr(self.llm, "openai_api_base", "") or "",
                "headers": headers,
                "body": {"note": "event hook didn't fire — check httpx version"},
            }
        )
        # Prefer the first LLM request of the turn for the legacy slot,
        # so runners that only look at request_captured see the INITIAL
        # prompt (not the last hop).
        for ev in turn_events:
            if ev["t"].startswith("llm_hop_") and ev.get("request"):
                request_captured = ev["request"]
                break

        response_captured = self._last_response or {
            "status": 0,
            "headers": {},
            "body": {"note": "event hook didn't fire"},
        }

        # Responses-API state chain: pull the per-response id from the
        # final AIMessage.response_metadata (ChatOpenAI populates it as
        # "id" when use_responses_api=True) so the runner + compact() can
        # chain / prune it.
        #
        # E13a: only update _last_response_id / _response_history when
        # in state_chain mode (api=responses + state=T). The +conv path
        # does NOT track per-response ids (continuity is via the
        # conversation container) — appending here would mislead
        # downstream consumers about the available chain.
        new_resp_id: str | None = None
        if api == "responses" and final_ai is not None:
            meta = getattr(final_ai, "response_metadata", None) or {}
            new_resp_id = meta.get("id") or meta.get("response_id")
            if new_resp_id and state_chain:
                self._last_response_id = new_resp_id
                self._response_history.append(new_resp_id)
        if state_chain:
            self._forced_prev_id = None

        envelope: dict[str, Any] = {
            "turn_id": turn_id,
            "assistant_msg": final_content,
            "tool_calls": last_tool_calls,
            "request_captured": request_captured,
            "response_captured": response_captured,
            "framework_events": self._events,
        }
        if api == "responses":
            envelope["_response_id"] = new_resp_id
        elif api == "responses+conv":
            # E13b: expose the conversation container so the runner /
            # inspector can correlate turns by container.
            envelope["_conversation_id"] = self._conversation_id
        return envelope

    async def compact(self, strategy: str) -> dict:
        """Plan B T10 — mutate `self._messages` per the requested strategy.

        For api=chat / api=messages: the message list holds langchain-core
        BaseMessage objects (HumanMessage / AIMessage / ToolMessage /
        SystemMessage), so the strategy implementations match the langchain
        adapter. See adapters/langchain/framework_bridge.py::Trial.compact.

        For api=responses+conv (E13b): continuity lives in the OpenAI
        conversation container (conv_xxx) which has no client-side trim
        primitive. compact() is a no-op — we report the strategy + a
        note. Continuity remains intact (server-side) for subsequent
        turns.
        """
        from langchain_core.messages import (
            SystemMessage, ToolMessage,
        )

        api = self.config.get("api", "chat")
        if api == "responses+conv":
            return {
                "strategy": strategy,
                "history_len_before": 0,
                "history_len_after": 0,
                "note": (
                    "responses+conv compact is a no-op: continuity lives "
                    "in the OpenAI conversation container "
                    f"({self._conversation_id or 'not yet minted'}); "
                    "the Conversations API has no client-side trim primitive."
                ),
            }

        before = len(self._messages)
        if strategy == "drop_half":
            sys_msgs = [m for m in self._messages if isinstance(m, SystemMessage)]
            rest = [m for m in self._messages if not isinstance(m, SystemMessage)]
            keep = rest[len(rest) // 2:]
            self._messages = sys_msgs + keep
        elif strategy == "drop_tool_calls":
            self._messages = [
                m for m in self._messages
                if not isinstance(m, ToolMessage)
                and not getattr(m, "tool_calls", None)
            ]
        elif strategy == "summarize":
            sys_msgs = [m for m in self._messages if isinstance(m, SystemMessage)]
            rest = [m for m in self._messages if not isinstance(m, SystemMessage)]
            keep = rest[len(rest) // 2:]
            dropped = len(rest) - len(keep)
            summary = SystemMessage(
                content=f"[summarized {dropped} earlier messages]"
            )
            self._messages = sys_msgs + [summary] + keep
        else:
            raise ValueError(f"unknown strategy: {strategy}")
        return {
            "strategy": strategy,
            "history_len_before": before,
            "history_len_after": len(self._messages),
        }

    async def aclose(self) -> None:
        """Release httpx client connections."""
        try:
            await self._http_client.aclose()
        except Exception:
            pass
        # api=messages path installs an extra sync httpx.Client on the
        # Trial for the anthropic SDK's sync code path. Close it too.
        sync_http = getattr(self, "_anthropic_sync_http", None)
        if sync_http is not None:
            try:
                sync_http.close()
            except Exception:
                pass


def _annotate_tool_calls(events: list[dict], new_messages: list[Any]) -> None:
    """Best-effort: fill in tool_name / args / result_summary on mcp_tools_call.

    Walks `new_messages` for ToolMessage objects (which carry the tool
    result text) and pairs them with the tool_call metadata on the prior
    AIMessage. Mutates events in-place.
    """
    try:
        # Collect (name, args, result_text) triples in the order they occurred.
        pending_calls: list[tuple[str, dict]] = []  # from AIMessage tool_calls
        triples: list[tuple[str, dict, str]] = []
        for m in new_messages:
            cls = m.__class__.__name__
            if cls == "AIMessage":
                tcs = getattr(m, "tool_calls", None) or []
                for tc in tcs:
                    name = tc.get("name") if isinstance(tc, dict) else getattr(tc, "name", None)
                    args = (tc.get("args") if isinstance(tc, dict) else getattr(tc, "args", None)) or {}
                    pending_calls.append((name or "", args))
            elif cls == "ToolMessage":
                content = getattr(m, "content", "") or ""
                result_text = content if isinstance(content, str) else str(content)
                if pending_calls:
                    name, args = pending_calls.pop(0)
                    triples.append((name, args, result_text))

        triple_iter = iter(triples)
        for ev in events:
            if ev["t"] == "mcp_tools_call":
                try:
                    name, args, result_text = next(triple_iter)
                except StopIteration:
                    break
                ev["tool_name"] = name
                ev["args"] = args
                ev["result_summary"] = result_text[:500]
    except Exception:
        # Never let annotation failure break event emission.
        pass


def _stringify_tool_result(raw: Any) -> str:
    """Render a langchain-mcp-adapters tool result as text for ToolMessage.

    Kept for parity with the langchain adapter even though langgraph's
    create_react_agent handles tool result rendering internally. Exposed
    in case a future code path needs it.
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
    for attr in ("content", "text"):
        v = getattr(raw, attr, None)
        if v is not None:
            return _stringify_tool_result(v)
    try:
        return json.dumps(raw, default=str)
    except Exception:
        return str(raw)
