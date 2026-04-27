"""Tests for harness/efficacy.py — verdict computation (Plan A: verdicts a + b)."""
from trials import Trial, TrialConfig, TurnPlan, Turn, AuditEntry, Verdict
from efficacy import compute_verdicts


def _trial_with(turns, audit_entries, routing="via_agw", api="chat",
                cfg=None):
    """Build a Trial for tests. Callers can pass `cfg=TrialConfig(...)` to
    control framework/api/state/llm; otherwise a default langchain/chat
    stateless config is used.
    """
    if cfg is None:
        cfg = TrialConfig(
            framework="langchain", api=api,
            stream=False, state=False,
            llm="ollama", mcp="NONE", routing=routing,
        )
    return Trial(
        trial_id="t", config=cfg, turn_plan=TurnPlan(turns=[]),
        turns=turns, audit_entries=audit_entries,
    )


def test_verdict_a_pass_when_cid_present_each_turn():
    turns = [
        Turn(turn_id="t0", turn_idx=0, kind="user_msg"),
        Turn(turn_id="t1", turn_idx=1, kind="user_msg"),
    ]
    audit = [
        AuditEntry(trial_id="t", turn_id="t0", phase="llm_request",
                   cid="ib_abc", backend="ollama", raw={}),
        AuditEntry(trial_id="t", turn_id="t1", phase="llm_request",
                   cid="ib_abc", backend="ollama", raw={}),
    ]
    trial = _trial_with(turns, audit)
    v = compute_verdicts(trial)
    assert v["a"].verdict == "pass"


def test_verdict_a_fail_when_turn_has_no_cid_entry():
    turns = [Turn(turn_id="t0", turn_idx=0, kind="user_msg")]
    audit = []  # no audit entries captured
    trial = _trial_with(turns, audit)
    v = compute_verdicts(trial)
    assert v["a"].verdict in ("fail", "error")


def test_verdict_b_pass_when_c2_marker_in_text_response():
    """Verdict b — text response carries marker matching audit cid."""
    turns = [Turn(
        turn_id="t0", turn_idx=0, kind="user_msg",
        response={
            "body": {
                "choices": [
                    {"message": {"content": "Here's info.<!-- ib:cid=ib_abc123def456 -->"}}
                ]
            }
        },
    )]
    audit = [
        AuditEntry(trial_id="t", turn_id="t0", phase="terminal",
                   cid="ib_abc123def456", backend="ollama", raw={}),
    ]
    trial = _trial_with(turns, audit)
    v = compute_verdicts(trial)
    assert v["b"].verdict == "pass"


def test_verdict_b_fail_when_text_response_missing_marker():
    turns = [Turn(
        turn_id="t0", turn_idx=0, kind="user_msg",
        response={"body": {"choices": [{"message": {"content": "plain text response"}}]}},
    )]
    audit = [
        AuditEntry(trial_id="t", turn_id="t0", phase="terminal",
                   cid="ib_abc123def456", backend="ollama", raw={}),
    ]
    trial = _trial_with(turns, audit)
    v = compute_verdicts(trial)
    assert v["b"].verdict == "fail"
    assert "C2" in v["b"].reason or "marker" in v["b"].reason.lower()


def test_verdict_b_pass_when_c1_in_tool_calls_args():
    turns = [Turn(
        turn_id="t0", turn_idx=0, kind="user_msg",
        response={
            "body": {
                "choices": [{
                    "message": {
                        "tool_calls": [{
                            "function": {
                                "name": "get_weather",
                                "arguments": '{"city":"Paris","_ib_cid":"ib_abc123def456"}',
                            }
                        }]
                    }
                }]
            }
        },
    )]
    audit = [
        AuditEntry(trial_id="t", turn_id="t0", phase="tool_planned",
                   cid="ib_abc123def456", backend="ollama", raw={}),
    ]
    trial = _trial_with(turns, audit)
    v = compute_verdicts(trial)
    assert v["b"].verdict == "pass"


def test_direct_mode_skips_all_verdicts():
    """routing=direct → all 5 verdicts are na."""
    turns = [Turn(turn_id="t0", turn_idx=0, kind="user_msg")]
    trial = _trial_with(turns, [], routing="direct")
    v = compute_verdicts(trial)
    for lvl in ("a", "b", "c", "d", "e"):
        assert v[lvl].verdict == "na"


def test_plan_b_verdicts_cd_return_na_no_special_turns():
    """After T11, verdict (c), (d), and (e) all return na on a 1-turn chat
    trial with no special turn kinds.

    * (c) needs ≥2 user_msg turns for continuity — 1 turn → na.
    * (d) needs a compact turn — none in plan → na.
    * (e) only applies to api=responses+state=True trials — a chat/stateless
          row short-circuits to na on the api/state prerequisite.
    """
    turns = [Turn(turn_id="t0", turn_idx=0, kind="user_msg")]
    audit = [
        AuditEntry(trial_id="t", turn_id="t0", phase="llm_request",
                   cid="ib_abc", backend="ollama", raw={}),
    ]
    trial = _trial_with(turns, audit)
    v = compute_verdicts(trial)
    # (c) should be na for a 1-turn trial (needs ≥2 turns).
    assert v["c"].verdict == "na"
    assert "≥2" in v["c"].reason or "user_msg turns" in v["c"].reason
    # (d) na with "no compact" reason when no compact turn in plan.
    assert v["d"].verdict == "na"
    assert "no compact" in v["d"].reason.lower()
    # (e) na with "responses" or "state" reason when trial uses chat/stateless.
    assert v["e"].verdict == "na"
    assert "responses" in v["e"].reason.lower() or "state" in v["e"].reason.lower()


# ── Verdict (f) GAR richness tests ─────────────────────────────────────

def test_verdict_f_pass_when_gar_populated_with_all_5_keys():
    """(f) GAR richness — valid 5-key GAR in tool_call args → pass."""
    import json as _json
    gar = _json.dumps({
        "goal": "find weather",
        "need": "user asked about Paris",
        "impact": "read-only",
        "dspm": "none",
        "alt": "web search",
    })
    args_inner = _json.dumps({"city": "Paris", "_ib_cid": "ib_abc", "_ib_gar": gar})
    turns = [Turn(
        turn_id="t0", turn_idx=0, kind="user_msg",
        response={"body": {"choices": [{"message": {"tool_calls": [{
            "function": {"name": "get_weather", "arguments": args_inner}
        }]}}]}},
    )]
    trial = _trial_with(turns, [])
    v = compute_verdicts(trial)
    assert v["f"].verdict == "pass", v["f"].reason


