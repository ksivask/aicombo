"""Curated model list per LLM provider, surfaced via GET /providers/{id}/models.

Pure-data module — no I/O at import time. Env override via the
``{PROVIDER}_MODELS`` env var (comma-separated) — surrenders metadata
since env can't carry capability flags.

Bumped as providers release/retire models. Companion to
``harness/providers.py`` (which lists providers + key availability) and
the ``DEFAULT_<PROVIDER>_MODEL`` env scalars (which select the runner's
default when a row's ``model`` field is empty).
"""
from __future__ import annotations

import os
from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class ModelInfo:
    id: str
    display: str
    tier: str  # "cheap" | "mid" | "reasoning" | "custom"
    supports_tools: bool = True
    supports_responses_api: bool = False


# Initial curated set — mirrors the table in docs/enhancements.md E9.
# Bump as providers release/retire models. The env override path
# (``{PROVIDER}_MODELS``) is the escape hatch when a new model drops
# before this dict catches up.
CURATED: dict[str, list[ModelInfo]] = {
    "ollama": [
        ModelInfo("qwen2.5:7b",      "Qwen 2.5 7B",     "cheap"),
        ModelInfo("llama3.1:8b",     "Llama 3.1 8B",    "mid"),
        ModelInfo("llama3.1:latest", "Llama 3.1 latest", "mid"),
        ModelInfo("mistral:7b",      "Mistral 7B",      "mid"),
    ],
    "mock": [
        ModelInfo("mock", "mock-llm", "cheap"),
    ],
    "chatgpt": [
        ModelInfo("gpt-4o-mini", "GPT-4o Mini",          "cheap",
                  supports_responses_api=True),
        ModelInfo("gpt-4o",      "GPT-4o",               "mid",
                  supports_responses_api=True),
        ModelInfo("o1-mini",     "o1-mini (reasoning)",  "reasoning",
                  supports_tools=False),
    ],
    "claude": [
        ModelInfo("claude-haiku-4-5",  "Haiku 4.5",  "cheap"),
        ModelInfo("claude-sonnet-4-6", "Sonnet 4.6", "mid"),
        ModelInfo("claude-opus-4-7",   "Opus 4.7",   "reasoning"),
    ],
    "gemini": [
        ModelInfo("gemini-2.0-flash",              "Gemini 2.0 Flash",         "cheap"),
        ModelInfo("gemini-2.0-pro",                "Gemini 2.0 Pro",           "mid"),
        ModelInfo("gemini-2.0-flash-thinking-exp", "Gemini 2.0 Flash Thinking", "reasoning"),
    ],
}


def get_models(provider: str) -> list[ModelInfo]:
    """Return models available for a provider. Env override wins.

    Env shape: ``<PROVIDER>_MODELS=id1,id2,id3`` (e.g. ``CLAUDE_MODELS=...``).
    Env-supplied entries lose metadata — display defaults to id, tier="custom".
    Empty list for unknown providers (UI can fall back to a free-text input).
    """
    env_var = f"{provider.upper()}_MODELS"
    raw = os.environ.get(env_var)
    if raw:
        return [
            ModelInfo(id=m.strip(), display=m.strip(), tier="custom")
            for m in raw.split(",")
            if m.strip()
        ]
    return CURATED.get(provider, [])


def to_jsonable(models: list[ModelInfo]) -> list[dict]:
    """Convert for HTTP envelope. Dataclass → dict (preserves all fields)."""
    return [asdict(m) for m in models]
