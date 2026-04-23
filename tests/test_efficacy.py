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
