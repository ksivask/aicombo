"""Tests for adapters/langchain — framework bridge logic (offline)."""
import os
import sys
from pathlib import Path

_ADAPTER_DIR = str(Path(__file__).resolve().parent.parent / "adapters" / "langchain")
_DIRECT_MCP_DIR = str(Path(__file__).resolve().parent.parent / "adapters" / "direct-mcp")


def _ensure_adapter_on_path():
    # Make the adapter module importable. Kept inside the test functions
    # (not at module level) to avoid shadowing harness/main.py during full
    # test suite collection.
    # Evict direct-mcp's framework_bridge (same module name) so our import wins.
    while _DIRECT_MCP_DIR in sys.path:
        sys.path.remove(_DIRECT_MCP_DIR)
    while _ADAPTER_DIR in sys.path:
        sys.path.remove(_ADAPTER_DIR)
    sys.path.insert(0, _ADAPTER_DIR)
    sys.modules.pop("framework_bridge", None)


def test_pick_llm_base_url_via_agw_ollama(monkeypatch):
    _ensure_adapter_on_path()
    monkeypatch.setenv("AGW_LLM_BASE_URL_OLLAMA", "http://agentgateway:8080/llm/ollama/v1")
    from framework_bridge import pick_llm_base_url
    url = pick_llm_base_url(routing="via_agw", llm="ollama")
    assert "agentgateway" in url and "ollama" in url


def test_pick_llm_base_url_direct_ollama(monkeypatch):
    _ensure_adapter_on_path()
    monkeypatch.setenv("DIRECT_LLM_BASE_URL_OLLAMA", "http://host.docker.internal:11434/v1")
    from framework_bridge import pick_llm_base_url
    url = pick_llm_base_url(routing="direct", llm="ollama")
    assert "host.docker.internal" in url or "11434" in url


def test_pick_mcp_base_url_via_agw_weather(monkeypatch):
    _ensure_adapter_on_path()
    monkeypatch.setenv("AGW_MCP_WEATHER", "http://agentgateway:8080/mcp/weather")
    from framework_bridge import pick_mcp_base_url
    url = pick_mcp_base_url(routing="via_agw", mcp="weather")
    assert "agentgateway" in url and "weather" in url


def test_pick_mcp_base_url_direct_weather(monkeypatch):
    _ensure_adapter_on_path()
    monkeypatch.setenv("DIRECT_MCP_WEATHER", "http://mcp-weather:8000/mcp")
    from framework_bridge import pick_mcp_base_url
    url = pick_mcp_base_url(routing="direct", mcp="weather")
    assert "mcp-weather" in url
