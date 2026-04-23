"""Tests for adapters/langgraph — framework bridge + create_react_agent glue."""
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

_LANGGRAPH_DIR = str(Path(__file__).resolve().parent.parent / "adapters" / "langgraph")
_LANGCHAIN_DIR = str(Path(__file__).resolve().parent.parent / "adapters" / "langchain")
_DIRECT_MCP_DIR = str(Path(__file__).resolve().parent.parent / "adapters" / "direct-mcp")


def _ensure_adapter_on_path():
    """Put langgraph's framework_bridge at the front of sys.path.

    Other adapters (langchain, direct-mcp) ship modules with the same
    name; we evict any cached import so this one wins.
    """
    for other in (_LANGCHAIN_DIR, _DIRECT_MCP_DIR):
        while other in sys.path:
            sys.path.remove(other)
    while _LANGGRAPH_DIR in sys.path:
        sys.path.remove(_LANGGRAPH_DIR)
    sys.path.insert(0, _LANGGRAPH_DIR)
    sys.modules.pop("framework_bridge", None)


def test_pick_llm_base_url_via_agw_ollama(monkeypatch):
    _ensure_adapter_on_path()
    monkeypatch.setenv("AGW_LLM_BASE_URL_OLLAMA", "http://agentgateway:8080/llm/ollama/v1")
    from framework_bridge import pick_llm_base_url
    url = pick_llm_base_url(routing="via_agw", llm="ollama")
    assert "agentgateway" in url


def test_pick_llm_base_url_via_agw_chatgpt(monkeypatch):
    _ensure_adapter_on_path()
    monkeypatch.setenv("AGW_LLM_BASE_URL_OPENAI", "http://agentgateway:8080/llm/chatgpt/v1")
    from framework_bridge import pick_llm_base_url
    url = pick_llm_base_url(routing="via_agw", llm="chatgpt")
    assert "chatgpt" in url


def test_pick_mcp_base_url_direct(monkeypatch):
    _ensure_adapter_on_path()
    monkeypatch.setenv("DIRECT_MCP_FETCH", "http://mcp-fetch:8000/mcp")
    from framework_bridge import pick_mcp_base_url
    url = pick_mcp_base_url(routing="direct", mcp="fetch")
    assert "mcp-fetch" in url


@pytest.mark.asyncio
async def test_trial_chat_only_invokes_graph_once(monkeypatch):
    """No MCP → graph built with empty tools → one graph.ainvoke → assistant reply."""
    _ensure_adapter_on_path()
    monkeypatch.setenv("AGW_LLM_BASE_URL_OLLAMA", "http://agentgateway:8080/llm/ollama/v1")
    from framework_bridge import Trial

    # Stub create_react_agent + its ainvoke.
    fake_graph = MagicMock()
    fake_ai_msg = MagicMock()
    fake_ai_msg.__class__.__name__ = "AIMessage"
    fake_ai_msg.content = "hello!"
    fake_ai_msg.tool_calls = []
    fake_human = MagicMock()
    fake_human.__class__.__name__ = "HumanMessage"
    fake_graph.ainvoke = AsyncMock(return_value={"messages": [fake_human, fake_ai_msg]})

    with patch("framework_bridge.create_react_agent", return_value=fake_graph):
        trial = Trial(trial_id="t0", config={
            "framework": "langgraph", "api": "chat",
            "stream": False, "state": False,
            "llm": "ollama", "mcp": "NONE", "routing": "via_agw",
        })
        try:
            out = await trial.turn("turn-0", "hi")
            assert out["assistant_msg"] == "hello!"
            fake_graph.ainvoke.assert_awaited_once()
        finally:
            await trial.aclose()


@pytest.mark.asyncio
async def test_trial_with_mcp_loads_tools_and_builds_graph(monkeypatch):
    """mcp=fetch → _setup_mcp_tools fires → graph built with those tools."""
    _ensure_adapter_on_path()
    monkeypatch.setenv("AGW_LLM_BASE_URL_OLLAMA", "http://agentgateway:8080/llm/ollama/v1")
    monkeypatch.setenv("AGW_MCP_FETCH", "http://agentgateway:8080/mcp/fetch")
    from framework_bridge import Trial

    fake_tool = MagicMock()
    fake_tool.name = "fetch_fetch"
    fake_ai = MagicMock()
    fake_ai.__class__.__name__ = "AIMessage"
    fake_ai.content = "done"
    fake_ai.tool_calls = []
    fake_graph = MagicMock()
    fake_graph.ainvoke = AsyncMock(return_value={"messages": [fake_ai]})

    with patch("langchain_mcp_adapters.tools.load_mcp_tools",
               AsyncMock(return_value=[fake_tool])), \
         patch("framework_bridge.create_react_agent", return_value=fake_graph) as mock_rea:
        trial = Trial(trial_id="t1", config={
            "framework": "langgraph", "api": "chat",
            "stream": False, "state": False,
            "llm": "ollama", "mcp": "fetch", "routing": "via_agw",
        })
        try:
            await trial.turn("turn-0", "fetch https://example.com")
            # create_react_agent invoked with our tool in the tools list.
            assert mock_rea.call_count >= 1
            call_args = mock_rea.call_args
            tools_arg = call_args[0][1] if len(call_args[0]) > 1 else call_args[1].get("tools")
            assert tools_arg is not None and fake_tool in tools_arg
        finally:
            await trial.aclose()