def test_verdict_f_na_when_gar_omitted_spec_92_compliant():
    """(f) GAR richness — LLM omitted _ib_gar entirely → na (spec §9.2)."""
    turns = [Turn(
        turn_id="t0", turn_idx=0, kind="user_msg",
        response={"body": {"choices": [{"message": {"tool_calls": [{
            "function": {"name": "get_weather",
                         "arguments": '{"city":"Paris","_ib_cid":"ib_abc"}'}
        }]}}]}},
    )]
    trial = _trial_with(turns, [])
    v = compute_verdicts(trial)
    assert v["f"].verdict == "na", v["f"].reason
    assert "omit" in v["f"].reason.lower() or "§9.2" in v["f"].reason


def test_verdict_f_fail_when_gar_malformed():
    """(f) GAR richness — _ib_gar present but missing keys → fail."""
    turns = [Turn(
        turn_id="t0", turn_idx=0, kind="user_msg",
        response={"body": {"choices": [{"message": {"tool_calls": [{
            "function": {"name": "get_weather",
                         "arguments": '{"city":"Paris","_ib_cid":"ib_abc","_ib_gar":"{\\"goal\\":\\"x\\"}"}'}
        }]}}]}},
    )]
    trial = _trial_with(turns, [])
    v = compute_verdicts(trial)
    assert v["f"].verdict == "fail", v["f"].reason


def test_verdict_f_na_when_no_tool_calls():
    """(f) GAR richness — chat-only (no tool_calls) → na."""
    turns = [Turn(
        turn_id="t0", turn_idx=0, kind="user_msg",
        response={"body": {"choices": [{
            "message": {"content": "hi!<!-- ib:cid=ib_abc123def456 -->"}
        }]}},
    )]
    trial = _trial_with(turns, [])
    v = compute_verdicts(trial)
    assert v["f"].verdict == "na", v["f"].reason
    assert "no tool" in v["f"].reason.lower()


# ── Verdict (c) multi-turn continuity tests ────────────────────────────

def test_verdict_c_pass_when_cid_preserved_across_3_turns():
    """All 3 user_msg turns share the same cid → continuity pass."""
    turns = [
        Turn(turn_id="t0", turn_idx=0, kind="user_msg",
             started_at="2026-04-23T10:00:00", finished_at="2026-04-23T10:00:05"),
        Turn(turn_id="t1", turn_idx=1, kind="user_msg",
             started_at="2026-04-23T10:00:10", finished_at="2026-04-23T10:00:15"),
        Turn(turn_id="t2", turn_idx=2, kind="user_msg",
             started_at="2026-04-23T10:00:20", finished_at="2026-04-23T10:00:25"),
    ]
    audit = [
        AuditEntry(trial_id="t", turn_id=None, phase="llm_request",
                   cid="ib_abc123def456", backend="ollama", raw={},
                   captured_at="2026-04-23T10:00:02"),
        AuditEntry(trial_id="t", turn_id=None, phase="llm_request",
                   cid="ib_abc123def456", backend="ollama", raw={},
                   captured_at="2026-04-23T10:00:12"),
        AuditEntry(trial_id="t", turn_id=None, phase="llm_request",
                   cid="ib_abc123def456", backend="ollama", raw={},
                   captured_at="2026-04-23T10:00:22"),
    ]
    trial = _trial_with(turns, audit)
    v = compute_verdicts(trial)
    assert v["c"].verdict == "pass", v["c"].reason


def test_verdict_c_fail_when_cid_changes_between_turns():
    """Turn 0 has cid_X, Turn 1 has cid_Y → continuity broken."""
    turns = [
        Turn(turn_id="t0", turn_idx=0, kind="user_msg",
             started_at="2026-04-23T10:00:00", finished_at="2026-04-23T10:00:05"),
        Turn(turn_id="t1", turn_idx=1, kind="user_msg",
             started_at="2026-04-23T10:00:10", finished_at="2026-04-23T10:00:15"),
    ]
    audit = [
        AuditEntry(trial_id="t", turn_id=None, phase="llm_request",
                   cid="ib_aaaaaaaaaaaa", backend="ollama", raw={},
                   captured_at="2026-04-23T10:00:02"),
        AuditEntry(trial_id="t", turn_id=None, phase="llm_request",
                   cid="ib_bbbbbbbbbbbb", backend="ollama", raw={},
                   captured_at="2026-04-23T10:00:12"),
    ]
    trial = _trial_with(turns, audit)
    v = compute_verdicts(trial)
    assert v["c"].verdict == "fail", v["c"].reason


def test_verdict_c_na_when_only_one_turn():
    turns = [Turn(turn_id="t0", turn_idx=0, kind="user_msg")]
    trial = _trial_with(turns, [])
    v = compute_verdicts(trial)
    assert v["c"].verdict == "na"


def test_verdict_c_error_when_audit_missing_for_some_turns():
    """If audit doesn't have cid-bearing entries for ≥2 turns → error (or fail)."""
    turns = [
        Turn(turn_id="t0", turn_idx=0, kind="user_msg",
             started_at="2026-04-23T10:00:00", finished_at="2026-04-23T10:00:05"),
        Turn(turn_id="t1", turn_idx=1, kind="user_msg",
             started_at="2026-04-23T10:00:10", finished_at="2026-04-23T10:00:15"),
    ]
    # only one audit entry covers turn 0; turn 1 has nothing in window
    audit = [
        AuditEntry(trial_id="t", turn_id=None, phase="llm_request",
                   cid="ib_aaa", backend="ollama", raw={},
                   captured_at="2026-04-23T10:00:02"),
    ]
    trial = _trial_with(turns, audit)
    v = compute_verdicts(trial)
    assert v["c"].verdict in ("error", "fail"), v["c"].reason


def test_verdict_c_pass_with_header_demux():
    """If audit entries carry turn_id (header demux available), use that path."""
    turns = [
        Turn(turn_id="turn-A", turn_idx=0, kind="user_msg"),
        Turn(turn_id="turn-B", turn_idx=1, kind="user_msg"),
    ]
    audit = [
        AuditEntry(trial_id="t", turn_id="turn-A", phase="llm_request",
                   cid="ib_xyz", backend="ollama", raw={}),
        AuditEntry(trial_id="t", turn_id="turn-B", phase="llm_request",
                   cid="ib_xyz", backend="ollama", raw={}),
    ]
    trial = _trial_with(turns, audit)
    v = compute_verdicts(trial)
    assert v["c"].verdict == "pass"


# ── E21: Verdict (c) bracket-aware reset_context tests ────────────────

