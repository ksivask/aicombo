"""Tests for adapters/pydantic_ai — framework bridge logic (offline)."""
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

_PYDANTIC_AI_DIR = str(Path(__file__).resolve().parent.parent / "adapters" / "pydantic_ai")
_CREWAI_DIR      = str(Path(__file__).resolve().parent.parent / "adapters" / "crewai")
_LANGCHAIN_DIR   = str(Path(__file__).resolve().parent.parent / "adapters" / "langchain")
_LANGGRAPH_DIR   = str(Path(__file__).resolve().parent.parent / "adapters" / "langgraph")
_DIRECT_MCP_DIR  = str(Path(__file__).resolve().parent.parent / "adapters" / "direct-mcp")


def _ensure_adapter_on_path():
    """Put pydantic_ai's framework_bridge at the front of sys.path; evict others."""
    for other in (_CREWAI_DIR, _LANGCHAIN_DIR, _LANGGRAPH_DIR, _DIRECT_MCP_DIR):
        while other in sys.path:
            sys.path.remove(other)
    while _PYDANTIC_AI_DIR in sys.path:
        sys.path.remove(_PYDANTIC_AI_DIR)
    sys.path.insert(0, _PYDANTIC_AI_DIR)
    sys.modules.pop("framework_bridge", None)


def test_pick_llm_base_url_via_agw_chatgpt(monkeypatch):
    _ensure_adapter_on_path()
    monkeypatch.setenv("AGW_LLM_BASE_URL_OPENAI", "http://agentgateway:8080/llm/chatgpt/v1")
    from framework_bridge import pick_llm_base_url
    url = pick_llm_base_url(routing="via_agw", llm="chatgpt")
    assert "chatgpt" in url


def test_pick_llm_base_url_via_agw_claude(monkeypatch):
    _ensure_adapter_on_path()
    monkeypatch.setenv("AGW_LLM_BASE_URL_ANTHROPIC", "http://agentgateway:8080/llm/claude")
    from framework_bridge import pick_llm_base_url
    url = pick_llm_base_url(routing="via_agw", llm="claude")
    assert "claude" in url


def test_default_model_name_picks_per_api_and_llm():
    """All three APIs resolve to a sensible default model identifier."""
    _ensure_adapter_on_path()
    from framework_bridge import _default_model_name
    # chat + ollama uses the DEFAULT_OLLAMA_MODEL env (default qwen2.5:7b)
    assert _default_model_name("chat", "ollama", None) != ""
    # messages + claude
    assert _default_model_name("messages", "claude", None).startswith("claude-")
    # responses + chatgpt
    assert _default_model_name("responses", "chatgpt", None).startswith("gpt-")
    # Explicit model wins
    assert _default_model_name("chat", "chatgpt", "gpt-4o") == "gpt-4o"


def test_build_mcp_servers_empty_when_no_url():
    """mcp='NONE' → no MCPServerStreamableHTTP constructed."""
    _ensure_adapter_on_path()
    from framework_bridge import _build_mcp_servers
    headers = {}
    out = _build_mcp_servers(mcp_url="", headers_ref=headers, http_client=MagicMock())
    assert out == []


@pytest.mark.asyncio
async def test_trial_create_and_close_sets_up_state(monkeypatch):
    """Smoke: Trial init + aclose without making real LLM calls.

    We mock pydantic_ai.Agent + _build_model so construction doesn't try
    real network activity. Verifies that state is initialized (empty
    messages list, shared headers dict wired to http_client) and aclose
    releases the httpx client.
    """
    _ensure_adapter_on_path()
    monkeypatch.setenv("AGW_LLM_BASE_URL_OPENAI", "http://agentgateway:8080/llm/chatgpt/v1")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-fake")
    import framework_bridge
    from framework_bridge import Trial

    fake_model = MagicMock(name="fake_model")
    fake_agent = MagicMock(name="fake_agent")
    with patch.object(framework_bridge, "_build_model", return_value=fake_model) as MockBuild, \
         patch("pydantic_ai.Agent", MagicMock(return_value=fake_agent)) as MockAgent:
        trial = Trial(trial_id="t-init", config={
            "framework": "pydantic-ai", "api": "chat",
            "stream": False, "state": False,
            "llm": "chatgpt", "mcp": "NONE", "routing": "via_agw",
        })
        try:
            assert trial.trial_id == "t-init"
            assert trial._messages == []
            assert trial._http_exchanges == []
            # Mutable shared headers dict is the same object that httpx
            # received — mutating it propagates to future requests.
            assert "X-Harness-Trial-ID" in trial._headers
            assert trial._headers["X-Harness-Trial-ID"] == "t-init"
            MockBuild.assert_called_once()
            MockAgent.assert_called_once()
        finally:
            await trial.aclose()


