"""Compute cidgar efficacy verdicts (Plan A: a + b; c/d/e stub to Plan B)."""
from __future__ import annotations

import json
import re
from typing import Any

from trials import Trial, Verdict

MARKER_RE = re.compile(r"<!--\s*ib:cid=(ib_[a-f0-9]{12})\s*-->")


def _user_msg_turns(trial: Trial):
    return [t for t in trial.turns if t.kind == "user_msg"]


def _has_header_demux(trial: Trial) -> bool:
    """True if audit entries carry turn_id (header-based demux available)."""
    return any(e.turn_id for e in trial.audit_entries)


def _audit_for_turn(trial: Trial, turn_id: str) -> list:
    """Header-demux path: entries with matching turn_id + a cid."""
    return [e for e in trial.audit_entries if e.turn_id == turn_id and e.cid]


def _audit_with_cid(trial: Trial) -> list:
    """Time-window path: all entries that carry a CID."""
    return [e for e in trial.audit_entries if e.cid]


def verdict_a_presence(trial: Trial) -> Verdict:
    """(a) presence — audit log shows cidgar fired for this trial's turns."""
    user_turns = _user_msg_turns(trial)
    if not user_turns:
        return Verdict("na", "no user_msg turns in trial")

    if not trial.audit_entries:
        return Verdict("error",
            "no AGW audit entries captured — check governance policy on route "
            "and that audit_tail can reach the AGW container")

    if _has_header_demux(trial):
        # Strict per-turn correlation (only when cidgar emits headers in governance log)
        cids = set()
        for t in user_turns:
            matching = _audit_for_turn(trial, t.turn_id)
            if not matching:
                return Verdict("fail", f"Turn {t.turn_idx} has no audit entry with a CID")
            cids.update(e.cid for e in matching)
        return Verdict("pass",
            f"CID present in all {len(user_turns)} turns; unique CIDs: {sorted(cids)}")

    # Time-window correlation (Plan A default; cidgar log has no headers)
    cid_entries = _audit_with_cid(trial)
    if not cid_entries:
        return Verdict("fail",
            f"{len(trial.audit_entries)} audit entries but none carry a CID")
    unique_cids = sorted({e.cid for e in cid_entries})
    if len(cid_entries) < len(user_turns):
        return Verdict("fail",
            f"only {len(cid_entries)} CID-bearing audit entries for "
            f"{len(user_turns)} turns (expected ≥ one per turn)")
    return Verdict("pass",
        f"CID present across {len(cid_entries)} audit entries for "
        f"{len(user_turns)} turns (time-window correlation); unique CIDs: {unique_cids}")


def _find_cid_in_text(text: str) -> str | None:
    if not isinstance(text, str):
        return None
    m = MARKER_RE.search(text)
    return m.group(1) if m else None


def _find_cid_in_tool_calls_openai(body: dict[str, Any]) -> list[str]:
    """Extract _ib_cid from openai-shape tool_calls[].function.arguments."""
    out = []
    choices = body.get("choices", []) or []
    for ch in choices:
        msg = ch.get("message", {}) or {}
        for tc in msg.get("tool_calls", []) or []:
            fn = tc.get("function", {}) or {}
            args_str = fn.get("arguments", "")
            try:
                args = json.loads(args_str) if isinstance(args_str, str) else args_str
                cid = args.get("_ib_cid")
                if cid:
                    out.append(cid)
            except (ValueError, AttributeError):
                continue
    return out


def _find_c2_marker_openai(body: dict[str, Any]) -> str | None:
    """Pull C2 marker from choices[].message.content (text-only response)."""
    choices = body.get("choices", []) or []
    for ch in choices:
        content = ch.get("message", {}).get("content", "")
        cid = _find_cid_in_text(content)
        if cid:
            return cid
    return None


def _all_response_bodies(turn) -> list[dict]:
    """All response bodies relevant to a turn — top-level + every event.

    Adapters that drive a multi-step flow (e.g. langchain MCP agent loop)
    surface intermediate LLM hops + MCP tool exchanges in
    `framework_events`. Each event may carry its own `response.body` —
    cidgar markers can appear in ANY of those (e.g. C1 in a tool_call body
    from hop 0, C2 in a final text body from hop 1). Verdict B must scan
    them all and accept a turn if ANY body satisfies the channel
    expectation.
    """
    bodies: list[dict] = []
    top = (turn.response or {}).get("body")
    if isinstance(top, dict):
        bodies.append(top)
    for ev in (turn.framework_events or []):
        if not isinstance(ev, dict):
            continue
        ev_resp = ev.get("response") or {}
        if not isinstance(ev_resp, dict):
            continue
        body = ev_resp.get("body")
        if isinstance(body, dict):
            bodies.append(body)
    return bodies