def test_verdict_c_segments_at_reset_context_no_leak_passes():
    """Two segments separated by reset_context, distinct CIDs per segment,
    no cross-segment overlap → pass with the new multi-segment message.
    """
    turns = [
        Turn(turn_id="t0", turn_idx=0, kind="user_msg",
             started_at="2026-04-26T10:00:00", finished_at="2026-04-26T10:00:05"),
        Turn(turn_id="t1", turn_idx=1, kind="user_msg",
             started_at="2026-04-26T10:00:10", finished_at="2026-04-26T10:00:15"),
        Turn(turn_id="t-reset", turn_idx=2, kind="reset_context"),
        Turn(turn_id="t3", turn_idx=3, kind="user_msg",
             started_at="2026-04-26T10:00:30", finished_at="2026-04-26T10:00:35"),
        Turn(turn_id="t4", turn_idx=4, kind="user_msg",
             started_at="2026-04-26T10:00:40", finished_at="2026-04-26T10:00:45"),
    ]
    audit = [
        # Segment 0: ib_aaa across both turns
        AuditEntry(trial_id="t", turn_id=None, phase="llm_request",
                   cid="ib_aaaaaaaaaaaa", backend="ollama", raw={},
                   captured_at="2026-04-26T10:00:02"),
        AuditEntry(trial_id="t", turn_id=None, phase="llm_request",
                   cid="ib_aaaaaaaaaaaa", backend="ollama", raw={},
                   captured_at="2026-04-26T10:00:12"),
        # Segment 1: ib_bbb across both turns (distinct from segment 0)
        AuditEntry(trial_id="t", turn_id=None, phase="llm_request",
                   cid="ib_bbbbbbbbbbbb", backend="ollama", raw={},
                   captured_at="2026-04-26T10:00:32"),
        AuditEntry(trial_id="t", turn_id=None, phase="llm_request",
                   cid="ib_bbbbbbbbbbbb", backend="ollama", raw={},
                   captured_at="2026-04-26T10:00:42"),
    ]
    trial = _trial_with(turns, audit)
    v = compute_verdicts(trial)
    assert v["c"].verdict == "pass", v["c"].reason
    # The new branch surfaces segment count + total CID count.
    assert "2 segment" in v["c"].reason
    assert "no cross-segment leak" in v["c"].reason


def test_verdict_c_detects_cross_segment_leak_fails():
    """Same CID appears in BOTH pre-reset and post-reset segments — that's
    a CID isolation breach (AGW failed to mint a fresh CID after the
    reset boundary, or framework leaked old context). New verdict signal."""
    turns = [
        Turn(turn_id="t0", turn_idx=0, kind="user_msg",
             started_at="2026-04-26T10:00:00", finished_at="2026-04-26T10:00:05"),
        Turn(turn_id="t1", turn_idx=1, kind="user_msg",
             started_at="2026-04-26T10:00:10", finished_at="2026-04-26T10:00:15"),
        Turn(turn_id="t-reset", turn_idx=2, kind="reset_context"),
        Turn(turn_id="t3", turn_idx=3, kind="user_msg",
             started_at="2026-04-26T10:00:30", finished_at="2026-04-26T10:00:35"),
    ]
    audit = [
        AuditEntry(trial_id="t", turn_id=None, phase="llm_request",
                   cid="ib_leakedcid12", backend="ollama", raw={},
                   captured_at="2026-04-26T10:00:02"),
        AuditEntry(trial_id="t", turn_id=None, phase="llm_request",
                   cid="ib_leakedcid12", backend="ollama", raw={},
                   captured_at="2026-04-26T10:00:12"),
        # CRITICAL: same cid appears AFTER the reset boundary.
        AuditEntry(trial_id="t", turn_id=None, phase="llm_request",
                   cid="ib_leakedcid12", backend="ollama", raw={},
                   captured_at="2026-04-26T10:00:32"),
    ]
    trial = _trial_with(turns, audit)
    v = compute_verdicts(trial)
    assert v["c"].verdict == "fail", v["c"].reason
    assert "leak" in v["c"].reason.lower()
    assert "ib_leakedcid12" in v["c"].reason


def test_verdict_c_handles_no_resets_unchanged_behavior():
    """Trial with NO reset_context turns falls through to the legacy
    single-segment branch, preserving the pre-E21 message format."""
    turns = [
        Turn(turn_id="t0", turn_idx=0, kind="user_msg",
             started_at="2026-04-26T10:00:00", finished_at="2026-04-26T10:00:05"),
        Turn(turn_id="t1", turn_idx=1, kind="user_msg",
             started_at="2026-04-26T10:00:10", finished_at="2026-04-26T10:00:15"),
    ]
    audit = [
        AuditEntry(trial_id="t", turn_id=None, phase="llm_request",
                   cid="ib_singlecid01", backend="ollama", raw={},
                   captured_at="2026-04-26T10:00:02"),
        AuditEntry(trial_id="t", turn_id=None, phase="llm_request",
                   cid="ib_singlecid01", backend="ollama", raw={},
                   captured_at="2026-04-26T10:00:12"),
    ]
    trial = _trial_with(turns, audit)
    v = compute_verdicts(trial)
    assert v["c"].verdict == "pass", v["c"].reason
    # Legacy single-segment message uses "consecutive turns" wording.
    assert "consecutive" in v["c"].reason


def test_verdict_c_refresh_tools_does_not_split_segments():
    """refresh_tools is a tool-cache event, not a CID boundary. A trial
    with the SAME cid spanning a refresh_tools turn passes (no leak,
    same single segment) — the cid is allowed to cross refresh_tools."""
    turns = [
        Turn(turn_id="t0", turn_idx=0, kind="user_msg",
             started_at="2026-04-26T10:00:00", finished_at="2026-04-26T10:00:05"),
        Turn(turn_id="t-refresh", turn_idx=1, kind="refresh_tools"),
        Turn(turn_id="t2", turn_idx=2, kind="user_msg",
             started_at="2026-04-26T10:00:20", finished_at="2026-04-26T10:00:25"),
    ]
    audit = [
        AuditEntry(trial_id="t", turn_id=None, phase="llm_request",
                   cid="ib_continuous1", backend="ollama", raw={},
                   captured_at="2026-04-26T10:00:02"),
        AuditEntry(trial_id="t", turn_id=None, phase="llm_request",
                   cid="ib_continuous1", backend="ollama", raw={},
                   captured_at="2026-04-26T10:00:22"),
    ]
    trial = _trial_with(turns, audit)
    v = compute_verdicts(trial)
    assert v["c"].verdict == "pass", v["c"].reason
    # Single-segment branch — CID continuity preserved (legacy message).
    assert "consecutive" in v["c"].reason


# ── Verdict (d) compaction resilience tests ────────────────────────────

