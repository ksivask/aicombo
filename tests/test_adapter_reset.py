"""E21 reset_context + refresh_tools per-adapter unit tests.

One Trial._drive_reset() and one Trial._drive_refresh_tools() check per
framework (langchain, langgraph, crewai, pydantic-ai, autogen, llamaindex,
direct-mcp). The check exercises:

  reset
    - populate the canonical history attrs that exist on the adapter's
      Trial (uses hasattr-driven attribute survey, mirroring the
      production helper)
    - call _drive_reset()
    - assert each populated attr is back to its empty/None state
    - response envelope reports {"reset": True, "api": <api>, ...}

  refresh_tools
    - mcp != "NONE" path: assert the cache attr is set to None on
      adapter Trials that maintain a client-side toolset cache
      (langchain / langgraph / crewai / autogen / llamaindex)
    - mcp == "NONE": assert the skipped sentinel envelope is returned
    - pydantic-ai + direct-mcp: assert the no-op envelope (no cache to
      invalidate per design doc fallback policy)

Each adapter dir gets put on sys.path one-at-a-time because the seven
adapters all expose `framework_bridge` (no per-framework prefix) — the
helper mirrors the path-swap pattern used by tests/test_adapter_*_compact.py.
"""
from __future__ import annotations

import contextlib
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_ROOT = Path(__file__).resolve().parent.parent
_ADAPTER_DIRS: dict[str, Path] = {
    "langchain":   _ROOT / "adapters" / "langchain",
    "langgraph":   _ROOT / "adapters" / "langgraph",
    "crewai":      _ROOT / "adapters" / "crewai",
    "pydantic-ai": _ROOT / "adapters" / "pydantic_ai",
    "autogen":     _ROOT / "adapters" / "autogen",
    "llamaindex":  _ROOT / "adapters" / "llamaindex",
    "direct-mcp":  _ROOT / "adapters" / "direct-mcp",
}


def _swap_to(framework: str) -> None:
    """Make `framework_bridge` resolve to the named adapter."""
    target = str(_ADAPTER_DIRS[framework])
    for other in _ADAPTER_DIRS.values():
        s = str(other)
        while s in sys.path:
            sys.path.remove(s)
    sys.path.insert(0, target)
    sys.modules.pop("framework_bridge", None)


def _common_env(monkeypatch) -> None:
    """Set the env vars every adapter's Trial.__init__ touches."""
    monkeypatch.setenv("AGW_LLM_BASE_URL_OLLAMA", "http://gateway:8080/llm/ollama/v1")
    monkeypatch.setenv("DEFAULT_OLLAMA_MODEL", "qwen2.5:7b")
    monkeypatch.setenv("DIRECT_LLM_BASE_URL_OLLAMA", "http://localhost:11434/v1")
    monkeypatch.setenv("AGW_MCP_WEATHER", "http://gateway:8080/mcp/weather")
    monkeypatch.setenv("DIRECT_MCP_WEATHER", "http://mcp-weather:8000/mcp")


@contextlib.contextmanager
def _build_llm_patches(framework: str):
    """Yield a patch context that lets each adapter's Trial.__init__ run
    without external SDK imports / network setup.

    Some adapters (llamaindex) lazy-import provider-specific modules
    (`llama_index.llms.openai_like`) that aren't shipped in the test
    environment; mocking the build helper sidesteps the import.
    """
    if framework == "llamaindex":
        import framework_bridge as _fb  # noqa: WPS433
        with patch.object(_fb, "_build_chat_llm",
                          return_value=MagicMock(name="fake_llm")):
            yield
        return
    # Other adapters either build cheap mocks (mock LLM provider) or
    # lazy-import langchain/anthropic/etc., which ARE installed.
    yield


# ─────────────────────────────────────────────────────────────────────
# reset_context — per-adapter

def _cfg(framework: str, api: str = "chat", mcp: str = "NONE") -> dict:
    return {
        "framework": framework, "api": api,
        "stream": False, "state": False,
        "llm": "ollama" if framework != "direct-mcp" else "NONE",
        "mcp": mcp if framework != "direct-mcp" else "weather",
        "routing": "via_agw",
        "model": "qwen2.5:7b" if framework != "direct-mcp" else None,
    }


