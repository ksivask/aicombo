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


async def test_build_llm_responses_conv_wires_state_chain(langchain_env):
    """api=responses+conv → ChatOpenAI + use_previous_response_id=True +
    empty per-Trial response-id state chain."""
    from framework_bridge import Trial
    from langchain_openai import ChatOpenAI

    trial = Trial(trial_id="t-resp-conv", config=_cfg("responses+conv", "chatgpt"))
    try:
        assert isinstance(trial.llm, ChatOpenAI)
        assert trial.llm.use_responses_api is True
        assert trial.llm.use_previous_response_id is True
        # State chain initialized empty, not-yet-forced.
        assert trial._last_response_id is None
        assert trial._response_history == []
        assert trial._forced_prev_id is None
    finally:
        await trial.aclose()


# ── compact() on responses+conv chain ────────────────────────────────

async def test_compact_responses_conv_drops_half_of_response_history(langchain_env):
    """compact('drop_half') on api=responses+conv halves the _response_history
    chain and re-pegs _last_response_id to the survivors' tail."""
    from framework_bridge import Trial

    trial = Trial(trial_id="t-compact-rc", config=_cfg("responses+conv", "chatgpt"))
    try:
        # Pre-seed a 6-id history (oldest → newest).
        trial._response_history = [f"resp_{i:03d}" for i in range(6)]
        trial._last_response_id = trial._response_history[-1]
        before = len(trial._response_history)

        out = await trial.compact("drop_half")

        assert out["strategy"] == "drop_half"
        assert out["history_len_before"] == before
        assert out["history_len_after"] == before - (before // 2)  # = 3
        assert len(trial._response_history) == 3
        # Tail survives (newest is kept).
        assert trial._response_history[-1] == "resp_005"
        # _last_response_id re-pegged to the survivors' tail.
        assert trial._last_response_id == "resp_005"
        # Note field present (mirrors autogen/llamaindex responses_direct).
        assert "note" in out
    finally:
        await trial.aclose()


async def test_compact_responses_conv_fallbacks_for_other_strategies(langchain_env):
    """drop_tool_calls / summarize also halve the chain on responses+conv
    (no per-message content to filter or summarize at this layer)."""
    from framework_bridge import Trial

    trial = Trial(trial_id="t-compact-rc-alt", config=_cfg("responses+conv", "chatgpt"))
    try:
        trial._response_history = [f"resp_{i:03d}" for i in range(4)]
        trial._last_response_id = trial._response_history[-1]

        out = await trial.compact("summarize")
        assert out["strategy"] == "summarize"
        assert out["history_len_before"] == 4
        assert out["history_len_after"] == 2
        assert trial._response_history[-1] == "resp_003"
    finally:
        await trial.aclose()


# ── I1 regression: force_state_ref must reach the outbound Responses payload ──

@pytest.mark.asyncio
async def test_force_state_ref_reaches_openai_responses_payload(monkeypatch):
    """Regression for code-review I1: `_forced_prev_id` must land in the
    outbound OpenAI Responses API request body — NOT get silently
    overwritten by `use_previous_response_id=True`'s auto-compute which
    walks `messages` backward for the most-recent AIMessage response id.

    Before the fix, `_get_request_payload` would set
    `payload["previous_response_id"]` to the newest AIMessage's id
    (e.g. "resp_RECENT2"), clobbering the forced id we injected via
    `invoke_kwargs["previous_response_id"]`. The fix strips
    `response_metadata.id` from in-memory AIMessages when `_forced_prev_id`
    is set, so the auto-compute returns None and our kwarg survives.
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
        "api": "responses+conv",
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
