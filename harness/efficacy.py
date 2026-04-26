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
        # B3 fix: surface the audit phases in the failure message. When
        # only `tools_list` entries are present (per spec §5.1 those don't
        # carry a CID), the prior text "N audit entries but none carry a
        # CID" reads like cidgar misbehaved — the real story is usually
        # that the adapter errored before the llm_request phase fired.
        phases = sorted({e.phase for e in trial.audit_entries if e.phase})
        return Verdict("fail",
            f"{len(trial.audit_entries)} audit entries observed "
            f"(phases: {phases or '∅'}) but none carry a CID — likely no "
            f"llm_request phase fired (adapter may have errored before "
            f"the LLM call)")
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


def _find_cid_in_tool_use_messages(body: dict[str, Any]) -> list[str]:
    """Extract _ib_cid from anthropic Messages-shape content[].type=tool_use.

    Mirrors _find_cid_in_tool_calls_openai for the Anthropic format. The
    `input` dict carries the args; cidgar's f3 PATH A injects _ib_cid
    there (see governance/messages_shape.rs::inject_cid_into_tool_use_response).
    """
    out: list[str] = []
    for c in body.get("content", []) or []:
        if not isinstance(c, dict) or c.get("type") != "tool_use":
            continue
        inp = c.get("input", {}) or {}
        if isinstance(inp, dict) and "_ib_cid" in inp:
            cid = inp["_ib_cid"]
            if isinstance(cid, str):
                out.append(cid)
    return out


def _extract_gar_strings_from_body_messages_shape(body: dict) -> list:
    """Pull _ib_gar from anthropic Messages-shape content[].type=tool_use.input.

    Returns a list of raw _ib_gar values. Cidgar may transmit GAR as a
    JSON-string per spec §3.2.1 — the verdict_f caller handles parsing.
    """
    out = []
    for c in body.get("content", []) or []:
        if not isinstance(c, dict) or c.get("type") != "tool_use":
            continue
        inp = c.get("input", {}) or {}
        if isinstance(inp, dict) and "_ib_gar" in inp:
            out.append(inp["_ib_gar"])
    return out


def _body_has_any_tool_call(body: dict) -> bool:
    """True if body carries tool calls in EITHER OpenAI or Anthropic shape."""
    if not isinstance(body, dict):
        return False
    # OpenAI Completions: choices[i].message.tool_calls
    for ch in body.get("choices", []) or []:
        if (ch.get("message", {}) or {}).get("tool_calls"):
            return True
    # Anthropic Messages: content[i].type=tool_use
    for c in body.get("content", []) or []:
        if isinstance(c, dict) and c.get("type") == "tool_use":
            return True
    return False


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
    # B2 fix: track turns that had no body to scan. When *every* turn is
    # skipped (typical when all adapter calls errored), the loop produces
    # zero issues and the function used to fall through to `pass` — a
    # silent lie about correlation that surfaced as a contradictory
    # verdict pair (a)=fail + (b)=pass on errored trials.
    skipped_turn_count = 0

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
            skipped_turn_count += 1
            continue

        # Track what we observed across ALL bodies in this turn so we
        # only flag a turn if NO body carries an expected CID.
        observed_c1: set[str] = set()
        observed_c2: set[str] = set()
        any_tool_calls_seen = False
        any_text_seen = False

        for body in bodies:
            choices = body.get("choices", []) or []
            has_tool_calls = _body_has_any_tool_call(body)
            has_text = any(
                (ch.get("message", {}) or {}).get("content")
                for ch in choices
            )
            if has_tool_calls:
                any_tool_calls_seen = True
                observed_c1.update(_find_cid_in_tool_calls_openai(body))
                observed_c1.update(_find_cid_in_tool_use_messages(body))
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

    # B2 fix: if no turn had any body to scan, surface "na" instead of
    # silently passing. Otherwise issues take precedence; on partial skip
    # the pass message mentions how many turns went un-scanned.
    if skipped_turn_count == len(user_turns):
        return Verdict("na",
            "no response bodies to scan — all turns errored or had non-dict bodies")
    if issues:
        return Verdict("fail", " | ".join(issues))
    mode = "header-demux" if header_demux else "time-window"
    scanned = len(user_turns) - skipped_turn_count
    suffix = f"; {skipped_turn_count} turns skipped (no body)" if skipped_turn_count else ""
    return Verdict("pass",
        f"channels carry expected CID across {scanned} turns ({mode}){suffix}")


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
            tcs_args = (
                _extract_gar_strings_from_body(body)
                + _extract_gar_strings_from_body_messages_shape(body)
            )
            # Separately count turns with ANY tool_call (even if _ib_gar omitted)
            if not turn_had_tool_call:
                if _body_has_any_tool_call(body):
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
            # OpenAI Completions shape — choices[].message.tool_calls
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
            # Anthropic Messages shape — content[].type=tool_use.input
            for c in body.get("content", []) or []:
                if not isinstance(c, dict) or c.get("type") != "tool_use":
                    continue
                inp = c.get("input", {}) or {}
                if isinstance(inp, dict) and "_ib_gar" not in inp:
                    gar_omitted += 1

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