_RESET_PARAMS = [
    "langchain", "langgraph", "crewai", "pydantic-ai",
    "autogen", "llamaindex", "direct-mcp",
]


@pytest.mark.parametrize("framework", _RESET_PARAMS)
async def test_drive_reset_clears_history(framework, monkeypatch):
    """Trial._drive_reset() zeroes every history attr the adapter has set."""
    _common_env(monkeypatch)
    _swap_to(framework)

    from framework_bridge import Trial  # type: ignore

    cfg = _cfg(framework)
    with _build_llm_patches(framework):
        trial = Trial(trial_id=f"t-reset-{framework}", config=cfg)
    try:
        # Populate every canonical attr the helper might wipe. hasattr-guarded
        # so we only set attrs the adapter actually defines.
        if hasattr(trial, "messages"):
            trial.messages = ["dummy-message-1", "dummy-message-2"]
        for attr in ("_messages", "_input_history", "_response_history",
                     "_agentchat_messages"):
            if hasattr(trial, attr):
                setattr(trial, attr, ["sentinel"])
        for attr in ("_last_response_id", "_forced_prev_id",
                     "_conversation_id"):
            if hasattr(trial, attr):
                setattr(trial, attr, "sentinel-id")

        out = await trial._drive_reset()

        # Envelope shape — adapter reports the API + a list of attrs cleared
        # (or a no-op note for direct-mcp).
        assert out["reset"] is True
        assert "api" in out

        # All wiped attrs are back to the empty/None state.
        if hasattr(trial, "messages"):
            assert trial.messages == [], (
                f"{framework}: messages not cleared: {trial.messages!r}"
            )
        for attr in ("_messages", "_input_history", "_response_history",
                     "_agentchat_messages"):
            if hasattr(trial, attr):
                assert getattr(trial, attr) == [], (
                    f"{framework}: {attr} not cleared: "
                    f"{getattr(trial, attr)!r}"
                )
        for attr in ("_last_response_id", "_forced_prev_id"):
            if hasattr(trial, attr):
                assert getattr(trial, attr) is None, (
                    f"{framework}: {attr} not cleared: "
                    f"{getattr(trial, attr)!r}"
                )
    finally:
        await trial.aclose()


@pytest.mark.parametrize("framework", [
    "langchain", "langgraph", "crewai", "autogen", "llamaindex",
])
async def test_drive_reset_responses_conv_clears_conversation_id(
    framework, monkeypatch,
):
    """+conv branch wipes _conversation_id so next turn re-mints the
    OpenAI Conversations container. Only adapters that support
    api=responses+conv exercise this path.
    """
    _common_env(monkeypatch)
    monkeypatch.setenv("AGW_LLM_BASE_URL_OPENAI", "http://gateway:8080/llm/openai/v1")
    monkeypatch.setenv("DIRECT_LLM_BASE_URL_OPENAI", "https://api.openai.com/v1")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-fake-test-key")
    _swap_to(framework)

    from framework_bridge import Trial  # type: ignore

    cfg = _cfg(framework, api="responses+conv")
    cfg["llm"] = "chatgpt"
    cfg["model"] = "gpt-4o-mini"
    try:
        with _build_llm_patches(framework):
            trial = Trial(trial_id=f"t-conv-{framework}", config=cfg)
    except Exception as e:
        # If the adapter rejects responses+conv at construction (e.g.
        # crewai which only supports chat+messages), skip — this branch
        # of the test isn't applicable to that adapter.
        pytest.skip(f"{framework} doesn't support responses+conv: {e}")
        return

    try:
        if not hasattr(trial, "_conversation_id"):
            pytest.skip(f"{framework} has no _conversation_id attr")

        trial._conversation_id = "conv_sentinel_value"
        out = await trial._drive_reset()
        assert trial._conversation_id is None, (
            f"{framework}: _conversation_id should be wiped"
        )
        assert "_conversation_id" in (out.get("cleared") or [])
    finally:
        await trial.aclose()


