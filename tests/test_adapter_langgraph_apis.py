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


async def test_build_llm_responses_conv_wires_state_chain(adapter_env):
    """api=responses+conv seeds the state chain + threads previous_response_id.

    Walks two turns via stubbed graph.ainvoke. After turn 1, the adapter
    records resp_001 as _last_response_id. On turn 2, the I1 fix rebuilds
    the graph with an llm bound to previous_response_id=resp_001 so the
    forced id reaches the outbound Responses API request (the old
    config.configurable route was silently dropped by ChatOpenAI).

    We patch `framework_bridge.create_react_agent` with a side_effect that
    returns a fresh MagicMock for EACH call and verify:
      - turn 1 uses the initially constructed graph (no llm.bind).
      - turn 2 triggers a second create_react_agent call whose llm arg is
        a RunnableBinding carrying `previous_response_id=resp_001`.
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

    graph1 = MagicMock()
    graph1.ainvoke = AsyncMock(
        return_value={"messages": [fake_human, fake_ai_1]},
    )
    graph2 = MagicMock()
    graph2.ainvoke = AsyncMock(
        return_value={"messages": [fake_human, fake_ai_1, fake_human, fake_ai_2]},
    )

    build_calls: list[tuple] = []

    def build_side_effect(llm, tools, *a, **kw):
        build_calls.append((llm, tools))
        return graph1 if len(build_calls) == 1 else graph2

    with patch(
        "framework_bridge.create_react_agent",
        side_effect=build_side_effect,
    ):
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

            # Only one graph built so far (constructor path at first turn).
            assert len(build_calls) == 1
            graph1.ainvoke.assert_called_once()

            out2 = await trial.turn("turn-1", "followup")
            assert out2["_response_id"] == "resp_002"
            assert trial._last_response_id == "resp_002"
            assert trial._response_history == ["resp_001", "resp_002"]

            # Turn 2 must have rebuilt the graph with a bound llm whose
            # kwargs include previous_response_id=resp_001. This is the
            # I1 fix — the old config.configurable route was silently
            # dropped by ChatOpenAI, so the forced id never landed in
            # the outbound OpenAI Responses request.
            assert len(build_calls) == 2
            bound_llm, _tools = build_calls[1]
            assert isinstance(bound_llm, RunnableBinding), \
                f"expected RunnableBinding from llm.bind(...), got {type(bound_llm)!r}"
            assert bound_llm.kwargs.get("previous_response_id") == "resp_001"
            graph2.ainvoke.assert_called_once()
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

    trial = Trial(trial_id="t-fsr-lg", config=_cfg("responses+conv", "chatgpt"))
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
