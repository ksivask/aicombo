"""Tests for harness/models.py — curated list + env override."""
import models


def test_get_models_returns_curated_for_known_provider():
    chatgpt = models.get_models("chatgpt")
    assert len(chatgpt) >= 3
    ids = [m.id for m in chatgpt]
    assert "gpt-4o-mini" in ids
    # Default tier annotations exist
    assert any(m.tier == "reasoning" for m in chatgpt)


def test_get_models_returns_empty_for_unknown_provider():
    assert models.get_models("unknown_provider_xyz") == []


def test_get_models_env_override_replaces_curated(monkeypatch):
    monkeypatch.setenv("CHATGPT_MODELS", "gpt-5,gpt-9-preview")
    out = models.get_models("chatgpt")
    assert [m.id for m in out] == ["gpt-5", "gpt-9-preview"]
    # Env-supplied entries get tier="custom"
    assert all(m.tier == "custom" for m in out)


def test_get_models_env_override_strips_whitespace(monkeypatch):
    monkeypatch.setenv("CLAUDE_MODELS", " claude-haiku-4-5 , claude-sonnet-4-6 ,, ")
    out = models.get_models("claude")
    assert [m.id for m in out] == ["claude-haiku-4-5", "claude-sonnet-4-6"]


def test_o1_mini_is_marked_no_tools():
    """Validator integration depends on this flag — pin it."""
    chatgpt = models.get_models("chatgpt")
    o1 = next((m for m in chatgpt if m.id == "o1-mini"), None)
    assert o1 is not None
    assert o1.supports_tools is False


def test_to_jsonable_returns_dicts_with_all_fields():
    out = models.to_jsonable(models.get_models("chatgpt"))
    assert all(isinstance(m, dict) for m in out)
    assert "id" in out[0] and "display" in out[0] and "tier" in out[0]
    assert "supports_tools" in out[0] and "supports_responses_api" in out[0]
