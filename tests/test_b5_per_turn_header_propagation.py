"""B5 regression — per-turn X-Harness-Turn-ID must reach outbound HTTP.

The B4 fix (commit b159fa3) surfaced that `httpx.AsyncClient(headers={...})`
COPIES the dict into its own internal `Headers` store at construction.
Subsequent mutations of the original dict do NOT propagate to outbound
requests. The "mutable headers dict trick" used across multiple adapter
`framework_bridge.py` files for per-turn `X-Harness-Turn-ID` injection
was silently broken wherever this pattern was used. The langchain /
langgraph / crewai variants used `default_headers=` on a wrapped LLM
instead — same shape: assignment after construction is dropped because
the underlying openai SDK snapshotted the empty dict at __init__.

This test module audits all 7 adapters by:
  1. Building a Trial with mocks that prevent real LLM/MCP HTTP traffic.
  2. Driving the adapter's actual `Trial.turn()` (or `_set_turn_headers`
     for direct-mcp) so the in-product header-application code path runs.
  3. Issuing an outbound httpx call through the trial's hooked
     `_http_client`, attaching a request-capture event hook to grab the
     real outbound headers (the request never reaches a real network —
     the example.test domain is unroutable; we only care about request
     *construction*, which fires before DNS).
  4. Asserting `x-harness-turn-id` matches the per-turn value.

If any test fails, the adapter has the B4-pattern bug: header mutation
went into a dict that httpx already copied or into a wrapper field that
the underlying SDK no longer reads.

Net consequence: header-demux at AGW (when E18 lands and cidgar logs
X-Harness-Trial-ID) becomes physically possible for the first time.
Until B5, the headers were silently dropped at adapter construction
on most paths — only time-window correlation worked for audit demux.
"""
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

_PYDANTIC_AI_DIR = str(Path(__file__).resolve().parent.parent / "adapters" / "pydantic_ai")
_AUTOGEN_DIR     = str(Path(__file__).resolve().parent.parent / "adapters" / "autogen")
_LLAMAINDEX_DIR  = str(Path(__file__).resolve().parent.parent / "adapters" / "llamaindex")
_LANGCHAIN_DIR   = str(Path(__file__).resolve().parent.parent / "adapters" / "langchain")
_LANGGRAPH_DIR   = str(Path(__file__).resolve().parent.parent / "adapters" / "langgraph")
_CREWAI_DIR      = str(Path(__file__).resolve().parent.parent / "adapters" / "crewai")
_DIRECT_MCP_DIR  = str(Path(__file__).resolve().parent.parent / "adapters" / "direct-mcp")

_ALL_ADAPTER_DIRS = (
    _PYDANTIC_AI_DIR, _AUTOGEN_DIR, _LLAMAINDEX_DIR,
    _LANGCHAIN_DIR, _LANGGRAPH_DIR, _CREWAI_DIR, _DIRECT_MCP_DIR,
)


def _select_adapter(adapter_dir: str) -> None:
    """Make `adapter_dir` win the framework_bridge import race."""
    for other in _ALL_ADAPTER_DIRS:
        if other == adapter_dir:
            continue
        while other in sys.path:
            sys.path.remove(other)
    while adapter_dir in sys.path:
        sys.path.remove(adapter_dir)
    sys.path.insert(0, adapter_dir)
    sys.modules.pop("framework_bridge", None)


async def _capture_outbound_headers(http_client) -> dict:
    """Issue a probe GET through `http_client` and return captured headers.

    The destination is unroutable (example.test); httpx still constructs
    the Request and fires the request event hook before any DNS / TCP
    activity, which is all we need to verify header propagation.
    """
    captured: dict = {}

    async def capture_req(request) -> None:
        captured["headers_lower"] = {
            k.lower(): v for k, v in request.headers.items()
        }

    http_client.event_hooks.setdefault("request", []).append(capture_req)
    try:
        await http_client.get("http://example.test/probe")
    except Exception:
        # Connection failure is expected — example.test is unroutable.
        # The request event hook fires BEFORE the connect attempt.
        pass
    return captured