# ─────────────────────────────────────────────────────────────────────
# refresh_tools — per-adapter

# Adapters that maintain a client-side toolset cache that _drive_refresh_tools
# explicitly invalidates.
_REFRESH_CACHED = ["langchain", "langgraph", "crewai", "autogen", "llamaindex"]
# Adapters whose framework re-fetches tools/list per call and where the
# helper documents itself as a no-op + log per design doc fallback policy.
_REFRESH_NOOP = ["pydantic-ai", "direct-mcp"]


@pytest.mark.parametrize("framework", _REFRESH_CACHED)
async def test_drive_refresh_tools_invalidates_cache(framework, monkeypatch):
    """For cache-bearing adapters, _drive_refresh_tools clears the cache."""
    _common_env(monkeypatch)
    _swap_to(framework)

    from framework_bridge import Trial  # type: ignore

    cfg = _cfg(framework, mcp="weather")
    with _build_llm_patches(framework):
        trial = Trial(trial_id=f"t-rt-{framework}", config=cfg)
    try:
        # Seed a fake cached tool list so we can observe invalidation.
        trial._mcp_tools = ["fake-tool-A", "fake-tool-B"]
        # langgraph also caches the compiled graph; langchain caches the
        # bound LLM; autogen caches the agent — seed each defensively.
        for attr in ("_graph", "_llm_with_tools", "_agent"):
            if hasattr(trial, attr):
                setattr(trial, attr, "sentinel-cache")

        out = await trial._drive_refresh_tools()

        assert out["refresh_tools"] is True, (
            f"{framework}: expected refresh_tools=True, got {out!r}"
        )
        assert out["prior_tool_count"] == 2
        assert trial._mcp_tools is None
        # Companion caches must also be cleared.
        for attr in ("_graph", "_llm_with_tools", "_agent"):
            if hasattr(trial, attr):
                assert getattr(trial, attr) is None, (
                    f"{framework}: {attr} should be cleared by "
                    f"refresh_tools, got {getattr(trial, attr)!r}"
                )
    finally:
        await trial.aclose()


@pytest.mark.parametrize("framework", _REFRESH_NOOP)
async def test_drive_refresh_tools_noop_for_uncached_adapters(
    framework, monkeypatch,
):
    """pydantic-ai re-fetches tools/list per agent.run; direct-mcp re-fetches
    every turn at the start of turn(). Both ship as documented no-ops."""
    _common_env(monkeypatch)
    _swap_to(framework)

    from framework_bridge import Trial  # type: ignore

    cfg = _cfg(framework, mcp="weather")
    with _build_llm_patches(framework):
        trial = Trial(trial_id=f"t-rtnoop-{framework}", config=cfg)
    try:
        out = await trial._drive_refresh_tools()
        # noop sentinel — string "noop", not boolean True.
        assert out["refresh_tools"] == "noop"
        assert "reason" in out
    finally:
        await trial.aclose()


@pytest.mark.parametrize("framework", _RESET_PARAMS)
async def test_drive_refresh_tools_skipped_when_mcp_none(
    framework, monkeypatch,
):
    """All adapters return a skipped sentinel envelope when mcp=NONE so
    trial scripts can call refresh_tools defensively across rows."""
    _common_env(monkeypatch)
    _swap_to(framework)

    from framework_bridge import Trial  # type: ignore

    if framework == "direct-mcp":
        # direct-mcp adapter rejects mcp=NONE at construction; the
        # mcp=NONE skip path doesn't apply here. Its baseline noop
        # path is covered by test_drive_refresh_tools_noop_for_uncached_adapters.
        pytest.skip("direct-mcp requires a concrete MCP")

    cfg = _cfg(framework, mcp="NONE")
    with _build_llm_patches(framework):
        trial = Trial(trial_id=f"t-rtskip-{framework}", config=cfg)
    try:
        out = await trial._drive_refresh_tools()
        assert out["refresh_tools"] == "skipped", (
            f"{framework}: expected skipped sentinel for mcp=NONE, got {out!r}"
        )
        assert out["reason"] == "mcp=NONE"
    finally:
        await trial.aclose()
