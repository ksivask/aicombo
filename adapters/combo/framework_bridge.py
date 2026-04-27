"""Combo adapter — multi-LLM-same-CID round-robin dispatch (E24) +
intra-turn multi-MCP fan-out (E24a).

This adapter accepts a `llm` LIST in its trial config and rotates per turn
across the providers in that list (round-robin). The point: verify that
AGW's CID survives an agent talking to MULTIPLE LLMs in one conversation,
i.e. cross-API governance fidelity.

E24a layers a `mcp` LIST on top: combo connects to every listed MCP server
at first-turn, advertises the UNION of their tools to the LLM, and routes
each `tools/call` to the right server based on a `(tool_name -> server)`
routing table. This unblocks "Seattle research" style trials where one
turn spans weather + news + library + fetch tools.

Why it works (design.md §E24):
  AGW is stateless — CID continuity is derived from incoming request
  content (Channel-2 text marker `<!-- ib:cid=... -->`, optional X-IB-CID
  header). State lives in the agent's accumulated context. So an adapter
  that preserves the AGW-injected text marker across LLM switches will see
  CID continuity automatically.

This adapter's job is shape-translation hygiene:
  - Maintain ONE canonical history (provider-agnostic)
  - Render that history into each API's required shape per turn
  - Capture wire bytes via shared hooked httpx.AsyncClient

Scope after E24a:
  - chat (ollama, mock, chatgpt, gemini) via openai.AsyncOpenAI
  - messages (claude) via anthropic.AsyncAnthropic
  - MULTI-MCP fan-out via fastmcp.Client pool (E24a, this commit)
  - OpenAI-shape tool calling (chat completions tool_calls loop)
  - Anthropic-shape tool calling: NOT YET — needs cross-shape `tool_use`
    translation. If `llm == "claude"` AND tools are wired, we LOG a
    warning and run claude turns with NO tools advertised. Cross-shape
    tool translation is E24b territory.
  - NO streaming (defer)
  - NO responses / responses+conv (defer to E24c)

Defense-in-depth: capture X-IB-CID from each response and replay as the
NEXT request's header. Best-effort — openai/anthropic SDK response objects
don't always expose underlying response headers cleanly. The Channel-2
text marker remains the primary carrier.
"""
from __future__ import annotations

import copy
import json
import logging
import os
from typing import Any

import httpx

log = logging.getLogger("aiplay.adapter.combo")

# Bound the OpenAI tool-call loop. 5 hops is comfortable headroom over the
# Seattle-style 4-tool turn (one hop per tool plus a final no-tool reply).
MAX_TOOL_HOPS = 5


# ── Provider routing helpers (ported from langchain bridge — same env-var
# convention used across all sibling adapters in docker-compose). ──

def pick_llm_base_url(routing: str, llm: str) -> str:
    """Resolve the base URL for `llm` under the given routing.

    `routing` for combo is always "via_agw" by design (the whole point of
    the adapter is to verify governance fidelity across LLMs); the env_map
    structure accepts "direct" too for parity with sibling adapters but
    no current matrix row exercises that branch.
    """
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
        raise ValueError(
            f"combo: no LLM base URL mapping for llm={llm} routing={routing}"
        )
    url = os.environ.get(var)
    if not url:
        raise ValueError(f"combo: env var {var} not set")
    return url


def pick_mcp_base_url(routing: str, mcp: str) -> str:
    """Resolve the base URL for an MCP server under the given routing.

    Mirror of langchain/direct-mcp adapters' env-var convention. Combo
    only supports `via_agw` for first cut (the whole point is governance
    fidelity), but we accept `direct` for parity in case a future trial
    wants it.
    """
    if mcp == "NONE":
        raise ValueError("combo: pick_mcp_base_url called with mcp='NONE'")
    prefix = "AGW_MCP_" if routing == "via_agw" else "DIRECT_MCP_"
    var = f"{prefix}{mcp.upper()}"
    url = os.environ.get(var)
    if not url:
        raise ValueError(f"combo: env var {var} not set (needed for mcp={mcp})")
    return url


_API_KEY_ENV_BY_LLM = {
    "ollama":  None,
    "mock":    None,
    "chatgpt": "OPENAI_API_KEY",
    "gemini":  "GOOGLE_API_KEY",
    "claude":  "ANTHROPIC_API_KEY",
}