# ── E13a regression: state=True on api=responses must thread previous_response_id ──

@pytest.mark.asyncio
async def test_state_true_on_api_responses_threads_previous_response_id(monkeypatch):
    """E13a: api=responses + state=True must chain previous_response_id.

    pydantic-ai's `OpenAIResponsesModel` does NOT auto-thread
    previous_response_id (unlike langchain-openai's
    `use_previous_response_id=True`). E13a adds explicit threading via
    `OpenAIResponsesModelSettings(openai_previous_response_id=...)`
    passed as `model_settings=` on `agent.run()`.

    Before E13a: pydantic-ai had NO previous_response_id mechanism in
    its adapter at all, and full message_history replay was the only
    path — so state=True was a silent no-op (just full-history replay
    with the state checkbox cosmetic).

    We mock the Agent so `agent.run(**kwargs)` captures kwargs; we
    pre-seed `_last_response_id` on the Trial and assert that the
    next turn() call passes a `model_settings` carrying that id.
    """
    _ensure_adapter_on_path()
    monkeypatch.setenv("AGW_LLM_BASE_URL_OPENAI", "http://agentgateway:8080/llm/chatgpt/v1")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-fake")
    import framework_bridge
    from framework_bridge import Trial

    captured: dict = {}

    async def fake_run(**kwargs):
        captured["kwargs"] = kwargs
        result = MagicMock(name="fake_result")
        result.output = "next-reply"
        result.all_messages = MagicMock(return_value=[])
        return result

    fake_model = MagicMock(name="fake_model")
    fake_agent = MagicMock(name="fake_agent")
    fake_agent.run = fake_run

    with patch.object(framework_bridge, "_build_model", return_value=fake_model), \
         patch("pydantic_ai.Agent", MagicMock(return_value=fake_agent)):
        trial = Trial(trial_id="t-state-t-pa", config={
            "framework": "pydantic-ai",
            "api": "responses",       # ← NOT responses+conv
            "stream": False,
            "state": True,             # ← E13a: chain via prev-id
            "llm": "chatgpt",
            "mcp": "NONE",
            "routing": "via_agw",
        })
        try:
            # Pre-seed _last_response_id (simulates a captured prior turn).
            trial._last_response_id = "resp_PRIOR"

            await trial.turn("t1", "next")

            assert "kwargs" in captured, "agent.run was never called"
            settings = captured["kwargs"].get("model_settings")
            assert settings is not None, (
                "state=True on api=responses should pass model_settings= "
                "with openai_previous_response_id; got no model_settings."
            )
            # `OpenAIResponsesModelSettings` is a TypedDict — can be accessed
            # via dict subscript.
            got = (settings or {}).get("openai_previous_response_id") \
                if hasattr(settings, "get") else getattr(settings, "openai_previous_response_id", None)
            assert got == "resp_PRIOR", (
                f"openai_previous_response_id should be 'resp_PRIOR'; "
                f"got {got!r}. Before E13a pydantic-ai had no "
                "previous_response_id threading at all."
            )
            # And: in state-chain mode we must NOT replay message_history
            # (the whole point is the server holds prior state).
            assert captured["kwargs"].get("message_history") is None, (
                "state-chain mode must not replay message_history; got "
                f"{captured['kwargs'].get('message_history')!r}"
            )
        finally:
            await trial.aclose()
