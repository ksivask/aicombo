"""Tests for harness/audit_tail.py — parse AGW json logs + demux by trial-id."""
import json

from audit_tail import parse_log_line, line_matches_trial
from trials import AuditEntry


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


def test_e26_body_carries_through_both_shapes_into_audit_entry():
    """E26 — both shape A (JSON) and shape B (regex-parsed text) MUST surface
    the governance body as a top-level `body` field on the parsed-event dict,
    AND that `body` MUST round-trip into the AuditEntry built from it
    (production path: api.py::audit_provider).

    Pre-E26 the AuditEntry dropped the top-level body on the floor —
    verdict (i) tools_list_correlation read None on every shape-B trial.
    """
    inner_body = {"correlation_lost": False, "snapshot_hash": "deadbeef"}

    # Shape A: JSON-per-line.
    line_a = json.dumps({
        "timestamp": "2026-04-22T14:23:12Z",
        "level": "INFO",
        "target": "agentgateway::governance",
        "fields": {
            "phase": "tool_call",
            "cid": "ib_abc123def456",
            "backend": "weather-mcp",
            "body": json.dumps(inner_body),
        },
    })
    parsed_a = parse_log_line(line_a)
    assert parsed_a is not None
    assert parsed_a["body"] == inner_body, "shape A must surface top-level body"

    # Shape B: AGW's default structured-text format.
    line_b = (
        '2026-04-22T14:23:12.000000Z  info  governance '
        'phase="tool_call" cid=Some("ib_abc123def456") '
        'backend=weather-mcp trace_id=None '
        f'body={json.dumps(inner_body)}'
    )
    parsed_b = parse_log_line(line_b)
    assert parsed_b is not None
    assert parsed_b["body"] == inner_body, "shape B must surface top-level body"

    # Both shapes' parsed dicts MUST construct a working AuditEntry whose
    # top-level `.body` carries the parsed body (mirrors api.py wiring).
    for parsed in (parsed_a, parsed_b):
        entry = AuditEntry(
            trial_id="t", turn_id=parsed.get("turn_id"),
            phase=parsed.get("phase"), cid=parsed.get("cid"),
            backend=parsed.get("backend"), raw=parsed.get("raw", {}),
            captured_at=parsed.get("timestamp", ""),
            body=parsed.get("body"),
        )
        assert entry.body == inner_body
        assert entry.body["correlation_lost"] is False
