"""E5b — adapter/langgraph: api in {chat, messages, responses, responses+conv}.

Constructor/shape tests only — no real LLM traffic. Verifies:
  1. api=chat            → langchain_openai.ChatOpenAI
  2. api=messages        → langchain_anthropic.ChatAnthropic (claude only)
  3. api=messages + llm != claude → ValueError at Trial.__init__
  4. api=responses       → ChatOpenAI with use_responses_api=True (chatgpt only)
  5. api=responses + llm != chatgpt → ValueError at Trial.__init__
  6. api=responses+conv  → same as responses + per-turn .bind(conversation=...)
                          with the lazy-minted conv_xxx id (E13b)
  7. compact(responses+conv) is a no-op (Conversations API container
                          has no client-side trim primitive)
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


async def test_build_llm_messages_installs_hooked_anthropic_clients(adapter_env):
    """api=messages: _install_anthropic_hooked_clients must have run, so the
    anthropic SDK's _async_client uses our hooked httpx.AsyncClient.

    This pins the E5a pattern port: ChatAnthropic's @cached_property
    descriptors for _client / _async_client are overridden before first
    read, so LLM wire bytes flow through the shared httpx hooks.
    """
    import anthropic
    from framework_bridge import Trial

    trial = Trial(trial_id="t-msg-hook", config=_cfg("messages", "claude"))
    try:
        # cached_property reads inst.__dict__ first — these are the instances
        # we installed, not lazily-built ones.
        assert isinstance(trial.llm._async_client, anthropic.AsyncClient)
        assert isinstance(trial.llm._client, anthropic.Client)
        # The async SDK client's underlying httpx.AsyncClient must be our
        # hooked one (same object identity — not a copy).
        assert trial.llm._async_client._client is trial._http_client
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


async def test_build_llm_responses_conv_wires_conv_container(adapter_env):
    """E13b: api=responses+conv binds `conversation:{id: conv_xxx}` per
    turn via .bind() on the ChatOpenAI, NOT previous_response_id chain.

    Walks two turns via stubbed graph.ainvoke + a stubbed
    _ensure_conversation_id helper that returns a fixed conv_xxx id.
    Each turn must rebuild the graph with a llm bound to the conversation
    kwarg; the SAME conv_xxx is used on every turn (lazy-mint, then
    cache).
    """
    from framework_bridge import Trial
    from langchain_core.runnables import RunnableBinding

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

    graph0 = MagicMock()  # constructor builds an unbound graph (never invoked)
    graph1 = MagicMock()
    graph1.ainvoke = AsyncMock(
        return_value={"messages": [fake_human, fake_ai_1]},
    )
    graph2 = MagicMock()
    graph2.ainvoke = AsyncMock(
        return_value={"messages": [fake_human, fake_ai_1, fake_human, fake_ai_2]},
    )

    build_calls: list[tuple] = []
    graphs_in_order = [graph0, graph1, graph2]

    def build_side_effect(llm, tools, *a, **kw):
        build_calls.append((llm, tools))
        return graphs_in_order[len(build_calls) - 1]

    with patch(
        "framework_bridge.create_react_agent",
        side_effect=build_side_effect,
    ):
        trial = Trial(
            trial_id="t-rc",
            config=_cfg("responses+conv", "chatgpt"),
        )
        # Conversation container starts unminted.
        assert trial._conversation_id is None

        # Stub the helper so no real /v1/conversations POST happens.
        async def fake_ensure_conv():
            trial._conversation_id = "conv_test_xxx"
            return "conv_test_xxx"
        trial._ensure_conversation_id = fake_ensure_conv

        try:
            out1 = await trial.turn("turn-0", "hi")
            # E13b: envelope exposes the conversation id, NOT a response id.
            assert out1.get("_conversation_id") == "conv_test_xxx"
            assert "_response_id" not in out1
            # E13b: per-message response chain is NOT tracked in +conv.
            assert trial._response_history == []
            assert trial._last_response_id is None

            # Constructor built graph0 (the unbound one). Turn-0 rebuilt
            # graph1 with the conversation-bound llm.
            assert len(build_calls) == 2
            bound_llm_1, _tools_1 = build_calls[1]
            assert isinstance(bound_llm_1, RunnableBinding)
            assert bound_llm_1.kwargs.get("conversation") == {"id": "conv_test_xxx"}
            # And NOT previous_response_id (chain mode is now state=T-only).
            assert "previous_response_id" not in bound_llm_1.kwargs
            graph1.ainvoke.assert_called_once()

            out2 = await trial.turn("turn-1", "followup")
            assert out2.get("_conversation_id") == "conv_test_xxx"
            # Turn 2 also rebuilds the graph with the SAME (cached) conv id.
            assert len(build_calls) == 3
            bound_llm_2, _tools_2 = build_calls[2]
            assert bound_llm_2.kwargs.get("conversation") == {"id": "conv_test_xxx"}
            graph2.ainvoke.assert_called_once()
        finally:
            await trial.aclose()


async def test_compact_responses_conv_is_noop_with_note(adapter_env):
    """E13b: compact() on api=responses+conv is a no-op.

    Continuity in +conv mode lives in the OpenAI conversation container
    (conv_xxx) which has no client-side trim primitive."""
    from framework_bridge import Trial

    trial = Trial(
        trial_id="t-compact",
        config=_cfg("responses+conv", "chatgpt"),
    )
    try:
        trial._conversation_id = "conv_test_xxx"

        out = await trial.compact("drop_half")
        assert out["strategy"] == "drop_half"
        assert out["history_len_before"] == 0
        assert out["history_len_after"] == 0
        assert "note" in out
        assert "no-op" in out["note"]
        assert "conv_test_xxx" in out["note"]
    finally:
        await trial.aclose()


async def test_compact_responses_conv_summarize_is_noop(adapter_env):
    """All strategies are no-ops on +conv (Conversations API has no
    container-level trim)."""
    from framework_bridge import Trial

    trial = Trial(
        trial_id="t-compact-sum",
        config=_cfg("responses+conv", "chatgpt"),
    )
    try:
        for strat in ("drop_half", "drop_tool_calls", "summarize"):
            out = await trial.compact(strat)
            assert out["strategy"] == strat
            assert out["history_len_before"] == 0
            assert out["history_len_after"] == 0
    finally:
        await trial.aclose()


# ── I1 regression: force_state_ref must reach the outbound Responses payload ──

@pytest.mark.asyncio
async def test_force_state_ref_reaches_openai_responses_payload(monkeypatch):
    """Regression for code-review I1: mirror of the langchain sibling.

    Langgraph's previous approach threaded previous_response_id via
    `config={"configurable": {"previous_response_id": ...}}` on
    `graph.ainvoke`, which is silently dropped because ChatOpenAI
    doesn't declare that as a configurable field. Separately, the
    same `use_previous_response_id=True` auto-compute from messages
    clobbering would apply. The fix: strip `response_metadata.id`
    from the `messages` list before `graph.ainvoke`, and thread the
    forced id via a bound ChatOpenAI kwarg that survives to the
    outbound Responses API request.
    """
    _ensure_adapter_on_path()
    monkeypatch.setenv("AGW_LLM_BASE_URL_OPENAI", "http://agentgateway:8080/llm/chatgpt/v1")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")

    from framework_bridge import Trial
    from langchain_core.messages import AIMessage, HumanMessage

    captured: dict = {}

    async def fake_create(self, *args, **kwargs):
        captured["kwargs"] = kwargs
        from openai.types.responses import Response
        return Response.model_construct(
            id="resp_NEW",
            object="response",
            created_at=0,
            model="gpt-4o-mini",
            status="completed",
            output=[],
            parallel_tool_calls=False,
            tool_choice="auto",
            tools=[],
            top_p=1.0,
            temperature=0.3,
            metadata={},
            incomplete_details=None,
            error=None,
            instructions=None,
            usage=None,
        )

    from openai.resources.responses import AsyncResponses
    monkeypatch.setattr(AsyncResponses, "create", fake_create)

    trial = Trial(
        trial_id="t-fsr-lg",
        config=_cfg("responses", "chatgpt", state=True),
    )
    try:
        trial._messages = [
            HumanMessage(content="turn 1 user"),
            AIMessage(
                content="turn 1 assistant",
                response_metadata={"id": "resp_RECENT"},
            ),
        ]
        trial._forced_prev_id = "resp_OLD"
        # Appending a fresh Human before turn() mirrors how the runner
        # would enqueue a user message before force_state_ref.
        trial._messages.append(HumanMessage(content="turn 2 force"))

        try:
            await trial.turn("t2", "hi")
        except Exception:
            pass  # Post-call logic isn't the subject here

        assert captured.get("kwargs") is not None, \
            "fake_create never invoked — patch did not intercept"
        got = captured["kwargs"].get("previous_response_id")
        assert got == "resp_OLD", (
            f"forced id dropped: got {got!r}; langchain-openai auto-compute "
            "clobbered our forced previous_response_id, or langgraph's "
            "`config.configurable.previous_response_id` was silently ignored."
        )
    finally:
        await trial.aclose()


# ── E13a regression: state=True on api=responses must thread previous_response_id ──

@pytest.mark.asyncio
async def test_state_true_on_api_responses_threads_previous_response_id(monkeypatch):
    """E13a: api=responses + state=True must chain previous_response_id,
    same as api=responses+conv. Was previously a silent no-op (the
    threading branch only checked api=='responses+conv'), so users who
    picked state=True on the responses API got state=False semantics
    (full history replay) at the wire — a config-vs-runtime mismatch.

    Pre-seeds _last_response_id (simulating a captured prior turn) and
    confirms the next outbound openai SDK call carries previous_response_id.
    """
    _ensure_adapter_on_path()
    monkeypatch.setenv("AGW_LLM_BASE_URL_OPENAI", "http://agentgateway:8080/llm/chatgpt/v1")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")

    from framework_bridge import Trial
    from langchain_core.messages import HumanMessage

    captured: dict = {}

    async def fake_create(self, *args, **kwargs):
        captured["kwargs"] = kwargs
        from openai.types.responses import Response
        return Response.model_construct(
            id="resp_NEW",
            object="response",
            created_at=0,
            model="gpt-4o-mini",
            status="completed",
            output=[],
            parallel_tool_calls=False,
            tool_choice="auto",
            tools=[],
            top_p=1.0,
            temperature=0.3,
            metadata={},
            incomplete_details=None,
            error=None,
            instructions=None,
            usage=None,
        )

    from openai.resources.responses import AsyncResponses
    monkeypatch.setattr(AsyncResponses, "create", fake_create)

    trial = Trial(trial_id="t-state-t-lg", config=_cfg(
        "responses", "chatgpt", state=True,
    ))
    try:
        # Pre-seed _last_response_id (simulates a prior turn having
        # captured a response id). The next turn() must pick this up
        # as the natural prev-id and thread it into the outbound payload.
        trial._last_response_id = "resp_PRIOR"
        # Mirror the runner: enqueue the user message into the graph
        # state before turn() drives graph.ainvoke.
        trial._messages.append(HumanMessage(content="next"))

        try:
            await trial.turn("t1", "next")
        except Exception:
            pass  # Don't care about post-call processing of synthetic resp

        assert captured.get("kwargs") is not None, \
            "fake_create never invoked — patch did not intercept"
        got = captured["kwargs"].get("previous_response_id")
        assert got == "resp_PRIOR", (
            f"state=True on api=responses should have threaded "
            f"previous_response_id; got {got!r}. Before E13a the "
            "threading branch only fired for api=='responses+conv'."
        )
    finally:
        await trial.aclose()


# ── E13b regression: responses+conv must use Conversations API container ──

@pytest.mark.asyncio
async def test_responses_conv_uses_conversation_field_not_previous_response_id(monkeypatch):
    """E13b: api=responses+conv must use Conversations API
    (conversation:{id: conv_xxx}) and NOT previous_response_id chain.

    Pre-caches the conversation_id so the test doesn't need to mock the
    /v1/conversations setup call; focuses purely on per-turn wire shape.
    """
    _ensure_adapter_on_path()
    monkeypatch.setenv("AGW_LLM_BASE_URL_OPENAI", "http://agentgateway:8080/llm/chatgpt/v1")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")

    from framework_bridge import Trial
    from langchain_core.messages import HumanMessage

    captured: dict = {}

    async def fake_create(self, *args, **kwargs):
        captured["kwargs"] = kwargs
        from openai.types.responses import Response
        return Response.model_construct(
            id="resp_NEW",
            object="response",
            created_at=0,
            model="gpt-4o-mini",
            status="completed",
            output=[],
            parallel_tool_calls=False,
            tool_choice="auto",
            tools=[],
            top_p=1.0,
            temperature=0.3,
            metadata={},
            incomplete_details=None,
            error=None,
            instructions=None,
            usage=None,
        )

    from openai.resources.responses import AsyncResponses
    monkeypatch.setattr(AsyncResponses, "create", fake_create)

    trial = Trial(trial_id="t-conv-lg", config=_cfg("responses+conv", "chatgpt"))
    try:
        # Pre-cache the conversation_id so the test skips the
        # /v1/conversations setup network call.
        trial._conversation_id = "conv_test_xxx"
        trial._messages.append(HumanMessage(content="hello"))

        try:
            await trial.turn("t1", "hello")
        except Exception:
            pass  # Don't care about post-call processing

        assert captured.get("kwargs") is not None, \
            "fake_create never invoked — patch did not intercept"
        conv_kw = captured["kwargs"].get("conversation")
        assert conv_kw is not None, (
            f"`conversation` kwarg missing from openai SDK call; "
            f"got kwargs={list(captured['kwargs'].keys())}"
        )
        if isinstance(conv_kw, dict):
            assert conv_kw.get("id") == "conv_test_xxx"
        else:
            assert conv_kw == "conv_test_xxx"
        assert captured["kwargs"].get("previous_response_id") is None, (
            "+conv mode must NOT thread previous_response_id (chain "
            "mode); the conversation container handles continuity "
            "server-side."
        )
    finally:
        await trial.aclose()
