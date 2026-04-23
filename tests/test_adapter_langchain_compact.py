"""M7 compact() unit tests — adapters/langchain.

Plan B T10 added `Trial.compact(strategy)` to every adapter. These tests
pin the per-strategy contract so a future framework minor-version upgrade
(e.g. langchain-core changing ToolMessage semantics or AIMessage.tool_calls
metadata shape) fails here instead of only surfacing in a live trial.

Scope — langchain adapter uses langchain_core BaseMessage objects directly
on `Trial.messages`:
  * drop_half — keeps SystemMessages, drops oldest 50% of non-system
  * drop_tool_calls — drops ToolMessage + any AIMessage carrying tool_calls
  * summarize — drop_half + prepends a SystemMessage summary marker
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_ADAPTER_DIR    = str(Path(__file__).resolve().parent.parent / "adapters" / "langchain")
_LANGGRAPH_DIR  = str(Path(__file__).resolve().parent.parent / "adapters" / "langgraph")
_DIRECT_MCP_DIR = str(Path(__file__).resolve().parent.parent / "adapters" / "direct-mcp")


def _ensure_adapter_on_path():
    for other in (_LANGGRAPH_DIR, _DIRECT_MCP_DIR):
        while other in sys.path:
            sys.path.remove(other)
    while _ADAPTER_DIR in sys.path:
        sys.path.remove(_ADAPTER_DIR)
    sys.path.insert(0, _ADAPTER_DIR)
    sys.modules.pop("framework_bridge", None)


def _cfg():
    return {
        "framework": "langchain",
        "api": "chat",
        "stream": False,
        "state": False,
        "llm": "ollama",
        "mcp": "NONE",
        "routing": "via_agw",
        "model": "qwen2.5:7b",
    }


def _seed_messages():
    """Six-message history covering system + user + assistant + tool_call + tool_result."""
    from langchain_core.messages import (
        AIMessage, HumanMessage, SystemMessage, ToolMessage,
    )
    return [
        SystemMessage(content="sys"),
        HumanMessage(content="hi 1"),
        AIMessage(
            content="resp 1",
            tool_calls=[{"id": "tc_1", "name": "weather", "args": {}}],
        ),
        ToolMessage(content="50F", tool_call_id="tc_1"),
        HumanMessage(content="hi 2"),
        AIMessage(content="resp 2"),
    ]


@pytest.fixture
def adapter_env(monkeypatch):
    monkeypatch.setenv("AGW_LLM_BASE_URL_OLLAMA", "http://gateway:8080/llm/ollama/v1")
    monkeypatch.setenv("DEFAULT_OLLAMA_MODEL", "qwen2.5:7b")
    _ensure_adapter_on_path()


async def test_compact_drop_half_keeps_system_shrinks_rest(adapter_env):
    """drop_half preserves SystemMessage, drops oldest 50% of non-system."""
    from langchain_core.messages import SystemMessage
    from framework_bridge import Trial

    trial = Trial(trial_id="t-dh", config=_cfg())
    try:
        trial.messages = _seed_messages()
        before = len(trial.messages)
        out = await trial.compact("drop_half")

        assert out["strategy"] == "drop_half"
        assert out["history_len_before"] == before
        assert out["history_len_after"] < before
        assert out["history_len_after"] == len(trial.messages)
        # System message always preserved.
        assert any(isinstance(m, SystemMessage) for m in trial.messages)
    finally:
        await trial.aclose()


async def test_compact_drop_tool_calls_removes_tool_traces(adapter_env):
    """drop_tool_calls removes ToolMessage AND AIMessage-with-tool_calls."""
    from langchain_core.messages import AIMessage, ToolMessage
    from framework_bridge import Trial

    trial = Trial(trial_id="t-dt", config=_cfg())
    try:
        trial.messages = _seed_messages()
        before = len(trial.messages)
        out = await trial.compact("drop_tool_calls")

        assert out["strategy"] == "drop_tool_calls"
        assert out["history_len_before"] == before
        assert out["history_len_after"] < before
        # No ToolMessage left.
        assert not any(isinstance(m, ToolMessage) for m in trial.messages)
        # No AIMessage carrying tool_calls left.
        assert not any(
            isinstance(m, AIMessage) and getattr(m, "tool_calls", None)
            for m in trial.messages
        )
    finally:
        await trial.aclose()


async def test_compact_summarize_adds_system_marker(adapter_env):
    """summarize = drop_half + a SystemMessage marker prepended to the kept block."""
    from langchain_core.messages import SystemMessage
    from framework_bridge import Trial

    trial = Trial(trial_id="t-sm", config=_cfg())
    try:
        trial.messages = _seed_messages()
        before = len(trial.messages)
        out = await trial.compact("summarize")

        assert out["strategy"] == "summarize"
        assert out["history_len_before"] == before
        assert out["history_len_after"] < before
        # The kept list contains at least one SystemMessage whose content
        # announces it's a synthesized summary marker.
        summary_hits = [
            m for m in trial.messages
            if isinstance(m, SystemMessage) and "summarized" in str(m.content)
        ]
        assert len(summary_hits) == 1
    finally:
        await trial.aclose()