def test_verdict_d_pass_when_cid_survives_compact():
    """user_msg → compact → user_msg with same cid in both → pass."""
    turns = [
        Turn(turn_id="t0", turn_idx=0, kind="user_msg",
             started_at="2026-04-23T10:00:00", finished_at="2026-04-23T10:00:05"),
        Turn(turn_id="t1", turn_idx=1, kind="compact"),
        Turn(turn_id="t2", turn_idx=2, kind="user_msg",
             started_at="2026-04-23T10:00:10", finished_at="2026-04-23T10:00:15"),
    ]
    audit = [
        AuditEntry(trial_id="t", turn_id=None, phase="llm_request",
                   cid="ib_abc", backend="ollama", raw={},
                   captured_at="2026-04-23T10:00:02"),
        AuditEntry(trial_id="t", turn_id=None, phase="llm_request",
                   cid="ib_abc", backend="ollama", raw={},
                   captured_at="2026-04-23T10:00:12"),
    ]
    trial = _trial_with(turns, audit)
    v = compute_verdicts(trial)
    assert v["d"].verdict == "pass", v["d"].reason


def test_verdict_d_fail_when_cid_lost_post_compact():
    """CID before compact differs from CID after → fail."""
    turns = [
        Turn(turn_id="t0", turn_idx=0, kind="user_msg",
             started_at="2026-04-23T10:00:00", finished_at="2026-04-23T10:00:05"),
        Turn(turn_id="t1", turn_idx=1, kind="compact"),
        Turn(turn_id="t2", turn_idx=2, kind="user_msg",
             started_at="2026-04-23T10:00:10", finished_at="2026-04-23T10:00:15"),
    ]
    audit = [
        AuditEntry(trial_id="t", turn_id=None, phase="llm_request",
                   cid="ib_aaa", backend="ollama", raw={},
                   captured_at="2026-04-23T10:00:02"),
        AuditEntry(trial_id="t", turn_id=None, phase="llm_request",
                   cid="ib_bbb", backend="ollama", raw={},
                   captured_at="2026-04-23T10:00:12"),
    ]
    trial = _trial_with(turns, audit)
    v = compute_verdicts(trial)
    assert v["d"].verdict == "fail", v["d"].reason


def test_verdict_d_na_when_no_compact_turn():
    """No compact turn in the plan → verdict (d) na."""
    turns = [Turn(turn_id="t0", turn_idx=0, kind="user_msg")]
    trial = _trial_with(turns, [])
    v = compute_verdicts(trial)
    assert v["d"].verdict == "na"
    assert "no compact" in v["d"].reason.lower()


def test_verdict_d_na_when_compact_lacks_post_turn():
    """compact turn without a following user_msg → can't measure → na."""
    turns = [
        Turn(turn_id="t0", turn_idx=0, kind="user_msg"),
        Turn(turn_id="t1", turn_idx=1, kind="compact"),
    ]
    trial = _trial_with(turns, [])
    v = compute_verdicts(trial)
    assert v["d"].verdict == "na"
    assert "can't measure" in v["d"].reason.lower() or "before and after" in v["d"].reason.lower()


# ── Verdict (e) state-mode gap tests ───────────────────────────────────

def _responses_state_cfg():
    """TrialConfig for a typical verdict-e row (autogen + responses + state)."""
    return TrialConfig(
        framework="autogen", api="responses", stream=False, state=True,
        llm="chatgpt", mcp="NONE", routing="via_agw",
    )


def test_verdict_e_pass_when_cid_survives_state_ref():
    """user_msg → force_state_ref share the same cid in audit → pass."""
    cfg = _responses_state_cfg()
    turns = [
        Turn(turn_id="t0", turn_idx=0, kind="user_msg",
             started_at="2026-04-23T10:00:00", finished_at="2026-04-23T10:00:05"),
        Turn(turn_id="t1", turn_idx=1, kind="force_state_ref",
             started_at="2026-04-23T10:00:10", finished_at="2026-04-23T10:00:15"),
    ]
    audit = [
        AuditEntry(trial_id="t", turn_id=None, phase="llm_request",
                   cid="ib_abc", backend="chatgpt", raw={},
                   captured_at="2026-04-23T10:00:02"),
        AuditEntry(trial_id="t", turn_id=None, phase="llm_request",
                   cid="ib_abc", backend="chatgpt", raw={},
                   captured_at="2026-04-23T10:00:12"),
    ]
    trial = _trial_with(turns, audit, cfg=cfg)
    v = compute_verdicts(trial)
    assert v["e"].verdict == "pass", v["e"].reason


def test_verdict_e_fail_when_cid_lost_on_state_ref():
    """Turn 0 cid_X, force_state_ref carries cid_Y → continuity broken → fail."""
    cfg = _responses_state_cfg()
    turns = [
        Turn(turn_id="t0", turn_idx=0, kind="user_msg",
             started_at="2026-04-23T10:00:00", finished_at="2026-04-23T10:00:05"),
        Turn(turn_id="t1", turn_idx=1, kind="force_state_ref",
             started_at="2026-04-23T10:00:10", finished_at="2026-04-23T10:00:15"),
    ]
    audit = [
        AuditEntry(trial_id="t", turn_id=None, phase="llm_request",
                   cid="ib_aaa", backend="chatgpt", raw={},
                   captured_at="2026-04-23T10:00:02"),
        AuditEntry(trial_id="t", turn_id=None, phase="llm_request",
                   cid="ib_bbb", backend="chatgpt", raw={},
                   captured_at="2026-04-23T10:00:12"),
    ]
    trial = _trial_with(turns, audit, cfg=cfg)
    v = compute_verdicts(trial)
    assert v["e"].verdict == "fail", v["e"].reason


def test_verdict_e_na_when_api_not_responses():
    """api=chat row → verdict (e) na on the api prerequisite."""
    cfg = TrialConfig(
        framework="langchain", api="chat", stream=False, state=False,
        llm="ollama", mcp="NONE", routing="via_agw",
    )
    turns = [Turn(turn_id="t0", turn_idx=0, kind="user_msg")]
    trial = _trial_with(turns, [], cfg=cfg)
    v = compute_verdicts(trial)
    assert v["e"].verdict == "na"
    assert "responses" in v["e"].reason.lower()


def test_verdict_e_na_when_state_false():
    """api=responses but state=False → verdict (e) na (no chain exists to jump)."""
    cfg = TrialConfig(
        framework="autogen", api="responses", stream=False, state=False,
        llm="chatgpt", mcp="NONE", routing="via_agw",
    )
    turns = [
        Turn(turn_id="t0", turn_idx=0, kind="user_msg"),
        Turn(turn_id="t1", turn_idx=1, kind="force_state_ref"),
    ]
    trial = _trial_with(turns, [], cfg=cfg)
    v = compute_verdicts(trial)
    assert v["e"].verdict == "na"
    assert "state" in v["e"].reason.lower()


def test_verdict_e_na_when_no_force_state_ref_turn():
    """No force_state_ref in plan → verdict (e) na on plan prerequisite."""
    cfg = _responses_state_cfg()
    turns = [Turn(turn_id="t0", turn_idx=0, kind="user_msg")]
    trial = _trial_with(turns, [], cfg=cfg)
    v = compute_verdicts(trial)
    assert v["e"].verdict == "na"
    assert "force_state_ref" in v["e"].reason.lower()


