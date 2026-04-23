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


GAR_REQUIRED_KEYS = {"goal", "need", "impact", "dspm", "alt"}


def _extract_gar_strings_from_body(body: dict) -> list[str]:
    """Pull out _ib_gar values from openai-shape tool_calls[].function.arguments."""
    out: list[str] = []
    choices = body.get("choices", []) or []
    for ch in choices:
        msg = ch.get("message", {}) or {}
        for tc in msg.get("tool_calls", []) or []:
            fn = tc.get("function", {}) or {}
            args_str = fn.get("arguments", "")
            try:
                args = json.loads(args_str) if isinstance(args_str, str) else args_str
                if isinstance(args, dict) and "_ib_gar" in args:
                    out.append(args["_ib_gar"])
            except (ValueError, AttributeError):
                continue
    return out


def verdict_f_gar_richness(trial: Trial) -> Verdict:
    """(f) GAR richness — did the LLM populate _ib_gar with the 5-key structure?

    Cidgar spec §3.2 defines _ib_gar as a JSON string with keys
    {goal, need, impact, dspm, alt}. Spec §9.2 acknowledges LLMs may omit it
    (governance logs gar:null + proceeds). This verdict distinguishes:

      pass   — at least one tool_call carries a well-formed GAR object with
               all 5 required keys.
      fail   — GAR is present but malformed (missing keys, not JSON).
      na     — LLM omitted GAR in all tool_calls (spec §9.2 compliant).
      na     — no tool_calls in any turn (verdict doesn't apply; C2-only flow).
    """
    user_turns = _user_msg_turns(trial)
    if not user_turns:
        return Verdict("na", "no user_msg turns in trial")

    tool_call_turns = 0
    gar_valid = 0
    gar_malformed_reasons: list[str] = []
    gar_omitted = 0

    for t in user_turns:
        bodies = _all_response_bodies(t)
        turn_had_tool_call = False
        for body in bodies:
            tcs_args = _extract_gar_strings_from_body(body)
            # Separately count turns with ANY tool_call (even if _ib_gar omitted)
            if not turn_had_tool_call:
                choices = body.get("choices", []) or []
                if any((ch.get("message", {}) or {}).get("tool_calls") for ch in choices):
                    turn_had_tool_call = True

            for gar_val in tcs_args:
                if gar_val is None or gar_val == "":
                    gar_omitted += 1
                    continue
                # Try parsing per spec §3.2.1 (transmitted as JSON-string)
                parsed: Any = gar_val
                if isinstance(gar_val, str):
                    try:
                        parsed = json.loads(gar_val)
                    except (ValueError, TypeError):
                        gar_malformed_reasons.append(
                            f"Turn {t.turn_idx}: _ib_gar is a string but not valid JSON "
                            f"(first 60 chars: {gar_val[:60]!r})"
                        )
                        continue
                if not isinstance(parsed, dict):
                    gar_malformed_reasons.append(
                        f"Turn {t.turn_idx}: _ib_gar parsed to {type(parsed).__name__}, expected object"
                    )
                    continue
                missing_keys = GAR_REQUIRED_KEYS - set(parsed.keys())
                if missing_keys:
                    gar_malformed_reasons.append(
                        f"Turn {t.turn_idx}: _ib_gar missing keys: {sorted(missing_keys)}"
                    )
                    continue
                # Has all 5 keys + is a dict — valid
                gar_valid += 1

            # Count tool_calls that had NO _ib_gar in their args
            choices = body.get("choices", []) or []
            for ch in choices:
                msg = ch.get("message", {}) or {}
                for tc in msg.get("tool_calls", []) or []:
                    fn = tc.get("function", {}) or {}
                    args_str = fn.get("arguments", "")
                    try:
                        args = json.loads(args_str) if isinstance(args_str, str) else args_str
                        if isinstance(args, dict) and "_ib_gar" not in args:
                            gar_omitted += 1
                    except (ValueError, AttributeError):
                        pass

        if turn_had_tool_call:
            tool_call_turns += 1

    if tool_call_turns == 0:
        return Verdict("na",
            "no tool_calls in any turn — GAR richness only applies to C1 (tool_use) flows")

    if gar_valid > 0 and not gar_malformed_reasons:
        return Verdict("pass",
            f"LLM populated _ib_gar with valid {{goal, need, impact, dspm, alt}} in "
            f"{gar_valid} tool_call(s); omitted in {gar_omitted}.")

    if gar_malformed_reasons:
        return Verdict("fail",
            f"_ib_gar present but malformed ({len(gar_malformed_reasons)} case(s)): "
            + "; ".join(gar_malformed_reasons[:3]))

    # All tool_calls omitted _ib_gar — spec §9.2 compliant but weak signal
    return Verdict("na",
        f"LLM omitted _ib_gar in all {gar_omitted} tool_call(s) (spec §9.2-compliant). "
        f"For richer GAR, try a stricter schema-follower: chatgpt, llama3.1:8b.")


