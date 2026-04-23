"""M7 compact() unit tests — adapters/crewai.

crewai's conversation history is a list of `{"role": str, "content": str}`
dicts (the adapter prepends them into each kickoff Task description).
The dict schema does not carry tool_calls metadata, so:
  * drop_half — straight slice, keeps tail
  * drop_tool_calls — falls back to drop_half (envelope carries `note`)
  * summarize — drop_half + prepends a synthesized system-role dict
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_CREWAI_DIR      = str(Path(__file__).resolve().parent.parent / "adapters" / "crewai")
_LANGCHAIN_DIR   = str(Path(__file__).resolve().parent.parent / "adapters" / "langchain")
_LANGGRAPH_DIR   = str(Path(__file__).resolve().parent.parent / "adapters" / "langgraph")
_DIRECT_MCP_DIR  = str(Path(__file__).resolve().parent.parent / "adapters" / "direct-mcp")


def _ensure_adapter_on_path():
    for other in (_LANGCHAIN_DIR, _LANGGRAPH_DIR, _DIRECT_MCP_DIR):
        while other in sys.path:
            sys.path.remove(other)
    while _CREWAI_DIR in sys.path:
        sys.path.remove(_CREWAI_DIR)
    sys.path.insert(0, _CREWAI_DIR)
    sys.modules.pop("framework_bridge", None)


def _cfg():
    return {
        "framework": "crewai",
        "api": "chat",
        "stream": False,
        "state": False,
        "llm": "chatgpt",
        "mcp": "NONE",
        "routing": "via_agw",
    }


def _seed_messages():
    return [
        {"role": "system",    "content": "sys"},
        {"role": "user",      "content": "hi 1"},
        {"role": "assistant", "content": "resp 1"},
        {"role": "user",      "content": "hi 2"},
        {"role": "assistant", "content": "resp 2"},
    ]


@pytest.fixture
def trial(monkeypatch):
    """Build a real Trial with crewai.LLM + rebuild_openai_clients mocked out."""
    monkeypatch.setenv("AGW_LLM_BASE_URL_OPENAI", "http://gateway:8080/llm/chatgpt/v1")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-fake")
    _ensure_adapter_on_path()
    from framework_bridge import Trial

    fake_llm = MagicMock()
    fake_llm.__class__.__name__ = "OpenAICompletion"
    with patch("framework_bridge.LLM", MagicMock(return_value=fake_llm)), \
         patch("framework_bridge._rebuild_openai_clients"):
        t = Trial(trial_id="t-crewai", config=_cfg())
    yield t
    # Cleanup happens in the test body via `await trial.aclose()`; this
    # fixture is sync so tests close the async resource themselves.


async def test_compact_drop_half(trial):
    trial._messages = _seed_messages()
    before = len(trial._messages)
    try:
        out = await trial.compact("drop_half")
        assert out["strategy"] == "drop_half"
        assert out["history_len_before"] == before
        assert out["history_len_after"] < before
        assert out["history_len_after"] == len(trial._messages)
        # No note for drop_half (it's the honest path).
        assert "note" not in out
    finally:
        await trial.aclose()


async def test_compact_drop_tool_calls_falls_back_with_note(trial):
    """drop_tool_calls is a fallback on crewai (dict schema has no tool_calls).

    Contract: still shrinks, and the envelope carries a `note` explaining
    the fallback so callers know this adapter didn't honor the strategy
    literally.
    """
    trial._messages = _seed_messages()
    before = len(trial._messages)
    try:
        out = await trial.compact("drop_tool_calls")
        assert out["strategy"] == "drop_tool_calls"
        assert out["history_len_before"] == before
        assert out["history_len_after"] < before
        assert "note" in out
        # Note mentions the fallback reason.
        assert "drop_half" in out["note"].lower() or "fell back" in out["note"].lower()
    finally:
        await trial.aclose()


async def test_compact_summarize_prepends_system_dict(trial):
    """summarize drops oldest half and prepends a role=system summary dict."""
    trial._messages = _seed_messages()
    before = len(trial._messages)
    try:
        out = await trial.compact("summarize")
        assert out["strategy"] == "summarize"
        assert out["history_len_before"] == before
        assert out["history_len_after"] <= before
        # First message is the synthesized system summary.
        first = trial._messages[0]
        assert first["role"] == "system"
        assert "summarized" in first["content"]
    finally:
        await trial.aclose()
