"""pydantic-ai-specific adapter logic.

pydantic-ai is a typed agent framework built around `pydantic_ai.Agent`.
Each provider-specific `Model` class (OpenAIModel, OpenAIResponsesModel,
AnthropicModel) accepts a `provider=` object, and each provider accepts
`http_client=httpx.AsyncClient` — so we hand it OUR event-hook'd client
directly and all LLM wire bytes flow into `_http_exchanges` without any
monkey-patching.

MCP integration is equally clean: `MCPServerStreamableHTTP(url=..., headers=...,
http_client=our_httpx)` is a `Toolset`, and the Agent accepts toolsets via
the `toolsets=` kwarg. The MCP server object drives initialize / tools/list /
tools/call over the same httpx client, so the MCP session's HTTP exchanges
are captured alongside the LLM calls.

Supported (api, llm) combos per Plan B spec:
  - api=chat,      llm in {ollama, chatgpt, gemini, mock}
  - api=messages,  llm=claude
  - api=responses, llm=chatgpt

Capture model mirrors adapters/langgraph/framework_bridge.py: mark once at
the start of `agent.run()`, then classify every HTTP exchange captured
during the run as `llm_hop_N` or `mcp_*` by URL + JSON-RPC method.

Per-turn X-Harness-Turn-ID: the httpx.AsyncClient is built once at Trial
init, but its `headers=` arg is a mutable dict we keep a reference to and
mutate before every turn — httpx reads from that dict for every outgoing
request.
"""
from __future__ import annotations

import copy
import json
import os
from typing import Any

import httpx


# ── LLM base URL resolution (mirrors crewai/langgraph adapter) ──

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


# Per-LLM API-key env var. Cloud providers need a real key; local ones accept
# a placeholder.
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
    if api == "messages":
        if llm == "claude":
            return "claude-3-5-haiku-20241022"
    if api == "responses":
        if llm == "chatgpt":
            return "gpt-4o-mini"
    raise ValueError(f"unsupported (api, llm) for pydantic-ai: ({api}, {llm})")


def _build_model(api: str, llm: str, model_name: str, base_url: str,
                 api_key: str, http_client: httpx.AsyncClient) -> Any:
    """Construct the appropriate pydantic-ai Model based on (api, llm).

    Each Model takes a `provider=` object, and each provider takes
    `http_client=` — which is where our event-hook'd httpx.AsyncClient
    gets plumbed through so LLM HTTP exchanges are captured.
    """
    if api == "chat":
        # OpenAI-chat-compatible endpoint. Covers ollama (AGW /llm/ollama/v1),
        # mock-llm, chatgpt (/llm/chatgpt/v1), and gemini's OpenAI-compat path.
        from pydantic_ai.models.openai import OpenAIChatModel
        from pydantic_ai.providers.openai import OpenAIProvider
        provider = OpenAIProvider(
            base_url=base_url,
            api_key=api_key,
            http_client=http_client,
        )
        return OpenAIChatModel(model_name, provider=provider)
    if api == "messages":
        if llm != "claude":
            raise ValueError(f"api=messages is claude-only; got llm={llm}")
        from pydantic_ai.models.anthropic import AnthropicModel
        from pydantic_ai.providers.anthropic import AnthropicProvider
        provider = AnthropicProvider(
            base_url=base_url,
            api_key=api_key,
            http_client=http_client,
        )
        return AnthropicModel(model_name, provider=provider)
    if api == "responses":
        if llm != "chatgpt":
            raise ValueError(f"api=responses is chatgpt-only; got llm={llm}")
        from pydantic_ai.models.openai import OpenAIResponsesModel
        from pydantic_ai.providers.openai import OpenAIProvider
        provider = OpenAIProvider(
            base_url=base_url,
            api_key=api_key,
            http_client=http_client,
        )
        return OpenAIResponsesModel(model_name, provider=provider)
    raise ValueError(f"unsupported api for pydantic-ai: {api}")


def _build_mcp_servers(mcp_url: str, headers_ref: dict,
                       http_client: httpx.AsyncClient) -> list[Any]:
    """Return a list with one MCPServerStreamableHTTP, or [] if mcp=NONE.

    The `headers_ref` dict is SHARED with the trial's http_client headers —
    mutating it per turn is sufficient to update X-Harness-Turn-ID since
    pydantic-ai's MCP server passes the dict straight to the httpx request.
    """
    if not mcp_url:
        return []
    from pydantic_ai.mcp import MCPServerStreamableHTTP
    return [
        MCPServerStreamableHTTP(
            url=mcp_url,
            headers=headers_ref,
            http_client=http_client,
        )
    ]


