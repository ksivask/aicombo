"""Langchain-specific adapter logic."""
from __future__ import annotations

import os
from typing import Any

from langchain_openai import ChatOpenAI


def pick_llm_base_url(routing: str, llm: str) -> str:
    env_map_via_agw = {
        "ollama": "AGW_LLM_BASE_URL_OLLAMA",
    }
    env_map_direct = {
        "ollama": "DIRECT_LLM_BASE_URL_OLLAMA",
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


class Trial:
    """Holds per-trial framework state for langchain."""
    def __init__(self, trial_id: str, config: dict):
        self.trial_id = trial_id
        self.config = config
        self.messages: list[dict] = []  # role/content pairs

        base_url = pick_llm_base_url(routing=config["routing"], llm=config["llm"])
        model = config.get("model") or os.environ.get("DEFAULT_OLLAMA_MODEL", "qwen2.5:7b-instruct")
        self.llm = ChatOpenAI(
            base_url=base_url,
            api_key="ollama",  # placeholder; Ollama doesn't validate
            model=model,
            default_headers={},  # populated per-turn in drive_turn
            temperature=0.3,
        )

    async def turn(self, turn_id: str, user_msg: str) -> dict:
        """One turn. Propagates X-Harness-* headers."""
        headers = {
            "X-Harness-Trial-ID": self.trial_id,
            "X-Harness-Turn-ID": turn_id,
        }
        self.llm.default_headers = headers

        self.messages.append({"role": "user", "content": user_msg})

        # For Plan A with MCP=NONE: plain chat
        # For Plan A with MCP=weather: pass tools — but langchain doesn't natively
        #   do MCP; we'd need langchain-mcp adapter. Plan A's MCP seeded row is
        #   primarily for the direct_mcp case (LLM=NONE + MCP=weather); the
        #   langchain+weather row can be a pure chat test where the LLM doesn't
        #   actually invoke tools.
        resp = await self.llm.ainvoke(self.messages)

        assistant_content = resp.content if hasattr(resp, "content") else str(resp)
        self.messages.append({"role": "assistant", "content": assistant_content})

        # Capture request/response at HTTP level. Langchain's OpenAI client
        # doesn't expose this cleanly; we reconstruct from what we know.
        request_captured = {
            "method": "POST",
            "url": f"{self.llm.openai_api_base}/chat/completions",
            "headers": headers,
            "body": {
                "model": self.llm.model_name,
                "messages": self.messages[:-1],  # everything before the assistant reply
            },
        }
        response_captured = {
            "status": 200,
            "headers": {},
            "body": {
                "choices": [{"message": {"content": assistant_content}}],
            },
        }

        return {
            "turn_id": turn_id,
            "assistant_msg": assistant_content,
            "tool_calls": [],
            "request_captured": request_captured,
            "response_captured": response_captured,
            "framework_events": [],
        }
