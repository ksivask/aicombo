"""Return current LLM provider availability based on env key detection."""
from __future__ import annotations

import os


def get_providers() -> list[dict]:
    """Return list of {id, name, available, unavailable_reason}."""

    def key_set(env: str) -> bool:
        return bool(os.environ.get(env, "").strip())

    return [
        {
            "id": "NONE",
            "name": "NONE (direct MCP only)",
            "available": True,
            "unavailable_reason": None,
        },
        {
            "id": "ollama",
            "name": "Ollama (local)",
            "available": True,
            "unavailable_reason": None,
        },
        {
            "id": "mock",
            "name": "Mock LLM (multi-choice test)",
            "available": True,
            "unavailable_reason": None,
        },
        {
            "id": "claude",
            "name": "claude.ai (Anthropic)",
            "available": key_set("ANTHROPIC_API_KEY"),
            "unavailable_reason":
                None if key_set("ANTHROPIC_API_KEY")
                else "ANTHROPIC_API_KEY not set in .env",
        },
        {
            "id": "chatgpt",
            "name": "chatgpt (OpenAI)",
            "available": key_set("OPENAI_API_KEY"),
            "unavailable_reason":
                None if key_set("OPENAI_API_KEY")
                else "OPENAI_API_KEY not set in .env",
        },
        {
            "id": "gemini",
            "name": "gemini (Google)",
            "available": key_set("GOOGLE_API_KEY"),
            "unavailable_reason":
                None if key_set("GOOGLE_API_KEY")
                else "GOOGLE_API_KEY not set in .env",
        },
    ]
