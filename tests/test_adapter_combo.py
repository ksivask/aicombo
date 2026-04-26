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


async def test_drive_refresh_tools_is_noop_first_cut(combo_env):
    """Combo first-cut has no MCP integration — refresh is documented no-op."""
    from framework_bridge import Trial
    trial = Trial(trial_id="t-rt", config=_cfg(llm=["chatgpt"]))
    try:
        result = await trial._drive_refresh_tools()
        assert result["refresh_tools"] == "skipped"
        assert "no MCP" in result["reason"]
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
