"""Tests for harness/providers.py — env-based key detection."""
import os

import pytest

from providers import get_providers


def test_ollama_always_available(monkeypatch):
    """Ollama doesn't need a key → always available."""
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    providers = get_providers()
    ollama = next(p for p in providers if p["id"] == "ollama")
    assert ollama["available"] is True


def test_chatgpt_available_when_openai_key_set(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    providers = get_providers()
    chatgpt = next(p for p in providers if p["id"] == "chatgpt")
    assert chatgpt["available"] is True


def test_chatgpt_unavailable_when_key_missing(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    providers = get_providers()
    chatgpt = next(p for p in providers if p["id"] == "chatgpt")
    assert chatgpt["available"] is False
    assert "OPENAI_API_KEY" in chatgpt["unavailable_reason"]


def test_all_4_providers_returned(monkeypatch):
    providers = get_providers()
    ids = {p["id"] for p in providers}
    assert {"NONE", "ollama", "claude", "chatgpt", "gemini"} <= ids
