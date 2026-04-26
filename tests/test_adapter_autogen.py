"""Tests for adapters/autogen — framework bridge logic (offline).

The autogen adapter is the first to cover all 4 APIs (chat / messages /
responses / responses+conv) plus a state-mode variant. These tests verify
URL resolution, model-name selection, state-mode chaining of response ids,
and the force_state_ref override path without touching real LLM endpoints.
"""
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
    """Put autogen's framework_bridge at the front of sys.path; evict others."""
    for other in (_PYDANTIC_AI_DIR, _CREWAI_DIR, _LANGCHAIN_DIR,
                  _LANGGRAPH_DIR, _DIRECT_MCP_DIR):
        while other in sys.path:
            sys.path.remove(other)
    while _AUTOGEN_DIR in sys.path:
        sys.path.remove(_AUTOGEN_DIR)
    sys.path.insert(0, _AUTOGEN_DIR)
    sys.modules.pop("framework_bridge", None)


def test_pick_llm_base_url_via_agw_chatgpt(monkeypatch):
    """chat → chatgpt resolves to the AGW OpenAI URL."""
    _ensure_adapter_on_path()
    monkeypatch.setenv("AGW_LLM_BASE_URL_OPENAI", "http://agentgateway:8080/llm/chatgpt/v1")
    from framework_bridge import pick_llm_base_url
    url = pick_llm_base_url(routing="via_agw", llm="chatgpt")
    assert "chatgpt" in url


def test_pick_llm_base_url_via_agw_claude(monkeypatch):
    """messages → claude resolves to the AGW Anthropic URL."""
    _ensure_adapter_on_path()
    monkeypatch.setenv("AGW_LLM_BASE_URL_ANTHROPIC", "http://agentgateway:8080/llm/claude")
    from framework_bridge import pick_llm_base_url
    url = pick_llm_base_url(routing="via_agw", llm="claude")
    assert "claude" in url


def test_default_model_name_covers_all_four_apis():
    """All four APIs resolve to a sensible default model identifier.

    This is the guarantee that sets autogen apart: a single adapter class
    picks the right model for chat/messages/responses/responses+conv.
    """
    _ensure_adapter_on_path()
    from framework_bridge import _default_model_name
    # chat + ollama: env-defaulted
    assert _default_model_name("chat", "ollama", None) != ""
    # messages + claude
    assert _default_model_name("messages", "claude", None).startswith("claude-")
    # responses + chatgpt
    assert _default_model_name("responses", "chatgpt", None).startswith("gpt-")
    # responses+conv + chatgpt (same model, different flow)
    assert _default_model_name("responses+conv", "chatgpt", None).startswith("gpt-")
    # Explicit model wins over defaults
    assert _default_model_name("responses+conv", "chatgpt", "gpt-5") == "gpt-5"


@pytest.mark.asyncio
async def test_trial_create_and_close_sets_up_state(monkeypatch):
    """Smoke: Trial init (chat mode) + aclose without real LLM calls.

    Mocks _build_model_client so construction doesn't need a real endpoint
    or catalog model. Verifies state is initialized (empty history, shared
    headers wired, no chained response id) and aclose releases the httpx
    client without raising.
    """
    _ensure_adapter_on_path()
    monkeypatch.setenv("AGW_LLM_BASE_URL_OPENAI", "http://agentgateway:8080/llm/chatgpt/v1")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-fake")
    import framework_bridge
    from framework_bridge import Trial

    fake_client = MagicMock(name="fake_model_client")
    with patch.object(framework_bridge, "_build_model_client",
                      return_value=fake_client) as MockBuild:
        trial = Trial(trial_id="t-init", config={
            "framework": "autogen", "api": "chat",
            "stream": False, "state": False,
            "llm": "chatgpt", "mcp": "NONE", "routing": "via_agw",
        })
        try:
            assert trial.trial_id == "t-init"
            assert trial._agentchat_messages == []
            assert trial._http_exchanges == []
            assert trial._last_response_id is None
            assert trial._response_history == []
            assert trial._forced_prev_id is None
            # Mutable shared headers dict is in place.
            assert trial._headers["X-Harness-Trial-ID"] == "t-init"
            MockBuild.assert_called_once()
            assert trial._mode == "agent"
        finally:
            await trial.aclose()