def verdict_e_state_mode_gap(trial: Trial) -> Verdict:
    """(e) state-mode gap — when adapter uses Responses-API `previous_response_id`
    state mode, does cidgar's CID survive across the state chain?

    Only meaningful for trials that:
      * run on api=responses / api=responses+conv, AND
      * have state=True (so `previous_response_id` is actually chained), AND
      * include at least one `force_state_ref` turn in their plan.

    Pass: the CID observed in the audit window of the user_msg turn
    IMMEDIATELY before the forced state-ref is also observed in the audit
    window of the forced-ref turn itself (intersection non-empty).
    Fail: disjoint sets — CID was lost when the framework switched to
    state-mode against an older response id.
    na:   any of the prerequisites above don't hold.
    """
    turns = trial.turns

    # Prerequisite 1 — API family
    if trial.config.api not in ("responses", "responses+conv"):
        return Verdict(
            "na",
            f"verdict (e) only meaningful for api in responses/responses+conv, "
            f"got api={trial.config.api}",
        )

    # Prerequisite 2 — state must be enabled for previous_response_id chain
    # to exist. api=responses+conv implies state at the runner layer; if the
    # config explicitly sets state=False for a bare api=responses row, no
    # chain exists for a gap to span.
    chain_active = trial.config.state or trial.config.api == "responses+conv"
    if not chain_active:
        return Verdict(
            "na",
            "verdict (e) requires state=True (or api=responses+conv) so "
            "previous_response_id chaining is in effect",
        )

    # Prerequisite 3 — a force_state_ref turn must be in the plan
    fsr_idx = next(
        (i for i, t in enumerate(turns) if t.kind == "force_state_ref"), None,
    )
    if fsr_idx is None:
        return Verdict("na", "no force_state_ref turn in this trial's plan")

    fsr_turn = turns[fsr_idx]

    # The user_msg turn IMMEDIATELY before the force_state_ref is the
    # "CID-established" anchor; we compare its audit cid set against the
    # forced-ref turn's audit cid set.
    pre_user = next(
        (t for t in reversed(turns[:fsr_idx]) if t.kind == "user_msg"), None,
    )
    if pre_user is None:
        return Verdict(
            "na",
            "force_state_ref turn has no preceding user_msg to compare against",
        )

    pre_cids = _cids_for_turn_window(trial, pre_user)
    forced_cids = _cids_for_turn_window(trial, fsr_turn)

    if not pre_cids or not forced_cids:
        return Verdict(
            "error",
            f"missing audit cids: pre={sorted(pre_cids)} "
            f"forced={sorted(forced_cids)}",
        )

    survivors = pre_cids & forced_cids
    if survivors:
        return Verdict(
            "pass",
            f"CID survived state-mode jump ({sorted(survivors)})",
        )
    return Verdict(
        "fail",
        f"CID lost on state-mode jump: pre={sorted(pre_cids)} "
        f"→ forced={sorted(forced_cids)}",
    )


