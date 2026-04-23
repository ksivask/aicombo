"""M7 compact() unit tests — adapters/pydantic_ai.

pydantic-ai's history is a list of `ModelMessage` subclasses (ModelRequest /
ModelResponse with a `parts` list). Walking `parts` to selectively strip
tool-use blocks is fragile across pydantic-ai minor versions, so the
adapter falls back to drop_half for `drop_tool_calls` and `summarize` —
with a `note` in the envelope announcing the fallback.

This test pre-seeds real ModelMessage objects and asserts:
  * drop_half honors the strategy (no note)
  * drop_tool_calls / summarize fall back to drop_half (note present)
  * length invariants hold across all three
"""
from __future__ import annotations

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
    for other in (_CREWAI_DIR, _LANGCHAIN_DIR, _LANGGRAPH_DIR, _DIRECT_MCP_DIR):
        while other in sys.path:
            sys.path.remove(other)
    while _PYDANTIC_AI_DIR in sys.path:
        sys.path.remove(_PYDANTIC_AI_DIR)
    sys.path.insert(0, _PYDANTIC_AI_DIR)
    sys.modules.pop("framework_bridge", None)


def _cfg():
    return {
        "framework": "pydantic-ai",
        "api": "chat",
        "stream": False,
        "state": False,
        "llm": "chatgpt",
        "mcp": "NONE",
        "routing": "via_agw",
    }


def _seed_messages():
    """Six ModelMessages: 3 user requests + 3 assistant responses."""
    from pydantic_ai.messages import (
        ModelRequest, ModelResponse, UserPromptPart, TextPart,
    )
    msgs = []
    for i in range(3):
        msgs.append(ModelRequest(parts=[UserPromptPart(content=f"hi {i}")]))
        msgs.append(ModelResponse(parts=[TextPart(content=f"resp {i}")]))
    return msgs


@pytest.fixture
def trial(monkeypatch):
    """Build a real Trial with _build_model + pydantic_ai.Agent mocked."""
    monkeypatch.setenv("AGW_LLM_BASE_URL_OPENAI", "http://gateway:8080/llm/chatgpt/v1")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-fake")
    _ensure_adapter_on_path()
    import framework_bridge
    from framework_bridge import Trial

    fake_model = MagicMock(name="fake_model")
    fake_agent = MagicMock(name="fake_agent")
    with patch.object(framework_bridge, "_build_model", return_value=fake_model), \
         patch("pydantic_ai.Agent", MagicMock(return_value=fake_agent)):
        t = Trial(trial_id="t-pai", config=_cfg())
    yield t


async def test_compact_drop_half_no_note(trial):
    trial._messages = _seed_messages()
    before = len(trial._messages)
    try:
        out = await trial.compact("drop_half")
        assert out["strategy"] == "drop_half"
        assert out["history_len_before"] == before
        assert out["history_len_after"] < before
        assert out["history_len_after"] == len(trial._messages)
        assert "note" not in out
    finally:
        await trial.aclose()


async def test_compact_drop_tool_calls_falls_back_with_note(trial):
    """drop_tool_calls falls back to drop_half; note field explains why."""
    trial._messages = _seed_messages()
    before = len(trial._messages)
    try:
        out = await trial.compact("drop_tool_calls")
        assert out["strategy"] == "drop_tool_calls"
        assert out["history_len_before"] == before
        assert out["history_len_after"] < before
        assert "note" in out
        assert "drop_half" in out["note"].lower() or "fell back" in out["note"].lower()
    finally:
        await trial.aclose()


async def test_compact_summarize_falls_back_with_note(trial):
    """summarize likewise falls back to drop_half; note field explains why."""
    trial._messages = _seed_messages()
    before = len(trial._messages)
    try:
        out = await trial.compact("summarize")
        assert out["strategy"] == "summarize"
        assert out["history_len_before"] == before
        assert out["history_len_after"] < before
        assert "note" in out
        assert "drop_half" in out["note"].lower() or "fell back" in out["note"].lower()
    finally:
        await trial.aclose()