@pytest.mark.asyncio
async def test_responses_mode_state_chains_response_ids(monkeypatch):
    """api=responses + state=T (E13a): _last_response_id updates after each
    turn; history accumulates; force_state_ref(idx) overrides the next
    turn's previous_response_id.

    Note: pre-E13b this test pinned api=responses+conv. After E13b that
    api uses the Conversations API container instead of previous_response_id
    chaining; the chain semantics are now exclusive to api=responses+state=T.
    See test_responses_conv_uses_conversation_field_not_previous_response_id
    for the +conv counterpart.
    """
    _ensure_adapter_on_path()
    monkeypatch.setenv("AGW_LLM_BASE_URL_OPENAI", "http://agentgateway:8080/llm/chatgpt/v1")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-fake")
    import framework_bridge
    from framework_bridge import Trial

    # Build an async mock `.responses.create` that returns a Response-like
    # object with incrementing .id, and records the `previous_response_id`
    # it was called with.
    call_log: list[dict] = []
    counter = {"n": 0}

    async def fake_create(**kwargs):
        counter["n"] += 1
        call_log.append(dict(kwargs))
        resp = MagicMock()
        resp.id = f"resp_{counter['n']}"
        resp.output_text = f"reply {counter['n']}"
        resp.output = []
        return resp

    fake_openai = MagicMock()
    fake_openai.responses.create = fake_create

    # Also need to avoid the real AsyncOpenAI constructor walking httpx internals.
    with patch.object(framework_bridge, "AsyncOpenAI", return_value=fake_openai, create=True):
        # The import inside Trial is `from openai import AsyncOpenAI` so
        # patch that directly.
        import openai
        with patch.object(openai, "AsyncOpenAI", return_value=fake_openai):
            trial = Trial(trial_id="t-resp", config={
                "framework": "autogen", "api": "responses",
                "stream": False, "state": True,
                "llm": "chatgpt", "mcp": "NONE", "routing": "via_agw",
            })
            try:
                assert trial._mode == "responses_direct"

                # Turn 1: no previous_response_id yet.
                r1 = await trial.turn("turn-1", "hello")
                assert r1["assistant_msg"] == "reply 1"
                assert trial._last_response_id == "resp_1"
                assert trial._response_history == ["resp_1"]
                assert call_log[-1].get("previous_response_id") is None

                # Turn 2: chain on resp_1.
                r2 = await trial.turn("turn-2", "continue")
                assert r2["assistant_msg"] == "reply 2"
                assert trial._last_response_id == "resp_2"
                assert trial._response_history == ["resp_1", "resp_2"]
                assert call_log[-1]["previous_response_id"] == "resp_1"

                # Turn 3: chain on resp_2.
                await trial.turn("turn-3", "more")
                assert trial._response_history == ["resp_1", "resp_2", "resp_3"]
                assert call_log[-1]["previous_response_id"] == "resp_2"

                # Now force the NEXT turn to reference turn-0 (resp_1)
                # instead of the natural most-recent resp_3.
                out = trial.force_state_ref(0)
                assert out["ok"] is True
                assert out["forced_prev_id"] == "resp_1"

                await trial.turn("turn-4", "branch-back")
                # The call used resp_1 (forced) despite resp_3 being latest.
                assert call_log[-1]["previous_response_id"] == "resp_1"
                # And force is consumed — subsequent turn chains on resp_4.
                assert trial._forced_prev_id is None
                await trial.turn("turn-5", "after-branch")
                assert call_log[-1]["previous_response_id"] == "resp_4"
            finally:
                await trial.aclose()


def test_force_state_ref_rejects_out_of_range(monkeypatch):
    """force_state_ref with an invalid index returns ok=False, doesn't crash.

    Pinned on api=responses + state=T (chain mode) since force_state_ref
    operates on the previous_response_id chain. After E13b, +conv mode
    uses the Conversations API container which has no per-turn ref to
    override.
    """
    _ensure_adapter_on_path()
    monkeypatch.setenv("AGW_LLM_BASE_URL_OPENAI", "http://agentgateway:8080/llm/chatgpt/v1")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-fake")
    import openai
    from framework_bridge import Trial

    with patch.object(openai, "AsyncOpenAI", return_value=MagicMock()):
        trial = Trial(trial_id="t-oor", config={
            "framework": "autogen", "api": "responses",
            "stream": False, "state": True,
            "llm": "chatgpt", "mcp": "NONE", "routing": "via_agw",
        })
        # No history yet → any index is out of range.
        out = trial.force_state_ref(0)
        assert out["ok"] is False
        # Negative also rejected.
        out = trial.force_state_ref(-1)
        assert out["ok"] is False
        # _forced_prev_id stays None.
        assert trial._forced_prev_id is None


# ── I-NEW-2 regression: runner-path force_state_ref wire shape ──

