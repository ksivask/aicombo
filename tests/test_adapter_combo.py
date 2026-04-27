"""Tests for adapters/combo — E24 multi-LLM-same-CID round-robin dispatch.

Covers Trial.__init__ shape resolution (str + list forms of `llm`),
round-robin LLM selection, canonical-history → API-shape translation
(both openai chat shape and anthropic messages shape), and the E21
reset hook. End-to-end network calls are NOT exercised here — those
require a live AGW + provider keys (and run in the docker compose
smoke); these tests pin offline shape contracts.

Pattern matches tests/test_adapter_langchain*.py: prune sibling adapter
dirs from sys.path so that the bare module name `framework_bridge`
resolves to combo's bridge.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_ADAPTER_DIR     = str(Path(__file__).resolve().parent.parent / "adapters" / "combo")
_LANGCHAIN_DIR   = str(Path(__file__).resolve().parent.parent / "adapters" / "langchain")
_LANGGRAPH_DIR   = str(Path(__file__).resolve().parent.parent / "adapters" / "langgraph")
_DIRECT_MCP_DIR  = str(Path(__file__).resolve().parent.parent / "adapters" / "direct-mcp")
_AUTOGEN_DIR     = str(Path(__file__).resolve().parent.parent / "adapters" / "autogen")
_CREWAI_DIR      = str(Path(__file__).resolve().parent.parent / "adapters" / "crewai")
_PYDANTIC_AI_DIR = str(Path(__file__).resolve().parent.parent / "adapters" / "pydantic_ai")
_LLAMAINDEX_DIR  = str(Path(__file__).resolve().parent.parent / "adapters" / "llamaindex")


def _ensure_adapter_on_path():
    """Force `framework_bridge` to resolve to the combo adapter's copy."""
    for other in (
        _LANGCHAIN_DIR, _LANGGRAPH_DIR, _DIRECT_MCP_DIR, _AUTOGEN_DIR,
        _CREWAI_DIR, _PYDANTIC_AI_DIR, _LLAMAINDEX_DIR,
    ):
        while other in sys.path:
            sys.path.remove(other)
    while _ADAPTER_DIR in sys.path:
        sys.path.remove(_ADAPTER_DIR)
    sys.path.insert(0, _ADAPTER_DIR)
    sys.modules.pop("framework_bridge", None)


@pytest.fixture
def combo_env(monkeypatch):
    """Minimal env wiring so Trial.__init__ resolves base URLs / keys."""
    monkeypatch.setenv("AGW_LLM_BASE_URL_OLLAMA",    "http://gateway:8080/llm/ollama/v1")
    monkeypatch.setenv("AGW_LLM_BASE_URL_OPENAI",    "http://gateway:8080/llm/chatgpt/v1")
    monkeypatch.setenv("AGW_LLM_BASE_URL_ANTHROPIC", "http://gateway:8080/llm/claude")
    monkeypatch.setenv("AGW_LLM_BASE_URL_MOCK",      "http://gateway:8080/llm/mock/v1")
    monkeypatch.setenv("AGW_LLM_BASE_URL_GEMINI",    "http://gateway:8080/llm/gemini/v1beta/openai")
    monkeypatch.setenv("OPENAI_API_KEY",    "sk-test-fake")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-fake")
    monkeypatch.setenv("DEFAULT_OLLAMA_MODEL", "qwen2.5:7b")
    _ensure_adapter_on_path()


def _cfg(llm, api: str = "chat", model=None, mcp: str = "NONE") -> dict:
    cfg = {
        "framework": "combo",
        "api": api,
        "stream": False,
        "state": False,
        "llm": llm,
        "mcp": mcp,
        "routing": "via_agw",
    }
    if model is not None:
        cfg["model"] = model
    return cfg


# ── __init__: llm-list resolution ──

async def test_init_resolves_llm_list_from_str_form(combo_env):
    """Single-string `llm` is accepted (legacy/shorthand): the round-robin
    just degenerates to a 1-element rotation."""
    from framework_bridge import Trial
    trial = Trial(trial_id="t-str", config=_cfg(llm="ollama"))
    try:
        assert trial._llm_list == ["ollama"]
        assert "ollama" in trial._clients
        # Round-robin on length-1 list always returns the same llm.
        assert trial._llm_for_turn(0) == "ollama"
        assert trial._llm_for_turn(7) == "ollama"
    finally:
        await trial.aclose()


