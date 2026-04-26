"""M7 compact() unit tests — adapters/llamaindex.

Two modes:
  * chat — self._messages is a list of ChatMessage objects; MessageRole
    enum distinguishes SYSTEM / USER / ASSISTANT / TOOL.
  * responses_direct — self._response_history is a list of response-id
    strings (same as the autogen responses_direct path).

This suite covers:
  * drop_half keeps SYSTEM, drops oldest 50% of non-system (chat mode)
  * drop_tool_calls filters role=TOOL messages (chat mode)
  * summarize prepends a SYSTEM summary marker (chat mode)
  * responses_direct compact trims _response_history chain
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_LLAMAINDEX_DIR  = str(Path(__file__).resolve().parent.parent / "adapters" / "llamaindex")
_AUTOGEN_DIR     = str(Path(__file__).resolve().parent.parent / "adapters" / "autogen")
_PYDANTIC_AI_DIR = str(Path(__file__).resolve().parent.parent / "adapters" / "pydantic_ai")
_CREWAI_DIR      = str(Path(__file__).resolve().parent.parent / "adapters" / "crewai")
_LANGCHAIN_DIR   = str(Path(__file__).resolve().parent.parent / "adapters" / "langchain")
_LANGGRAPH_DIR   = str(Path(__file__).resolve().parent.parent / "adapters" / "langgraph")
_DIRECT_MCP_DIR  = str(Path(__file__).resolve().parent.parent / "adapters" / "direct-mcp")


def _ensure_adapter_on_path():
    for other in (_AUTOGEN_DIR, _PYDANTIC_AI_DIR, _CREWAI_DIR,
                  _LANGCHAIN_DIR, _LANGGRAPH_DIR, _DIRECT_MCP_DIR):
        while other in sys.path:
            sys.path.remove(other)
    while _LLAMAINDEX_DIR in sys.path:
        sys.path.remove(_LLAMAINDEX_DIR)
    sys.path.insert(0, _LLAMAINDEX_DIR)
    sys.modules.pop("framework_bridge", None)


def _cfg_chat():
    return {
        "framework": "llamaindex",
        "api": "chat",
        "stream": False,
        "state": False,
        "llm": "chatgpt",
        "mcp": "NONE",
        "routing": "via_agw",
    }


def _cfg_responses():
    """B-NEW-1: pin the chain-trim path on api=responses+state=T (E13a),
    NOT on api=responses+conv. After B-NEW-1, +conv compact is an
    honest no-op (continuity lives in the OpenAI conversation container,
    not in `_response_history`). The chain-trim assertions below only
    apply to chain mode."""
    return {
        "framework": "llamaindex",
        "api": "responses",
        "stream": False,
        "state": True,
        "llm": "chatgpt",
        "mcp": "NONE",
        "routing": "via_agw",
    }


def _seed_chat_messages():
    from llama_index.core.base.llms.types import ChatMessage, MessageRole
    return [
        ChatMessage(role=MessageRole.SYSTEM,    content="sys"),
        ChatMessage(role=MessageRole.USER,      content="hi 1"),
        ChatMessage(role=MessageRole.ASSISTANT, content="resp 1"),
        ChatMessage(
            role=MessageRole.TOOL,
            content="tool result",
            additional_kwargs={"tool_call_id": "tc_1"},
        ),
        ChatMessage(role=MessageRole.USER,      content="hi 2"),
        ChatMessage(role=MessageRole.ASSISTANT, content="resp 2"),
    ]


@pytest.fixture
def chat_trial(monkeypatch):
    """Build a real Trial in chat mode with _build_chat_llm mocked."""
    monkeypatch.setenv("AGW_LLM_BASE_URL_OPENAI", "http://gateway:8080/llm/chatgpt/v1")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-fake")
    _ensure_adapter_on_path()
    import framework_bridge
    from framework_bridge import Trial

    fake_llm = MagicMock(name="fake_llm")
    with patch.object(framework_bridge, "_build_chat_llm", return_value=fake_llm):
        t = Trial(trial_id="t-li-chat", config=_cfg_chat())
    assert t._mode == "chat"
    yield t


@pytest.fixture
def responses_trial(monkeypatch):
    """Build a real Trial in responses_direct mode with openai.AsyncOpenAI mocked."""
    monkeypatch.setenv("AGW_LLM_BASE_URL_OPENAI", "http://gateway:8080/llm/chatgpt/v1")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-fake")
    _ensure_adapter_on_path()
    import openai
    from framework_bridge import Trial

    with patch.object(openai, "AsyncOpenAI", return_value=MagicMock()):
        t = Trial(trial_id="t-li-resp", config=_cfg_responses())
    assert t._mode == "responses_direct"
    yield t


async def test_compact_chat_drop_half_keeps_system(chat_trial):
    from llama_index.core.base.llms.types import MessageRole

    chat_trial._messages = _seed_chat_messages()
    before = len(chat_trial._messages)
    try:
        out = await chat_trial.compact("drop_half")
        assert out["strategy"] == "drop_half"
        assert out["history_len_before"] == before
        assert out["history_len_after"] < before
        # SYSTEM message survives.
        assert any(m.role == MessageRole.SYSTEM for m in chat_trial._messages)
    finally:
        await chat_trial.aclose()


async def test_compact_chat_drop_tool_calls_filters_role_tool(chat_trial):
    """drop_tool_calls must remove every ChatMessage with role=MessageRole.TOOL.

    This is the exact regression scenario that would surface if
    llama-index renamed MessageRole.TOOL in a minor version.
    """
    from llama_index.core.base.llms.types import MessageRole

    chat_trial._messages = _seed_chat_messages()
    before = len(chat_trial._messages)
    assert any(m.role == MessageRole.TOOL for m in chat_trial._messages)
    try:
        out = await chat_trial.compact("drop_tool_calls")
        assert out["strategy"] == "drop_tool_calls"
        assert out["history_len_before"] == before
        # Exactly 1 TOOL-role message dropped from the six-message seed.
        assert out["history_len_after"] == before - 1
        assert not any(m.role == MessageRole.TOOL for m in chat_trial._messages)
    finally:
        await chat_trial.aclose()


async def test_compact_chat_summarize_prepends_system_marker(chat_trial):
    from llama_index.core.base.llms.types import MessageRole

    chat_trial._messages = _seed_chat_messages()
    before = len(chat_trial._messages)
    try:
        out = await chat_trial.compact("summarize")
        assert out["strategy"] == "summarize"
        assert out["history_len_before"] == before
        # One SYSTEM message announces it's a summary of dropped messages.
        summaries = [
            m for m in chat_trial._messages
            if m.role == MessageRole.SYSTEM and "summarized" in str(m.content)
        ]
        assert len(summaries) == 1
    finally:
        await chat_trial.aclose()


@pytest.mark.parametrize("strategy", ["drop_half", "drop_tool_calls", "summarize"])
async def test_compact_responses_direct_trims_chain(responses_trial, strategy):
    """responses_direct: all three strategies halve _response_history + set note."""
    responses_trial._response_history = ["resp_1", "resp_2", "resp_3", "resp_4"]
    responses_trial._last_response_id = "resp_4"
    before = len(responses_trial._response_history)
    try:
        out = await responses_trial.compact(strategy)
        assert out["strategy"] == strategy
        assert out["history_len_before"] == before
        assert out["history_len_after"] == before - (before // 2)
        assert responses_trial._response_history[-1] == "resp_4"
        assert "note" in out
    finally:
        await responses_trial.aclose()


# ── B-NEW-1 regression: +conv compact must be an honest no-op ──

@pytest.mark.parametrize("strategy", ["drop_half", "drop_tool_calls", "summarize"])
async def test_compact_responses_conv_is_honest_noop(monkeypatch, strategy):
    """B-NEW-1 regression: +conv compact must report 'no client-side history'
    not the misleading 'compacted _response_history chain instead'.

    Pre-fix the llamaindex adapter's `_compact_responses()` fell through
    to the chain-trim logic for +conv mode and emitted a `note` claiming
    it had compacted the chain. But +conv keeps `_response_history` empty
    (continuity lives server-side in the conversation container) so the
    note was always false. Mirrors the existing langchain +conv branch."""
    monkeypatch.setenv("AGW_LLM_BASE_URL_OPENAI", "http://gateway:8080/llm/chatgpt/v1")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-fake")
    _ensure_adapter_on_path()
    import openai
    from framework_bridge import Trial

    cfg = {
        "framework": "llamaindex",
        "api": "responses+conv",
        "stream": False,
        "state": True,
        "llm": "chatgpt",
        "mcp": "NONE",
        "routing": "via_agw",
    }
    with patch.object(openai, "AsyncOpenAI", return_value=MagicMock()):
        trial = Trial(trial_id="t-li-conv-compact", config=cfg)
    try:
        # Simulate an already-minted conversation container so the note
        # references it (matches the langchain regression's shape).
        trial._conversation_id = "conv_test_xxx"

        out = await trial.compact(strategy)
        assert out["strategy"] == strategy
        assert out["history_len_before"] == 0
        assert out["history_len_after"] == 0
        note = out.get("note", "").lower()
        assert "conversation container" in note or "no client-side history" in note, (
            f"misleading note for +conv compact: {out.get('note')!r}"
        )
        # Anti-regression: must NOT mention "chain" (the misleading old text).
        assert "chain" not in note, (
            f"+conv mode misreports as chain-mode: {note!r}"
        )
        # Must mention the conversation id so operators can correlate.
        assert "conv_test_xxx" in out["note"]
    finally:
        await trial.aclose()
