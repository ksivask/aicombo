"""LLM/key provider matrix for the langchain adapter.

Pins the chatgpt + gemini wiring added when the langchain adapter was
retrofitted with multi-provider support (Plan B never reached this
adapter originally, so a row config of llm=chatgpt + framework=langchain
crashed Trial.__init__ with a 500 from pick_llm_base_url's KeyError).

Covers:
  - pick_llm_base_url for ollama, chatgpt, gemini under via_agw routing
  - explicit rejection for an unknown provider name
  - _pick_api_key for ollama (placeholder), chatgpt (env-sourced),
    and the missing-env-var error path
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_ADAPTER_DIR    = str(Path(__file__).resolve().parent.parent / "adapters" / "langchain")
_LANGGRAPH_DIR  = str(Path(__file__).resolve().parent.parent / "adapters" / "langgraph")
_DIRECT_MCP_DIR = str(Path(__file__).resolve().parent.parent / "adapters" / "direct-mcp")


def _ensure_adapter_on_path():
    """Force `framework_bridge` to resolve to the langchain adapter copy.

    Multiple adapters ship a top-level module named `framework_bridge`;
    we prune any sibling adapter dirs from sys.path + drop a stale cache
    so the import in each test resolves to the langchain bridge.
    """
    for other in (_LANGGRAPH_DIR, _DIRECT_MCP_DIR):
        while other in sys.path:
            sys.path.remove(other)
    while _ADAPTER_DIR in sys.path:
        sys.path.remove(_ADAPTER_DIR)
    sys.path.insert(0, _ADAPTER_DIR)
    sys.modules.pop("framework_bridge", None)


def test_pick_llm_base_url_ollama_via_agw(monkeypatch):
    _ensure_adapter_on_path()
    from framework_bridge import pick_llm_base_url

    monkeypatch.setenv("AGW_LLM_BASE_URL_OLLAMA", "http://agentgateway:8080/llm/ollama/v1")
    assert pick_llm_base_url("via_agw", "ollama") == "http://agentgateway:8080/llm/ollama/v1"


def test_pick_llm_base_url_chatgpt_via_agw(monkeypatch):
    _ensure_adapter_on_path()
    from framework_bridge import pick_llm_base_url

    monkeypatch.setenv("AGW_LLM_BASE_URL_OPENAI", "http://agentgateway:8080/llm/chatgpt/v1")
    assert pick_llm_base_url("via_agw", "chatgpt") == "http://agentgateway:8080/llm/chatgpt/v1"


def test_pick_llm_base_url_gemini_via_agw(monkeypatch):
    _ensure_adapter_on_path()
    from framework_bridge import pick_llm_base_url

    monkeypatch.setenv(
        "AGW_LLM_BASE_URL_GEMINI",
        "http://agentgateway:8080/llm/gemini/v1beta/openai",
    )
    assert (
        pick_llm_base_url("via_agw", "gemini")
        == "http://agentgateway:8080/llm/gemini/v1beta/openai"
    )


def test_pick_llm_base_url_unknown_llm_raises():
    _ensure_adapter_on_path()
    from framework_bridge import pick_llm_base_url

    with pytest.raises(ValueError, match="no LLM base URL mapping"):
        pick_llm_base_url("via_agw", "unknown_provider_42")


def test_pick_api_key_ollama_returns_placeholder():
    _ensure_adapter_on_path()
    from framework_bridge import _pick_api_key

    # ChatOpenAI requires SOMETHING non-empty for api_key; Ollama doesn't
    # validate it, so a fixed placeholder is the contract here.
    assert _pick_api_key("ollama") == "placeholder"


def test_pick_api_key_chatgpt_reads_env(monkeypatch):
    _ensure_adapter_on_path()
    from framework_bridge import _pick_api_key

    monkeypatch.setenv("OPENAI_API_KEY", "sk-test123")
    assert _pick_api_key("chatgpt") == "sk-test123"


def test_pick_api_key_chatgpt_raises_when_missing(monkeypatch):
    _ensure_adapter_on_path()
    from framework_bridge import _pick_api_key

    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(ValueError, match="OPENAI_API_KEY"):
        _pick_api_key("chatgpt")
