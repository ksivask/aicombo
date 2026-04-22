"""Langchain-specific adapter logic."""
from __future__ import annotations

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


class Trial:
    """Holds per-trial framework state for langchain.

    Uses httpx event hooks to capture the ACTUAL HTTP request + response
    bytes going over the wire — essential for cidgar pedagogy (diff what
    the framework sent vs. what cidgar returned).
    """

    def __init__(self, trial_id: str, config: dict):
        self.trial_id = trial_id
        self.config = config
        self.messages: list[dict] = []  # role/content pairs

        # Per-turn capture slots. Populated by httpx event hooks.
        self._last_request: dict | None = None
        self._last_response: dict | None = None

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

    async def turn(self, turn_id: str, user_msg: str) -> dict:
        """One turn. Propagates X-Harness-* headers and captures wire bytes."""
        headers = {
            "X-Harness-Trial-ID": self.trial_id,
            "X-Harness-Turn-ID": turn_id,
        }
        # Replace default_headers so each turn's X-Harness-Turn-ID updates
        self.llm.default_headers = headers

        # Reset capture slots for this turn
        self._last_request = None
        self._last_response = None

        self.messages.append({"role": "user", "content": user_msg})

        # Invoke. Event hooks populate self._last_request and self._last_response.
        resp = await self.llm.ainvoke(self.messages)

        assistant_content = resp.content if hasattr(resp, "content") else str(resp)
        # Preserve tool_calls / additional_kwargs if present (Plan B will use these)
        tool_calls = []
        if hasattr(resp, "tool_calls") and resp.tool_calls:
            tool_calls = [
                {"name": tc.get("name"), "args": tc.get("args", {}), "id": tc.get("id")}
                for tc in resp.tool_calls
            ]

        self.messages.append({"role": "assistant", "content": assistant_content})

        request_captured = self._last_request or {
            "method": "POST",
            "url": f"{self.llm.openai_api_base}/chat/completions",
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
            "assistant_msg": assistant_content,
            "tool_calls": tool_calls,
            "request_captured": request_captured,
            "response_captured": response_captured,
            "framework_events": [],
        }

    async def aclose(self) -> None:
        """Release httpx client connections."""
        try:
            await self._http_client.aclose()
        except Exception:
            pass
