"""E5b — adapter/langgraph: api in {chat, messages, responses, responses+conv}.

Constructor/shape tests only — no real LLM traffic. Verifies:
  1. api=chat            → langchain_openai.ChatOpenAI
  2. api=messages        → langchain_anthropic.ChatAnthropic (claude only)
  3. api=messages + llm != claude → ValueError at Trial.__init__
  4. api=responses       → ChatOpenAI with use_responses_api=True (chatgpt only)
  5. api=responses + llm != chatgpt → ValueError at Trial.__init__
  6. api=responses+conv  → same as responses; responses-state chain is
                          initialized + force_state_ref threads _forced_prev_id
                          into the next graph.ainvoke config
  7. compact(responses+conv) drops half of _response_history
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

_ADAPTER_DIR    = str(Path(__file__).resolve().parent.parent / "adapters" / "langgraph")
_LANGCHAIN_DIR  = str(Path(__file__).resolve().parent.parent / "adapters" / "langchain")
_DIRECT_MCP_DIR = str(Path(__file__).resolve().parent.parent / "adapters" / "direct-mcp")


def _ensure_adapter_on_path():
    """Put langgraph's framework_bridge at the front of sys.path.

    Other adapters ship modules with the same name (`framework_bridge`,
    `main`); evict any cached import so the langgraph copy wins.
    """
    for other in (_LANGCHAIN_DIR, _DIRECT_MCP_DIR):
        while other in sys.path:
            sys.path.remove(other)
    while _ADAPTER_DIR in sys.path:
        sys.path.remove(_ADAPTER_DIR)
    sys.path.insert(0, _ADAPTER_DIR)
    sys.modules.pop("framework_bridge", None)


@pytest.fixture
def adapter_env(monkeypatch):
    """Env vars the different api/llm combos need. Set broadly so each
    test only has to choose its config — no per-test env wiring."""
    monkeypatch.setenv("AGW_LLM_BASE_URL_OLLAMA",   "http://agw:8080/llm/ollama/v1")
    monkeypatch.setenv("AGW_LLM_BASE_URL_OPENAI",   "http://agw:8080/llm/chatgpt/v1")
    monkeypatch.setenv("AGW_LLM_BASE_URL_GEMINI",   "http://agw:8080/llm/gemini/v1")
    monkeypatch.setenv("AGW_LLM_BASE_URL_ANTHROPIC","http://agw:8080/llm/claude/v1")
    monkeypatch.setenv("OPENAI_API_KEY",    "sk-test")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.setenv("GOOGLE_API_KEY",    "gk-test")
    monkeypatch.setenv("DEFAULT_OLLAMA_MODEL",   "qwen2.5:7b")
    monkeypatch.setenv("DEFAULT_ANTHROPIC_MODEL","claude-3-5-haiku-20241022")
    _ensure_adapter_on_path()


def _cfg(api: str, llm: str, **overrides) -> dict:
    base = {
        "framework": "langgraph",
        "api": api,
        "stream": False,
        "state": False,
        "llm": llm,
        "mcp": "NONE",
        "routing": "via_agw",
    }
    base.update(overrides)
    return base


async def test_build_llm_chat_returns_chat_openai(adapter_env):
    """api=chat with any LLM → langchain_openai.ChatOpenAI."""
    from langchain_openai import ChatOpenAI
    from framework_bridge import Trial

    trial = Trial(trial_id="t-chat", config=_cfg("chat", "ollama"))
    try:
        assert isinstance(trial.llm, ChatOpenAI)
        # Plain chat must NOT flip the responses-api toggle. The default is
        # None (not False) — any non-True value means the chat/completions
        # endpoint is in effect.
        assert getattr(trial.llm, "use_responses_api", None) is not True
    finally:
        await trial.aclose()


async def test_build_llm_messages_returns_chat_anthropic(adapter_env):
    """api=messages + llm=claude → langchain_anthropic.ChatAnthropic."""
    from langchain_anthropic import ChatAnthropic
    from framework_bridge import Trial

    trial = Trial(trial_id="t-msg", config=_cfg("messages", "claude"))
    try:
        assert isinstance(trial.llm, ChatAnthropic)
        # Base URL from AGW_LLM_BASE_URL_ANTHROPIC lands on anthropic_api_url.
        assert "agw" in str(trial.llm.anthropic_api_url)
    finally:
        await trial.aclose()


async def test_build_llm_messages_rejects_non_claude(adapter_env):
    """api=messages + llm != claude must raise at Trial.__init__."""
    from framework_bridge import Trial

    with pytest.raises(ValueError, match="api=messages requires llm=claude"):
        Trial(trial_id="t-bad", config=_cfg("messages", "chatgpt"))


async def test_build_llm_responses_returns_chat_openai_with_responses_flag(adapter_env):
    """api=responses + llm=chatgpt → ChatOpenAI(use_responses_api=True)."""
    from langchain_openai import ChatOpenAI
    from framework_bridge import Trial

    trial = Trial(trial_id="t-resp", config=_cfg("responses", "chatgpt"))
    try:
        assert isinstance(trial.llm, ChatOpenAI)
        assert trial.llm.use_responses_api is True
    finally:
        await trial.aclose()


async def test_build_llm_responses_rejects_non_chatgpt(adapter_env):
    """api=responses + llm != chatgpt must raise at Trial.__init__."""
    from framework_bridge import Trial

    with pytest.raises(ValueError, match="requires llm=chatgpt"):
        Trial(trial_id="t-bad", config=_cfg("responses", "claude"))


async def test_build_llm_responses_conv_wires_state_chain(adapter_env):
    """api=responses+conv seeds the state chain + threads previous_response_id.

    Walks one turn() through a stubbed graph.ainvoke whose AIMessage carries
    response_metadata={"id": ...}, confirms _last_response_id + history
    are populated, then verifies the NEXT ainvoke receives that id via
    config={"configurable": {"previous_response_id": ...}}.
    """
    from framework_bridge import Trial

    fake_ai_1 = MagicMock()
    fake_ai_1.__class__.__name__ = "AIMessage"
    fake_ai_1.content = "turn 1 reply"
    fake_ai_1.tool_calls = []
    fake_ai_1.response_metadata = {"id": "resp_001"}

    fake_ai_2 = MagicMock()
    fake_ai_2.__class__.__name__ = "AIMessage"
    fake_ai_2.content = "turn 2 reply"
    fake_ai_2.tool_calls = []
    fake_ai_2.response_metadata = {"id": "resp_002"}

    fake_human = MagicMock()
    fake_human.__class__.__name__ = "HumanMessage"

    fake_graph = MagicMock()
    # ainvoke is called twice; return different payloads each time.
    fake_graph.ainvoke = AsyncMock(side_effect=[
        {"messages": [fake_human, fake_ai_1]},
        {"messages": [fake_human, fake_ai_1, fake_human, fake_ai_2]},
    ])

    with patch("framework_bridge.create_react_agent", return_value=fake_graph):
        trial = Trial(
            trial_id="t-rc",
            config=_cfg("responses+conv", "chatgpt"),
        )
        # State chain should be empty at construction.
        assert trial._last_response_id is None
        assert trial._response_history == []

        try:
            out1 = await trial.turn("turn-0", "hi")
            # Envelope carries the response id back to the caller.
            assert out1["_response_id"] == "resp_001"
            assert trial._last_response_id == "resp_001"
            assert trial._response_history == ["resp_001"]

            # First ainvoke must NOT have a config (no prev id yet).
            first_call = fake_graph.ainvoke.call_args_list[0]
            assert "config" not in first_call.kwargs

            out2 = await trial.turn("turn-1", "followup")
            assert out2["_response_id"] == "resp_002"
            assert trial._last_response_id == "resp_002"
            assert trial._response_history == ["resp_001", "resp_002"]

            # Second ainvoke MUST include previous_response_id = resp_001
            # (the prior turn's id, threaded via config.configurable).
            second_call = fake_graph.ainvoke.call_args_list[1]
            cfg = second_call.kwargs.get("config") or {}
            assert cfg.get("configurable", {}).get("previous_response_id") == "resp_001"
        finally:
            await trial.aclose()


async def test_compact_responses_conv_drops_half_of_response_history(adapter_env):
    """compact(drop_half) on api=responses+conv shrinks _response_history."""
    from framework_bridge import Trial

    trial = Trial(
        trial_id="t-compact",
        config=_cfg("responses+conv", "chatgpt"),
    )
    try:
        trial._response_history = ["r1", "r2", "r3", "r4"]
        trial._last_response_id = "r4"
        before = len(trial._response_history)

        out = await trial.compact("drop_half")

        assert out["strategy"] == "drop_half"
        assert out["history_len_before"] == before
        assert out["history_len_after"] == len(trial._response_history)
        assert out["history_len_after"] < before
        # _last_response_id must still be a live entry in the chain.
        assert trial._last_response_id in trial._response_history
    finally:
        await trial.aclose()


async def test_compact_responses_conv_summarize_falls_back_with_note(adapter_env):
    """compact(summarize) on responses+conv prunes + emits a note."""
    from framework_bridge import Trial

    trial = Trial(
        trial_id="t-compact-sum",
        config=_cfg("responses+conv", "chatgpt"),
    )
    try:
        trial._response_history = ["r1", "r2", "r3", "r4"]
        trial._last_response_id = "r4"
        before = len(trial._response_history)

        out = await trial.compact("summarize")

        assert out["strategy"] == "summarize"
        assert out["history_len_before"] == before
        assert out["history_len_after"] < before
        assert "note" in out
        assert "summarize" in out["note"]
    finally:
        await trial.aclose()
