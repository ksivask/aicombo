"""M7 compact() unit tests — adapters/autogen.

autogen has two compact paths:
  * agent mode (api=chat / api=messages) — pokes AssistantAgent's private
    model_context._messages. Exercised end-to-end by the live smoke suite.
  * responses_direct mode (api=responses / api=responses+conv) — trims
    the `_response_history` chain. Testable here without spinning up a
    real AssistantAgent.

This suite covers the responses_direct path (unit-testable). Agent-mode
compact has no deterministic assertion that doesn't require standing up
an autogen AssistantAgent, so it's intentionally left to live smoke.
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_AUTOGEN_DIR     = str(Path(__file__).resolve().parent.parent / "adapters" / "autogen")
_PYDANTIC_AI_DIR = str(Path(__file__).resolve().parent.parent / "adapters" / "pydantic_ai")
_CREWAI_DIR      = str(Path(__file__).resolve().parent.parent / "adapters" / "crewai")
_LANGCHAIN_DIR   = str(Path(__file__).resolve().parent.parent / "adapters" / "langchain")
_LANGGRAPH_DIR   = str(Path(__file__).resolve().parent.parent / "adapters" / "langgraph")
_DIRECT_MCP_DIR  = str(Path(__file__).resolve().parent.parent / "adapters" / "direct-mcp")


def _ensure_adapter_on_path():
    for other in (_PYDANTIC_AI_DIR, _CREWAI_DIR, _LANGCHAIN_DIR,
                  _LANGGRAPH_DIR, _DIRECT_MCP_DIR):
        while other in sys.path:
            sys.path.remove(other)
    while _AUTOGEN_DIR in sys.path:
        sys.path.remove(_AUTOGEN_DIR)
    sys.path.insert(0, _AUTOGEN_DIR)
    sys.modules.pop("framework_bridge", None)


def _cfg_responses():
    return {
        "framework": "autogen",
        "api": "responses+conv",
        "stream": False,
        "state": True,
        "llm": "chatgpt",
        "mcp": "NONE",
        "routing": "via_agw",
    }


@pytest.fixture
def trial(monkeypatch):
    """Build a real Trial in responses_direct mode with openai.AsyncOpenAI mocked."""
    monkeypatch.setenv("AGW_LLM_BASE_URL_OPENAI", "http://gateway:8080/llm/chatgpt/v1")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-fake")
    _ensure_adapter_on_path()
    import openai
    from framework_bridge import Trial

    with patch.object(openai, "AsyncOpenAI", return_value=MagicMock()):
        t = Trial(trial_id="t-ag-resp", config=_cfg_responses())
    assert t._mode == "responses_direct"
    yield t


@pytest.mark.parametrize("strategy", ["drop_half", "drop_tool_calls", "summarize"])
async def test_compact_responses_direct_trims_response_history(trial, strategy):
    """All three strategies halve _response_history in responses_direct mode.

    The `note` field is always present on this path (the adapter
    acknowledges there's no per-message history — only a response-id chain).
    """
    trial._response_history = ["resp_001", "resp_002", "resp_003", "resp_004"]
    trial._last_response_id = "resp_004"
    before = len(trial._response_history)
    try:
        out = await trial.compact(strategy)
        assert out["strategy"] == strategy
        assert out["history_len_before"] == before
        assert out["history_len_after"] == before - (before // 2)
        assert out["history_len_after"] == len(trial._response_history)
        # The tail of the chain survives.
        assert trial._response_history[-1] == "resp_004"
        # Note field is present (responses_direct always notes the fallback).
        assert "note" in out
        assert "responses_direct" in out["note"] or "response_history" in out["note"]
    finally:
        await trial.aclose()


async def test_compact_responses_direct_empty_history_is_noop(trial):
    """Empty _response_history compacts cleanly (no crash, 0→0)."""
    trial._response_history = []
    trial._last_response_id = None
    try:
        out = await trial.compact("drop_half")
        assert out["history_len_before"] == 0
        assert out["history_len_after"] == 0
        assert trial._last_response_id is None
    finally:
        await trial.aclose()
