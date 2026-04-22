"""Compute cidgar efficacy verdicts (Plan A: a + b; c/d/e stub to Plan B)."""
from __future__ import annotations

import json
import re
from typing import Any

from trials import Trial, Verdict

MARKER_RE = re.compile(r"<!--\s*ib:cid=(ib_[a-f0-9]{12})\s*-->")


def _user_msg_turns(trial: Trial):
    return [t for t in trial.turns if t.kind == "user_msg"]


def _audit_for_turn(trial: Trial, turn_id: str) -> list:
    return [e for e in trial.audit_entries if e.turn_id == turn_id and e.cid]


def verdict_a_presence(trial: Trial) -> Verdict:
    """(a) presence — each user_msg turn has ≥1 audit entry with a cid."""
    user_turns = _user_msg_turns(trial)
    if not user_turns:
        return Verdict("na", "no user_msg turns in trial")
    cids = set()
    for t in user_turns:
        matching = _audit_for_turn(trial, t.turn_id)
        if not matching:
            if not trial.audit_entries:
                return Verdict("error",
                    "no AGW audit entries captured — check governance policy on route "
                    "and RUST_LOG_FORMAT=json")
            return Verdict("fail", f"Turn {t.turn_idx} has no audit entry with a CID")
        cids.update(e.cid for e in matching)
    return Verdict("pass", f"CID present in all {len(user_turns)} turns; unique CIDs: {sorted(cids)}")


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


def verdict_b_channel_structure(trial: Trial) -> Verdict:
    """(b) channel structure — expected channels carry audit-reported CID."""
    user_turns = _user_msg_turns(trial)
    if not user_turns:
        return Verdict("na", "no user_msg turns in trial")

    issues = []

    for t in user_turns:
        audit = _audit_for_turn(trial, t.turn_id)
        if not audit:
            return Verdict("error", f"turn {t.turn_idx} has no audit entry — verdict_a should have caught this")
        expected_cid = audit[0].cid
        body = (t.response or {}).get("body", {}) or {}

        # Detect whether this turn carries tool_calls (→ C1 expected) or text (→ C2 expected)
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
            c1_cids = _find_cid_in_tool_calls_openai(body)
            if expected_cid not in c1_cids:
                issues.append(f"Turn {t.turn_idx}: C1 missing — expected cid={expected_cid} "
                              f"in tool_calls[].function.arguments._ib_cid; found={c1_cids}")
        elif has_text:
            c2_cid = _find_c2_marker_openai(body)
            if c2_cid != expected_cid:
                issues.append(f"Turn {t.turn_idx}: C2 text marker missing or mismatched — "
                              f"expected cid={expected_cid}; found={c2_cid}")

    if issues:
        return Verdict("fail", " | ".join(issues))
    return Verdict("pass", f"all channels carry expected CID across {len(user_turns)} turns")


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
