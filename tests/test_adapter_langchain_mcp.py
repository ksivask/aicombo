"""Tests for langchain MCP integration in the langchain adapter.

These are offline unit tests — every external dependency (fastmcp,
the MCP SDK, langchain-mcp-adapters' tool factory, ChatOpenAI's
`ainvoke`) is mocked so the suite runs without network or a real LLM.
The shape we exercise:

  Trial.__init__ → ChatOpenAI created lazily
    Trial.turn(turn_id, user_msg)
      ├─ if mcp != NONE: _setup_mcp_tools (only on first turn)
      │     └─ load_mcp_tools(connection=conn) — patched
      ├─ llm_to_use.ainvoke(messages) — patched
      ├─ for each tool_call → tool.ainvoke(args) — patched
      └─ next llm.ainvoke OR break

We validate:
  * mcp=NONE skips MCP setup entirely (chat-only path unchanged).
  * mcp=<name> populates `_mcp_tools` and uses bound LLM.
  * agent loop terminates immediately when LLM returns no tool_calls.
  * agent loop executes a tool call and feeds result back to LLM.
  * agent loop hits hop limit cleanly (logs hop_limit_reached event).
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

_ADAPTER_DIR = str(Path(__file__).resolve().parent.parent / "adapters" / "langchain")
_DIRECT_MCP_DIR = str(Path(__file__).resolve().parent.parent / "adapters" / "direct-mcp")


def _ensure_adapter_on_path():
    """Insert the langchain adapter dir onto sys.path; evict the direct-mcp
    one (which has the same `framework_bridge` module name).
    """
    while _DIRECT_MCP_DIR in sys.path:
        sys.path.remove(_DIRECT_MCP_DIR)
    while _ADAPTER_DIR in sys.path:
        sys.path.remove(_ADAPTER_DIR)
    sys.path.insert(0, _ADAPTER_DIR)
    sys.modules.pop("framework_bridge", None)


@pytest.fixture
def adapter_env(monkeypatch):
    """Set every env var the adapter needs so Trial.__init__ doesn't bail."""
    monkeypatch.setenv("AGW_LLM_BASE_URL_OLLAMA", "http://gateway:8080/llm/ollama/v1")
    monkeypatch.setenv("AGW_MCP_FETCH", "http://gateway:8080/mcp/fetch")
    monkeypatch.setenv("AGW_MCP_WEATHER", "http://gateway:8080/mcp/weather")
    monkeypatch.setenv("DIRECT_LLM_BASE_URL_OLLAMA", "http://host:11434/v1")
    monkeypatch.setenv("DEFAULT_OLLAMA_MODEL", "qwen2.5:7b")
    _ensure_adapter_on_path()


def _cfg(mcp="NONE", routing="via_agw", llm="ollama"):
    return {
        "framework": "langchain",
        "api": "chat",
        "stream": False,
        "state": False,
        "llm": llm,
        "mcp": mcp,
        "routing": routing,
        "model": "qwen2.5:7b",
    }


async def test_trial_with_mcp_none_skips_mcp_setup(adapter_env):
    """mcp=NONE: _setup_mcp_tools is a no-op; _mcp_tools stays None."""
    from framework_bridge import Trial

    trial = Trial(trial_id="t-none", config=_cfg(mcp="NONE"))
    try:
        # mcp_url should be empty for NONE
        assert trial.mcp_url == ""
        # _setup_mcp_tools is safe to call; should be a no-op
        await trial._setup_mcp_tools({"X-Harness-Trial-ID": "t-none"})
        assert trial._mcp_tools is None
        assert trial._llm_with_tools is None
    finally:
        await trial.aclose()


async def test_trial_with_mcp_loads_tools(adapter_env):
    """mcp=fetch: _setup_mcp_tools populates _mcp_tools via load_mcp_tools."""
    from framework_bridge import Trial
    from langchain_openai import ChatOpenAI

    trial = Trial(trial_id="t-mcp", config=_cfg(mcp="fetch"))
    try:
        # Mock langchain_mcp_adapters.tools.load_mcp_tools to return one
        # fake StructuredTool-like object (only `.name` is read by the adapter).
        fake_tool = MagicMock()
        fake_tool.name = "fetch_url"
        fake_tool.ainvoke = AsyncMock(return_value="<html>ok</html>")
        # ChatOpenAI is a pydantic model — instance-level attribute writes
        # raise. Patch bind_tools at the class level for the test scope so
        # every ChatOpenAI instance picks it up.
        with patch("langchain_mcp_adapters.tools.load_mcp_tools",
                   AsyncMock(return_value=[fake_tool])) as loader, \
             patch.object(ChatOpenAI, "bind_tools",
                          MagicMock(return_value=MagicMock(name="bound_llm"))):
            await trial._setup_mcp_tools({"X-Harness-Trial-ID": "t-mcp"})

        assert loader.await_count == 1
        # connection-based call: session=None and a streamable_http connection dict
        args, kwargs = loader.call_args
        assert kwargs["session"] is None
        conn = kwargs["connection"]
        assert conn["transport"] == "streamable_http"
        assert conn["url"] == "http://gateway:8080/mcp/fetch"
        assert "httpx_client_factory" in conn

        assert trial._mcp_tools == [fake_tool]
        assert trial._llm_with_tools is not None
        # Idempotent: second call doesn't re-fetch
        with patch("langchain_mcp_adapters.tools.load_mcp_tools",
                   AsyncMock(return_value=[])) as loader2:
            await trial._setup_mcp_tools({"X-Harness-Trial-ID": "t-mcp"})
            assert loader2.await_count == 0
    finally:
        await trial.aclose()