class Trial:
    """Per-trial state for the pydantic-ai adapter."""

    def __init__(self, trial_id: str, config: dict):
        self.trial_id = trial_id
        self.config = config

        # pydantic-ai uses ModelMessage objects directly; we accumulate them
        # across turns and replay via `message_history=`.
        self._messages: list[Any] = []

        # Per-turn capture
        self._last_request: dict | None = None
        self._last_response: dict | None = None
        self._events: list[dict] = []

        # Full HTTP exchange log; per-turn window marked by _exchange_mark.
        self._http_exchanges: list[dict] = []
        self._exchange_mark: int = 0

        # Mutable headers dict shared with httpx.AsyncClient: mutated per
        # turn so X-Harness-Turn-ID is always current. httpx reads from
        # this dict fresh on each outgoing request.
        self._headers: dict[str, str] = {
            "X-Harness-Trial-ID": trial_id,
            "X-Harness-Turn-ID":  "",
        }

        # httpx event hooks that capture wire bytes.
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

        # Single hooked AsyncClient — used by both the LLM provider and
        # (when MCP is enabled) the MCPServerStreamableHTTP toolset.
        # headers= points to our mutable self._headers dict so per-turn
        # header mutations propagate automatically.
        self._http_client = httpx.AsyncClient(
            event_hooks={"request": [log_req], "response": [log_resp]},
            headers=self._headers,
            timeout=httpx.Timeout(120.0),
        )

        # Build LLM model + provider with the hooked http_client.
        base_url = pick_llm_base_url(routing=config["routing"], llm=config["llm"])
        api_key = _pick_api_key(config["llm"])
        model_name = _default_model_name(config["api"], config["llm"], config.get("model"))

        self.model = _build_model(
            api=config["api"],
            llm=config["llm"],
            model_name=model_name,
            base_url=base_url,
            api_key=api_key,
            http_client=self._http_client,
        )

        # MCP wiring (toolsets passed at Agent construction time).
        self.mcp_url = (
            pick_mcp_base_url(config["routing"], config["mcp"])
            if config.get("mcp") and config["mcp"] != "NONE"
            else ""
        )
        self._mcp_servers = _build_mcp_servers(
            self.mcp_url, self._headers, self._http_client,
        )

        # Build the Agent. pydantic-ai 1.x takes toolsets= (NOT mcp_servers=).
        # MCPServerStreamableHTTP IS a toolset, so they go into the same list.
        from pydantic_ai import Agent
        self._agent = Agent(
            self.model,
            toolsets=self._mcp_servers or None,
            system_prompt=(
                "You are a concise, helpful assistant. "
                "Use tools when relevant; otherwise answer directly."
            ),
        )

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

    # ── Turn driver ──

    async def turn(self, turn_id: str, user_msg: str) -> dict:
        """Drive one turn via agent.run(), capturing all HTTP exchanges.

        Mutates `self._headers` in place so the next HTTP request through
        the shared httpx.AsyncClient carries the updated turn-id.
        """
        # Update shared headers dict — httpx reads this for every request.
        self._headers["X-Harness-Trial-ID"] = self.trial_id
        self._headers["X-Harness-Turn-ID"] = turn_id

        # Reset per-turn capture
        self._last_request = None
        self._last_response = None
        self._events = []

        # Mark window for this turn's agent.run, then invoke.
        self._mark_exchange_start()
        try:
            result = await self._agent.run(
                user_prompt=user_msg,
                message_history=self._messages or None,
            )
        except Exception as e:  # noqa: BLE001
            result = None
            final_text = f"(pydantic-ai error: {e.__class__.__name__}: {e})"
        else:
            out = getattr(result, "output", None)
            final_text = str(out) if out is not None else ""
            # Accumulate conversation history: pydantic-ai owns the full
            # list via result.all_messages().
            try:
                self._messages = list(result.all_messages())
            except Exception:
                pass

        if not final_text:
            final_text = "(no response)"

        # Classify all HTTP exchanges that happened during agent.run.
        turn_events = self._capture_events_since_mark()
        self._events.extend(turn_events)

        # Annotate mcp_tools_list events with tool metadata (best-effort).
        for ev in turn_events:
            if ev["t"] == "mcp_tools_list":
                tools = []
                resp = ev.get("response") or {}
                body = resp.get("body")
                if isinstance(body, dict):
                    # JSON-RPC result: {"result": {"tools": [...]}}
                    tools = ((body.get("result") or {}).get("tools")) or []
                elif isinstance(body, str) and body.startswith("event:"):
                    # SSE framing — best-effort: try to extract the data: line.
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

        # Best-effort tool_calls extraction from captured LLM responses.
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

        pydantic-ai's message history is a list of `ModelMessage` objects
        (ModelRequest / ModelResponse with a `parts` list). Walking `parts`
        to selectively strip tool-use blocks is fragile across pydantic-ai
        minor versions, so `drop_tool_calls` falls back to `drop_half` here.
        `summarize` also falls back to drop_half because manufacturing a
        synthetic ModelMessage without importing internal part classes
        would be worse than a simple slice. Return-envelope carries a
        `note` in those fallback cases.
        """
        before = len(self._messages)
        if strategy == "drop_half":
            self._messages = self._messages[before // 2:]
            note = None
        elif strategy == "drop_tool_calls":
            # pydantic-ai: tool_calls live inside ModelResponse.parts as
            # tool_call_part objects; filtering them without breaking the
            # req/resp pair structure is risky. Drop_half is the safest
            # approximation of "forget tool activity".
            self._messages = self._messages[before // 2:]
            note = (
                "pydantic-ai ModelMessage parts filter is fragile — "
                "fell back to drop_half"
            )
        elif strategy == "summarize":
            # Same fallback as drop_half; synthesizing a ModelMessage with
            # a text-only summary would require importing internal part
            # classes which shift across versions.
            self._messages = self._messages[before // 2:]
            note = (
                "pydantic-ai lacks a simple public summary-message ctor — "
                "fell back to drop_half"
            )
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


def _extract_tool_calls_from_response(response_snap: dict) -> list[dict]:
    """Parse tool_calls off a captured LLM response body.

    Handles the three shapes this adapter produces:
      - OpenAI chat.completions: body.choices[].message.tool_calls[]
      - OpenAI Responses API:    body.output[] with type=function_call
      - Anthropic messages:      body.content[] with type=tool_use
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
