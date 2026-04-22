"""Validate a matrix row config; return disabled cells + forced values + runnability."""
from __future__ import annotations

from typing import Any

API_VALID_STATE = {
    "chat": {"valid": [False], "forced": False, "disabled": True},
    "responses": {"valid": [True, False], "forced": None, "disabled": False},
    "responses+conv": {"valid": [True], "forced": True, "disabled": True},
    "messages": {"valid": [False], "forced": False, "disabled": True},
}

API_TO_PROVIDERS = {
    "chat": ["ollama", "mock", "claude", "chatgpt", "gemini"],
    "responses": ["chatgpt"],
    "responses+conv": ["chatgpt"],
    "messages": ["claude"],
}

PROVIDER_TO_KEY = {
    "ollama": None,          # no key required
    "mock": None,            # compose-internal mock_llm service
    "chatgpt": "openai",
    "claude": "anthropic",
    "gemini": "google",
}


def validate(row: dict[str, Any], available_keys: dict[str, bool] | None = None) -> dict[str, Any]:
    """Validate a row config. Returns disabled_cells, forced_values, runnable, disabled_dropdown_options."""
    available_keys = available_keys or {}
    disabled: list[str] = []
    forced: dict[str, Any] = {}
    warnings: list[str] = []
    disabled_dropdown_options: dict[str, list[dict[str, str]]] = {"llm": []}

    llm = row.get("llm", "NONE")
    mcp = row.get("mcp", "NONE")
    api = row.get("api", "chat")
    state = row.get("state", False)

    # Rule 1: LLM=NONE disables api/provider/stream/state (direct-MCP only mode)
    if llm == "NONE":
        for cell in ("api", "stream", "state", "provider"):
            disabled.append(cell)

    # Rule 2: LLM=NONE AND MCP=NONE → not runnable
    if llm == "NONE" and mcp == "NONE":
        return {
            "disabled_cells": disabled,
            "forced_values": forced,
            "runnable": False,
            "warnings": ["LLM=NONE AND MCP=NONE is not a valid combination"],
            "disabled_dropdown_options": disabled_dropdown_options,
        }

    # Rule 3: Per-API state constraints
    if llm != "NONE":
        rules = API_VALID_STATE.get(api, {})
        if rules.get("disabled"):
            disabled.append("state")
            forced["state"] = rules.get("forced")

    # Rule 4: provider availability (keys detected)
    for provider, env_key in PROVIDER_TO_KEY.items():
        if env_key is None:
            continue
        if not available_keys.get(env_key, True):
            disabled_dropdown_options["llm"].append({
                "id": provider,
                "reason": f"{env_key.upper()}_API_KEY not set in .env",
            })

    return {
        "disabled_cells": disabled,
        "forced_values": forced,
        "runnable": True,
        "warnings": warnings,
        "disabled_dropdown_options": disabled_dropdown_options,
    }