async def test_agent_loop_terminates_on_no_tool_calls(adapter_env):
    """LLM returns no tool_calls → loop exits after first hop."""
    from framework_bridge import Trial
    from langchain_openai import ChatOpenAI

    trial = Trial(trial_id="t-noTC", config=_cfg(mcp="NONE"))
    try:
        # AIMessage-shape mock: content=str, tool_calls=[]
        fake_resp = MagicMock()
        fake_resp.content = "Sure, I can help with that."
        fake_resp.tool_calls = []
        with patch.object(ChatOpenAI, "ainvoke",
                          AsyncMock(return_value=fake_resp)) as mock_ainvoke:
            out = await trial.turn("turn-001", "hello there")
            assert mock_ainvoke.await_count == 1

        assert out["assistant_msg"] == "Sure, I can help with that."
        assert out["tool_calls"] == []
        # Exactly ONE LLM hop event; no mcp_tool_call events
        kinds = [e.get("t") for e in out["framework_events"]]
        assert kinds == ["llm_hop_0"]
    finally:
        await trial.aclose()


async def test_agent_loop_executes_tool_call_then_followup(adapter_env):
    """Hop0: LLM emits tool_call → adapter invokes tool → Hop1: LLM emits final text.

    Verifies the canonical multi-step agent-loop flow: tools_list event +
    llm_hop_0 + mcp_tool_call + llm_hop_1, terminating with text content.
    """
    from framework_bridge import Trial

    trial = Trial(trial_id="t-loop", config=_cfg(mcp="fetch"))
    try:
        # Build one fake tool the LLM can "call".
        fake_tool = MagicMock()
        fake_tool.name = "fetch_url"
        fake_tool.ainvoke = AsyncMock(return_value="page body bytes")

        # Hop 0: LLM emits a tool_call
        hop0 = MagicMock()
        hop0.content = ""
        hop0.tool_calls = [{
            "name": "fetch_url",
            "args": {"url": "https://example.com"},
            "id": "call_42",
        }]
        # Hop 1: LLM produces final text
        hop1 = MagicMock()
        hop1.content = "Fetched. Page is the IANA example domain."
        hop1.tool_calls = []
        bound_llm = MagicMock()
        bound_llm.ainvoke = AsyncMock(side_effect=[hop0, hop1])

        from langchain_openai import ChatOpenAI
        with patch("langchain_mcp_adapters.tools.load_mcp_tools",
                   AsyncMock(return_value=[fake_tool])), \
             patch.object(ChatOpenAI, "bind_tools",
                          MagicMock(return_value=bound_llm)):
            out = await trial.turn("turn-002", "fetch https://example.com")

        assert out["assistant_msg"] == "Fetched. Page is the IANA example domain."
        # bound LLM was hit twice (tool_call + followup)
        assert bound_llm.ainvoke.await_count == 2
        # tool was invoked once with the LLM's args
        fake_tool.ainvoke.assert_awaited_once_with({"url": "https://example.com"})
        # framework_events: tools_list (if last_request is set), llm_hop_0, mcp_tool_call, llm_hop_1
        kinds = [e.get("t") for e in out["framework_events"]]
        # tools_list MAY or MAY NOT be present depending on whether the
        # mocked load_mcp_tools triggered an httpx event hook. The
        # essential ordering for the agent loop must be present.
        assert "llm_hop_0" in kinds
        assert "mcp_tool_call" in kinds
        assert "llm_hop_1" in kinds
        assert kinds.index("llm_hop_0") < kinds.index("mcp_tool_call") < kinds.index("llm_hop_1")
        # The mcp_tool_call event captures tool metadata
        tool_ev = next(e for e in out["framework_events"] if e.get("t") == "mcp_tool_call")
        assert tool_ev["tool_name"] == "fetch_url"
        assert tool_ev["args"] == {"url": "https://example.com"}
        assert "page body" in tool_ev["result_summary"]
    finally:
        await trial.aclose()


async def test_agent_loop_hits_hop_limit(adapter_env):
    """LLM keeps emitting tool_calls forever → adapter caps at MAX_LLM_HOPS."""
    from framework_bridge import MAX_LLM_HOPS, Trial

    trial = Trial(trial_id="t-cap", config=_cfg(mcp="fetch"))
    try:
        fake_tool = MagicMock()
        fake_tool.name = "fetch_url"
        fake_tool.ainvoke = AsyncMock(return_value="ok")

        # Every LLM hop emits ANOTHER tool_call → loop never naturally exits.
        def make_hop():
            r = MagicMock()
            r.content = ""
            r.tool_calls = [{"name": "fetch_url", "args": {"url": "x"}, "id": "c"}]
            return r

        bound_llm = MagicMock()
        bound_llm.ainvoke = AsyncMock(side_effect=[make_hop() for _ in range(MAX_LLM_HOPS + 2)])

        from langchain_openai import ChatOpenAI
        with patch("langchain_mcp_adapters.tools.load_mcp_tools",
                   AsyncMock(return_value=[fake_tool])), \
             patch.object(ChatOpenAI, "bind_tools",
                          MagicMock(return_value=bound_llm)):
            out = await trial.turn("turn-003", "loop forever please")

        # Capped: bound LLM invoked exactly MAX_LLM_HOPS times.
        assert bound_llm.ainvoke.await_count == MAX_LLM_HOPS
        kinds = [e.get("t") for e in out["framework_events"]]
        assert "hop_limit_reached" in kinds
        # llm_hop_0..MAX_LLM_HOPS-1 all present
        for i in range(MAX_LLM_HOPS):
            assert f"llm_hop_{i}" in kinds
    finally:
        await trial.aclose()