def verdict_b_channel_structure(trial: Trial) -> Verdict:
    """(b) channel structure — response bodies carry CIDs matching audit log."""
    user_turns = _user_msg_turns(trial)
    if not user_turns:
        return Verdict("na", "no user_msg turns in trial")

    header_demux = _has_header_demux(trial)
    all_audit_cids = {e.cid for e in trial.audit_entries if e.cid}
    issues = []

    for t in user_turns:
        if header_demux:
            audit = _audit_for_turn(trial, t.turn_id)
            if not audit:
                return Verdict("error",
                    f"turn {t.turn_idx} has no audit entry — verdict_a should have caught this")
            expected_cids = {audit[0].cid}
        else:
            # Time-window correlation — any CID seen in audit is acceptable
            expected_cids = all_audit_cids

        bodies = _all_response_bodies(t)
        if not bodies:
            # Non-OpenAI / SSE / dict-less body — verdict_a already covered
            # the audit-log presence check. Skip channel-structure scan.
            continue

        # Track what we observed across ALL bodies in this turn so we
        # only flag a turn if NO body carries an expected CID.
        observed_c1: set[str] = set()
        observed_c2: set[str] = set()
        any_tool_calls_seen = False
        any_text_seen = False

        for body in bodies:
            choices = body.get("choices", []) or []
            has_tool_calls = any(
                (ch.get("message", {}) or {}).get("tool_calls")
                for ch in choices
            )
            has_text = any(
                (ch.get("message", {}) or {}).get("content")
                for ch in choices
            )
            if has_tool_calls:
                any_tool_calls_seen = True
                observed_c1.update(_find_cid_in_tool_calls_openai(body))
            if has_text:
                any_text_seen = True
                c2 = _find_c2_marker_openai(body)
                if c2:
                    observed_c2.add(c2)

        # Pass condition for this turn: at least one expected CID showed
        # up via either Channel 1 or Channel 2 across the turn's bodies.
        match_c1 = bool(observed_c1 & expected_cids)
        match_c2 = bool(observed_c2 & expected_cids)
        if match_c1 or match_c2:
            continue
        # No match — describe what we saw vs expected.
        if any_tool_calls_seen and not any_text_seen:
            issues.append(f"Turn {t.turn_idx}: C1 missing — no tool_calls cid "
                          f"matched audit (found={observed_c1 or '∅'}, "
                          f"expected one of={expected_cids})")
        elif any_text_seen and not any_tool_calls_seen:
            if not observed_c2:
                issues.append(f"Turn {t.turn_idx}: C2 text marker absent from response content")
            else:
                issues.append(f"Turn {t.turn_idx}: C2 marker cid={observed_c2} "
                              f"not in audit CIDs {expected_cids}")
        elif any_tool_calls_seen or any_text_seen:
            issues.append(
                f"Turn {t.turn_idx}: neither C1 (tool_calls cid {observed_c1 or '∅'}) "
                f"nor C2 (content marker {observed_c2 or '∅'}) matched "
                f"audit cids {expected_cids}"
            )
        # If no choices anywhere, treat as no-op (audit covers presence).

    if issues:
        return Verdict("fail", " | ".join(issues))
    mode = "header-demux" if header_demux else "time-window"
    return Verdict("pass",
        f"channels carry expected CID across {len(user_turns)} turns ({mode})")


def compute_verdicts(trial: Trial) -> dict[str, Verdict]:
    """Return {a, b, c, d, e} verdicts. Plan A computes a+b; c/d/e na."""
    if trial.config.routing == "direct":
        na = Verdict("na", "baseline — cidgar not in path")
        return {"a": na, "b": na, "c": na, "d": na, "e": na}
    if trial.status == "aborted":
        na = Verdict("na", "trial aborted before completion")
        return {"a": na, "b": na, "c": na, "d": na, "e": na}
    return {
        "a": verdict_a_presence(trial),
        "b": verdict_b_channel_structure(trial),
        "c": Verdict("na", "deferred to Plan B"),
        "d": Verdict("na", "deferred to Plan B"),
        "e": Verdict("na", "deferred to Plan B"),
    }