async def test_init_resolves_llm_list_from_list_form(combo_env):
    """List `llm` is the primary intent — each unique provider gets its
    own SDK client (openai-shape vs anthropic-shape)."""
    import openai
    import anthropic
    from framework_bridge import Trial
    trial = Trial(
        trial_id="t-list", config=_cfg(llm=["chatgpt", "claude"], api="chat"),
    )
    try:
        assert trial._llm_list == ["chatgpt", "claude"]
        # Two unique LLMs → two clients, one per provider shape.
        assert isinstance(trial._clients["chatgpt"], openai.AsyncOpenAI)
        assert isinstance(trial._clients["claude"], anthropic.AsyncAnthropic)
    finally:
        await trial.aclose()


def test_init_rejects_empty_llm_list(combo_env):
    """Non-empty llm is required — empty list is a hard ValueError."""
    from framework_bridge import Trial
    with pytest.raises(ValueError, match="non-empty llm"):
        Trial(trial_id="t-empty", config=_cfg(llm=[]))


def test_init_rejects_unsupported_llm_name(combo_env):
    """Unknown llm names fall through pick_llm_base_url's ValueError."""
    from framework_bridge import Trial
    with pytest.raises(ValueError):
        Trial(trial_id="t-bad", config=_cfg(llm="not-a-real-llm"))


# ── Round-robin selection ──

async def test_llm_for_turn_round_robins_two_providers(combo_env):
    """turn_idx 0,1,2,3 over a 2-LLM list → llm[0], llm[1], llm[0], llm[1]."""
    from framework_bridge import Trial
    trial = Trial(
        trial_id="t-rr", config=_cfg(llm=["chatgpt", "claude"], api="chat"),
    )
    try:
        assert trial._llm_for_turn(0) == "chatgpt"
        assert trial._llm_for_turn(1) == "claude"
        assert trial._llm_for_turn(2) == "chatgpt"
        assert trial._llm_for_turn(3) == "claude"
    finally:
        await trial.aclose()


async def test_llm_for_turn_round_robins_three_providers(combo_env):
    """3-LLM list → modular indexing across 6 turns."""
    from framework_bridge import Trial
    trial = Trial(
        trial_id="t-rr3",
        config=_cfg(llm=["ollama", "chatgpt", "claude"], api="chat"),
    )
    try:
        seq = [trial._llm_for_turn(i) for i in range(6)]
        assert seq == ["ollama", "chatgpt", "claude",
                       "ollama", "chatgpt", "claude"]
    finally:
        await trial.aclose()


# ── Shape translation: canonical → API-specific wire shape ──

async def test_to_shape_openai_format_round_trips_marker(combo_env):
    """Canonical history → openai chat shape: {role, content: <string>}.
    Marker text rides verbatim in `content` so AGW's regex sees it."""
    from framework_bridge import Trial
    trial = Trial(trial_id="t-sh1", config=_cfg(llm=["chatgpt"]))
    try:
        canonical = [
            {"role": "user", "content_text": "hello"},
            {"role": "assistant",
             "content_text": "hi back<!-- ib:cid=ib_aaaaaaaaaaaa -->"},
        ]
        shape = trial._to_shape(canonical, "chatgpt")
        assert shape == [
            {"role": "user", "content": "hello"},
            {"role": "assistant",
             "content": "hi back<!-- ib:cid=ib_aaaaaaaaaaaa -->"},
        ]
        # Same shape for ollama/mock/gemini (all openai-compat).
        for openai_shape_llm in ("ollama", "mock", "gemini"):
            assert trial._to_shape(canonical, openai_shape_llm) == shape
    finally:
        await trial.aclose()


async def test_to_shape_anthropic_format_round_trips_marker(combo_env):
    """Canonical history → anthropic messages shape: {role, content: [{type:text, text: <string>}]}.
    Marker text rides inside the content block's `text` field."""
    from framework_bridge import Trial
    trial = Trial(
        trial_id="t-sh2", config=_cfg(llm=["claude"], api="messages"),
    )
    try:
        canonical = [
            {"role": "user", "content_text": "hello"},
            {"role": "assistant",
             "content_text": "hi<!-- ib:cid=ib_bbbbbbbbbbbb -->"},
        ]
        shape = trial._to_shape(canonical, "claude")
        assert shape == [
            {"role": "user",
             "content": [{"type": "text", "text": "hello"}]},
            {"role": "assistant",
             "content": [{"type": "text",
                          "text": "hi<!-- ib:cid=ib_bbbbbbbbbbbb -->"}]},
        ]
    finally:
        await trial.aclose()


