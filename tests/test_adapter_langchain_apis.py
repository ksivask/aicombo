"""Tests for adapters/langchain — Plan B T5 multi-API support.

The langchain adapter was originally chat-only; Plan B T5 extends it to
cover all four APIs (chat, messages, responses, responses+conv). These
tests pin the Trial.__init__ branching logic + compact behavior on the
responses+conv state chain, WITHOUT touching real LLM endpoints.

Shape tests only — an end-to-end turn test would need mocking out the
langchain SDK layers (ChatOpenAI.ainvoke / ChatAnthropic.ainvoke) which
is out of scope for this file. The live smoke lives in docker compose.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_ADAPTER_DIR     = str(Path(__file__).resolve().parent.parent / "adapters" / "langchain")
_LANGGRAPH_DIR   = str(Path(__file__).resolve().parent.parent / "adapters" / "langgraph")
_DIRECT_MCP_DIR  = str(Path(__file__).resolve().parent.parent / "adapters" / "direct-mcp")
_AUTOGEN_DIR     = str(Path(__file__).resolve().parent.parent / "adapters" / "autogen")
_CREWAI_DIR      = str(Path(__file__).resolve().parent.parent / "adapters" / "crewai")
_PYDANTIC_AI_DIR = str(Path(__file__).resolve().parent.parent / "adapters" / "pydantic_ai")
_LLAMAINDEX_DIR  = str(Path(__file__).resolve().parent.parent / "adapters" / "llamaindex")


def _ensure_adapter_on_path():
    """Force `framework_bridge` to resolve to the langchain adapter copy.

    Multiple adapters ship a top-level module named `framework_bridge`;
    we prune any sibling adapter dirs from sys.path + drop a stale cache
    so the import in each test resolves to the langchain bridge.
    """
    for other in (
        _LANGGRAPH_DIR, _DIRECT_MCP_DIR, _AUTOGEN_DIR, _CREWAI_DIR,
        _PYDANTIC_AI_DIR, _LLAMAINDEX_DIR,
    ):
        while other in sys.path:
            sys.path.remove(other)
    while _ADAPTER_DIR in sys.path:
        sys.path.remove(_ADAPTER_DIR)
    sys.path.insert(0, _ADAPTER_DIR)
    sys.modules.pop("framework_bridge", None)


def _cfg(api: str, llm: str, model: str | None = None) -> dict:
    cfg = {
        "framework": "langchain",
        "api": api,
        "stream": False,
        "state": False,
        "llm": llm,
        "mcp": "NONE",
        "routing": "via_agw",
    }
    if model is not None:
        cfg["model"] = model
    return cfg


@pytest.fixture
def langchain_env(monkeypatch):
    """Minimal env wiring for Trial __init__ across the four APIs."""
    monkeypatch.setenv("AGW_LLM_BASE_URL_OLLAMA",    "http://gateway:8080/llm/ollama/v1")
    monkeypatch.setenv("AGW_LLM_BASE_URL_OPENAI",    "http://gateway:8080/llm/chatgpt/v1")
    monkeypatch.setenv("AGW_LLM_BASE_URL_ANTHROPIC", "http://gateway:8080/llm/claude")
    monkeypatch.setenv("OPENAI_API_KEY",    "sk-test-fake")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-fake")
    monkeypatch.setenv("DEFAULT_OLLAMA_MODEL", "qwen2.5:7b")
    _ensure_adapter_on_path()


# ── _build_llm API-branching shape tests ──────────────────────────────

async def test_build_llm_chat_returns_chat_openai(langchain_env):
    """api=chat, llm=ollama → ChatOpenAI (openai-compat provider class)."""
    from framework_bridge import Trial
    from langchain_openai import ChatOpenAI

    trial = Trial(trial_id="t-chat", config=_cfg("chat", "ollama"))
    try:
        assert isinstance(trial.llm, ChatOpenAI)
        # chat path does NOT flip the responses flag. langchain-openai's
        # default for use_responses_api is None (three-state: None/False
        # behave the same; True opts into the Responses API). Accept either.
        assert not trial.llm.use_responses_api
        assert trial.llm.use_previous_response_id is False
    finally:
        await trial.aclose()


async def test_build_llm_messages_returns_chat_anthropic(langchain_env):
    """api=messages, llm=claude → ChatAnthropic with hooked anthropic clients."""
    import anthropic
    import httpx
    from framework_bridge import Trial
    from langchain_anthropic import ChatAnthropic

    trial = Trial(trial_id="t-msg", config=_cfg("messages", "claude"))
    try:
        assert isinstance(trial.llm, ChatAnthropic)
        # Our _install_anthropic_hooked_clients override MUST replace the
        # cached_property _async_client with a real anthropic.AsyncClient
        # before the framework reads it — otherwise wire-byte capture is
        # lost. Accessing `_async_client` should yield our override (not
        # trigger the cached_property factory).
        assert isinstance(trial.llm._async_client, anthropic.AsyncClient)
        # Sync client installed too (for the anthropic SDK sync code path).
        assert isinstance(trial.llm._client, anthropic.Client)
        # And the sync httpx.Client we stashed on the trial (for aclose).
        assert isinstance(trial._anthropic_sync_http, httpx.Client)
    finally:
        await trial.aclose()


def test_build_llm_messages_rejects_non_claude(langchain_env):
    """api=messages + llm!=claude is a hard error (ValueError), not a silent fallback."""
    from framework_bridge import Trial

    with pytest.raises(ValueError, match="api=messages requires llm=claude"):
        Trial(trial_id="t-bad-msg", config=_cfg("messages", "chatgpt"))


async def test_build_llm_responses_returns_chat_openai_with_responses_flag(langchain_env):
    """api=responses, llm=chatgpt → ChatOpenAI with use_responses_api=True."""
    from framework_bridge import Trial
    from langchain_openai import ChatOpenAI

    trial = Trial(trial_id="t-resp", config=_cfg("responses", "chatgpt"))
    try:
        assert isinstance(trial.llm, ChatOpenAI)
        assert trial.llm.use_responses_api is True
        # Stateless responses — DON'T auto-thread prev-id.
        assert trial.llm.use_previous_response_id is False
    finally:
        await trial.aclose()


def test_build_llm_responses_rejects_non_chatgpt(langchain_env):
    """api=responses requires llm=chatgpt (Responses is an OpenAI-only API)."""
    from framework_bridge import Trial

    with pytest.raises(ValueError, match="api=responses requires llm=chatgpt"):
        Trial(trial_id="t-bad-resp", config=_cfg("responses", "claude"))


async def test_build_llm_responses_conv_wires_conv_container(langchain_env):
    """E13b: api=responses+conv → ChatOpenAI(use_responses_api=True) +
    empty conversation-container state. The previous_response_id
    auto-thread flag is INTENTIONALLY NOT set anymore — +conv now uses
    the OpenAI Conversations API container reference instead of the
    chain-of-prev-ids mechanism."""
    from framework_bridge import Trial
    from langchain_openai import ChatOpenAI

    trial = Trial(trial_id="t-resp-conv", config=_cfg("responses+conv", "chatgpt"))
    try:
        assert isinstance(trial.llm, ChatOpenAI)
        assert trial.llm.use_responses_api is True
        # E13b: NOT the chain-mode auto-thread flag — that path is now
        # only used by api=responses + state=T.
        assert trial.llm.use_previous_response_id is False
        # Conversation container starts unminted; lazy-minted on first turn.
        assert trial._conversation_id is None
    finally:
        await trial.aclose()


# ── compact() on responses+conv conversation container ──────────────

async def test_compact_responses_conv_is_noop_with_note(langchain_env):
    """E13b: compact() on api=responses+conv is a deliberate no-op.

    Continuity in +conv mode lives in the OpenAI conversation container
    (conv_xxx) which has no client-side trim primitive. The old behavior
    (halving a fake _response_history chain) was a leftover from the
    misnamed previous_response_id implementation — now the conversation
    is the only state-tracking artifact, and the OpenAI Conversations
    API doesn't expose container-level compaction at all."""
    from framework_bridge import Trial

    trial = Trial(trial_id="t-compact-rc", config=_cfg("responses+conv", "chatgpt"))
    try:
        # Simulate an already-minted container (so the note can reference it).
        trial._conversation_id = "conv_test_xxx"

        out = await trial.compact("drop_half")
        assert out["strategy"] == "drop_half"
        assert out["history_len_before"] == 0
        assert out["history_len_after"] == 0
        assert "note" in out
        # Note mentions both the no-op nature AND the conversation id.
        assert "conv_test_xxx" in out["note"]
        assert "no-op" in out["note"]
    finally:
        await trial.aclose()