def _default_pair_resolver(trial: Trial) -> Trial | None:
    """Production resolver — looks up the baseline trial paired with `trial`
    via matrix.json on disk.

    Returns the baseline Trial when:
      * trial is governed (routing != 'direct'), AND
      * a matrix row points to trial.trial_id via last_trial_id, AND
      * a sibling row exists with baseline_of == that row's row_id, AND
      * the sibling row has a last_trial_id pointing at a saved trial JSON.

    Returns None on any failure path. Tests inject a stub via
    compute_verdicts(trial, pair_resolver=...) instead of mocking the
    filesystem.
    """
    import os
    import json
    from pathlib import Path

    matrix_path = Path(os.environ.get("DATA_DIR", "/data")) / "matrix.json"
    if not matrix_path.exists():
        return None

    try:
        with matrix_path.open() as f:
            rows = json.load(f).get("rows", [])
    except (OSError, ValueError):
        return None

    self_row = next(
        (r for r in rows if r.get("last_trial_id") == trial.trial_id), None,
    )
    if not self_row:
        return None

    sibling = next(
        (r for r in rows if r.get("baseline_of") == self_row["row_id"]), None,
    )
    if not sibling or not sibling.get("last_trial_id"):
        return None

    from trials import TrialStore
    store = TrialStore(Path(os.environ.get("DATA_DIR", "/data")) / "trials")
    try:
        return store.load(sibling["last_trial_id"])
    except FileNotFoundError:
        return None