async def test_to_shape_rejects_unknown_llm(combo_env):
    """Shape translator fails fast on an unmapped llm name (defensive
    guard — should never trigger in normal flow because _build_clients
    has already rejected unknown llms)."""
    from framework_bridge import Trial
    trial = Trial(trial_id="t-sh3", config=_cfg(llm=["chatgpt"]))
    try:
        with pytest.raises(ValueError, match="unsupported llm shape"):
            trial._to_shape([], "not-a-real-llm")
    finally:
        await trial.aclose()


# ── Model resolution ──

async def test_model_for_turn_uses_default_when_no_override(combo_env):
    """No `model` in config + no override → DEFAULT_MODEL_PER_LLM."""
    from framework_bridge import Trial, DEFAULT_MODEL_PER_LLM
    trial = Trial(
        trial_id="t-m1", config=_cfg(llm=["chatgpt", "claude"]),
    )
    try:
        assert trial._model_for_turn(0, None) == DEFAULT_MODEL_PER_LLM["chatgpt"]
        assert trial._model_for_turn(1, None) == DEFAULT_MODEL_PER_LLM["claude"]
    finally:
        await trial.aclose()


async def test_model_for_turn_string_applies_to_all(combo_env):
    """`model` as a string is used for every turn regardless of llm."""
    from framework_bridge import Trial
    trial = Trial(
        trial_id="t-m2",
        config=_cfg(llm=["chatgpt", "claude"], model="custom-model"),
    )
    try:
        assert trial._model_for_turn(0, None) == "custom-model"
        assert trial._model_for_turn(1, None) == "custom-model"
    finally:
        await trial.aclose()


async def test_model_for_turn_list_pairs_with_llm(combo_env):
    """`model` as a list pairs 1:1 with the llm list (E23 schema)."""
    from framework_bridge import Trial
    trial = Trial(
        trial_id="t-m3",
        config=_cfg(llm=["chatgpt", "claude"],
                    model=["gpt-4o-mini", "claude-haiku-4-5"]),
    )
    try:
        assert trial._model_for_turn(0, None) == "gpt-4o-mini"
        assert trial._model_for_turn(1, None) == "claude-haiku-4-5"
        # And it round-robins on subsequent turns too.
        assert trial._model_for_turn(2, None) == "gpt-4o-mini"
    finally:
        await trial.aclose()


# ── E21 reset hook ──

async def test_drive_reset_clears_canonical_history(combo_env):
    """reset wipes _canonical_history + drops the captured X-IB-CID."""
    from framework_bridge import Trial
    trial = Trial(trial_id="t-reset", config=_cfg(llm=["chatgpt"]))
    try:
        # Pre-seed state so we can verify the wipe.
        trial._canonical_history = [
            {"role": "user", "content_text": "hi"},
            {"role": "assistant", "content_text": "hello"},
        ]
        trial._observed_cid_header = "ib_aaaaaaaaaaaa"
        trial._http_client.headers["X-IB-CID"] = "ib_aaaaaaaaaaaa"

        result = await trial._drive_reset()
        assert result["reset"] is True
        assert trial._canonical_history == []
        assert trial._observed_cid_header is None
        assert "X-IB-CID" not in trial._http_client.headers
        # The cleared list should mention each thing that got wiped.
        assert "_canonical_history" in result["cleared"]
        assert "_observed_cid_header" in result["cleared"]
    finally:
        await trial.aclose()


async def test_drive_refresh_tools_is_noop_when_no_mcps(combo_env):
    """Without any MCPs configured, refresh is a documented no-op (skipped)."""
    from framework_bridge import Trial
    trial = Trial(trial_id="t-rt", config=_cfg(llm=["chatgpt"]))
    try:
        result = await trial._drive_refresh_tools()
        assert result["refresh_tools"] == "skipped"
        assert "no MCPs" in result["reason"]
    finally:
        await trial.aclose()


# ── Marker preservation across shape translations ──

