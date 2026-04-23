"""Validate a matrix row config; return disabled cells + forced values + runnability."""
from __future__ import annotations

from typing import Any

API_VALID_STATE = {
    "chat": {"valid": [False], "forced": False, "disabled": True},
    "responses": {"valid": [True, False], "forced": None, "disabled": False},
    "responses+conv": {"valid": [True], "forced": True, "disabled": True},
    "messages": {"valid": [False], "forced": False, "disabled": True},
}

# API ↔ provider compatibility matrix.
#  - chat:           ollama, chatgpt, gemini    (real chat-completions providers)
#                    + mock (synthetic, for multi-choice n>1 testing)
#  - responses:      chatgpt only               (OpenAI-only API)
#  - responses+conv: chatgpt only               (OpenAI-only API + previous_response_id)
#  - messages:       claude only                (Anthropic Messages API)
# Note: claude is INTENTIONALLY excluded from chat — Anthropic does not expose
# an OpenAI-compat chat-completions endpoint. mock is included as a chat
# provider since it implements the OpenAI shape for testing only.
API_TO_PROVIDERS = {
    "chat": ["ollama", "mock", "chatgpt", "gemini"],
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

# Which LLM providers actually implement OpenAI Responses API + state
# (previous_response_id semantics). Only chatgpt today; copilot etc come in Plan B.
LLM_SUPPORTS_RESPONSES_STATE = {"chatgpt"}

# Which Plan A adapters implement which APIs.
# Plan A ships only:
#   - adapter-langchain: chat completions only
#   - adapter-direct-mcp: no LLM (auto-selected when llm=NONE)
# Plan B will add adapters for langgraph, crewai, pydantic-ai, autogen,
# llamaindex covering responses / responses+conv / messages.
ADAPTER_CAPABILITIES = {
    "langchain":   {"chat"},
    "direct-mcp":  set(),  # MCP-only adapter, no LLM API
    "langgraph":   {"chat"},  # Plan B T2: create_react_agent, chat-completions only
    "crewai":      {"chat", "messages"},  # Plan B T3: Crew+Agent+Task via native SDKs
    "pydantic-ai": {"chat", "messages", "responses"},  # Plan B T4: typed Agent + Model + Toolset
    # Plan B (not yet built):
    # "autogen":    {"chat", "messages", "responses", "responses+conv"},
    # "llamaindex": {"chat", "responses", "responses+conv"},
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

    # Rule 3a: Per-API state constraints (chat/messages → F forced; responses+conv → T forced)
    if llm != "NONE":
        rules = API_VALID_STATE.get(api, {})
        if rules.get("disabled"):
            disabled.append("state")
            forced["state"] = rules.get("forced")

    # Rule 3b: LLM-level state support — even if API allows state (e.g. responses),
    # the selected LLM may not implement it. Disable + force F in that case so the
    # checkbox doesn't claim an unsupported config.
    if llm != "NONE" and api in ("responses", "responses+conv"):
        if llm not in LLM_SUPPORTS_RESPONSES_STATE:
            if "state" not in disabled:
                disabled.append("state")
            forced["state"] = False
            warnings.append(
                f"llm={llm} does not implement Responses API state — "
                f"select chatgpt to enable state. Row will not be runnable as-is."
            )

    # Rule 4: provider availability (keys detected)
    for provider, env_key in PROVIDER_TO_KEY.items():
        if env_key is None:
            continue
        if not available_keys.get(env_key, True):
            disabled_dropdown_options["llm"].append({
                "id": provider,
                "reason": f"{env_key.upper()}_API_KEY not set in .env",
            })

    # Rule 5: API ↔ LLM compatibility (using API_TO_PROVIDERS).
    # If the selected LLM isn't in the API's supported list, mark unrunnable.
    runnable = True
    if llm != "NONE":
        api_providers = API_TO_PROVIDERS.get(api, [])
        if api_providers and llm not in api_providers:
            runnable = False
            warnings.append(
                f"api={api} is not supported by llm={llm} "
                f"(supported: {', '.join(api_providers)})"
            )

    # Rule 6: Adapter capability — does any Plan A adapter actually implement
    # this (framework, api) combo? Validator may say "messages+claude" is
    # provider-valid, but if no adapter implements messages, the trial WILL
    # 400 from the adapter. Block at the validator instead.
    if llm != "NONE":
        framework = row.get("framework", "langchain")
        adapter_apis = ADAPTER_CAPABILITIES.get(framework, set())
        if api not in adapter_apis:
            runnable = False
            available_apis = ", ".join(sorted(adapter_apis)) or "(none)"
            warnings.append(
                f"Plan A's {framework} adapter does not implement api={api} "
                f"(available: {available_apis}). Plan B will add the missing adapters."
            )

    return {
        "disabled_cells": disabled,
        "forced_values": forced,
        "runnable": runnable,
        "warnings": warnings,
        "disabled_dropdown_options": disabled_dropdown_options,
    }