def verdict_h_overhead(trial: Trial, pair_resolver=None) -> Verdict:
    """(h) latency overhead — per-turn p50 added by cidgar governance.

    Only meaningful when the trial has a paired baseline (T13 clone-baseline).
    Resolution is delegated to `pair_resolver(trial) -> Trial | None` so
    tests can inject a stub instead of mocking matrix.json on disk. Defaults
    to `_default_pair_resolver` which reads from $DATA_DIR/matrix.json.

    Thresholds (per E4 brainstorm §6):
      pass — median overhead ≤ 200 ms
      fail — median overhead > 2000 ms OR > 100% of baseline median
      na   — direct-routed trial, no pair, or no comparable turns
      pass (with note) — middle band [200, 2000] and ≤ baseline median;
             flagged in the reason but doesn't fail the verdict (we do not
             want to introduce a 'warn' status just for this metric).
    """
    if pair_resolver is None:
        pair_resolver = _default_pair_resolver

    if trial.config.routing == "direct":
        return Verdict("na", "verdict (h) measured on the governed side only")

    baseline = pair_resolver(trial)
    if baseline is None:
        return Verdict("na", "no baseline pair has been run yet")

    from datetime import datetime
    deltas: list[tuple[float, float, float]] = []  # (overhead, g_dur, b_dur)
    for g_turn in trial.turns:
        b_turn = next(
            (t for t in baseline.turns if t.turn_idx == g_turn.turn_idx), None,
        )
        if not b_turn:
            continue
        if not g_turn.started_at or not g_turn.finished_at:
            continue
        if not b_turn.started_at or not b_turn.finished_at:
            continue
        try:
            g_dur = (datetime.fromisoformat(g_turn.finished_at) -
                     datetime.fromisoformat(g_turn.started_at)).total_seconds() * 1000
            b_dur = (datetime.fromisoformat(b_turn.finished_at) -
                     datetime.fromisoformat(b_turn.started_at)).total_seconds() * 1000
            deltas.append((g_dur - b_dur, g_dur, b_dur))
        except (TypeError, ValueError):
            continue

    if not deltas:
        return Verdict("na", "no comparable turns for latency measurement")

    overheads = sorted(d[0] for d in deltas)
    baseline_durs = sorted(d[2] for d in deltas)
    n = len(overheads)
    median = overheads[n // 2] if n % 2 else (overheads[n // 2 - 1] + overheads[n // 2]) / 2
    baseline_median = (
        baseline_durs[n // 2] if n % 2
        else (baseline_durs[n // 2 - 1] + baseline_durs[n // 2]) / 2
    )

    # Fail conditions first — explicit budgets from §6.
    if median > 2000:
        return Verdict(
            "fail",
            f"median overhead {median:.0f}ms exceeds 2000ms absolute budget "
            f"over {n} turn{'s' if n != 1 else ''}",
        )
    if baseline_median > 0 and median > baseline_median:
        return Verdict(
            "fail",
            f"median overhead {median:.0f}ms exceeds 100% of baseline median "
            f"{baseline_median:.0f}ms over {n} turn{'s' if n != 1 else ''}",
        )

    if median <= 200:
        return Verdict(
            "pass",
            f"median overhead {median:.0f}ms (≤200ms budget) over {n} turn"
            f"{'s' if n != 1 else ''}",
        )

    # Middle band [200, 2000] and ≤ baseline median — within absolute budget
    # but worth monitoring. Pass with a note rather than introducing 'warn'.
    return Verdict(
        "pass",
        f"median overhead {median:.0f}ms (within absolute budget but elevated; "
        f"baseline median {baseline_median:.0f}ms) over {n} turn"
        f"{'s' if n != 1 else ''}",
    )


def _audit_correlation_lost(entry) -> bool | None:
    """E20 — extract `correlation_lost` from a `tool_call` audit entry.

    Tolerant to two shapes the verdict can encounter:

      1. AuditEntry dataclass populated by audit_tail / api.py (production):
         the `correlation_lost` boolean lives inside `entry.raw["body"]` (the
         parsed cidgar log entry). For shape-B (regex-parsed) lines, `raw`
         only carries `{"line": <raw_text>}` — there is no body dict to
         walk. The verdict treats unrecoverable entries as `correlation_lost
         = True` (worst-case observable: we can't prove the call WAS
         correlated, so for a reliability metric we count it against).

      2. Synthetic dicts / dataclasses that test fixtures hand to the
         verdict directly. Tests typically set `entry.raw = {...}` with
         `correlation_lost` and `original_tool_name` as top-level keys for
         readability. Walk both.

    Returns None when the entry doesn't carry the field at all; the caller
    decides the unrecoverable-entry policy.
    """
    raw = getattr(entry, "raw", None) if not isinstance(entry, dict) else entry.get("raw", entry)
    if isinstance(raw, dict):
        if "correlation_lost" in raw:
            v = raw["correlation_lost"]
            return bool(v) if v is not None else None
        body = raw.get("body")
        if isinstance(body, dict) and "correlation_lost" in body:
            v = body["correlation_lost"]
            return bool(v) if v is not None else None
        # JSON-shape (audit_tail shape A): full event dict where body lives
        # under fields.body. Tolerate either shape so the verdict works on
        # both shape-A and shape-B production logs once audit_tail starts
        # forwarding body alongside raw (today AuditEntry only carries
        # raw — see audit_tail.py for the shape-A vs shape-B note).
        fields = raw.get("fields")
        if isinstance(fields, dict):
            fbody = fields.get("body")
            if isinstance(fbody, dict) and "correlation_lost" in fbody:
                v = fbody["correlation_lost"]
                return bool(v) if v is not None else None
    return None


def _audit_kind(entry) -> str | None:
    """E20 — extract the audit's logical kind ("tool_call" / "tools_list" / ...).

    Cidgar emits via `Phase` whose serde-renamed name lands in the
    `phase` field of AuditEntry. Synthetic test entries may set either
    `phase` or a `kind` key in `raw` for readability. The verdict accepts
    both.
    """
    if isinstance(entry, dict):
        kind = entry.get("kind") or entry.get("phase")
        if kind:
            return kind
        raw = entry.get("raw")
        if isinstance(raw, dict):
            return raw.get("kind") or raw.get("phase")
        return None
    phase = getattr(entry, "phase", None)
    if phase:
        return phase
    raw = getattr(entry, "raw", None)
    if isinstance(raw, dict):
        return raw.get("kind") or raw.get("phase")
    return None


def verdict_i_tools_list_correlation(trial: Trial) -> Verdict:
    """(i) tools_list_correlation — E20 reliability rollup.

    Aggregate AGW's per-call `correlation_lost` flags into a trial-level
    verdict. Source: `tool_call` audit entries (Phase::ToolCall in cidgar)
    extended by E20 with the `correlation_lost` boolean (true iff the LLM
    failed to round-trip the `_ib_ss` snapshot id back into the call args).

    Threshold: pass when ≥80% of `tool_call` audits had
    `correlation_lost=false`. Below that the (framework, model) combo is
    flagged as unreliable for snapshot correlation — operators can
    tolerate the gap, escalate to a stronger model, or pivot to a
    different carrier (the future E20a function-name suffix pattern).

    Returns na when the trial recorded no `tool_call` audits at all
    (chat-only conversations, or adapter errored before any tool call);
    that is a meaningful absence — not a fail — because there was nothing
    to correlate.

    See enhancements.md §E20 for the design rationale.
    """
    audits = trial.audit_entries if hasattr(trial, "audit_entries") else trial.get("audit_entries", [])
    tools_calls = [a for a in audits if _audit_kind(a) == "tool_call"]
    if not tools_calls:
        return Verdict("na", "no tool_call audits observed")
    # `correlation_lost` defaults to True for entries without the flag set
    # (E20 design: unrecoverable entries count against the reliability
    # rate; we cannot prove a call WAS correlated when the audit body is
    # opaque to us).
    correlated = sum(1 for a in tools_calls if _audit_correlation_lost(a) is False)
    rate = correlated / len(tools_calls)
    if rate >= 0.80:
        return Verdict(
            "pass",
            f"correlation rate {rate:.0%} ({correlated}/{len(tools_calls)})",
        )
    return Verdict(
        "fail",
        f"correlation rate {rate:.0%} ({correlated}/{len(tools_calls)}) "
        f"below 80% threshold",
    )


def compute_verdicts(trial: Trial, pair_resolver=None) -> dict[str, Verdict]:
    """Return {a, b, c, d, e, f, h, i} verdicts.

    Plan A computed a+b+f; Plan B T9 added c; T10 added d; T11 added e;
    E4 added h (latency overhead vs baseline pair); E20 (this commit)
    adds i (tools_list snapshot correlation rate).

    `pair_resolver` is injected straight through to verdict_h_overhead so
    callers (esp. tests) can avoid the disk-based matrix lookup.
    """
    if trial.config.routing == "direct":
        na = Verdict("na", "baseline — cidgar not in path")
        return {"a": na, "b": na, "c": na, "d": na, "e": na, "f": na, "h": na, "i": na}
    if trial.status == "aborted":
        na = Verdict("na", "trial aborted before completion")
        return {"a": na, "b": na, "c": na, "d": na, "e": na, "f": na, "h": na, "i": na}
    return {
        "a": verdict_a_presence(trial),
        "b": verdict_b_channel_structure(trial),
        "c": verdict_c_continuity(trial),
        "d": verdict_d_resilience(trial),
        "e": verdict_e_state_mode_gap(trial),
        "f": verdict_f_gar_richness(trial),
        "h": verdict_h_overhead(trial, pair_resolver=pair_resolver),
        "i": verdict_i_tools_list_correlation(trial),
    }