@pytest.mark.asyncio
async def test_runner_path_force_state_ref_string_target_reaches_outbound_payload(monkeypatch):
    """I-NEW-2 regression: the harness/runner.py path drives force_state_ref
    via ``drive_turn(turn_kind="force_state_ref", target_response_id=<str>)``,
    not via ``Trial.force_state_ref(int)``. The adapter's ``main.py``
    dispatcher accepts the string target, assigns ``trial._forced_prev_id =
    req.target_response_id`` directly, then runs the turn — bypassing
    ``Trial.force_state_ref(int)`` (which serves the standalone
    ``POST /trials/{id}/force_state_ref`` HTTP route + unit tests).

    This pins the wire shape: a string target_response_id from the runner
    must land as ``previous_response_id`` on the next outbound openai
    Responses call. If a future refactor moves the assignment from
    ``main.py`` into ``Trial.force_state_ref(int)`` (which only accepts
    an int index into ``_response_history``), the runner path silently
    fails because the target isn't in history (it's an arbitrary string
    from another trial / external caller).
    """
    _ensure_adapter_on_path()
    monkeypatch.setenv("AGW_LLM_BASE_URL_OPENAI", "http://agentgateway:8080/llm/chatgpt/v1")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-fake")
    import openai
    from framework_bridge import Trial
    # main.py is the dispatcher under test — same module-path setup as the
    # framework_bridge import above (both live in adapters/autogen/).
    import importlib
    import sys
    sys.modules.pop("main", None)
    main_mod = importlib.import_module("main")

    call_log: list[dict] = []

    async def fake_create(**kwargs):
        call_log.append(dict(kwargs))
        resp = MagicMock()
        resp.id = "resp_NEW"
        resp.output_text = "ok"
        resp.output = []
        return resp

    fake_openai = MagicMock()
    fake_openai.responses.create = fake_create

    with patch.object(openai, "AsyncOpenAI", return_value=fake_openai):
        trial = Trial(trial_id="t-runner-fsr", config={
            "framework": "autogen", "api": "responses",
            "stream": False, "state": True,
            "llm": "chatgpt", "mcp": "NONE", "routing": "via_agw",
        })
        # Inject directly into the adapter's TRIALS registry — that's
        # the same path POST /trials populates on a real adapter call.
        main_mod.TRIALS[trial.trial_id] = trial
        try:
            assert trial._mode == "responses_direct"
            # Drive a runner-shaped force_state_ref turn. target_response_id
            # is a STRING that doesn't appear in _response_history (which is
            # empty here) — proving the dispatcher uses the string directly,
            # not via Trial.force_state_ref(int).
            req = main_mod.TurnReq(
                turn_id="t-fsr-1",
                user_msg="branch back",
                turn_kind="force_state_ref",
                target_response_id="resp_FROM_PRIOR_TRIAL",
            )
            await main_mod.drive_turn(trial.trial_id, req)

            assert call_log, "fake_create was never called via runner path"
            got = call_log[-1].get("previous_response_id")
            assert got == "resp_FROM_PRIOR_TRIAL", (
                f"runner-path force_state_ref dropped the string "
                f"target_response_id: outbound previous_response_id={got!r}; "
                "expected 'resp_FROM_PRIOR_TRIAL' (the value the runner "
                "passes via drive_turn body)"
            )
            # And the override is consumed exactly once.
            assert trial._forced_prev_id is None, (
                "trial._forced_prev_id should be cleared after the turn"
            )
        finally:
            main_mod.TRIALS.pop(trial.trial_id, None)
            await trial.aclose()


# ── E13a regression: state=True on api=responses must thread previous_response_id ──

@pytest.mark.asyncio
async def test_state_true_on_api_responses_threads_previous_response_id(monkeypatch):
    """E13a: api=responses + state=True must chain previous_response_id,
    same as api=responses+conv. The autogen adapter has long had the
    correct gate (`state_mode = bool(self.config.get("state")) or
    api == "responses+conv"`), so this test is a regression pin: if a
    future refactor narrows the condition back to responses+conv only,
    this test catches it.
    """
    _ensure_adapter_on_path()
    monkeypatch.setenv("AGW_LLM_BASE_URL_OPENAI", "http://agentgateway:8080/llm/chatgpt/v1")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-fake")
    import openai
    from framework_bridge import Trial

    call_log: list[dict] = []

    async def fake_create(**kwargs):
        call_log.append(dict(kwargs))
        resp = MagicMock()
        resp.id = "resp_NEW"
        resp.output_text = "next-reply"
        resp.output = []
        return resp

    fake_openai = MagicMock()
    fake_openai.responses.create = fake_create

    with patch.object(openai, "AsyncOpenAI", return_value=fake_openai):
        trial = Trial(trial_id="t-state-t-ag", config={
            "framework": "autogen",
            "api": "responses",         # ← NOT responses+conv
            "stream": False,
            "state": True,               # ← E13a: chain via prev-id
            "llm": "chatgpt",
            "mcp": "NONE",
            "routing": "via_agw",
        })
        try:
            assert trial._mode == "responses_direct"
            # Pre-seed _last_response_id (simulates a captured prior turn).
            trial._last_response_id = "resp_PRIOR"

            await trial.turn("t1", "next")

            assert call_log, "fake_create was never called"
            got = call_log[-1].get("previous_response_id")
            assert got == "resp_PRIOR", (
                f"state=True on api=responses should have threaded "
                f"previous_response_id; got {got!r}. Before E13a the "
                "threading gate only fired for api=='responses+conv'."
            )
        finally:
            await trial.aclose()