def pick_api_key(llm: str) -> str:
    """Return a key string suitable for the SDK constructor.

    Self-hosted providers (ollama, mock) get a literal "placeholder" so
    the SDK's non-empty validation passes; cloud providers must have the
    matching env var set.
    """
    env_var = _API_KEY_ENV_BY_LLM.get(llm)
    if env_var is None:
        return "placeholder"
    key = os.environ.get(env_var, "")
    if not key:
        raise ValueError(
            f"combo: {env_var} not set in adapter env — needed for llm={llm}"
        )
    return key


# Per-LLM default model (env-overridable). Picked when neither the row
# config nor an explicit override supplies one. Mirrors the convention
# used across sibling adapters.
DEFAULT_MODEL_PER_LLM: dict[str, str] = {
    "ollama":  os.environ.get("DEFAULT_OLLAMA_MODEL",  "llama3.1:latest"),
    "mock":    "mock",
    "chatgpt": os.environ.get("DEFAULT_OPENAI_MODEL",  "gpt-4o-mini"),
    "gemini":  os.environ.get("DEFAULT_GEMINI_MODEL",  "gemini-2.0-flash"),
    "claude":  os.environ.get("DEFAULT_CLAUDE_MODEL",  "claude-haiku-4-5"),
}


# Which API shape each LLM speaks. Drives both client construction
# (openai vs anthropic SDK) and shape translation.
_LLM_SHAPE: dict[str, str] = {
    "ollama":  "openai",
    "mock":    "openai",
    "chatgpt": "openai",
    "gemini":  "openai",
    "claude":  "anthropic",
}


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


def _tool_to_dict(t: Any) -> dict:
    """Normalize a fastmcp Tool object (pydantic) into a plain dict.

    Mirrors the helper in adapters/direct-mcp/framework_bridge.py — fastmcp
    swaps inputSchema/input_schema across versions, so try both.
    """
    if isinstance(t, dict):
        return {
            "name": t.get("name"),
            "description": t.get("description"),
            "inputSchema": t.get("inputSchema") or t.get("input_schema") or {},
        }
    d: dict = {}
    for attr in ("name", "description"):
        if hasattr(t, attr):
            d[attr] = getattr(t, attr)
    for attr in ("inputSchema", "input_schema"):
        if hasattr(t, attr):
            d["inputSchema"] = getattr(t, attr) or {}
            break
    d.setdefault("inputSchema", {})
    return d