async def test_canonical_history_preserves_marker_text_across_shapes(combo_env):
    """The whole point of the combo adapter: marker text rides through
    BOTH shape translations verbatim, so an assistant message carrying
    `<!-- ib:cid=ib_xxx -->` from turn N (e.g. openai chat) lands in
    turn N+1's input shape (e.g. anthropic messages) where AGW's regex
    can re-detect the same CID and reuse it."""
    from framework_bridge import Trial
    trial = Trial(
        trial_id="t-pres", config=_cfg(llm=["chatgpt", "claude"]),
    )
    try:
        marker = "<!-- ib:cid=ib_abcdef012345 -->"
        canonical = [
            {"role": "user", "content_text": "hello"},
            {"role": "assistant", "content_text": f"hi back {marker}"},
        ]
        # Render into BOTH shapes; both must contain the marker substring
        # in a place AGW's marker scanner can find it.
        openai_shape = trial._to_shape(canonical, "chatgpt")
        anthropic_shape = trial._to_shape(canonical, "claude")
        assert marker in openai_shape[1]["content"]
        assert marker in anthropic_shape[1]["content"][0]["text"]
    finally:
        await trial.aclose()


# ── E24a — multi-MCP fan-out (list-form `mcp` + connect/dispatch) ──

class _FakeMCPClient:
    """Minimal stand-in for fastmcp.Client used by E24a unit tests.

    Behaves as an async context manager (mirrors fastmcp's `async with`
    session semantics that combo's `_dispatch_tool_call` and
    `_connect_mcps_if_needed` rely on) and exposes async `list_tools`
    and `call_tool` methods returning canned data.
    """

    def __init__(self, tools=None, call_results=None):
        self._tools = tools or []
        self._call_results = call_results or {}
        self.list_tools_calls = 0
        self.call_tool_calls: list[tuple[str, dict]] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def list_tools(self):
        self.list_tools_calls += 1
        return list(self._tools)

    async def call_tool(self, name, args):
        self.call_tool_calls.append((name, args))
        result = self._call_results.get(name, {"content": [{"type": "text", "text": f"ok:{name}"}]})
        return result


def _fake_tool(name, description="", input_schema=None):
    """Build a dict-shaped tool entry — combo's _tool_to_dict accepts dicts."""
    return {
        "name": name,
        "description": description,
        "inputSchema": input_schema or {"type": "object", "properties": {}},
    }


def test_init_accepts_list_form_mcp(combo_env):
    """E24a: config with `mcp: [..]` populates `_mcp_list` from the list
    and leaves the pool unconnected (eager-once happens at first turn)."""
    from framework_bridge import Trial
    trial = Trial(
        trial_id="t-mcplist",
        config=_cfg(llm="chatgpt", mcp=["weather", "fetch"]),
    )
    try:
        assert trial._mcp_list == ["weather", "fetch"]
        assert trial._mcp_connected is False
        assert trial._mcp_clients == {}
        assert trial._merged_tool_catalog == []
        assert trial._tool_routing == {}
    finally:
        import asyncio
        asyncio.get_event_loop().run_until_complete(trial.aclose())


def test_init_accepts_str_form_mcp_for_backwards_compat(combo_env):
    """E24a: legacy single-string `mcp: "weather"` coerces to a 1-elt list."""
    from framework_bridge import Trial
    trial = Trial(
        trial_id="t-mcpstr",
        config=_cfg(llm="chatgpt", mcp="weather"),
    )
    try:
        assert trial._mcp_list == ["weather"]
        assert trial._mcp_connected is False
    finally:
        import asyncio
        asyncio.get_event_loop().run_until_complete(trial.aclose())


def test_init_accepts_none_mcp_as_empty_list(combo_env):
    """E24a: mcp='NONE' (and the missing field default) yields an empty
    `_mcp_list` so the no-MCP path stays the same as pre-E24a behavior."""
    from framework_bridge import Trial
    trial = Trial(
        trial_id="t-mcpnone",
        config=_cfg(llm="chatgpt", mcp="NONE"),
    )
    try:
        assert trial._mcp_list == []
    finally:
        import asyncio
        asyncio.get_event_loop().run_until_complete(trial.aclose())