# ── E13b regression: responses+conv must use Conversations API container ──

@pytest.mark.asyncio
async def test_responses_conv_uses_conversation_field_not_previous_response_id(monkeypatch):
    """E13b: api=responses+conv must use Conversations API
    (conversation:{id: conv_xxx}) and NOT previous_response_id chain.

    Pre-caches the conversation_id so the test doesn't need to mock the
    /v1/conversations setup call; focuses purely on per-turn wire shape
    at the openai SDK boundary.
    """
    _ensure_adapter_on_path()
    monkeypatch.setenv("AGW_LLM_BASE_URL_OPENAI", "http://agentgateway:8080/llm/chatgpt/v1")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-fake")
    import openai
    from framework_bridge import Trial

    call_log: list[dict] = []

    async def fake_create(**kwargs):
        call_log.append(dict(kwargs))
        resp = MagicMock()
        resp.id = "resp_NEW"
        resp.output_text = "next-reply"
        resp.output = []
        return resp

    fake_openai = MagicMock()
    fake_openai.responses.create = fake_create

    with patch.object(openai, "AsyncOpenAI", return_value=fake_openai):
        trial = Trial(trial_id="t-conv-ag", config={
            "framework": "autogen", "api": "responses+conv",
            "stream": False, "state": True,
            "llm": "chatgpt", "mcp": "NONE", "routing": "via_agw",
        })
        try:
            assert trial._mode == "responses_direct"
            # Pre-cache the conversation_id so no real
            # /v1/conversations POST is attempted.
            trial._conversation_id = "conv_test_xxx"

            await trial.turn("t1", "hello")

            assert call_log, "fake_create was never called"
            conv_kw = call_log[-1].get("conversation")
            assert conv_kw is not None, (
                f"`conversation` kwarg missing from openai SDK call; "
                f"got kwargs={list(call_log[-1].keys())}"
            )
            if isinstance(conv_kw, dict):
                assert conv_kw.get("id") == "conv_test_xxx"
            else:
                assert conv_kw == "conv_test_xxx"
            assert call_log[-1].get("previous_response_id") is None, (
                "+conv mode must NOT thread previous_response_id (chain "
                "mode); the conversation container handles continuity "
                "server-side."
            )
        finally:
            await trial.aclose()


# ── B1 regression: autogen MCP tool wrapper FunctionTool construction ──

def test_make_autogen_mcp_tool_constructs_without_kwargs_keyerror():
    """B1 regression: _make_autogen_mcp_tool's dynamic _call closure must
    have annotated **kwargs so autogen-core's FunctionTool introspector
    (which dereferences typing.get_type_hints(_call)[param.name] for every
    parameter) doesn't raise `KeyError: 'kwargs'`.

    This was 100% reproducible on any autogen+MCP trial — the bridge built
    the tool, FunctionTool walked the signature, KeyError, agent.run never
    started. Trials 0b590d37 (autogen+messages+claude+library) and
    640a06d3 (autogen+chat+ollama+library) both bombed identically,
    confirming the bug lives at the autogen+MCP wrapper layer (API-agnostic).
    """
    _ensure_adapter_on_path()
    from framework_bridge import _make_autogen_mcp_tool

    # Minimal fake tool + trial — we only need the wrapper to construct,
    # not actually call anything. The FunctionTool() construction is where
    # autogen's get_typed_signature runs.
    class _FakeMcpTool:
        name = "weather"
        description = "fake tool for B1 regression"
        inputSchema = {"type": "object", "properties": {}}

    class _FakeTrial:
        _httpx_factory = None

    # Should NOT raise KeyError('kwargs'). Any other downstream error is
    # acceptable here — the bug we're pinning is specifically the typed
    # signature crash at construction time.
    tool = _make_autogen_mcp_tool(
        _FakeMcpTool(), "http://x/mcp", {}, _FakeTrial(),
    )
    # Sanity: it's a FunctionTool-shaped object.
    assert tool is not None
    assert getattr(tool, "name", None) == "weather"
