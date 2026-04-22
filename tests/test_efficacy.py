"""Tests for harness/efficacy.py — verdict computation (Plan A: verdicts a + b)."""
from trials import Trial, TrialConfig, TurnPlan, Turn, AuditEntry, Verdict
from efficacy import compute_verdicts


def _trial_with(turns, audit_entries, routing="via_agw", api="chat"):
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


def test_plan_b_verdicts_cde_return_na():
    """Plan A reports c/d/e as na with reason 'deferred to Plan B'."""
    turns = [Turn(turn_id="t0", turn_idx=0, kind="user_msg")]
    audit = [
        AuditEntry(trial_id="t", turn_id="t0", phase="llm_request",
                   cid="ib_abc", backend="ollama", raw={}),
    ]
    trial = _trial_with(turns, audit)
    v = compute_verdicts(trial)
    for lvl in ("c", "d", "e"):
        assert v[lvl].verdict == "na"
        assert "plan b" in v[lvl].reason.lower() or "deferred" in v[lvl].reason.lower()