async def test_connect_mcps_merges_tool_catalogs(combo_env, monkeypatch):
    """E24a: connecting to N MCPs merges their tools into one catalog +
    populates the `(tool_name -> server)` routing table."""
    from framework_bridge import Trial
    trial = Trial(
        trial_id="t-merge",
        config=_cfg(llm="chatgpt", mcp=["weather", "fetch"]),
    )
    try:
        weather_client = _FakeMCPClient(tools=[
            _fake_tool("weather_get", "get the weather"),
            _fake_tool("weather_forecast", "10-day forecast"),
        ])
        fetch_client = _FakeMCPClient(tools=[
            _fake_tool("fetch_url", "fetch a URL"),
        ])
        clients_by_name = {"weather": weather_client, "fetch": fetch_client}

        def fake_build(self, mcp_name):
            return clients_by_name[mcp_name]

        monkeypatch.setattr(Trial, "_build_mcp_client", fake_build)

        await trial._connect_mcps_if_needed()

        # Pool populated for both servers + connect flag set.
        assert trial._mcp_connected is True
        assert set(trial._mcp_clients.keys()) == {"weather", "fetch"}
        # Merged catalog has all 3 tools (preserving each server's tools).
        names = {t["name"] for t in trial._merged_tool_catalog}
        assert names == {"weather_get", "weather_forecast", "fetch_url"}
        # Routing table maps each tool to its originating server.
        assert trial._tool_routing == {
            "weather_get":      "weather",
            "weather_forecast": "weather",
            "fetch_url":        "fetch",
        }
        # Idempotent: a second call doesn't re-list_tools on either client.
        await trial._connect_mcps_if_needed()
        assert weather_client.list_tools_calls == 1
        assert fetch_client.list_tools_calls == 1
    finally:
        await trial.aclose()


async def test_connect_mcps_warns_on_name_collision(combo_env, monkeypatch, caplog):
    """E24a: when two MCPs both expose `get_thing`, the LATER server wins
    in the routing table and a WARNING is logged with both server names."""
    import logging
    from framework_bridge import Trial
    trial = Trial(
        trial_id="t-collide",
        config=_cfg(llm="chatgpt", mcp=["weather", "fetch"]),
    )
    try:
        weather_client = _FakeMCPClient(tools=[
            _fake_tool("get_thing", "weather flavor"),
        ])
        fetch_client = _FakeMCPClient(tools=[
            _fake_tool("get_thing", "fetch flavor"),
        ])
        clients_by_name = {"weather": weather_client, "fetch": fetch_client}

        def fake_build(self, mcp_name):
            return clients_by_name[mcp_name]

        monkeypatch.setattr(Trial, "_build_mcp_client", fake_build)

        with caplog.at_level(logging.WARNING, logger="aiplay.adapter.combo"):
            await trial._connect_mcps_if_needed()

        # Last-server-wins on routing.
        assert trial._tool_routing["get_thing"] == "fetch"
        # Catalog has the LATE entry only (deduplicated by name).
        gt_entries = [t for t in trial._merged_tool_catalog
                      if t["name"] == "get_thing"]
        assert len(gt_entries) == 1
        assert gt_entries[0]["description"] == "fetch flavor"
        # Warning surfaced and mentions BOTH server names.
        warn_records = [
            r for r in caplog.records
            if r.levelname == "WARNING" and "collision" in r.getMessage()
        ]
        assert warn_records, "expected a collision WARNING"
        msg = warn_records[0].getMessage()
        assert "weather" in msg and "fetch" in msg
    finally:
        await trial.aclose()


async def test_dispatch_tool_call_routes_to_correct_server(combo_env):
    """E24a: _dispatch_tool_call("weather_get", ..) hits weather's
    fastmcp.Client, NOT fetch's."""
    from framework_bridge import Trial
    trial = Trial(
        trial_id="t-route",
        config=_cfg(llm="chatgpt", mcp=["weather", "fetch"]),
    )
    try:
        weather_client = _FakeMCPClient(call_results={
            "weather_get": {"content": [{"type": "text", "text": "sunny 72F"}]},
        })
        fetch_client = _FakeMCPClient()
        # Hand-build the routing table so this test focuses on dispatch only
        # (not on the connect path that test_connect_mcps_merges_tool_catalogs covers).
        trial._mcp_clients = {"weather": weather_client, "fetch": fetch_client}
        trial._tool_routing = {"weather_get": "weather", "fetch_url": "fetch"}
        trial._mcp_connected = True

        result = await trial._dispatch_tool_call("weather_get", {"city": "Seattle"})

        assert weather_client.call_tool_calls == [("weather_get", {"city": "Seattle"})]
        assert fetch_client.call_tool_calls == []  # not touched
        assert "sunny 72F" in result["content"]
    finally:
        await trial.aclose()


async def test_dispatch_tool_call_unknown_tool_errors(combo_env):
    """E24a: dispatching an unmapped tool name returns an `error` dict
    rather than crashing; this lets the LLM see + recover."""
    from framework_bridge import Trial
    trial = Trial(
        trial_id="t-unknown",
        config=_cfg(llm="chatgpt", mcp=["weather"]),
    )
    try:
        # Empty routing table — every call is unknown.
        trial._mcp_clients = {"weather": _FakeMCPClient()}
        trial._tool_routing = {}
        trial._mcp_connected = True

        result = await trial._dispatch_tool_call("nope_tool", {"x": 1})

        assert "error" in result
        assert "nope_tool" in result["error"]
    finally:
        await trial.aclose()