def _assert_header_reaches_wire(captured: dict, expected_turn_id: str,
                                adapter_name: str) -> None:
    """Common assertion: `X-Harness-Turn-ID` is on the wire and equal."""
    headers_lower = captured.get("headers_lower") or {}
    assert "x-harness-turn-id" in headers_lower, (
        f"B5 [{adapter_name}]: X-Harness-Turn-ID is NOT on the outbound "
        f"request — the adapter's per-turn header mutation was silently "
        f"dropped (B4 pattern: httpx copy-on-construction OR wrapped-SDK "
        f"default_headers snapshot at __init__). "
        f"Captured headers: {sorted(headers_lower.keys())}"
    )
    assert headers_lower["x-harness-turn-id"] == expected_turn_id, (
        f"B5 [{adapter_name}]: X-Harness-Turn-ID on the wire is "
        f"{headers_lower['x-harness-turn-id']!r}, expected "
        f"{expected_turn_id!r} — the live httpx Headers store wasn't "
        f"updated by the per-turn code path."
    )


# ──────────────────────────────────────────────────────────────────────
# 1. pydantic-ai — already fixed in B4 (commit b159fa3).
# ──────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_b5_pydantic_ai_per_turn_header_reaches_outbound(monkeypatch):
    _select_adapter(_PYDANTIC_AI_DIR)
    monkeypatch.setenv("AGW_LLM_BASE_URL_OPENAI", "http://example.test/v1")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-fake")
    import framework_bridge
    from framework_bridge import Trial

    fake_model = MagicMock(name="fake_model")
    fake_agent = MagicMock(name="fake_agent")
    # agent.run must return a result-like object so turn() doesn't crash.
    async def fake_run(**_):
        r = MagicMock()
        r.output = "ok"
        r.all_messages = MagicMock(return_value=[])
        return r
    fake_agent.run = fake_run

    with patch.object(framework_bridge, "_build_model", return_value=fake_model), \
         patch("pydantic_ai.Agent", MagicMock(return_value=fake_agent)):
        trial = Trial(trial_id="t-pa-b5", config={
            "framework": "pydantic-ai", "api": "chat",
            "stream": False, "state": False,
            "llm": "chatgpt", "mcp": "NONE", "routing": "via_agw",
        })
        try:
            # Drive the real turn() — its header-application code is the
            # B4 fix that mirrors onto self._http_client.headers.
            await trial.turn("turn-pa-b5", "hi")
            captured = await _capture_outbound_headers(trial._http_client)
            _assert_header_reaches_wire(captured, "turn-pa-b5", "pydantic-ai")
        finally:
            await trial.aclose()


# ──────────────────────────────────────────────────────────────────────
# 2. autogen — had the B4 dict-snapshot bug. B5 fix mirrors onto
#    self._http_client.headers in turn().
# ──────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_b5_autogen_per_turn_header_reaches_outbound(monkeypatch):
    _select_adapter(_AUTOGEN_DIR)
    monkeypatch.setenv("AGW_LLM_BASE_URL_OLLAMA", "http://example.test/v1")
    import framework_bridge
    from framework_bridge import Trial

    fake_client = MagicMock(name="fake_model_client")
    with patch.object(framework_bridge, "_build_model_client", return_value=fake_client):
        trial = Trial(trial_id="t-ag-b5", config={
            "framework": "autogen", "api": "chat",
            "stream": False, "state": False,
            "llm": "ollama", "mcp": "NONE", "routing": "via_agw",
        })
        try:
            # Stub the agent so turn() doesn't try to call autogen for real.
            trial._agent = MagicMock(name="stub_agent")
            async def stub_run(*_args, **_kw):
                r = MagicMock()
                r.messages = []
                return r
            trial._agent.run = stub_run

            await trial.turn("turn-ag-b5", "hi")
            captured = await _capture_outbound_headers(trial._http_client)
            _assert_header_reaches_wire(captured, "turn-ag-b5", "autogen")
        finally:
            await trial.aclose()


