"""Combo adapter — multi-LLM-same-CID round-robin dispatch (E24).

This adapter accepts a `llm` LIST in its trial config and rotates per turn
across the providers in that list (round-robin). The point: verify that
AGW's CID survives an agent talking to MULTIPLE LLMs in one conversation,
i.e. cross-API governance fidelity.

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

First-cut scope (E24):
  - chat (ollama, mock, chatgpt, gemini) via openai.AsyncOpenAI
  - messages (claude) via anthropic.AsyncAnthropic
  - NO MCP integration (defer)
  - NO tool calling (defer to E24b)
  - NO streaming (defer)
  - NO responses / responses+conv (defer to E24a)

Defense-in-depth: capture X-IB-CID from each response and replay as the
NEXT request's header. Best-effort — openai/anthropic SDK response objects
don't always expose underlying response headers cleanly. The Channel-2
text marker remains the primary carrier.
"""
from __future__ import annotations

import copy
import json
import os
from typing import Any

import httpx


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
    "ollama":  os.environ.get("DEFAULT_OLLAMA_MODEL",  "qwen2.5:7b"),
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


class Trial:
    """One combo trial — multi-LLM round-robin dispatch.

    State:
      _llm_list           — resolved list of llm names for this trial
      _http_client        — shared hooked httpx.AsyncClient (wire-bytes capture)
      _clients            — {llm_name: AsyncOpenAI | AsyncAnthropic}
      _canonical_history  — list of {"role", "content_text"}; SOURCE OF TRUTH
                            (provider-agnostic; shape-translated per turn)
      _observed_cid_header — last X-IB-CID captured from a response (best-effort)
      _exchanges          — full per-trial httpx exchange log; each turn's
                            slice is returned in framework_events

    See design.md §E24 for carrier mechanics.
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

        # First-cut: no MCP. Warn loudly if a row tries to wire one.
        mcp = config.get("mcp", "NONE")
        if mcp not in (None, "NONE", "", []):
            # Don't fail — just note it. Future E24a/b adds MCP support.
            self._mcp_warning = (
                f"combo adapter first-cut has no MCP integration; "
                f"received mcp={mcp!r}, ignoring"
            )
        else:
            self._mcp_warning = None

        # Build hooked httpx.AsyncClient + per-LLM SDK clients.
        self._exchanges: list[dict] = []
        self._http_client = self._build_hooked_client()
        self._clients = self._build_clients()

        # Source of truth — provider-agnostic.
        self._canonical_history: list[dict[str, str]] = []

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

    def _to_shape(self, canonical: list[dict[str, str]], llm: str) -> list[dict]:
        """Render the canonical history into the LLM's required wire shape.

        OpenAI chat   — {role, content: <string>}
        Anthropic msg — {role, content: [{type: text, text: <string>}]}

        Critically: the Channel-2 text marker `<!-- ib:cid=... -->` is part
        of `content_text` and round-trips through both shapes verbatim
        (the regex AGW uses to scan inbound history is shape-agnostic).
        """
        shape = _LLM_SHAPE.get(llm)
        if shape == "openai":
            return [
                {"role": h["role"], "content": h["content_text"]}
                for h in canonical
            ]
        if shape == "anthropic":
            return [
                {
                    "role": h["role"],
                    "content": [{"type": "text", "text": h["content_text"]}],
                }
                for h in canonical
            ]
        raise ValueError(f"combo: unsupported llm shape: {llm}")

    # ── The main turn driver ──

    async def turn(
        self,
        turn_id: str,
        user_msg: str,
        model_override: str | None = None,
    ) -> dict:
        """Drive one turn with the round-robin-selected LLM.

        We use len(self._canonical_history) // 2 as the "turn index" for
        round-robin (each turn appends 2 messages: user + assistant). This
        matches the natural turn-number progression even when the caller
        doesn't pass an explicit index.
        """
        turn_idx = len(self._canonical_history) // 2

        # Append user message to canonical history (provider-agnostic).
        self._canonical_history.append(
            {"role": "user", "content_text": user_msg}
        )

        llm = self._llm_for_turn(turn_idx)
        client = self._clients[llm]
        api_shape = self._to_shape(self._canonical_history, llm)
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
        if shape == "openai":
            resp = await client.chat.completions.create(
                model=model_for_turn,
                messages=api_shape,
                max_tokens=512,
            )
            assistant_text = (resp.choices[0].message.content or "")
        elif shape == "anthropic":
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
        else:
            raise ValueError(f"combo: unreachable shape {shape}")

        # Preserve marker-bearing text in canonical history (the whole
        # point — next turn's shape translator carries this text into the
        # next LLM's request, which AGW scans).
        self._canonical_history.append(
            {"role": "assistant", "content_text": assistant_text}
        )

        # Defense-in-depth: try to capture X-IB-CID from the response.
        # openai-python's BaseModel exposes raw response via different
        # paths in different versions; anthropic-python similarly varies.
        # Both are best-effort; the text marker remains the primary carrier.
        try:
            raw = getattr(resp, "_response", None) or getattr(resp, "response", None)
            headers = getattr(raw, "headers", None) if raw is not None else None
            if headers is not None:
                hval = headers.get("X-IB-CID") or headers.get("x-ib-cid")
                if hval:
                    self._observed_cid_header = hval
        except Exception:
            pass

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

        return {
            "turn_id": turn_id,
            "assistant_msg": assistant_text,
            "tool_calls": [],  # no tool calling in first cut
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
            "framework_events": [
                {
                    "t": f"llm_dispatch_{i}",
                    "llm_for_turn": llm,
                    "model": model_for_turn,
                    "request": copy.deepcopy(ex.get("req")),
                    "response": copy.deepcopy(ex.get("resp")),
                }
                for i, ex in enumerate(turn_exchanges)
            ],
            # Combo-specific surface for the inspector / verdict_k / tests.
            "llm_for_turn": llm,
            "model": model_for_turn,
        }

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
        """E21 — combo has no MCP integration in the first cut. Document
        the no-op so trial scripts can call this defensively.
        """
        return {
            "refresh_tools": "skipped",
            "reason": "combo first-cut has no MCP integration",
        }

    async def aclose(self) -> None:
        """Release the shared hooked httpx client."""
        try:
            await self._http_client.aclose()
        except Exception:
            pass