# ── E4 — verdict (h) latency overhead vs baseline pair ─────────────────

def _t(idx: int, started_iso: str, finished_iso: str) -> Turn:
    """Convenience: build a user_msg Turn with timing for verdict-h tests."""
    return Turn(
        turn_id=f"t{idx}", turn_idx=idx, kind="user_msg",
        started_at=started_iso, finished_at=finished_iso,
    )


def test_verdict_h_pass_when_overhead_under_budget():
    """Median overhead ≤200ms over comparable turns → pass."""
    governed_turns = [
        _t(0, "2026-04-23T00:00:00+00:00", "2026-04-23T00:00:01.100000+00:00"),
        _t(1, "2026-04-23T00:00:02+00:00", "2026-04-23T00:00:03.150000+00:00"),
    ]
    baseline_turns = [
        _t(0, "2026-04-23T00:00:00+00:00", "2026-04-23T00:00:01+00:00"),
        _t(1, "2026-04-23T00:00:02+00:00", "2026-04-23T00:00:03+00:00"),
    ]
    governed = _trial_with(governed_turns, [])
    baseline = _trial_with(baseline_turns, [], routing="direct")

    v = compute_verdicts(governed, pair_resolver=lambda _t: baseline)
    # Overheads: 100ms, 150ms → median 125ms ≤ 200ms budget
    assert v["h"].verdict == "pass", v["h"].reason
    assert "overhead" in v["h"].reason.lower()


def test_verdict_h_fail_when_overhead_exceeds_absolute_budget():
    """Median overhead > 2000ms → fail (regardless of baseline median)."""
    governed_turns = [
        _t(0, "2026-04-23T00:00:00+00:00", "2026-04-23T00:00:05+00:00"),  # 5000ms
        _t(1, "2026-04-23T00:00:10+00:00", "2026-04-23T00:00:13+00:00"),  # 3000ms
    ]
    baseline_turns = [
        _t(0, "2026-04-23T00:00:00+00:00", "2026-04-23T00:00:00.500000+00:00"),
        _t(1, "2026-04-23T00:00:10+00:00", "2026-04-23T00:00:10.400000+00:00"),
    ]
    governed = _trial_with(governed_turns, [])
    baseline = _trial_with(baseline_turns, [], routing="direct")

    v = compute_verdicts(governed, pair_resolver=lambda _t: baseline)
    # Overheads: 4500ms, 2600ms → median ~3550 → fail on absolute budget
    assert v["h"].verdict == "fail"
    assert "2000ms" in v["h"].reason or "absolute budget" in v["h"].reason


def test_verdict_h_na_when_no_baseline_pair():
    """No paired baseline (resolver returns None) → na."""
    governed_turns = [_t(0, "2026-04-23T00:00:00+00:00", "2026-04-23T00:00:01+00:00")]
    governed = _trial_with(governed_turns, [])

    v = compute_verdicts(governed, pair_resolver=lambda _t: None)
    assert v["h"].verdict == "na"
    assert "baseline" in v["h"].reason.lower()


def test_verdict_h_na_when_trial_is_direct_routed():
    """Direct-routed (baseline-side) trial → na (verdict measured on governed
    side only)."""
    turns = [_t(0, "2026-04-23T00:00:00+00:00", "2026-04-23T00:00:01+00:00")]
    direct_trial = _trial_with(turns, [], routing="direct")

    # routing=direct short-circuits at compute_verdicts before resolver runs;
    # all verdicts return na with the baseline-routing reason.
    v = compute_verdicts(direct_trial, pair_resolver=lambda _t: None)
    assert v["h"].verdict == "na"
    # The compute_verdicts short-circuit reason is "baseline — cidgar not in path".
    assert "cidgar" in v["h"].reason.lower() or "baseline" in v["h"].reason.lower()


# ── Anthropic Messages shape tool_use walker tests ────────────────────

def test_verdict_f_pass_anthropic_messages_tool_use_with_full_gar():
    """Anthropic Messages content[].type=tool_use with input._ib_gar present
    (5-key JSON string) — verdict (f) must report pass, not na."""
    cfg = TrialConfig(framework="langchain", api="messages", stream=False, state=False,
                      llm="claude", mcp="fetch", routing="via_agw")
    body = {
        "id": "msg_test",
        "role": "assistant",
        "content": [{
            "type": "tool_use",
            "id": "toolu_1",
            "name": "fetch_fetch",
            "input": {
                "url": "https://example.com",
                "_ib_cid": "ib_test12345abc",
                "_ib_gar": '{"goal":"x","need":"y","impact":"z","dspm":"a","alt":"b"}',
            },
        }],
    }
    turn = Turn(turn_id="t0", turn_idx=0, kind="user_msg",
                response={"body": body},
                started_at="2026-04-25T10:00:00",
                finished_at="2026-04-25T10:00:05")
    trial = _trial_with([turn], [], cfg=cfg)
    v = compute_verdicts(trial)
    assert v["f"].verdict == "pass", f"got {v['f']}"


def test_verdict_f_fail_anthropic_messages_tool_use_with_malformed_gar():
    """Messages tool_use with _ib_gar missing required keys → fail."""
    cfg = TrialConfig(framework="langchain", api="messages", stream=False, state=False,
                      llm="claude", mcp="NONE", routing="via_agw")
    body = {
        "content": [{
            "type": "tool_use", "id": "toolu_1", "name": "weather",
            "input": {"_ib_gar": '{"goal":"x"}'},  # Only 1 of 5 required keys
        }],
    }
    turn = Turn(turn_id="t0", turn_idx=0, kind="user_msg",
                response={"body": body},
                started_at="2026-04-25T10:00:00",
                finished_at="2026-04-25T10:00:05")
    trial = _trial_with([turn], [], cfg=cfg)
    v = compute_verdicts(trial)
    assert v["f"].verdict == "fail"
    assert "missing keys" in v["f"].reason.lower()


def test_verdict_b_anthropic_messages_validates_channel_1_not_just_text():
    """Regression: verdict (b) must detect Anthropic tool_use as Channel 1
    evidence, not silently fall through to Channel 2 only."""
    # Construct a trial where Channel 1 is present (tool_use) but Channel 2
    # is ABSENT. With the bug, verdict (b) would have to invent something or
    # fail; correct behavior: pass via Ch1.
    cfg = TrialConfig(framework="langchain", api="messages", stream=False, state=False,
                      llm="claude", mcp="weather", routing="via_agw")
    body = {
        "content": [{
            "type": "tool_use", "id": "toolu_1", "name": "weather",
            "input": {"location": "SF", "_ib_cid": "ib_test12345abc"},
        }],
        # No text content block — only tool_use
    }
    turn = Turn(turn_id="t0", turn_idx=0, kind="user_msg",
                response={"body": body},
                started_at="2026-04-25T10:00:00",
                finished_at="2026-04-25T10:00:05")
    audit = [
        AuditEntry(trial_id="t", turn_id=None, phase="llm_request",
                   cid="ib_test12345abc", backend="claude", raw={},
                   captured_at="2026-04-25T10:00:02"),
    ]
    trial = _trial_with([turn], audit, cfg=cfg)
    v = compute_verdicts(trial)
    assert v["b"].verdict == "pass", f"got {v['b']}"


