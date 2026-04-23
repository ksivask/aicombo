"""M7 compact() unit tests — adapters/direct-mcp.

direct-mcp has no LLM conversation history (every turn is a fresh
tools/list + tools/call), so compact is a documented no-op. The endpoint
exists only for HTTP-contract parity with the other six adapters.

This test pins the envelope shape for all three strategies:
  {strategy, note, history_len_before: 0, history_len_after: 0}
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_ADAPTER_DIR    = str(Path(__file__).resolve().parent.parent / "adapters" / "direct-mcp")
_LANGCHAIN_DIR  = str(Path(__file__).resolve().parent.parent / "adapters" / "langchain")
_LANGGRAPH_DIR  = str(Path(__file__).resolve().parent.parent / "adapters" / "langgraph")


def _ensure_adapter_on_path():
    for other in (_LANGCHAIN_DIR, _LANGGRAPH_DIR):
        while other in sys.path:
            sys.path.remove(other)
    while _ADAPTER_DIR in sys.path:
        sys.path.remove(_ADAPTER_DIR)
    sys.path.insert(0, _ADAPTER_DIR)
    sys.modules.pop("framework_bridge", None)


def _cfg():
    return {
        "framework": "direct-mcp",
        "api": "chat",
        "stream": False,
        "state": False,
        "llm": "ollama",
        "mcp": "weather",
        "routing": "via_agw",
    }


@pytest.fixture
def trial(monkeypatch):
    monkeypatch.setenv("AGW_MCP_WEATHER", "http://gateway:8080/mcp/weather")
    _ensure_adapter_on_path()
    from framework_bridge import Trial

    yield Trial(trial_id="t-directmcp", config=_cfg())


@pytest.mark.parametrize("strategy", ["drop_half", "drop_tool_calls", "summarize"])
async def test_compact_is_noop_for_all_strategies(trial, strategy):
    """No LLM history → compact always returns 0→0 with an explanatory note."""
    try:
        out = await trial.compact(strategy)
        assert out["strategy"] == strategy
        assert out["history_len_before"] == 0
        assert out["history_len_after"] == 0
        assert "note" in out
        assert "no LLM history" in out["note"]
    finally:
        await trial.aclose()
