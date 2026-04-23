"""Tests for adapters/crewai — framework bridge logic (offline)."""
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_CREWAI_DIR = str(Path(__file__).resolve().parent.parent / "adapters" / "crewai")
_LANGCHAIN_DIR = str(Path(__file__).resolve().parent.parent / "adapters" / "langchain")
_LANGGRAPH_DIR = str(Path(__file__).resolve().parent.parent / "adapters" / "langgraph")
_DIRECT_MCP_DIR = str(Path(__file__).resolve().parent.parent / "adapters" / "direct-mcp")


def _ensure_adapter_on_path():
    """Put crewai's framework_bridge at the front of sys.path; evict others."""
    for other in (_LANGCHAIN_DIR, _LANGGRAPH_DIR, _DIRECT_MCP_DIR):
        while other in sys.path:
            sys.path.remove(other)
    while _CREWAI_DIR in sys.path:
        sys.path.remove(_CREWAI_DIR)
    sys.path.insert(0, _CREWAI_DIR)
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


def test_llm_model_string_chat_chatgpt():
    _ensure_adapter_on_path()
    from framework_bridge import _llm_model_string
    s = _llm_model_string({"api": "chat", "llm": "chatgpt", "model": None})
    assert s.startswith("openai/")


def test_llm_model_string_messages_claude():
    _ensure_adapter_on_path()
    from framework_bridge import _llm_model_string
    s = _llm_model_string({"api": "messages", "llm": "claude", "model": None})
    assert s.startswith("anthropic/")


@pytest.mark.asyncio
async def test_trial_create_and_close_sets_up_state(monkeypatch):
    """Smoke: Trial init + aclose without making real LLM calls.

    We mock crewai.LLM so construction doesn't require a real endpoint or
    valid API key. Verifies that state is initialized (empty messages list,
    hooks wired) and aclose releases the httpx clients.
    """
    _ensure_adapter_on_path()
    monkeypatch.setenv("AGW_LLM_BASE_URL_OPENAI", "http://agentgateway:8080/llm/chatgpt/v1")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-fake")
    from framework_bridge import Trial

    # Mock crewai.LLM so it doesn't try real init.
    fake_llm = MagicMock()
    fake_llm.__class__.__name__ = "OpenAICompletion"
    with patch("framework_bridge.LLM", MagicMock(return_value=fake_llm)) as MockLLM, \
         patch("framework_bridge._rebuild_openai_clients") as MockRebuild:
        trial = Trial(trial_id="t-init", config={
            "framework": "crewai", "api": "chat",
            "stream": False, "state": False,
            "llm": "chatgpt", "mcp": "NONE", "routing": "via_agw",
        })
        try:
            assert trial.trial_id == "t-init"
            assert trial._messages == []
            assert trial._http_exchanges == []
            MockLLM.assert_called_once()
            # Rebuild should have been attempted once (OpenAICompletion path).
            assert MockRebuild.call_count == 1
        finally:
            await trial.aclose()