def test_extract_gar_strings_from_body_messages_shape_helper():
    """Direct unit test of the new helper."""
    from efficacy import _extract_gar_strings_from_body_messages_shape
    body = {
        "content": [
            {"type": "text", "text": "hello"},  # ignored
            {"type": "tool_use", "input": {"_ib_gar": "abc"}},
            {"type": "tool_use", "input": {"other": "x"}},  # no _ib_gar — skip
            {"type": "tool_use", "input": {"_ib_gar": "def"}},
        ],
    }
    assert _extract_gar_strings_from_body_messages_shape(body) == ["abc", "def"]


# ── E20 — verdict (i) tools_list_correlation ──────────────────────────


def _tool_call_audit(correlation_lost: bool, *, hash_val: str = "deadbeef",
                     tool_name: str = "weather") -> AuditEntry:
    """Build a synthetic tool_call audit carrying the E20 correlation_lost
    flag. Mirrors what cidgar emits via the extended `Phase::ToolCall`
    variant: `phase="tool_call"`, body holds `correlation_lost` +
    `snapshot_hash` + `original_tool_name`.

    The verdict's `_audit_correlation_lost` helper walks `entry.raw["body"]`
    OR `entry.raw` directly — fixtures use the latter for readability.
    """
    return AuditEntry(
        trial_id="t",
        turn_id=None,
        phase="tool_call",
        cid="ib_abcdef012345",
        backend="weather-mcp",
        raw={
            "correlation_lost": correlation_lost,
            "snapshot_hash": hash_val if not correlation_lost else None,
            "original_tool_name": tool_name,
        },
        captured_at="2026-04-26T00:00:00+00:00",
    )


def test_verdict_i_pass_when_all_tool_calls_correlated():
    """E20 — 100% correlation rate → pass. Three tool_call audits, each
    with `correlation_lost=False`."""
    audits = [
        _tool_call_audit(correlation_lost=False),
        _tool_call_audit(correlation_lost=False),
        _tool_call_audit(correlation_lost=False),
    ]
    trial = _trial_with(turns=[], audit_entries=audits)
    v = compute_verdicts(trial)
    assert v["i"].verdict == "pass", f"got {v['i']}"
    assert "100%" in v["i"].reason


def test_verdict_i_fail_when_correlation_rate_below_threshold():
    """E20 — 70% correlation (7/10) is below the 80% threshold → fail."""
    audits = [_tool_call_audit(correlation_lost=False) for _ in range(7)]
    audits += [_tool_call_audit(correlation_lost=True) for _ in range(3)]
    trial = _trial_with(turns=[], audit_entries=audits)
    v = compute_verdicts(trial)
    assert v["i"].verdict == "fail", f"got {v['i']}"
    assert "70%" in v["i"].reason
    assert "80%" in v["i"].reason


def test_verdict_i_na_when_no_tool_call_audits():
    """E20 — chat-only trial with no tool_call audits → na (nothing to
    correlate, not a fail). `tools_list` audits alone do NOT trigger the
    verdict — correlation is measured at call time, not snapshot time."""
    audits = [
        AuditEntry(trial_id="t", turn_id=None, phase="tools_list",
                   cid=None, backend="weather-mcp", raw={}),
        AuditEntry(trial_id="t", turn_id=None, phase="llm_request",
                   cid="ib_abc", backend="ollama", raw={}),
        AuditEntry(trial_id="t", turn_id=None, phase="terminal",
                   cid="ib_abc", backend="ollama", raw={}),
    ]
    trial = _trial_with(turns=[], audit_entries=audits)
    v = compute_verdicts(trial)
    assert v["i"].verdict == "na", f"got {v['i']}"
    assert "no tool_call audits" in v["i"].reason


def test_verdict_i_reads_body_from_top_level_field():
    """E26 — production audit_tail surfaces governance body on the new
    top-level `AuditEntry.body` field. Verdict (i) MUST prefer it over the
    legacy `raw["body"]` walk; this is the only path that works for
    shape-B (regex-parsed) cidgar log lines, where `raw` is just
    `{"line": <text>}`. Mirror the production shape exactly: body lives
    on the top-level field, raw carries no body dict."""
    audits = [
        AuditEntry(
            trial_id="t", turn_id=None, phase="tool_call",
            cid="ib_abcdef012345", backend="weather-mcp",
            raw={"line": "shape-B governance text"},  # no body in raw
            body={
                "correlation_lost": False,
                "snapshot_hash": "deadbeef",
                "original_tool_name": "get_weather",
            },
            captured_at="2026-04-26T00:00:00+00:00",
        ),
        AuditEntry(
            trial_id="t", turn_id=None, phase="tool_call",
            cid="ib_abcdef012345", backend="weather-mcp",
            raw={"line": "shape-B governance text"},
            body={
                "correlation_lost": False,
                "snapshot_hash": "cafef00d",
                "original_tool_name": "get_weather",
            },
            captured_at="2026-04-26T00:00:01+00:00",
        ),
    ]
    trial = _trial_with(turns=[], audit_entries=audits)
    v = compute_verdicts(trial)
    assert v["i"].verdict == "pass", f"got {v['i']}"
    assert "100%" in v["i"].reason


def test_verdict_i_legacy_raw_body_fallback_still_works():
    """E26 — legacy persisted trials (pre-E26) have body=None on the
    AuditEntry and the body dict only survived under raw["body"]
    (shape A) or raw["fields"]["body"] (shape A loaded from JSON). Verdict
    (i) MUST fall back to those raw walks for backward compatibility."""
    audits = [
        # Legacy shape: top-level body field is None (default), correlation
        # only available under raw["body"].
        AuditEntry(
            trial_id="t", turn_id=None, phase="tool_call",
            cid="ib_abcdef012345", backend="weather-mcp",
            raw={"body": {"correlation_lost": False, "snapshot_hash": "abc"}},
            body=None,
            captured_at="2026-04-26T00:00:00+00:00",
        ),
    ]
    trial = _trial_with(turns=[], audit_entries=audits)
    v = compute_verdicts(trial)
    assert v["i"].verdict == "pass", f"got {v['i']}"


# ── E24 — verdict (k) cross-API continuity (combo / multi-LLM trials) ──