def _cids_for_turn_window(trial: Trial, turn) -> set[str]:
    """Return the set of cids observable for a turn.

    Prefers header-demux (audit entries carrying matching turn_id) when that
    channel is available on the trial; falls back to a time-window scan using
    the turn's [started_at, finished_at] envelope.
    """
    # Header-demux path: entries tagged directly with this turn_id.
    direct = [e.cid for e in trial.audit_entries
              if e.turn_id == turn.turn_id and e.cid]
    if direct:
        return set(direct)

    # Time-window path: entries whose captured_at lies within turn window.
    if not turn.started_at or not turn.finished_at:
        return set()
    win_start, win_end = turn.started_at, turn.finished_at
    cids: set[str] = set()
    for e in trial.audit_entries:
        if not e.cid or not e.captured_at:
            continue
        # ISO-8601 strings are lexicographically orderable when uniformly
        # formatted, which is the format Harness writes.
        if win_start <= e.captured_at <= win_end:
            cids.add(e.cid)
    return cids


def verdict_c_continuity(trial: Trial) -> Verdict:
    """(c) multi-turn continuity — CID preserved across consecutive turns.

    Per design doc §7.4: framework correctly propagates the per-turn CID
    across turn boundaries when its conversation history survives. A break
    means the framework dropped the marker (e.g. state=off compaction) and
    AGW re-minted a fresh CID on the next turn.
    """
    user_turns = _user_msg_turns(trial)
    if len(user_turns) < 2:
        return Verdict("na", "needs ≥2 user_msg turns for continuity check")

    cids_per_turn: list[set[str]] = [
        _cids_for_turn_window(trial, t) for t in user_turns
    ]

    # Keep only turns that actually have at least one cid-bearing audit entry.
    indexed = [(i, cids) for i, cids in enumerate(cids_per_turn) if cids]
    if len(indexed) < 2:
        return Verdict("error",
            f"only {len(indexed)} turn(s) have audit-bearing CIDs (need ≥2)")

    # Every consecutive indexed pair must share at least one cid.
    breaks: list[str] = []
    for (i_a, cids_a), (i_b, cids_b) in zip(indexed, indexed[1:]):
        if not (cids_a & cids_b):
            breaks.append(
                f"turn {i_a} cids {sorted(cids_a)} ↔ turn {i_b} cids {sorted(cids_b)}"
            )

    if breaks:
        return Verdict("fail",
            f"CID continuity broken across {len(breaks)} turn boundary(ies): "
            + " | ".join(breaks))

    cids_overall: set[str] = set().union(*[cs for _, cs in indexed])
    return Verdict("pass",
        f"CID preserved across {len(indexed)} consecutive turns "
        f"(unique CIDs: {sorted(cids_overall)})")


def verdict_d_resilience(trial: Trial) -> Verdict:
    """(d) compaction resilience — CID survives across a `compact` turn.

    Inspect the turn list. Find any compact turn. Identify the user_msg
    turn immediately before AND after the compact. Compare:
      pre_cid  = cids observed in pre-compact turn's audit window
      post_cid = cids observed in post-compact turn's audit window
    Pass: pre_cid ∩ post_cid != ∅ (CID survived via at least one channel).
    Fail: disjoint sets (CID lost; framework dropped all channels).
    na: no compact turn in plan, or no post-compact user_msg turn.
    """
    turns = trial.turns
    compact_idx = next(
        (i for i, t in enumerate(turns) if t.kind == "compact"), None
    )
    if compact_idx is None:
        return Verdict("na", "no compact turn in this trial's plan")

    # Nearest user_msg before the compact (walk backwards) and the first
    # user_msg after (walk forwards).
    pre_user = next(
        (t for t in reversed(turns[:compact_idx]) if t.kind == "user_msg"),
        None,
    )
    post_user = next(
        (t for t in turns[compact_idx + 1:] if t.kind == "user_msg"),
        None,
    )

    if pre_user is None or post_user is None:
        return Verdict(
            "na",
            "compact turn lacks user_msg turn before AND after — can't measure",
        )

    pre_cids = _cids_for_turn_window(trial, pre_user)
    post_cids = _cids_for_turn_window(trial, post_user)

    if not pre_cids or not post_cids:
        return Verdict(
            "error",
            f"missing audit cids: pre={sorted(pre_cids)} post={sorted(post_cids)}",
        )

    survivors = pre_cids & post_cids
    if survivors:
        return Verdict(
            "pass",
            f"CID survived compact ({sorted(survivors)})",
        )
    return Verdict(
        "fail",
        f"CID lost across compact: pre={sorted(pre_cids)} → post={sorted(post_cids)}",
    )


def compute_verdicts(trial: Trial) -> dict[str, Verdict]:
    """Return {a, b, c, d, e, f} verdicts.

    Plan A computed a+b+f; Plan B T9 added c; Plan B T10 adds d. e still na.
    """
    if trial.config.routing == "direct":
        na = Verdict("na", "baseline — cidgar not in path")
        return {"a": na, "b": na, "c": na, "d": na, "e": na, "f": na}
    if trial.status == "aborted":
        na = Verdict("na", "trial aborted before completion")
        return {"a": na, "b": na, "c": na, "d": na, "e": na, "f": na}
    return {
        "a": verdict_a_presence(trial),
        "b": verdict_b_channel_structure(trial),
        "c": verdict_c_continuity(trial),
        "d": verdict_d_resilience(trial),
        "e": Verdict("na", "deferred to Plan B"),
        "f": verdict_f_gar_richness(trial),
    }
