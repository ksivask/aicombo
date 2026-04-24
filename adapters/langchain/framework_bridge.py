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


def pick_llm_base_url(routing: str, llm: str) -> str:
    env_map_via_agw = {
        "ollama":  "AGW_LLM_BASE_URL_OLLAMA",
        "mock":    "AGW_LLM_BASE_URL_MOCK",
        "chatgpt": "AGW_LLM_BASE_URL_OPENAI",
        "gemini":  "AGW_LLM_BASE_URL_GEMINI",
        # api=messages uses ChatAnthropic. The env-var name follows the
        # existing convention used across crewai/pydantic_ai/autogen/llamaindex
        # adapters + docker-compose (AGW_LLM_BASE_URL_ANTHROPIC), NOT the
        # _CLAUDE suffix — switching it would break container wiring.
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


# Per-LLM API-key env var. Cloud providers (chatgpt/gemini) need a real
# key — we surface this clearly at Trial construction so a misconfigured
# adapter fails loudly instead of silently sending "placeholder".
# Mirrors adapters/langgraph/framework_bridge.py::_API_KEY_ENV_BY_LLM.
_API_KEY_ENV_BY_LLM = {
    "ollama":  None,      # local; any placeholder is fine (Ollama doesn't validate)
    "mock":    None,      # local mock-llm; no validation
    "chatgpt": "OPENAI_API_KEY",
    "gemini":  "GOOGLE_API_KEY",
    "claude":  "ANTHROPIC_API_KEY",
}


def _pick_api_key(llm: str) -> str:
    """Resolve the LLM API key for ChatOpenAI.

    Returns a placeholder string for self-hosted providers (ollama, mock)
    since ChatOpenAI requires *some* non-empty api_key value. Returns the
    real env-sourced key for cloud providers (chatgpt, gemini), raising
    ValueError when the required env var is missing or empty.
    """
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

    Mirrors the per-provider defaults convention used across Plan B
    adapters. Env-var overrides let operators pin a specific model
    without code changes.
    """
    if llm == "ollama":
        return os.environ.get("DEFAULT_OLLAMA_MODEL", "qwen2.5:7b")
    if llm == "chatgpt":
        return os.environ.get("DEFAULT_OPENAI_MODEL", "gpt-4o-mini")
    if llm == "gemini":
        return os.environ.get("DEFAULT_GEMINI_MODEL", "gemini-2.0-flash")
    if llm == "mock":
        return "mock"
    if llm == "claude":
        # Mirrors the crewai adapter's default (claude-3-5-haiku) — fast
        # + inexpensive for Plan B smoke coverage; operator can override
        # via DEFAULT_CLAUDE_MODEL.
        return os.environ.get("DEFAULT_CLAUDE_MODEL", "claude-3-5-haiku-20241022")
    return "unknown"


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

        # Full log of HTTP exchanges captured by the event hooks — used to
        # reconstruct per-MCP-operation event snapshots. A single MCP
        # "operation" (tools/list or tools/call) involves MULTIPLE HTTP
        # calls (initialize, notifications/initialized, GET SSE, POST method,
        # DELETE). We grab the slice that corresponds to the current op.
        self._http_exchanges: list[dict] = []
        self._exchange_mark: int = 0  # index into _http_exchanges at start of current op

        base_url = pick_llm_base_url(routing=config["routing"], llm=config["llm"])
        model = config.get("model") or _default_model(config["llm"])
        api_key = _pick_api_key(config["llm"])

        # httpx client with event hooks that capture real wire bytes.
        # Writes to BOTH _last_* (back-compat for simple cases) AND the
        # _http_exchanges list (used to demux the MCP multi-call session).
        async def log_req(request: httpx.Request) -> None:
            body_bytes = request.content or b""
            req_snap = {
                "method": request.method,
                "url": str(request.url),
                "headers": {k: v for k, v in request.headers.items()},
                "body": _safe_json(body_bytes),
                "body_bytes_len": len(body_bytes),
                "_req_id": id(request),  # match with response later
            }
            self._last_request = req_snap
            self._http_exchanges.append({"req": req_snap, "resp": None})

        async def log_resp(response: httpx.Response) -> None:
            # Read the body now so it's captured before returning
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
            # Attach to the matching request entry (by id(request))
            req_id = id(response.request)
            for ex in reversed(self._http_exchanges):
                if ex["resp"] is None and ex["req"].get("_req_id") == req_id:
                    ex["resp"] = resp_snap
                    break
            else:
                # Fallback: attach to last unfilled entry (preserves order for
                # transports that don't expose response.request symmetrically)
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

        # Build the LLM per (api, llm). Historically this adapter was
        # chat-only; Plan B T5 extends to messages/responses/responses+conv.
        # If the api+llm combo is rejected (ValueError), release the
        # already-constructed httpx.AsyncClient to avoid an "unclosed client"
        # warning at teardown.
        api = config.get("api", "chat")
        try:
            self.llm = self._build_llm(
                api=api, llm=config["llm"], model=model,
                base_url=base_url, api_key=api_key,
            )
        except Exception:
            # Best-effort close; httpx.AsyncClient must be aclosed from an
            # event loop. If no loop is running, just discard — the test
            # context manager / process teardown will GC it.
            import asyncio
            try:
                loop = asyncio.get_event_loop()
                if loop.is_running():
                    # Can't block from within a running loop — rely on GC.
                    pass
                else:
                    loop.run_until_complete(self._http_client.aclose())
            except Exception:
                pass
            raise

        # Responses+conv state chain (mirrors autogen/llamaindex T11 pattern).
        # Even though langchain-openai's `use_previous_response_id=True`
        # would thread prev-id automatically for a simple linear chain,
        # we maintain our own history list so `force_state_ref` can point
        # the next turn at an earlier response id (verdict-e test path).
        self._last_response_id: str | None = None
        self._response_history: list[str] = []
        self._forced_prev_id: str | None = None

        # MCP wiring (lazy — fetched on first turn that needs it).
        self.mcp_url = (
            pick_mcp_base_url(config["routing"], config["mcp"])
            if config.get("mcp") and config["mcp"] != "NONE"
            else ""
        )
        self._mcp_tools: list | None = None  # populated by _setup_mcp_tools
        self._llm_with_tools: Any | None = None

    def _build_llm(
        self,
        api: str,
        llm: str,
        model: str,
        base_url: str,
        api_key: str,
    ) -> Any:
        """Construct the LangChain LLM object for this (api, llm).

        api=chat
            → langchain_openai.ChatOpenAI (OpenAI-compat; ollama/chatgpt/
              gemini/mock providers).
        api=messages
            → langchain_anthropic.ChatAnthropic (claude only). The underlying
              anthropic SDK client is built on first use via a cached_property;
              we override both `_client` and `_async_client` to inject our
              hooked httpx clients so wire bytes flow into `_http_exchanges`.
        api=responses
            → ChatOpenAI with `use_responses_api=True`. chatgpt only.
        api=responses+conv
            → ChatOpenAI with `use_responses_api=True` AND
              `use_previous_response_id=True`. chatgpt only. We also track
              `_response_history` ourselves so `force_state_ref` can
              override the next turn's prev-id.
        """
        if api == "chat":
            from langchain_openai import ChatOpenAI
            return ChatOpenAI(
                base_url=base_url,
                api_key=api_key,
                model=model,
                http_async_client=self._http_client,
                default_headers={},   # populated per-turn in turn()
                temperature=0.3,
            )

        if api in ("responses", "responses+conv"):
            if llm != "chatgpt":
                raise ValueError(
                    f"api={api} requires llm=chatgpt; got llm={llm}"
                )
            from langchain_openai import ChatOpenAI
            # Verified against langchain-openai 1.1.16: both flags exist
            # as top-level model fields. `use_previous_response_id=True`
            # makes ainvoke auto-thread the prior response's id on every
            # call — we ALSO track it ourselves for force_state_ref.
            kwargs: dict[str, Any] = {
                "base_url": base_url,
                "api_key": api_key,
                "model": model,
                "http_async_client": self._http_client,
                "default_headers": {},
                "temperature": 0.3,
                "use_responses_api": True,
            }
            if api == "responses+conv":
                kwargs["use_previous_response_id"] = True
            return ChatOpenAI(**kwargs)

        if api == "messages":
            if llm != "claude":
                raise ValueError(
                    f"api=messages requires llm=claude; got llm={llm}"
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

    def _mark_exchange_start(self) -> None:
        """Remember where in _http_exchanges the next MCP operation starts."""
        self._exchange_mark = len(self._http_exchanges)

    def _exchanges_since_mark(self) -> list[dict]:
        """Slice of exchanges captured since the last mark (this op)."""
        return list(self._http_exchanges[self._exchange_mark:])

    @staticmethod
    def _rpc_method(exchange: dict) -> str | None:
        """Extract JSON-RPC method from an HTTP exchange's request body.

        MCP streamable-http uses JSON-RPC POST bodies. Returns the method
        (e.g. 'tools/list', 'tools/call', 'initialize') or None if not
        a JSON-RPC POST."""
        req = exchange.get("req") or {}
        body = req.get("body")
        if isinstance(body, dict):
            return body.get("method")
        return None

    def _capture_mcp_op_events(self, base_kind: str, **extra) -> list[dict]:
        """Convert every HTTP exchange of an MCP operation into labeled events.

        Rather than collapsing the entire MCP session into one event that
        may snapshot the wrong call (DELETE instead of tools/list — the
        bug this method fixes), emit one event per HTTP exchange with a
        specific label:
          - mcp_initialize, mcp_notif_initialized, mcp_sse, mcp_tools_list,
            mcp_tools_call, mcp_session_close, mcp_other
        """
        method_to_kind = {
            "initialize": "mcp_initialize",
            "notifications/initialized": "mcp_notif_initialized",
            "tools/list": "mcp_tools_list",
            "tools/call": "mcp_tools_call",
        }
        out = []
        exchanges = self._exchanges_since_mark()
        for ex in exchanges:
            req = ex.get("req") or {}
            method = req.get("method", "")
            rpc_method = self._rpc_method(ex)
            if method == "DELETE":
                kind = "mcp_session_close"
            elif method == "GET" and "text/event-stream" in str(req.get("headers", {}).get("accept", "")):
                kind = "mcp_sse_open"
            elif rpc_method and rpc_method in method_to_kind:
                kind = method_to_kind[rpc_method]
            elif rpc_method:
                kind = f"mcp_rpc_{rpc_method.replace('/', '_')}"
            else:
                kind = "mcp_http"
            ev = {
                "t": kind,
                "rpc_method": rpc_method,
                "request": copy.deepcopy(ex.get("req") or {}),
                "response": copy.deepcopy(ex.get("resp") or {}),
            }
            # Annotate the primary events with the caller's context
            if kind == f"mcp_{base_kind}" or kind == base_kind:
                ev.update(extra)
            out.append(ev)

        # Fallback: if no HTTP exchanges were captured in this window (e.g.
        # the tool was mocked out, or fastmcp failed before issuing a
        # request), emit one synthetic event carrying the operation
        # metadata so the drawer + tests see SOMETHING for the op.
        # Named with singular suffix (mcp_tools_list → mcp_tool_list, etc.)
        # so it's distinct from the "real HTTP" variant.
        if not exchanges:
            synthetic_kind = "mcp_tool_call" if base_kind == "tools_call" else (
                "mcp_tool_list" if base_kind == "tools_list" else f"mcp_{base_kind}"
            )
            synth = {
                "t": synthetic_kind,
                "rpc_method": None,
                "request": None,
                "response": None,
                "_synthetic": True,
            }
            synth.update(extra)
            out.append(synth)
        return out

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

        api = self.config.get("api", "chat")

        headers = {
            "X-Harness-Trial-ID": self.trial_id,
            "X-Harness-Turn-ID": turn_id,
        }
        # Replace default_headers so each turn's X-Harness-Turn-ID updates.
        # ChatOpenAI is built with this http_async_client; default_headers
        # are sent by the OpenAI SDK on every call. ChatAnthropic's
        # _async_client was overridden in __init__ so we bypass the
        # framework's own default_headers path — update the SDK client
        # directly via its `_custom_headers` attr (anthropic SDK internal,
        # stable across 0.x — see anthropic-sdk-python BaseClient).
        try:
            self.llm.default_headers = headers
        except Exception:
            pass
        if api == "messages":
            async_client = getattr(self.llm, "_async_client", None)
            if async_client is not None and hasattr(async_client, "_custom_headers"):
                async_client._custom_headers = dict(headers)

        # Reset per-turn capture
        self._last_request = None
        self._last_response = None
        self._events = []

        # Set up MCP tools once if needed. Captures every HTTP call of the
        # MCP session (initialize → notif → SSE → tools/list → DELETE) as
        # separate events, NOT collapsed into one.
        if self.mcp_url and self._mcp_tools is None:
            self._mark_exchange_start()
            await self._setup_mcp_tools(headers)
            mcp_events = self._capture_mcp_op_events(
                "tools_list",
                tool_count=len(self._mcp_tools or []),
                tool_names=[t.name for t in (self._mcp_tools or [])],
            )
            self._events.extend(mcp_events)

        self.messages.append(HumanMessage(content=user_msg))

        llm_to_use = self._llm_with_tools if self._llm_with_tools is not None else self.llm
        first_request = None
        final_resp: Any | None = None
        last_assistant_content = ""
        last_tool_calls: list[dict] = []

        # Iterative agent loop, max MAX_LLM_HOPS LLM hops.
        for hop in range(MAX_LLM_HOPS):
            # For api=responses+conv, thread previous_response_id as a
            # per-call kwarg. On hop > 0 we don't override (the hop
            # exchange is internal); only the first hop needs the cross-
            # turn link. `use_previous_response_id=True` on the ChatOpenAI
            # instance makes the framework auto-thread the most recent
            # response id by default — passing prev_id=<forced> overrides.
            invoke_kwargs: dict[str, Any] = {}
            if api == "responses+conv" and hop == 0 and self._forced_prev_id:
                invoke_kwargs["previous_response_id"] = self._forced_prev_id
                # Consumed — future turns fall back to the natural chain.
                self._forced_prev_id = None
            resp = await llm_to_use.ainvoke(self.messages, **invoke_kwargs)
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

            # Record the response id for responses/responses+conv modes so
            # force_state_ref + _response_id envelope field work. The id
            # lives on resp.response_metadata["id"] for the OpenAI
            # Responses API path in langchain-openai 1.1.x.
            if api in ("responses", "responses+conv"):
                md = getattr(resp, "response_metadata", None) or {}
                rid = md.get("id") or md.get("response_id")
                if rid:
                    self._response_history.append(rid)
                    if api == "responses+conv":
                        self._last_response_id = rid

            if not tool_calls:
                final_resp = resp
                break

            # Execute each tool call against MCP.
            for tc in tool_calls:
                tool = next((t for t in (self._mcp_tools or []) if t.name == tc["name"]), None)
                self._mark_exchange_start()  # new MCP op window
                if tool is None:
                    tool_result_text = f"unknown tool: {tc['name']}"
                    err = "unknown_tool"
                else:
                    try:
                        # `ainvoke(args)` runs the StructuredTool's coroutine,
                        # which (since we passed connection= not session=)
                        # opens a fresh MCP session via our httpx factory.
                        raw_result = await tool.ainvoke(tc.get("args", {}) or {})
                        tool_result_text = _stringify_tool_result(raw_result)
                        err = None
                    except Exception as e:  # noqa: BLE001
                        tool_result_text = f"tool error: {e}"
                        err = str(e)
                # Emit one event per HTTP exchange in the MCP session, with
                # labels like mcp_initialize / mcp_notif_initialized /
                # mcp_sse_open / mcp_tools_call / mcp_session_close. The
                # tools_call event carries the tool_name / args / result.
                mcp_events = self._capture_mcp_op_events(
                    "tools_call",
                    tool_name=tc["name"],
                    args=tc.get("args", {}),
                    result_summary=tool_result_text[:500],
                    **({"error": err} if err else {}),
                )
                self._events.extend(mcp_events)

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

        envelope: dict = {
            "turn_id": turn_id,
            "assistant_msg": final_content,
            "tool_calls": last_tool_calls,
            "request_captured": request_captured,
            "response_captured": response_captured,
            "framework_events": self._events,
        }
        # T11: expose the latest Responses-API response id so the runner
        # can thread it through a subsequent force_state_ref turn.
        if api in ("responses", "responses+conv"):
            envelope["_response_id"] = (
                self._response_history[-1]
                if self._response_history else None
            )
        return envelope

    async def compact(self, strategy: str) -> dict:
        """Plan B T10 — mutate `self.messages` per the requested strategy.

        Strategies (spec §10):
          * drop_half — keep SystemMessages; drop oldest 50% of others.
          * drop_tool_calls — drop every ToolMessage + every message carrying
            tool_calls. Preserves user + plain assistant text only.
          * summarize — drop_half + inject a SystemMessage summary marker at
            the head of the kept (non-system) segment.

        For api=responses+conv the framework has no per-message history we
        own — only the response-id chain. compact trims `_response_history`
        and re-pegs `_last_response_id` to the surviving tail. All three
        strategies fall through to drop_half on this path (mirrors
        autogen/llamaindex responses_direct compact shape).

        Returns a small envelope the runner captures as the compact turn's
        response body.
        """
        api = self.config.get("api", "chat")
        if api == "responses+conv":
            before = len(self._response_history)
            half = before // 2
            self._response_history = self._response_history[half:]
            self._last_response_id = (
                self._response_history[-1] if self._response_history else None
            )
            out: dict = {
                "strategy": strategy,
                "history_len_before": before,
                "history_len_after": len(self._response_history),
                "note": (
                    "responses+conv has no per-message history — "
                    "compacted _response_history chain instead"
                ),
            }
            return out

        # Lazy import matches the turn() path — tests don't need langchain
        # until a real turn runs.
        from langchain_core.messages import (
            SystemMessage, ToolMessage,
        )

        before = len(self.messages)
        if strategy == "drop_half":
            sys_msgs = [m for m in self.messages if isinstance(m, SystemMessage)]
            rest = [m for m in self.messages if not isinstance(m, SystemMessage)]
            keep = rest[len(rest) // 2:]
            self.messages = sys_msgs + keep
        elif strategy == "drop_tool_calls":
            self.messages = [
                m for m in self.messages
                if not isinstance(m, ToolMessage)
                and not getattr(m, "tool_calls", None)
            ]
        elif strategy == "summarize":
            sys_msgs = [m for m in self.messages if isinstance(m, SystemMessage)]
            rest = [m for m in self.messages if not isinstance(m, SystemMessage)]
            keep = rest[len(rest) // 2:]
            dropped = len(rest) - len(keep)
            summary = SystemMessage(
                content=f"[summarized {dropped} earlier messages]"
            )
            self.messages = sys_msgs + [summary] + keep
        else:
            raise ValueError(f"unknown strategy: {strategy}")
        return {
            "strategy": strategy,
            "history_len_before": before,
            "history_len_after": len(self.messages),
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
