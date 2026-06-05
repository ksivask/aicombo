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
            "cid": "ibc_abc123def456",
            "backend": "ollama",
            "trace_id": "abc",
            "body": '{"headers": {"X-Harness-Trial-ID": "trial-1", "X-Harness-Turn-ID": "turn-1"}}'
        }
    })
    entry = parse_log_line(line)
    assert entry is not None
    assert entry["phase"] == "llm_request"
    assert entry["cid"] == "ibc_abc123def456"
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
            "cid": "ibc_abc123def456",
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
        'phase="tool_call" cid=Some("ibc_abc123def456") '
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


# ── CHG-27: cid_source / rid_source / cid_header_bag / rid_header_bag ──
#
# AGW now emits these optional fields inside the llm_request phase body:
#   cid_source        — "header" | "scan" | "generated" (omitted when "generated")
#   rid_source        — "header" | "scan" | "generated" (omitted when "generated")
#   cid_header_bag    — [[key, value], ...] from the structured X-IB-CID pairs
#   rid_header_bag    — [[key, value], ...] from the structured X-IB-RID pairs
#
# parse_log_line surfaces the body dict as entry["body"] in both shapes;
# consumers read these fields from there. Tests verify:
#   1. New fields round-trip through parse_log_line → AuditEntry.body (both shapes).
#   2. Absence of these fields (legacy logs) is tolerated — no KeyError, no
#      None dereference. AuditEntry.body is still accessible; missing keys
#      just aren't present.


def _make_llm_request_body(extra=None):
    """Build a minimal llm_request phase body, optionally merging extra fields."""
    base = {
        "messages": [{"role": "user", "content": "hello"}],
    }
    if extra:
        base.update(extra)
    return base


def test_cid_source_header_in_body_json_shape():
    """Shape A (JSON): cid_source='header' in body is surfaced in entry.body."""
    body = _make_llm_request_body({"cid_source": "header"})
    line = json.dumps({
        "timestamp": "2026-06-03T10:00:00Z",
        "level": "INFO",
        "target": "agentgateway::governance",
        "fields": {
            "phase": "llm_request",
            "cid": "ibc_aabbcc001122",
            "backend": "ollama",
            "body": json.dumps(body),
        },
    })
    entry = parse_log_line(line)
    assert entry is not None
    assert entry["body"]["cid_source"] == "header"
    assert entry["phase"] == "llm_request"


def test_rid_source_header_in_body_json_shape():
    """Shape A: rid_source='header' in body is surfaced in entry.body."""
    body = _make_llm_request_body({"rid_source": "header"})
    line = json.dumps({
        "timestamp": "2026-06-03T10:00:00Z",
        "level": "INFO",
        "target": "agentgateway::governance",
        "fields": {
            "phase": "llm_request",
            "cid": "ibc_112233445566",
            "backend": "chatgpt",
            "body": json.dumps(body),
        },
    })
    entry = parse_log_line(line)
    assert entry is not None
    assert entry["body"]["rid_source"] == "header"


def test_cid_header_bag_in_body_json_shape():
    """Shape A: cid_header_bag (list of [key,value] pairs) round-trips through
    parse_log_line and AuditEntry.body without modification."""
    bag = [["conv_id", "ibc_aabbcc001122"], ["tid", "session-42"], ["env", "prod"]]
    body = _make_llm_request_body({"cid_source": "header", "cid_header_bag": bag})
    line = json.dumps({
        "timestamp": "2026-06-03T10:00:00Z",
        "level": "INFO",
        "target": "agentgateway::governance",
        "fields": {
            "phase": "llm_request",
            "cid": "ibc_aabbcc001122",
            "backend": "ollama",
            "body": json.dumps(body),
        },
    })
    entry = parse_log_line(line)
    assert entry is not None
    assert entry["body"]["cid_header_bag"] == bag

    # Round-trip through AuditEntry.body.
    from trials import AuditEntry
    ae = AuditEntry(
        trial_id="t", turn_id=None,
        phase=entry["phase"], cid=entry["cid"],
        backend=entry["backend"], raw=entry["raw"],
        captured_at=entry.get("timestamp", ""),
        body=entry["body"],
    )
    assert ae.body["cid_header_bag"] == bag


def test_rid_header_bag_in_body_json_shape():
    """Shape A: rid_header_bag (list of [key,value] pairs) round-trips through
    parse_log_line and AuditEntry.body without modification."""
    bag = [["run_id", "ibr_aabbcc001122"], ["prun_id", "ibr_deadbeef0000"]]
    body = _make_llm_request_body({"rid_source": "header", "rid_header_bag": bag})
    line = json.dumps({
        "timestamp": "2026-06-03T10:00:00Z",
        "level": "INFO",
        "target": "agentgateway::governance",
        "fields": {
            "phase": "llm_request",
            "cid": "ibc_112233445566",
            "backend": "ollama",
            "body": json.dumps(body),
        },
    })
    entry = parse_log_line(line)
    assert entry is not None
    assert entry["body"]["rid_header_bag"] == bag

    from trials import AuditEntry
    ae = AuditEntry(
        trial_id="t", turn_id=None,
        phase=entry["phase"], cid=entry["cid"],
        backend=entry["backend"], raw=entry["raw"],
        captured_at=entry.get("timestamp", ""),
        body=entry["body"],
    )
    assert ae.body["rid_header_bag"] == bag