def _llm_audit(backend: str, cid: str | None) -> AuditEntry:
    """Build an llm-* backend AuditEntry. Mirrors the convention used by
    AGW's audit log: backend strings for routed LLM calls are prefixed
    `llm-<provider>` (verdict_k filters on this prefix)."""
    return AuditEntry(
        trial_id="t",
        turn_id=None,
        phase="llm_request",
        cid=cid,
        backend=backend,
        raw={},
        captured_at="2026-04-26T00:00:00+00:00",
    )


def test_verdict_k_pass_when_cid_common_across_routes():
    """Two routes (llm-chatgpt, llm-claude) both carry CID ib_aaa →
    intersection non-empty → pass. The "design promise" of E24."""
    audits = [
        _llm_audit("llm-chatgpt", "ib_aaaaaaaaaaaa"),
        _llm_audit("llm-claude",  "ib_aaaaaaaaaaaa"),
        _llm_audit("llm-chatgpt", "ib_aaaaaaaaaaaa"),
    ]
    trial = _trial_with(turns=[], audit_entries=audits)
    v = compute_verdicts(trial)
    assert v["k"].verdict == "pass", f"got {v['k']}"
    assert "ib_aaaaaaaaaaaa" in v["k"].reason


def test_verdict_k_fail_when_no_common_cid_across_routes():
    """Two routes, distinct CIDs (chatgpt has ib_aaa, claude has ib_bbb,
    no overlap) → fail. With no marker-shaped text in any request body
    (no turns/framework_events here), this is failure mode B: AGW minted
    distinct CIDs — possible isolation breach."""
    audits = [
        _llm_audit("llm-chatgpt", "ib_aaaaaaaaaaaa"),
        _llm_audit("llm-claude",  "ib_bbbbbbbbbbbb"),
    ]
    trial = _trial_with(turns=[], audit_entries=audits)
    v = compute_verdicts(trial)
    assert v["k"].verdict == "fail", f"got {v['k']}"
    assert "no CID common" in v["k"].reason
    # Mode B-specific phrasing — distinguishes from the mode-A
    # "agent-side propagation gap" and mode-C "model paraphrase".
    assert "AGW minted distinct CIDs" in v["k"].reason


def test_verdict_k_fail_when_route_lacks_any_cid():
    """One route had LLM traffic but no CID-bearing audits — that's a
    failure of CID propagation (failure mode A: agent-side gap), not a
    "verdict not applicable"."""
    audits = [
        _llm_audit("llm-chatgpt", "ib_aaaaaaaaaaaa"),
        _llm_audit("llm-claude",  None),  # no CID on this route's entry
    ]
    trial = _trial_with(turns=[], audit_entries=audits)
    v = compute_verdicts(trial)
    assert v["k"].verdict == "fail", f"got {v['k']}"
    # Failure mode A reason — distinct from B/C so operators can route
    # the investigation to the agent-side, not AGW.
    assert "agent-side propagation gap" in v["k"].reason
    assert "llm-claude" in v["k"].reason


def test_verdict_k_na_for_single_route():
    """Single LLM route → not a multi-LLM trial → verdict (k) doesn't
    apply (it's the sibling of verdict (c) for cross-API specifically)."""
    audits = [
        _llm_audit("llm-chatgpt", "ib_aaaaaaaaaaaa"),
        _llm_audit("llm-chatgpt", "ib_aaaaaaaaaaaa"),
    ]
    trial = _trial_with(turns=[], audit_entries=audits)
    v = compute_verdicts(trial)
    assert v["k"].verdict == "na", f"got {v['k']}"
    assert "single-route" in v["k"].reason


def test_verdict_k_na_for_no_llm_audits():
    """No llm-* backend audits at all (e.g. direct-mcp run) → na."""
    audits = [
        AuditEntry(trial_id="t", turn_id=None, phase="tools_list",
                   cid=None, backend="weather-mcp", raw={}),
    ]
    trial = _trial_with(turns=[], audit_entries=audits)
    v = compute_verdicts(trial)
    assert v["k"].verdict == "na", f"got {v['k']}"
    assert "no llm-" in v["k"].reason


def test_verdict_k_pass_three_routes_all_share_one_cid():
    """3 routes (chatgpt, claude, ollama) all carry the same CID ib_xxx →
    intersection {ib_xxx} → pass. Validates the set.intersection semantics
    when more than 2 routes are involved."""
    audits = [
        _llm_audit("llm-chatgpt", "ib_xxxxxxxxxxxx"),
        _llm_audit("llm-chatgpt", "ib_unique_chat"),  # extra CID on one route
        _llm_audit("llm-claude",  "ib_xxxxxxxxxxxx"),
        _llm_audit("llm-ollama",  "ib_xxxxxxxxxxxx"),
    ]
    trial = _trial_with(turns=[], audit_entries=audits)
    v = compute_verdicts(trial)
    assert v["k"].verdict == "pass", f"got {v['k']}"
    assert "ib_xxxxxxxxxxxx" in v["k"].reason
    # Must report all 3 routes in the reason (sanity check).
    assert "3 routes" in v["k"].reason


def test_verdict_k_distinguishes_route_with_no_cid():
    """Failure mode A — a route had LLM traffic but no CID at all. The
    reason string MUST point at agent-side propagation, NOT switch
    breach (which is mode B). This is the same scenario as
    test_verdict_k_fail_when_route_lacks_any_cid but pinning the
    operator-facing language — easy to silently regress otherwise."""
    audits = [
        _llm_audit("llm-chatgpt", "ib_aaaaaaaaaaaa"),
        _llm_audit("llm-claude",  None),
    ]
    trial = _trial_with(turns=[], audit_entries=audits)
    v = compute_verdicts(trial)
    assert v["k"].verdict == "fail", f"got {v['k']}"
    # Mode A — agent-side, NOT switch-breach (B) or marker-corruption (C).
    assert "agent-side propagation gap" in v["k"].reason
    assert "not a switch breach" in v["k"].reason
    # Must NOT misreport this as the AGW-minted-distinct-CIDs (mode B) reason.
    assert "AGW minted distinct CIDs" not in v["k"].reason
    assert "model paraphrase" not in v["k"].reason