async def test_compact_responses_conv_noop_independent_of_strategy(langchain_env):
    """All strategies (drop_half / drop_tool_calls / summarize) are no-ops
    on +conv — the Conversations API doesn't expose any of them."""
    from framework_bridge import Trial

    trial = Trial(trial_id="t-compact-rc-alt", config=_cfg("responses+conv", "chatgpt"))
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
    """Regression for code-review I1: `_forced_prev_id` must land in the
    outbound OpenAI Responses API request body — NOT get silently
    overwritten by langchain-openai's `_get_last_messages` auto-compute
    which walks `messages` backward for the most-recent AIMessage
    response id.

    The fix strips `response_metadata.id` from in-memory AIMessages when
    `_forced_prev_id` is set, so the auto-compute returns None and our
    kwarg survives.

    Note: this test originally pinned the +conv code path. After E13b,
    +conv uses the Conversations API container (conversation kwarg) and
    no longer threads previous_response_id, so the regression target
    moved to the api=responses + state=T path which IS still chain-mode.
    """
    _ensure_adapter_on_path()
    monkeypatch.setenv("AGW_LLM_BASE_URL_OPENAI", "http://agentgateway:8080/llm/chatgpt/v1")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")

    from framework_bridge import Trial
    from langchain_core.messages import AIMessage, HumanMessage

    # Intercept the openai SDK call boundary with a fake create()
    # returning a minimal Response pydantic model (via model_construct
    # to bypass validation on fields we don't care about).
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

    # Patch BEFORE Trial() constructs ChatOpenAI — its AsyncOpenAI client
    # resolves `.responses` lazily via cached_property, so the patched
    # AsyncResponses.create gets picked up on first use.
    from openai.resources.responses import AsyncResponses
    monkeypatch.setattr(AsyncResponses, "create", fake_create)

    trial = Trial(trial_id="t-fsr", config={
        "framework": "langchain",
        "api": "responses",          # E13b: +conv no longer chain-mode; pin on state=T
        "llm": "chatgpt",
        "stream": False,
        "state": True,
        "mcp": "NONE",
        "routing": "via_agw",
    })
    try:
        # Prime `messages` with prior AIMessages carrying response_metadata.id.
        # `_get_last_messages` in langchain-openai walks BACKWARD and would
        # pick up "resp_RECENT2" as the auto-computed previous_response_id,
        # overwriting our forced "resp_OLD".
        trial.messages = [
            HumanMessage(content="turn 1 user"),
            AIMessage(content="turn 1 assistant", response_metadata={"id": "resp_RECENT"}),
            HumanMessage(content="turn 2 user"),
            AIMessage(content="turn 2 assistant", response_metadata={"id": "resp_RECENT2"}),
        ]
        trial._forced_prev_id = "resp_OLD"

        # Run the turn — we only care about the payload captured, not the
        # response post-processing (which may fail on our synthetic Response).
        try:
            await trial.turn("t3", "turn 3 force-ref")
        except Exception:
            pass  # Post-call logic (metadata parsing) isn't the subject here

        # CRITICAL: the outbound request MUST carry our forced id.
        assert captured.get("kwargs") is not None, \
            "fake_create was never called — patch did not intercept"
        got = captured["kwargs"].get("previous_response_id")
        assert got == "resp_OLD", (
            f"forced id dropped by langchain-openai auto-compute: got {got!r}; "
            "`_get_last_messages` walked messages backward and clobbered "
            "invoke_kwargs['previous_response_id']"
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

    This test pre-seeds `_last_response_id` (simulating a captured prior
    turn) and confirms the next outbound openai SDK call carries
    `previous_response_id` in its kwargs.
    """
    _ensure_adapter_on_path()
    monkeypatch.setenv("AGW_LLM_BASE_URL_OPENAI", "http://agentgateway:8080/llm/chatgpt/v1")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")

    from framework_bridge import Trial

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

    trial = Trial(trial_id="t-state-t", config={
        "framework": "langchain",
        "api": "responses",            # ← NOT responses+conv
        "llm": "chatgpt",
        "stream": False,
        "state": True,                  # ← E13a: chain via prev-id
        "mcp": "NONE",
        "routing": "via_agw",
    })
    try:
        # Pre-seed _last_response_id (simulates a prior turn having
        # captured a response id). The next turn() should pick this up
        # as the natural prev-id and thread it into the outbound payload.
        trial._last_response_id = "resp_PRIOR"

        try:
            await trial.turn("t1", "next")
        except Exception:
            pass  # Don't care about post-call processing of synthetic resp

        assert captured.get("kwargs") is not None, \
            "fake_create was never called — patch did not intercept"
        got = captured["kwargs"].get("previous_response_id")
        assert got == "resp_PRIOR", (
            f"state=True on api=responses should have threaded "
            f"previous_response_id; got {got!r}. Before E13a the "
            "threading branch only fired for api=='responses+conv'."
        )
    finally:
        await trial.aclose()


# ── E13b regression: responses+conv must use the Conversations API container ──

@pytest.mark.asyncio
async def test_responses_conv_uses_conversation_field_not_previous_response_id(monkeypatch):
    """E13b: api=responses+conv must use Conversations API
    (conversation:{id: conv_xxx}) and NOT previous_response_id chain.

    The two were collapsed in the pre-E13b code path (+conv was a misnomer
    that actually threaded previous_response_id, identical to E13a's
    state=T behavior at the wire). This test pins them apart by inspecting
    the openai SDK boundary's outbound kwargs.
    """
    _ensure_adapter_on_path()
    monkeypatch.setenv("AGW_LLM_BASE_URL_OPENAI", "http://agentgateway:8080/llm/chatgpt/v1")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")

    from framework_bridge import Trial

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

    trial = Trial(trial_id="t-conv", config={
        "framework": "langchain",
        "api": "responses+conv",
        "llm": "chatgpt",
        "stream": False,
        "state": True,
        "mcp": "NONE",
        "routing": "via_agw",
    })
    try:
        # Pre-cache the conversation_id so the test doesn't need to mock
        # the /v1/conversations setup call. (The setup call IS captured
        # by _http_exchanges in real runs; this test focuses on the
        # per-turn wire shape.)
        trial._conversation_id = "conv_test_xxx"

        try:
            await trial.turn("t1", "hello")
        except Exception:
            pass  # Post-call processing not the subject here

        assert captured.get("kwargs") is not None, \
            "fake_create was never called — patch did not intercept"
        # CRITICAL: outbound MUST contain the conversation field (E13b).
        conv_kw = captured["kwargs"].get("conversation")
        assert conv_kw is not None, (
            f"`conversation` kwarg missing from openai SDK call; "
            f"got kwargs={list(captured['kwargs'].keys())}"
        )
        # SDK accepts either str or {"id": str}; we pass dict-form.
        if isinstance(conv_kw, dict):
            assert conv_kw.get("id") == "conv_test_xxx"
        else:
            assert conv_kw == "conv_test_xxx"
        # And NOT previous_response_id (would mean we still chain-threaded).
        assert captured["kwargs"].get("previous_response_id") is None, (
            "+conv mode must NOT thread previous_response_id (that's "
            "chain mode). Conversations API container handles continuity "
            "server-side."
        )
    finally:
        await trial.aclose()
