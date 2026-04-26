"""I-NEW-3: spot-checks for the trial-detail page's NOTE-tab rule registry.

The NOTE-tab condition→note rules live in `frontend/trial.js::collectNotes()`
(JS, not Python). After I-NEW-1, the framework-capability rules consume
`/info.frameworks` — itself a mirror of `harness/validator.py::ADAPTER_CAPABILITIES`.

There is no Python NOTE registry to test directly. Instead these 5
spot-checks pin the SOURCE-OF-TRUTH capability bits the JS rules depend
on, plus their `/info` exposure. If a contributor edits
ADAPTER_CAPABILITIES (e.g. adds Responses support to crewai), the
relevant test fails BEFORE the corresponding JS rule silently misfires
on a stale assumption.

Picks (matched to specific JS rules in trial.js):
  1. langchain supports all 4 APIs — baseline 'no NOTE rule fires'.
  2. crewai supports {chat, messages} only — JS rule fires for
     responses / responses+conv.
  3. pydantic-ai supports {chat, messages, responses} — JS rule fires
     for responses+conv only.
  4. llamaindex supports {chat, responses, responses+conv} — JS rule
     fires for messages only.
  5. direct-mcp supports nothing — used when llm=NONE; the JS no-MCP
     note depends on this being a 'no APIs' framework.

Each test asserts BOTH the validator dict AND the /info.frameworks
mirror, so drift is caught at either layer.
"""
from __future__ import annotations

from fastapi.testclient import TestClient

from main import app
from validator import ADAPTER_CAPABILITIES


# ── Helpers ──

def _info_frameworks() -> dict:
    """Fetch /info and return its `frameworks` dict for assertions."""
    with TestClient(app) as client:
        r = client.get("/info")
        assert r.status_code == 200, f"/info returned {r.status_code}"
        return r.json()["frameworks"]


# ── Spot-check 1: langchain supports all 4 APIs ──

def test_langchain_supports_all_four_apis():
    """JS NOTE-tab baseline: a langchain row with any (chat | messages |
    responses | responses+conv) does NOT trigger a 'framework doesn't
    implement api' note. If langchain ever loses an API, the JS rule
    registry needs a new entry — this test forces that conversation."""
    expected = {"chat", "messages", "responses", "responses+conv"}
    assert ADAPTER_CAPABILITIES["langchain"] == expected, (
        "validator: langchain capability set drift"
    )
    info_fw = _info_frameworks()
    assert info_fw["langchain"]["supported_apis"] == sorted(expected), (
        "/info: langchain capability mirror drift"
    )


# ── Spot-check 2: crewai supports {chat, messages} only ──

def test_crewai_supports_chat_and_messages_only():
    """JS rule (FRAMEWORK_CAPABILITY_NOTES, framework=crewai,
    apis=[responses, responses+conv]) fires for those APIs only when
    /info confirms crewai genuinely doesn't implement them. If a
    future crewai release adds Responses support and the validator is
    updated, this test fails — prompting the contributor to remove
    the now-stale JS rule."""
    expected = {"chat", "messages"}
    assert ADAPTER_CAPABILITIES["crewai"] == expected, (
        "validator: crewai capability set drift"
    )
    info_fw = _info_frameworks()
    assert info_fw["crewai"]["supported_apis"] == sorted(expected)
    # The negative invariants the JS rule depends on:
    assert "responses" not in expected
    assert "responses+conv" not in expected


# ── Spot-check 3: pydantic-ai supports {chat, messages, responses} ──

def test_pydantic_ai_does_not_support_responses_conv():
    """JS rule (framework=pydantic-ai, apis=[responses+conv]) fires
    for responses+conv only. If pydantic-ai's adapter gains
    responses+conv support (E5d resolution) the validator must be
    updated — at which point this test fails and the JS rule should
    be retired."""
    expected = {"chat", "messages", "responses"}
    assert ADAPTER_CAPABILITIES["pydantic-ai"] == expected, (
        "validator: pydantic-ai capability set drift"
    )
    info_fw = _info_frameworks()
    assert info_fw["pydantic-ai"]["supported_apis"] == sorted(expected)
    assert "responses+conv" not in expected


# ── Spot-check 4: llamaindex supports {chat, responses, responses+conv} ──

def test_llamaindex_does_not_support_messages():
    """JS rule (framework=llamaindex, apis=[messages]) fires for
    messages only. Tied to E5e (no llama-index-llms-anthropic). If
    that changes, validator + JS rule both need updating; this test
    forces the discussion."""
    expected = {"chat", "responses", "responses+conv"}
    assert ADAPTER_CAPABILITIES["llamaindex"] == expected, (
        "validator: llamaindex capability set drift"
    )
    info_fw = _info_frameworks()
    assert info_fw["llamaindex"]["supported_apis"] == sorted(expected)
    assert "messages" not in expected


# ── Spot-check 5: direct-mcp has zero LLM API support ──

def test_direct_mcp_supports_no_llm_apis():
    """The 'No MCP — Channel 3 won't fire' JS note (and the runner's
    direct-mcp auto-selection when llm=NONE) depend on direct-mcp
    being a no-LLM framework. If a future direct-mcp adapter gains
    an LLM API, the runner's selection logic in api.py:362-364 needs
    revisiting and this assumption is no longer load-bearing."""
    expected: set = set()
    assert ADAPTER_CAPABILITIES["direct-mcp"] == expected, (
        "validator: direct-mcp must remain a no-LLM-API framework"
    )
    info_fw = _info_frameworks()
    assert info_fw["direct-mcp"]["supported_apis"] == [], (
        "/info: direct-mcp must surface as 'no supported APIs'"
    )