def test_verdict_k_distinguishes_marker_paraphrase_from_isolation_breach():
    """Failure mode C — every route HAS a CID (so mode A doesn't
    apply), CIDs don't overlap (so it would default to mode B), BUT
    request bodies on multiple routes contain marker-shaped text.

    That bytes-present-but-AGW-didn't-reuse signal points at the model
    (paraphrased the marker into AGW-unrecognizable form), not AGW
    isolation. Reason must say "model paraphrase" — operators look at
    assistant_text, not gateway config.
    """
    cfg = TrialConfig(framework="combo", api="chat", stream=False, state=False,
                      llm=["chatgpt", "claude"], mcp="NONE", routing="via_agw")
    # Each turn carries a framework_event whose request body contains a
    # marker-shaped string (MARKER_RE matches `<!-- ib:cid=ib_xxxxxxxxxxxx -->`).
    # The marker text is present on BOTH routes, but each route's
    # AGW-extracted CID differs (no overlap). That's mode C.
    marker_str = "user said: <!-- ib:cid=ib_aaaaaaaaaaaa --> hi"
    turn0 = Turn(
        turn_id="t0", turn_idx=0, kind="user_msg",
        framework_events=[{
            "t": "llm_dispatch_0",
            "llm_for_turn": "chatgpt",
            "request": {
                "url": "http://agentgateway:8080/llm/chatgpt/v1/chat/completions",
                "body": {"messages": [{"role": "user", "content": marker_str}]},
            },
            "response": None,
        }],
    )
    turn1 = Turn(
        turn_id="t1", turn_idx=1, kind="user_msg",
        framework_events=[{
            "t": "llm_dispatch_0",
            "llm_for_turn": "claude",
            "request": {
                "url": "http://agentgateway:8080/llm/claude/v1/messages",
                "body": {"messages": [{"role": "user", "content": marker_str}]},
            },
            "response": None,
        }],
    )
    audits = [
        _llm_audit("llm-chatgpt", "ib_aaaaaaaaaaaa"),  # AGW saw a CID here…
        _llm_audit("llm-claude",  "ib_bbbbbbbbbbbb"),  # …and a different CID here
    ]
    trial = _trial_with(turns=[turn0, turn1], audit_entries=audits, cfg=cfg)
    v = compute_verdicts(trial)
    assert v["k"].verdict == "fail", f"got {v['k']}"
    # Mode C-specific phrasing — points operator at the model (paraphrase),
    # NOT at AGW (mode B) or the agent (mode A).
    assert "model paraphrase" in v["k"].reason
    assert "AGW minted distinct CIDs" not in v["k"].reason
    assert "agent-side propagation gap" not in v["k"].reason


def test_body_has_any_tool_call_detects_both_shapes():
    """The shape-agnostic detector must trigger on either format."""
    from efficacy import _body_has_any_tool_call
    # OpenAI shape
    assert _body_has_any_tool_call({
        "choices": [{"message": {"tool_calls": [{"id": "x"}]}}]
    })
    # Anthropic shape
    assert _body_has_any_tool_call({
        "content": [{"type": "tool_use", "id": "x"}]
    })
    # Neither
    assert not _body_has_any_tool_call({"choices": [{"message": {"content": "hi"}}]})
    assert not _body_has_any_tool_call({"content": [{"type": "text", "text": "hi"}]})
    assert not _body_has_any_tool_call({})


# ── B2 regression: verdict_b should not silently pass on errored trials ──

def test_verdict_b_na_when_all_turns_errored():
    """B2 regression: when every turn errored at the adapter (response={},
    no framework_events with bodies), verdict_b used to silently fall
    through to `pass` because the body-scan loop's "if not bodies: continue"
    branch dropped every turn and `issues` stayed empty. Now we return
    `na` so the trial doesn't lie about correlation it never observed.

    This compounded with verdict (a) failing to produce mutually
    contradictory verdicts on errored trials.
    """
    turns = [
        Turn(turn_id=f"t{i}", turn_idx=i, kind="user_msg",
             response={}, error={"msg": "adapter blew up"}, framework_events=[])
        for i in range(3)
    ]
    audit = []  # no audit either — fully errored trial
    trial = _trial_with(turns, audit)
    v = compute_verdicts(trial)
    assert v["b"].verdict == "na", (
        f"expected na on all-errored turns, got {v['b'].verdict!r}: "
        f"{v['b'].reason}"
    )
    assert "no response bodies" in v["b"].reason.lower()


def test_verdict_b_pass_message_mentions_skipped():
    """B2 regression: on partial skip (some turns scanned, some had no
    body), the pass message should mention how many turns were skipped
    so the reason text doesn't overstate scan coverage.
    """
    turns = [
        # Turn 0: errored, no body to scan.
        Turn(turn_id="t0", turn_idx=0, kind="user_msg",
             response={}, error={"msg": "boom"}, framework_events=[]),
        # Turn 1 + 2: clean text response with C2 marker matching audit cid.
        Turn(turn_id="t1", turn_idx=1, kind="user_msg",
             response={"body": {"choices": [
                 {"message": {"content": "ok<!-- ib:cid=ib_abc123def456 -->"}}
             ]}}),
        Turn(turn_id="t2", turn_idx=2, kind="user_msg",
             response={"body": {"choices": [
                 {"message": {"content": "ok2<!-- ib:cid=ib_abc123def456 -->"}}
             ]}}),
    ]
    # Use turn_id=None on audit entries to force time-window mode so the
    # "turn 0 has no header-demux audit" early-error doesn't fire (the
    # purpose of THIS test is the skipped-turn message, not header-demux).
    audit = [
        AuditEntry(trial_id="t", turn_id=None, phase="terminal",
                   cid="ib_abc123def456", backend="ollama", raw={}),
        AuditEntry(trial_id="t", turn_id=None, phase="terminal",
                   cid="ib_abc123def456", backend="ollama", raw={}),
    ]
    trial = _trial_with(turns, audit)
    v = compute_verdicts(trial)
    assert v["b"].verdict == "pass", (
        f"expected pass, got {v['b'].verdict!r}: {v['b'].reason}"
    )
    assert "1 turns skipped" in v["b"].reason, (
        f"expected pass reason to flag the 1 skipped turn; got: {v['b'].reason}"
    )


# ── B3 regression: verdict_a fail message should surface audit phases ──

def test_verdict_a_fail_message_lists_audit_phases():
    """B3 regression: when audit has entries but none carry a CID — typical
    when only `tools_list` phase entries fired (per spec §5.1 those don't
    carry a CID by design) — the failure reason should include the phases
    observed so the user understands the adapter likely never reached the
    llm_request phase. The old message ("N audit entries but none carry a
    CID") read like cidgar misbehaved.
    """
    turns = [Turn(turn_id="t0", turn_idx=0, kind="user_msg")]
    # Three tools_list audit entries, no CIDs. (Per spec §5.1 tools_list
    # never has a CID — there's no LLM in the loop yet.)
    audit = [
        AuditEntry(trial_id="t", turn_id=None, phase="tools_list",
                   cid=None, backend="ollama", raw={}),
        AuditEntry(trial_id="t", turn_id=None, phase="tools_list",
                   cid=None, backend="ollama", raw={}),
        AuditEntry(trial_id="t", turn_id=None, phase="tools_list",
                   cid=None, backend="ollama", raw={}),
    ]
    trial = _trial_with(turns, audit)
    v = compute_verdicts(trial)
    assert v["a"].verdict == "fail"
    assert "tools_list" in v["a"].reason, (
        f"expected reason to surface tools_list phase; got: {v['a'].reason}"
    )