# ──────────────────────────────────────────────────────────────────────
# 3. llamaindex — had the B4 dict-snapshot bug. B5 fix mirrors onto
#    self._http_client.headers in turn().
# ──────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_b5_llamaindex_per_turn_header_reaches_outbound(monkeypatch):
    _select_adapter(_LLAMAINDEX_DIR)
    monkeypatch.setenv("AGW_LLM_BASE_URL_OLLAMA", "http://example.test/v1")
    import framework_bridge
    from framework_bridge import Trial

    fake_llm = MagicMock(name="fake_llm")
    # achat returns a ChatResponse-like object with .message.content.
    async def fake_achat(messages, **_):
        m = MagicMock()
        m.content = "ok"
        r = MagicMock()
        r.message = m
        return r
    fake_llm.achat = fake_achat

    with patch.object(framework_bridge, "_build_chat_llm", return_value=fake_llm):
        trial = Trial(trial_id="t-li-b5", config={
            "framework": "llamaindex", "api": "chat",
            "stream": False, "state": False,
            "llm": "ollama", "mcp": "NONE", "routing": "via_agw",
        })
        try:
            await trial.turn("turn-li-b5", "hi")
            captured = await _capture_outbound_headers(trial._http_client)
            _assert_header_reaches_wire(captured, "turn-li-b5", "llamaindex")
        finally:
            await trial.aclose()


# ──────────────────────────────────────────────────────────────────────
# 4. langchain — had the default_headers-snapshot variant of B4.
#    Setting `self.llm.default_headers` post-init is silently dropped
#    by the openai SDK (it snapshotted the original dict into
#    `_custom_headers` at __init__). B5 fix mutates the live httpx
#    Headers store via self._http_client.headers per turn.
# ──────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_b5_langchain_per_turn_header_reaches_outbound(monkeypatch):
    _select_adapter(_LANGCHAIN_DIR)
    monkeypatch.setenv("AGW_LLM_BASE_URL_OLLAMA", "http://example.test/v1")
    import framework_bridge
    from framework_bridge import Trial

    fake_llm = MagicMock(name="fake_llm")
    fake_llm.default_headers = {}
    fake_llm.openai_api_base = "http://example.test/v1"
    # ainvoke returns an AIMessage-like object (no tool_calls → loop ends).
    async def fake_ainvoke(messages, **_):
        m = MagicMock()
        m.content = "ok"
        m.tool_calls = []
        m.response_metadata = {}
        return m
    fake_llm.ainvoke = fake_ainvoke

    with patch.object(Trial, "_build_llm", return_value=fake_llm):
        trial = Trial(trial_id="t-lc-b5", config={
            "framework": "langchain", "api": "chat",
            "stream": False, "state": False,
            "llm": "ollama", "mcp": "NONE", "routing": "via_agw",
        })
        try:
            await trial.turn("turn-lc-b5", "hi")
            captured = await _capture_outbound_headers(trial._http_client)
            _assert_header_reaches_wire(captured, "turn-lc-b5", "langchain")
        finally:
            await trial.aclose()


# ──────────────────────────────────────────────────────────────────────
# 5. langgraph — same default_headers-snapshot variant. B5 fix mutates
#    the live httpx Headers store too.
# ──────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_b5_langgraph_per_turn_header_reaches_outbound(monkeypatch):
    _select_adapter(_LANGGRAPH_DIR)
    monkeypatch.setenv("AGW_LLM_BASE_URL_OLLAMA", "http://example.test/v1")
    import framework_bridge
    from framework_bridge import Trial

    fake_llm = MagicMock(name="fake_llm")
    fake_llm.default_headers = {}

    with patch.object(Trial, "_build_llm", return_value=fake_llm):
        trial = Trial(trial_id="t-lg-b5", config={
            "framework": "langgraph", "api": "chat",
            "stream": False, "state": False,
            "llm": "ollama", "mcp": "NONE", "routing": "via_agw",
        })
        try:
            # Stub the compiled graph so we don't need create_react_agent
            # to run a real LLM. _setup_mcp_tools is the path that builds
            # _graph; intercept by setting _graph directly.
            stub_graph = MagicMock(name="stub_graph")
            async def stub_ainvoke(state, **_):
                from langchain_core.messages import AIMessage
                msgs = list(state.get("messages", [])) + [AIMessage(content="ok")]
                return {"messages": msgs}
            stub_graph.ainvoke = stub_ainvoke
            trial._graph = stub_graph

            await trial.turn("turn-lg-b5", "hi")
            captured = await _capture_outbound_headers(trial._http_client)
            _assert_header_reaches_wire(captured, "turn-lg-b5", "langgraph")
        finally:
            await trial.aclose()


