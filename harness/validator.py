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

# E19 — frameworks whose adapter knows how to merge tool sets from
# multiple MCP servers (list-form `mcp` in RowConfig). Initially empty:
# the schema lands now so adapter wiring can opt in incrementally without
# a second migration. When an adapter learns multi-MCP merging, add its
# framework name here and the validator will accept list-form for it.
MULTI_MCP_FRAMEWORKS: set[str] = set()

# E23 — frameworks whose adapter knows how to dispatch across multiple
# LLMs (list-form `llm` in RowConfig). The combo adapter (E24) is the
# only opt-in today; its round-robin per-turn dispatch consumes the list.
# Other adapters can opt in later by adding themselves here.
MULTI_LLM_FRAMEWORKS: set[str] = {"combo"}

# Which Plan A adapters implement which APIs.
# Plan A ships only:
#   - adapter-langchain: chat completions only
#   - adapter-direct-mcp: no LLM (auto-selected when llm=NONE)
# Plan B will add adapters for langgraph, crewai, pydantic-ai, autogen,
# llamaindex covering responses / responses+conv / messages.
ADAPTER_CAPABILITIES = {
    "langchain":   {"chat", "messages", "responses", "responses+conv"},  # E5a: ChatOpenAI / ChatAnthropic / ChatOpenAI(use_responses_api=True)
    "direct-mcp":  set(),  # MCP-only adapter, no LLM API
    "langgraph":   {"chat", "messages", "responses", "responses+conv"},  # E5b: same langchain wrappers via create_react_agent
    "crewai":      {"chat", "messages"},  # Plan B T3: Crew+Agent+Task via native SDKs (responses+conv via bypass — E5c)
    "pydantic-ai": {"chat", "messages", "responses"},  # Plan B T4: typed Agent + Model + Toolset (responses+conv via bypass — E5d)
    "autogen":     {"chat", "messages", "responses", "responses+conv"},  # Plan B T5: AssistantAgent + openai responses bypass
    "llamaindex":  {"chat", "responses", "responses+conv"},  # Plan B T6: llama_index OpenAI + openai responses bypass (messages via E5e)
    "combo":       {"chat", "messages"},  # E24: multi-LLM-same-CID first cut (no MCP, no tool calling, no responses APIs yet — see E24a/b/c)
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
    # E23: when llm is a list (multi-LLM form), this string-keyed lookup is
    # ill-defined; skip it. The list-form block below enforces per-element
    # API compatibility; state-mode constraints are handled per LLM at
    # adapter time.
    if isinstance(llm, str) and llm != "NONE" and api in ("responses", "responses+conv"):
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
    # E23: list-form `llm` is checked per-element by the dedicated block
    # below (which also enforces MULTI_LLM_FRAMEWORKS opt-in); skip the
    # string-only path here so a list value doesn't yield a confusing
    # "[provider list] not in api_providers" warning.
    runnable = True
    if isinstance(llm, str) and llm != "NONE":
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

    # Rule 7 (E19): list-form `mcp` requires a multi-MCP-aware adapter.
    # MULTI_MCP_FRAMEWORKS is intentionally empty in the first cut — schema
    # lands now, adapters opt in later. A list value with framework not in
    # the set is non-runnable so users get a clear "not wired up yet"
    # signal instead of an opaque adapter error mid-trial.
    if isinstance(mcp, list):
        framework = row.get("framework", "langchain")
        if framework not in MULTI_MCP_FRAMEWORKS:
            runnable = False
            supported = sorted(MULTI_MCP_FRAMEWORKS) or "none yet"
            warnings.append(
                f"framework={framework} doesn't support multi-MCP form "
                f"(mcp as list); supported: {supported}"
            )
        for m in mcp:
            if m == "NONE":
                warnings.append(
                    "mcp list contains 'NONE' — drop it (use empty list "
                    "or single 'NONE' instead)"
                )

    # Rule 8 (E23): list-form `llm` requires a multi-LLM-aware adapter
    # (combo only today). Each element must be API-compatible per
    # API_TO_PROVIDERS, and a sibling list-form `model` must match length.
    if isinstance(llm, list):
        framework = row.get("framework", "langchain")
        if framework not in MULTI_LLM_FRAMEWORKS:
            runnable = False
            warnings.append(
                f"framework={framework} doesn't support multi-LLM form "
                f"(llm as list); supported: {sorted(MULTI_LLM_FRAMEWORKS)}"
            )
        api_providers = API_TO_PROVIDERS.get(api, [])
        for entry in llm:
            if entry == "NONE":
                continue
            if api_providers and entry not in api_providers:
                runnable = False
                warnings.append(
                    f"llm list contains {entry} which is not compatible with "
                    f"api={api} (supported: {', '.join(api_providers)})"
                )
        model = row.get("model")
        if isinstance(model, list) and len(model) != len(llm):
            runnable = False
            warnings.append(
                f"model list length ({len(model)}) must match llm list "
                f"length ({len(llm)})"
            )

    return {
        "disabled_cells": disabled,
        "forced_values": forced,
        "runnable": runnable,
        "warnings": warnings,
        "disabled_dropdown_options": disabled_dropdown_options,
    }