def test_source_fields_absent_in_legacy_logs():
    """Old llm_request log lines with no cid_source/rid_source/bag fields
    parse without error and AuditEntry.body does not contain those keys
    (tolerant of absence — callers must use .get())."""
    body = _make_llm_request_body()  # no source/bag fields
    line = json.dumps({
        "timestamp": "2026-04-01T00:00:00Z",
        "level": "INFO",
        "target": "agentgateway::governance",
        "fields": {
            "phase": "llm_request",
            "cid": "ibc_000000000000",
            "backend": "ollama",
            "body": json.dumps(body),
        },
    })
    entry = parse_log_line(line)
    assert entry is not None
    body_out = entry["body"]
    # New fields absent — callers must tolerate missing keys.
    assert "cid_source" not in body_out
    assert "rid_source" not in body_out
    assert "cid_header_bag" not in body_out
    assert "rid_header_bag" not in body_out

    from trials import AuditEntry
    ae = AuditEntry(
        trial_id="t", turn_id=None,
        phase=entry["phase"], cid=entry["cid"],
        backend=entry["backend"], raw=entry["raw"],
        captured_at=entry.get("timestamp", ""),
        body=entry["body"],
    )
    assert ae.body is not None
    assert ae.body.get("cid_source") is None


def test_cid_source_scan_in_body_json_shape():
    """Shape A: cid_source='scan' (marker found in history) round-trips."""
    body = _make_llm_request_body({"cid_source": "scan"})
    line = json.dumps({
        "timestamp": "2026-06-03T10:01:00Z",
        "level": "INFO",
        "target": "agentgateway::governance",
        "fields": {
            "phase": "llm_request",
            "cid": "ibc_scanned00001",
            "backend": "ollama",
            "body": json.dumps(body),
        },
    })
    entry = parse_log_line(line)
    assert entry is not None
    assert entry["body"]["cid_source"] == "scan"


def test_all_new_fields_together_in_body_json_shape():
    """Shape A: cid_source + rid_source + both bags all present simultaneously
    (header passthrough active on both X-IB-CID and X-IB-RID)."""
    cid_bag = [["conv_id", "ibc_aabbcc001122"], ["tid", "t-99"]]
    rid_bag = [["run_id", "ibr_001122334455"], ["prun_id", "ibr_aabbcc001122"]]
    body = _make_llm_request_body({
        "cid_source":     "header",
        "rid_source":     "header",
        "cid_header_bag": cid_bag,
        "rid_header_bag": rid_bag,
    })
    line = json.dumps({
        "timestamp": "2026-06-03T10:02:00Z",
        "level": "INFO",
        "target": "agentgateway::governance",
        "fields": {
            "phase": "llm_request",
            "cid": "ibc_aabbcc001122",
            "backend": "chatgpt",
            "body": json.dumps(body),
        },
    })
    entry = parse_log_line(line)
    assert entry is not None
    b = entry["body"]
    assert b["cid_source"] == "header"
    assert b["rid_source"] == "header"
    assert b["cid_header_bag"] == cid_bag
    assert b["rid_header_bag"] == rid_bag

    from trials import AuditEntry
    ae = AuditEntry(
        trial_id="t", turn_id=None,
        phase=entry["phase"], cid=entry["cid"],
        backend=entry["backend"], raw=entry["raw"],
        captured_at=entry.get("timestamp", ""),
        body=entry["body"],
    )
    assert ae.body["cid_source"] == "header"
    assert ae.body["rid_source"] == "header"
    assert ae.body["cid_header_bag"] == cid_bag
    assert ae.body["rid_header_bag"] == rid_bag


def test_structured_text_shape_body_with_cid_source():
    """Shape B (structured text): body containing cid_source surfaces correctly
    through the regex parser as entry['body']['cid_source']."""
    inner_body = {"cid_source": "header", "cid_header_bag": [["conv_id", "ibc_fedcba987654"]]}
    line = (
        "2026-06-03T10:03:00.000000Z  info  governance "
        'phase="llm_request" cid=Some("ibc_fedcba987654") '
        "backend=ollama trace_id=None "
        f"body={json.dumps(inner_body)}"
    )
    entry = parse_log_line(line)
    assert entry is not None
    assert entry["body"]["cid_source"] == "header"
    assert entry["body"]["cid_header_bag"] == [["conv_id", "ibc_fedcba987654"]]