def _tool_result_to_string(result: Any) -> str:
    """Render a fastmcp CallToolResult as a readable string for tool-role
    history messages. Mirrors direct-mcp's helper — fastmcp returns a
    CallToolResult-ish object with `.content` (list of typed blocks).
    """
    content = getattr(result, "content", None)
    if content is None and isinstance(result, dict):
        content = result.get("content")
    if not content:
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
    """One combo trial — multi-LLM round-robin dispatch.

    State:
      _llm_list           — resolved list of llm names for this trial
      _mcp_list           — resolved list of MCP server names (E24a)
      _mcp_clients        — {server_name: fastmcp.Client} pool (E24a)
      _merged_tool_catalog — union of all listed MCPs' tools (E24a)
      _tool_routing       — {tool_name: server_name} dispatch table (E24a)
      _mcp_connected      — idempotency flag for _connect_mcps_if_needed
      _http_client        — shared hooked httpx.AsyncClient (wire-bytes capture)
      _clients            — {llm_name: AsyncOpenAI | AsyncAnthropic}
      _canonical_history  — list of history entries; SOURCE OF TRUTH (provider-
                            agnostic; shape-translated per turn). Entry shape:
                              {"role": "user"|"assistant"|"tool",
                               "content_text": str,
                               # optional, for tool-call rounds:
                               "tool_calls": [{"id","name","arguments"}],
                               "tool_call_id": str}
      _observed_cid_header — last X-IB-CID captured from a response (best-effort)
      _exchanges          — full per-trial httpx exchange log; each turn's
                            slice is returned in framework_events
      _claude_tool_warned — one-shot flag so the "claude+tools needs E24b"
                            warning fires once per trial, not per turn
      _mcp_connect_failures — list of {mcp, error} dicts recorded by
                            _connect_mcps_if_needed when a per-MCP build
                            or list_tools call raises. Surfaced as
                            synthetic `mcp_connect_failure` framework_events
                            on turn 0 so operators see failures in the
                            trial JSON without grepping container logs.

    See design.md §E24 + enhancements.md §E24a for carrier mechanics.
    """

    def __init__(self, trial_id: str, config: dict):
        self.trial_id = trial_id
        self.config = config

        # Resolve the llm list from string-or-list form. The schema (E23)
        # already accepts both, so handle both at the adapter boundary.
        llm_field = config.get("llm")
        if isinstance(llm_field, list):
            self._llm_list: list[str] = list(llm_field)
        elif isinstance(llm_field, str) and llm_field:
            self._llm_list = [llm_field]
        else:
            raise ValueError(
                f"combo adapter requires non-empty llm list/string, got {llm_field!r}"
            )
        if not self._llm_list:
            raise ValueError("combo adapter requires non-empty llm list")

        # E24a: accept str | list[str] | "NONE" | empty-list. Coerce single
        # string to a 1-elt list so downstream pool / fan-out logic is uniform.
        mcp_field = config.get("mcp", "NONE")
        if mcp_field in (None, "NONE", "", []):
            self._mcp_list: list[str] = []
        elif isinstance(mcp_field, list):
            self._mcp_list = [m for m in mcp_field if m and m != "NONE"]
        elif isinstance(mcp_field, str):
            self._mcp_list = [mcp_field]
        else:
            raise ValueError(
                f"combo: unsupported mcp field shape {type(mcp_field).__name__}"
            )

        # Build hooked httpx.AsyncClient + per-LLM SDK clients.
        self._exchanges: list[dict] = []
        self._http_client = self._build_hooked_client()
        self._clients = self._build_clients()

        # E24a — multi-MCP fan-out pool, lazy-initialized at first turn so
        # construction in unit tests doesn't require a live MCP server.
        self._mcp_clients: dict[str, Any] = {}
        self._merged_tool_catalog: list[dict] = []
        self._tool_routing: dict[str, str] = {}
        self._mcp_connected: bool = False
        # One-shot warning flag — claude + tools is logged once per trial,
        # then claude turns silently fall back to no-tools mode (E24b deferred).
        self._claude_tool_warned: bool = False
        # Per-MCP connect-time failures (build or list_tools). Recorded by
        # _connect_mcps_if_needed and surfaced on turn 0 as synthetic
        # framework_events so operators see them without grepping logs.
        self._mcp_connect_failures: list[dict] = []
        # Idempotency guard so the synthetic failure events emit ONCE
        # (on the first turn after connect ran), even though the failure
        # list itself remains queryable for the lifetime of the trial.
        self._mcp_connect_failures_emitted: bool = False

        # Source of truth — provider-agnostic.
        self._canonical_history: list[dict[str, Any]] = []

        # Defense-in-depth header carrier.
        self._observed_cid_header: str | None = None

    # ── Hooked client + per-LLM SDK clients ──

    def _build_hooked_client(self) -> httpx.AsyncClient:
        """Build an httpx.AsyncClient whose request/response hooks append
        every exchange to self._exchanges. Both openai + anthropic SDKs
        accept `http_client=` and route through this single client.
        """
        async def log_req(request: httpx.Request) -> None:
            body_bytes = request.content or b""
            self._exchanges.append({
                "req": {
                    "method": request.method,
                    "url": str(request.url),
                    "headers": {k: v for k, v in request.headers.items()},
                    "body": _safe_json(body_bytes),
                    "body_bytes_len": len(body_bytes),
                    "_req_id": id(request),
                },
                "resp": None,
            })

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
            req_id = id(response.request)
            for ex in reversed(self._exchanges):
                if ex["resp"] is None and ex["req"].get("_req_id") == req_id:
                    ex["resp"] = resp_snap
                    return
            for ex in reversed(self._exchanges):
                if ex["resp"] is None:
                    ex["resp"] = resp_snap
                    return

        return httpx.AsyncClient(
            event_hooks={"request": [log_req], "response": [log_resp]},
            timeout=httpx.Timeout(120.0),
        )

    def _build_clients(self) -> dict[str, Any]:
        """Construct one SDK client per UNIQUE llm in self._llm_list.

        openai-shape providers (ollama, mock, chatgpt, gemini) → AsyncOpenAI
        anthropic-shape providers (claude)                     → AsyncAnthropic

        All clients share the same hooked self._http_client so wire bytes
        for every dispatch flow into self._exchanges regardless of provider.
        """
        from openai import AsyncOpenAI
        from anthropic import AsyncAnthropic

        routing = self.config.get("routing", "via_agw")
        out: dict[str, Any] = {}
        for llm in set(self._llm_list):
            base = pick_llm_base_url(routing, llm)
            api_key = pick_api_key(llm)
            shape = _LLM_SHAPE.get(llm)
            if shape == "openai":
                out[llm] = AsyncOpenAI(
                    base_url=base, api_key=api_key,
                    http_client=self._http_client,
                )
            elif shape == "anthropic":
                out[llm] = AsyncAnthropic(
                    base_url=base, api_key=api_key,
                    http_client=self._http_client,
                )
            else:
                raise ValueError(f"combo adapter: unsupported llm {llm}")
        return out

    # ── E24a: multi-MCP pool + fan-out ──

    def _build_mcp_client(self, mcp_name: str) -> Any:
        """Construct a fastmcp.Client for `mcp_name` whose underlying
        httpx clients route through OUR event hooks (so MCP wire bytes
        flow into self._exchanges alongside LLM traffic).

        Mirror of adapters/direct-mcp/framework_bridge.py — fastmcp's
        StreamableHttpTransport accepts a `httpx_client_factory` that
        we use to inject per-request/response hooks.
        """
        from fastmcp import Client as FastMCPClient  # lazy import (test-isolation)
        from fastmcp.client.transports import StreamableHttpTransport

        routing = self.config.get("routing", "via_agw")
        base_url = pick_mcp_base_url(routing, mcp_name)

        # Snapshot the existing event hooks off our shared httpx client so
        # the factory can prepend them onto whatever transport fastmcp builds.
        existing_hooks = self._http_client.event_hooks or {}
        req_hooks = list(existing_hooks.get("request", []))
        resp_hooks = list(existing_hooks.get("response", []))

        def _httpx_factory(**kwargs):
            kw_hooks = kwargs.pop("event_hooks", {}) or {}
            kwargs["event_hooks"] = {
                "request":  req_hooks  + list(kw_hooks.get("request",  [])),
                "response": resp_hooks + list(kw_hooks.get("response", [])),
            }
            return httpx.AsyncClient(**kwargs)

        headers = {
            "X-Harness-Trial-ID": self.trial_id,
        }
        transport = StreamableHttpTransport(
            base_url, headers=headers, httpx_client_factory=_httpx_factory,
        )
        return FastMCPClient(transport)

    async def _connect_mcps_if_needed(self) -> None:
        """Eager-connect to every MCP at first turn.

        Builds self._merged_tool_catalog (union of all listed servers'
        tools) and self._tool_routing (tool_name -> server_name lookup).
        Idempotent via self._mcp_connected. Logs + skips servers whose
        tools/list fails so a single broken MCP doesn't tank the trial.
        Last-server-wins on tool-name collisions, with a WARNING log
        (E24a-prefix is the future variant if collisions become real).
        """
        if self._mcp_connected or not self._mcp_list:
            return
        for mcp_name in self._mcp_list:
            try:
                client = self._build_mcp_client(mcp_name)
            except Exception as e:
                log.error(
                    "combo: failed to build MCP client for mcp=%s: %s",
                    mcp_name, e,
                )
                self._mcp_connect_failures.append(
                    {"mcp": mcp_name, "error": str(e)}
                )
                continue
            self._mcp_clients[mcp_name] = client
            try:
                async with client:
                    tools_raw = await client.list_tools()
            except Exception as e:
                log.error(
                    "combo: failed tools/list for mcp=%s: %s", mcp_name, e,
                )
                self._mcp_connect_failures.append(
                    {"mcp": mcp_name, "error": str(e)}
                )
                continue
            for tool_raw in tools_raw:
                tool = _tool_to_dict(tool_raw)
                tname = tool.get("name")
                if not tname:
                    continue
                if tname in self._tool_routing:
                    log.warning(
                        "combo: tool name collision: %r in both %r and %r — "
                        "last wins (consider renaming one tool or using "
                        "prefixed mode in a future E24a-prefix enhancement)",
                        tname, self._tool_routing[tname], mcp_name,
                    )
                    # Replace the existing entry in the merged catalog so
                    # the LLM sees the last-wins description/inputSchema too.
                    self._merged_tool_catalog = [
                        t for t in self._merged_tool_catalog
                        if t.get("name") != tname
                    ]
                self._tool_routing[tname] = mcp_name
                self._merged_tool_catalog.append(tool)
        self._mcp_connected = True

    async def _dispatch_tool_call(self, name: str, arguments: dict) -> dict:
        """Route a tool call to the correct MCP server based on the routing
        table built at connect time. Returns a dict with either a `content`
        string (success) or an `error` string (unknown tool / dispatch fail).
        """
        server = self._tool_routing.get(name)
        if not server:
            return {"error": f"unknown tool: {name}"}
        client = self._mcp_clients.get(server)
        if client is None:
            return {"error": f"no client for server {server!r} (tool={name})"}
        try:
            async with client:
                result = await client.call_tool(name, arguments)
        except Exception as e:
            return {"error": f"tool call failed: {e}"}
        return {"content": _tool_result_to_string(result)}

    def _openai_tool_specs(self) -> list[dict]:
        """Translate self._merged_tool_catalog into the OpenAI chat
        completions tool-spec format:
          {"type":"function","function":{"name","description","parameters":<inputSchema>}}
        """
        out: list[dict] = []
        for t in self._merged_tool_catalog:
            out.append({
                "type": "function",
                "function": {
                    "name":        t.get("name"),
                    "description": t.get("description") or "",
                    "parameters":  t.get("inputSchema") or {},
                },
            })
        return out

    # ── Round-robin + model selection ──

    def _llm_for_turn(self, turn_idx: int) -> str:
        """Round-robin: turn N hits llm_list[N % len]."""
        return self._llm_list[turn_idx % len(self._llm_list)]

    def _model_for_turn(self, turn_idx: int, override: str | None) -> str:
        """Pick the model to use for this turn.

        Resolution order:
          1. Explicit override passed by main.py (per-turn, rare)
          2. self.config["model"] (str applies to all; list pairs 1:1 with llm)
          3. DEFAULT_MODEL_PER_LLM for the round-robin-selected llm
        """
        if override:
            return override
        m = self.config.get("model")
        llm = self._llm_for_turn(turn_idx)
        if isinstance(m, list) and m:
            return m[turn_idx % len(m)]
        if isinstance(m, str) and m:
            return m
        return DEFAULT_MODEL_PER_LLM.get(llm, "gpt-4o-mini")

    # ── Shape translation (canonical → API-specific) ──

    def _to_shape(self, canonical: list[dict[str, Any]], llm: str) -> list[dict]:
        """Render the canonical history into the LLM's required wire shape.

        OpenAI chat   — {role, content: <string>} for user/assistant; for
                        E24a tool-call rounds, assistant messages may carry
                        a `tool_calls` array, and `role: tool` messages carry
                        a `tool_call_id` linking back to the originating
                        assistant call.
        Anthropic msg — {role, content: [{type: text, text: <string>}]}.
                        E24a does NOT yet translate tool_calls/tool messages
                        into anthropic's `tool_use`/`tool_result` block shape
                        (that's E24b territory). Here we filter those out
                        with one informational log so the conversation can
                        still continue text-only.

        Critically: the Channel-2 text marker `<!-- ib:cid=... -->` is part
        of `content_text` and round-trips through both shapes verbatim
        (the regex AGW uses to scan inbound history is shape-agnostic).
        """
        shape = _LLM_SHAPE.get(llm)
        if shape == "openai":
            out: list[dict] = []
            for h in canonical:
                role = h["role"]
                if role == "assistant" and h.get("tool_calls"):
                    msg: dict[str, Any] = {
                        "role": "assistant",
                        "content": h.get("content_text") or None,
                        "tool_calls": [
                            {
                                "id":   tc["id"],
                                "type": "function",
                                "function": {
                                    "name":      tc["name"],
                                    "arguments": tc["arguments"]
                                        if isinstance(tc["arguments"], str)
                                        else json.dumps(tc["arguments"] or {}),
                                },
                            }
                            for tc in h["tool_calls"]
                        ],
                    }
                    out.append(msg)
                    continue
                if role == "tool":
                    out.append({
                        "role": "tool",
                        "tool_call_id": h.get("tool_call_id", ""),
                        "content": h.get("content_text", ""),
                    })
                    continue
                out.append({"role": role, "content": h.get("content_text", "")})
            return out
        if shape == "anthropic":
            # Filter out E24a tool-call / tool-role entries; anthropic-shape
            # cross-translation is E24b. We DO preserve user / plain
            # assistant text (which may contain the AGW CID marker — the
            # whole point of the combo adapter).
            kept: list[dict] = []
            had_tool_msg = False
            for h in canonical:
                role = h["role"]
                if role == "tool" or (role == "assistant" and h.get("tool_calls")):
                    had_tool_msg = True
                    continue
                kept.append({
                    "role": role,
                    "content": [
                        {"type": "text", "text": h.get("content_text", "")}
                    ],
                })
            if had_tool_msg and not self._claude_tool_warned:
                log.warning(
                    "combo: dropping tool-call / tool-role messages from claude "
                    "history shape — cross-shape tool translation requires E24b"
                )
                self._claude_tool_warned = True
            return kept
        raise ValueError(f"combo: unsupported llm shape: {llm}")

    # ── The main turn driver ──

    async def turn(
        self,
        turn_id: str,
        user_msg: str,
        model_override: str | None = None,
    ) -> dict:
        """Drive one turn with the round-robin-selected LLM.

        Round-robin index = number of user-role messages already in canonical
        history (BEFORE appending this turn's user message). This is robust
        to E24a tool-call rounds, which append additional assistant + tool
        messages without bumping the user count.
        """
        # E24a — eager-once MCP pool connect (no-op if already connected
        # or if no MCPs configured).
        await self._connect_mcps_if_needed()

        turn_idx = sum(
            1 for h in self._canonical_history if h.get("role") == "user"
        )

        # Append user message to canonical history (provider-agnostic).
        self._canonical_history.append(
            {"role": "user", "content_text": user_msg}
        )

        llm = self._llm_for_turn(turn_idx)
        client = self._clients[llm]
        model_for_turn = self._model_for_turn(turn_idx, model_override)

        # Per-turn header injection (matches the pattern every other adapter
        # uses post-B5 fix). Mutating self._http_client.headers is the
        # reliable surface — the openai/anthropic SDKs share this client.
        self._http_client.headers["X-Harness-Trial-ID"] = self.trial_id
        self._http_client.headers["X-Harness-Turn-ID"] = turn_id
        # If we previously captured X-IB-CID, replay it (defense-in-depth).
        if self._observed_cid_header:
            self._http_client.headers["X-IB-CID"] = self._observed_cid_header

        # Mark the slice of self._exchanges this turn produces.
        mark_idx = len(self._exchanges)

        shape = _LLM_SHAPE[llm]
        emitted_tool_calls: list[dict] = []
        # Decide whether to advertise tools. Tools require:
        #   (1) MCPs configured + connected
        #   (2) An openai-shape LLM (claude/anthropic-shape tool_use is E24b)
        tools_enabled = bool(self._merged_tool_catalog) and shape == "openai"
        if (self._merged_tool_catalog and shape == "anthropic"
                and not self._claude_tool_warned):
            log.warning(
                "combo: tool calling under llm=%s + tools=%d disabled — "
                "anthropic tool_use cross-shape translation requires E24b. "
                "Running this turn text-only.",
                llm, len(self._merged_tool_catalog),
            )
            self._claude_tool_warned = True

        if shape == "openai":
            assistant_text, emitted_tool_calls = await self._run_openai_loop(
                client=client, model=model_for_turn,
                tools_enabled=tools_enabled,
            )
        elif shape == "anthropic":
            api_shape = self._to_shape(self._canonical_history, llm)
            resp = await client.messages.create(
                model=model_for_turn,
                messages=api_shape,
                max_tokens=512,
            )
            # Anthropic content is a list of typed blocks. Concatenate text
            # blocks (the marker rides as plain text inside one of these).
            assistant_text = "".join(
                getattr(b, "text", "") for b in resp.content
                if getattr(b, "type", None) == "text"
            )
            self._canonical_history.append(
                {"role": "assistant", "content_text": assistant_text}
            )
            self._capture_cid_header(resp)
        else:
            raise ValueError(f"combo: unreachable shape {shape}")

        # Slice exchanges captured during this turn for framework_events.
        turn_exchanges = self._exchanges[mark_idx:]

        # Build the runner-shaped envelope. Mirror the field names other
        # adapters return so harness/runner.py treats this identically.
        last_request: dict | None = None
        last_response: dict | None = None
        for ex in turn_exchanges:
            if ex.get("req"):
                last_request = copy.deepcopy(ex["req"])
            if ex.get("resp"):
                last_response = copy.deepcopy(ex["resp"])

        # Build framework_events list: prepend any pending mcp_connect_failure
        # synthetic events on turn 0 (before the LLM dispatch events). This
        # surfaces silent per-MCP build/list_tools failures into the trial JSON
        # so operators see "mcp_connect_failure" entries on Trial detail
        # pages without grepping container logs. One-shot via
        # _mcp_connect_failures_emitted — turns 1..N still see the (already
        # log.error'd) failures via the queryable list, but only emit
        # synthetic events once.
        framework_events: list[dict] = []
        if self._mcp_connect_failures and not self._mcp_connect_failures_emitted:
            for fail in self._mcp_connect_failures:
                framework_events.append({
                    "t": "mcp_connect_failure",
                    "mcp": fail["mcp"],
                    "request": {
                        "url": f"<MCP {fail['mcp']} connect>",
                        "method": "MCP",
                    },
                    "response": {
                        "status": "error",
                        "body": fail["error"],
                    },
                })
            self._mcp_connect_failures_emitted = True
        framework_events.extend(
            {
                "t": f"llm_dispatch_{i}",
                "llm_for_turn": llm,
                "model": model_for_turn,
                "request": copy.deepcopy(ex.get("req")),
                "response": copy.deepcopy(ex.get("resp")),
            }
            for i, ex in enumerate(turn_exchanges)
        )

        return {
            "turn_id": turn_id,
            "assistant_msg": assistant_text,
            "tool_calls": emitted_tool_calls,
            "request_captured": last_request or {
                "method": "POST",
                "url": "",
                "headers": {},
                "body": {"note": "no exchange captured this turn"},
            },
            "response_captured": last_response or {
                "status": 0,
                "headers": {},
                "body": {"note": "no exchange captured this turn"},
            },
            "framework_events": framework_events,
            # Combo-specific surface for the inspector / verdict_k / tests.
            "llm_for_turn": llm,
            "model": model_for_turn,
        }

    async def _run_openai_loop(
        self,
        client: Any,
        model: str,
        tools_enabled: bool,
    ) -> tuple[str, list[dict]]:
        """Drive the OpenAI chat-completions tool-call agent loop.

        Returns (final_assistant_text, list-of-emitted-tool-calls).

        Loop:
          1. Render canonical history -> openai shape (incl. prior tool_calls).
          2. Call chat.completions.create(messages=, tools=) (tools omitted
             if not enabled).
          3. If response has no tool_calls, append assistant text to canonical,
             return.
          4. Else: append assistant message (with tool_calls) to canonical,
             dispatch each tool_call via _dispatch_tool_call, append a
             role=tool message per call, loop. Cap at MAX_TOOL_HOPS to
             prevent runaway.
        """
        emitted: list[dict] = []
        final_text: str = ""
        tool_specs = self._openai_tool_specs() if tools_enabled else None

        for hop in range(MAX_TOOL_HOPS):
            api_shape = self._to_shape(self._canonical_history, "chatgpt")
            kwargs: dict[str, Any] = {
                "model": model,
                "messages": api_shape,
                "max_tokens": 512,
            }
            if tool_specs:
                kwargs["tools"] = tool_specs
            resp = await client.chat.completions.create(**kwargs)
            self._capture_cid_header(resp)

            choice = resp.choices[0]
            msg = choice.message
            text = msg.content or ""
            tool_calls = getattr(msg, "tool_calls", None) or []

            if not tool_calls:
                final_text = text
                self._canonical_history.append(
                    {"role": "assistant", "content_text": text}
                )
                break

            # Normalize tool_calls -> dicts for canonical history.
            normalized: list[dict] = []
            for tc in tool_calls:
                fn = getattr(tc, "function", None) or {}
                fn_name = getattr(fn, "name", None) if not isinstance(fn, dict) else fn.get("name")
                fn_args = getattr(fn, "arguments", None) if not isinstance(fn, dict) else fn.get("arguments")
                normalized.append({
                    "id":        getattr(tc, "id", None) or (tc.get("id") if isinstance(tc, dict) else None) or "",
                    "name":      fn_name or "",
                    "arguments": fn_args if fn_args is not None else "{}",
                })
            emitted.extend(normalized)

            # Append the assistant tool-call announcement to canonical history.
            self._canonical_history.append({
                "role": "assistant",
                "content_text": text,
                "tool_calls": normalized,
            })

            # Dispatch each tool call + append tool-role messages.
            for tc in normalized:
                args_obj: dict
                args_raw = tc["arguments"]
                if isinstance(args_raw, dict):
                    args_obj = args_raw
                else:
                    try:
                        args_obj = json.loads(args_raw or "{}")
                    except (ValueError, TypeError):
                        args_obj = {}
                result = await self._dispatch_tool_call(tc["name"], args_obj)
                content_str = result.get("content") or json.dumps(result)
                self._canonical_history.append({
                    "role": "tool",
                    "tool_call_id": tc["id"],
                    "content_text": content_str,
                })
            # …loop and re-call so the LLM can react to tool results.
        else:
            # Hop limit reached without a clean text-only assistant message.
            log.warning(
                "combo: openai tool-call loop hit MAX_TOOL_HOPS=%d without "
                "terminating in a text reply", MAX_TOOL_HOPS,
            )
            final_text = ""

        return final_text, emitted

    def _capture_cid_header(self, resp: Any) -> None:
        """Best-effort X-IB-CID extract from an SDK response object.
        openai-python / anthropic-python expose raw headers under different
        attrs across versions; failure is silent (text marker is primary).
        """
        try:
            raw = getattr(resp, "_response", None) or getattr(resp, "response", None)
            headers = getattr(raw, "headers", None) if raw is not None else None
            if headers is not None:
                hval = headers.get("X-IB-CID") or headers.get("x-ib-cid")
                if hval:
                    self._observed_cid_header = hval
        except Exception:
            pass

    # ── E21 lifecycle hooks (parity with all other adapters) ──

    async def compact(self, strategy: str) -> dict:
        """Combo doesn't model framework-side history compaction in the
        first cut — the canonical history is the whole conversation, no
        summarizer/dropper. Provided for endpoint parity so the runner
        can issue a uniform compact call regardless of framework.
        """
        return {
            "strategy": strategy,
            "history_len_before": len(self._canonical_history),
            "history_len_after": len(self._canonical_history),
            "note": (
                "combo adapter has no compact strategy in the first cut "
                "(E24a may add one); canonical history is unchanged"
            ),
        }

    async def _drive_reset(self) -> dict:
        """E21 — wipe canonical history + drop the captured X-IB-CID
        header so the next turn starts CID-fresh.
        """
        cleared: list[str] = []
        if self._canonical_history:
            self._canonical_history = []
            cleared.append("_canonical_history")
        if self._observed_cid_header:
            self._observed_cid_header = None
            cleared.append("_observed_cid_header")
        if "X-IB-CID" in self._http_client.headers:
            del self._http_client.headers["X-IB-CID"]
            cleared.append("X-IB-CID header")
        return {"reset": True, "api": "combo", "cleared": cleared}

    async def _drive_refresh_tools(self) -> dict:
        """E21 — drop the cached MCP catalog + routing table so the next
        turn re-runs _connect_mcps_if_needed and re-fetches tools/list
        from each server. No-op if no MCPs configured.
        """
        if not self._mcp_list:
            return {
                "refresh_tools": "skipped",
                "reason": "no MCPs configured for this trial",
            }
        self._merged_tool_catalog = []
        self._tool_routing = {}
        self._mcp_clients = {}
        self._mcp_connected = False
        # Clear stale connect-failure list + emit-flag so a re-attempted
        # connect can record a fresh set of (or absence of) failures and
        # surface them again on the next turn.
        self._mcp_connect_failures = []
        self._mcp_connect_failures_emitted = False
        return {
            "refresh_tools": "ok",
            "mcp_list": list(self._mcp_list),
            "note": "catalog + routing dropped; will rebuild on next turn",
        }

    async def aclose(self) -> None:
        """Release the shared hooked httpx client + drop MCP client pool."""
        # fastmcp.Client sessions are opened per-op via `async with` in our
        # usage, so individual client objects don't hold open transports
        # here — clearing the pool is sufficient. Defensive close attempt
        # regardless, in case a future fastmcp version exposes one.
        for mcp_name, client in list(self._mcp_clients.items()):
            close_fn = getattr(client, "aclose", None) or getattr(client, "close", None)
            if close_fn is None:
                continue
            try:
                res = close_fn()
                if hasattr(res, "__await__"):
                    await res
            except Exception:
                pass
        self._mcp_clients = {}
        try:
            await self._http_client.aclose()
        except Exception:
            pass
