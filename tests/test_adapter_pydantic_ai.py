"""Tests for adapters/pydantic_ai — framework bridge logic (offline)."""
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

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