async def test_openai_tool_specs_translates_merged_catalog(combo_env):
    """E24a: _openai_tool_specs renders the merged catalog into the
    OpenAI chat-completions tool-spec format that goes on the wire."""
    from framework_bridge import Trial
    trial = Trial(
        trial_id="t-specs",
        config=_cfg(llm="chatgpt", mcp=["weather"]),
    )
    try:
        trial._merged_tool_catalog = [
            _fake_tool("weather_get", "get the weather",
                       {"type": "object", "properties": {"city": {"type": "string"}}}),
        ]
        specs = trial._openai_tool_specs()
        assert specs == [{
            "type": "function",
            "function": {
                "name":        "weather_get",
                "description": "get the weather",
                "parameters":  {"type": "object", "properties": {"city": {"type": "string"}}},
            },
        }]
    finally:
        await trial.aclose()


async def test_to_shape_openai_renders_tool_call_history(combo_env):
    """E24a: canonical tool_calls / tool messages translate into the
    OpenAI chat-completions wire shape (assistant.tool_calls + role=tool).
    """
    from framework_bridge import Trial
    trial = Trial(trial_id="t-tc-shape", config=_cfg(llm="chatgpt"))
    try:
        canonical = [
            {"role": "user", "content_text": "what's the weather?"},
            {
                "role": "assistant",
                "content_text": "",
                "tool_calls": [
                    {"id": "call_1", "name": "weather_get",
                     "arguments": {"city": "Seattle"}},
                ],
            },
            {"role": "tool", "tool_call_id": "call_1",
             "content_text": "sunny 72F"},
            {"role": "assistant", "content_text": "It's sunny in Seattle."},
        ]
        out = trial._to_shape(canonical, "chatgpt")
        # User message preserved.
        assert out[0] == {"role": "user", "content": "what's the weather?"}
        # Assistant tool-call message: tool_calls present, content None-ish.
        assert out[1]["role"] == "assistant"
        assert out[1]["tool_calls"][0]["id"] == "call_1"
        assert out[1]["tool_calls"][0]["function"]["name"] == "weather_get"
        # Arguments serialised to JSON string per OpenAI spec.
        import json as _json
        assert _json.loads(out[1]["tool_calls"][0]["function"]["arguments"]) == {"city": "Seattle"}
        # Tool-role message carries tool_call_id linking back to the call.
        assert out[2] == {"role": "tool", "tool_call_id": "call_1", "content": "sunny 72F"}
        # Final plain-text assistant.
        assert out[3] == {"role": "assistant", "content": "It's sunny in Seattle."}
    finally:
        await trial.aclose()


async def test_to_shape_anthropic_filters_tool_messages(combo_env, caplog):
    """E24a: until E24b lands cross-shape tool_use translation, claude
    history shape SILENTLY DROPS tool_calls/tool messages and logs ONE
    informational warning per trial. User + plain assistant text (which
    carries the AGW CID marker) is preserved."""
    import logging
    from framework_bridge import Trial
    trial = Trial(trial_id="t-cs", config=_cfg(llm=["claude"], api="messages"))
    try:
        canonical = [
            {"role": "user", "content_text": "hi"},
            {"role": "assistant", "content_text": "",
             "tool_calls": [{"id": "c", "name": "n", "arguments": {}}]},
            {"role": "tool", "tool_call_id": "c", "content_text": "result"},
            {"role": "assistant", "content_text": "the answer is 42"},
        ]
        with caplog.at_level(logging.WARNING, logger="aiplay.adapter.combo"):
            out = trial._to_shape(canonical, "claude")

        # Only user + plain assistant survive; both rendered as anthropic blocks.
        assert len(out) == 2
        assert out[0]["role"] == "user"
        assert out[0]["content"][0]["text"] == "hi"
        assert out[1]["role"] == "assistant"
        assert out[1]["content"][0]["text"] == "the answer is 42"
        # Warning surfaced exactly once and mentions E24b.
        warn_msgs = [r.getMessage() for r in caplog.records
                     if r.levelname == "WARNING"]
        assert any("E24b" in m for m in warn_msgs)
    finally:
        await trial.aclose()
