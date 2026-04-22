"""Tests for harness/audit_tail.py — parse AGW json logs + demux by trial-id."""
import json

from audit_tail import parse_log_line, line_matches_trial


def test_parse_json_log_line():
    """AGW emits json; we parse out governance fields."""
    line = json.dumps({
        "timestamp": "2026-04-22T14:23:12Z",
        "level": "INFO",
        "target": "agentgateway::governance",
        "fields": {
            "phase": "llm_request",
            "cid": "ib_abc123def456",
            "backend": "ollama",
            "trace_id": "abc",
            "body": '{"headers": {"X-Harness-Trial-ID": "trial-1", "X-Harness-Turn-ID": "turn-1"}}'
        }
    })
    entry = parse_log_line(line)
    assert entry is not None
    assert entry["phase"] == "llm_request"
    assert entry["cid"] == "ib_abc123def456"
    assert entry["trial_id"] == "trial-1"
    assert entry["turn_id"] == "turn-1"


def test_parse_non_governance_line_returns_none():
    """Lines from other log targets are skipped."""
    line = json.dumps({
        "target": "agentgateway::proxy",
        "fields": {"msg": "proxied request"},
    })
    assert parse_log_line(line) is None


def test_parse_malformed_line_returns_none():
    assert parse_log_line("not json at all") is None
    assert parse_log_line("") is None
    assert parse_log_line("{}") is None


def test_line_matches_trial():
    entry = {"trial_id": "trial-42"}
    assert line_matches_trial(entry, "trial-42") is True
    assert line_matches_trial(entry, "other-trial") is False
    assert line_matches_trial({"trial_id": None}, "trial-42") is False