# ──────────────────────────────────────────────────────────────────────
# 6. crewai — had the same wrapped-SDK default_headers snapshot bug.
#    Has BOTH a sync and async httpx client (sync drives the native SDK
#    in to_thread; async drives fastmcp). B5 fix mutates BOTH live
#    Headers stores per turn so headers land regardless of path.
# ──────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_b5_crewai_per_turn_header_reaches_outbound(monkeypatch):
    _select_adapter(_CREWAI_DIR)
    monkeypatch.setenv("AGW_LLM_BASE_URL_OLLAMA", "http://example.test/v1")
    import framework_bridge

    fake_llm = MagicMock(name="fake_crewai_llm")
    fake_llm.__class__.__name__ = "OpenAICompatibleCompletion"
    fake_llm.base_url = "http://example.test/v1"
    fake_llm.default_headers = {}
    with patch.object(framework_bridge, "LLM", MagicMock(return_value=fake_llm)), \
         patch.object(framework_bridge, "_rebuild_openai_clients", MagicMock()), \
         patch.object(framework_bridge, "_rebuild_anthropic_clients", MagicMock()):
        from framework_bridge import Trial
        trial = Trial(trial_id="t-cw-b5", config={
            "framework": "crewai", "api": "chat",
            "stream": False, "state": False,
            "llm": "ollama", "mcp": "NONE", "routing": "via_agw",
        })
        try:
            # Crewai's turn() builds an Agent + Crew + Task from real
            # crewai classes and calls Crew.kickoff_async — too heavy to
            # mock the whole stack. Instead, replicate the header-mutation
            # PRELUDE from turn() by calling it through a tiny shim that
            # invokes only the two header-application blocks. We do this
            # by patching Crew so kickoff_async is a no-op stub.
            class _StubCrew:
                def __init__(self, *a, **kw):
                    pass
                async def kickoff_async(self, **_):
                    r = MagicMock()
                    r.raw = "ok"
                    return r
            class _StubAgent:
                def __init__(self, *a, **kw):
                    pass
            class _StubTask:
                def __init__(self, *a, **kw):
                    pass
            with patch.object(framework_bridge, "Crew", _StubCrew), \
                 patch.object(framework_bridge, "Agent", _StubAgent), \
                 patch.object(framework_bridge, "Task", _StubTask):
                await trial.turn("turn-cw-b5", "hi")

            captured = await _capture_outbound_headers(trial._http_client)
            _assert_header_reaches_wire(captured, "turn-cw-b5", "crewai")

            # Also verify the SYNC client carries it — that's the path
            # crewai's native SDK uses inside to_thread.
            assert trial._sync_http_client.headers.get("x-harness-turn-id") \
                == "turn-cw-b5", (
                "B5 [crewai]: X-Harness-Turn-ID missing from the SYNC "
                "httpx client headers — the path crewai's native SDK "
                "uses in to_thread won't carry it. "
                f"Got: {dict(trial._sync_http_client.headers)}"
            )
        finally:
            await trial.aclose()


# ──────────────────────────────────────────────────────────────────────
# 7. direct-mcp — no LLM. The httpx client used for the actual MCP
#    session is built fresh per turn by _httpx_factory inside fastmcp's
#    StreamableHttpTransport (with a NEW headers dict — this avoids
#    the B4 copy-on-construction trap by construction). The placeholder
#    `_http_client` (used for symmetry / aclose) gets its headers
#    updated via _set_turn_headers which turn() invokes first.
# ──────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_b5_direct_mcp_per_turn_header_reaches_outbound(monkeypatch):
    _select_adapter(_DIRECT_MCP_DIR)
    monkeypatch.setenv("AGW_MCP_WEATHER", "http://example.test/mcp")
    from framework_bridge import Trial

    trial = Trial(trial_id="t-dm-b5", config={
        "framework": "direct-mcp", "api": "chat",
        "stream": False, "state": False,
        "llm": "ollama", "mcp": "weather", "routing": "via_agw",
    })
    try:
        # _set_turn_headers is the in-product path turn() uses first.
        # It updates the placeholder _http_client.headers AND rebuilds
        # the fastmcp client with fresh headers for actual MCP traffic.
        trial._set_turn_headers("turn-dm-b5")

        captured = await _capture_outbound_headers(trial._http_client)
        _assert_header_reaches_wire(captured, "turn-dm-b5", "direct-mcp")
    finally:
        await trial.aclose()
