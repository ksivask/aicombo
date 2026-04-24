"""Tests for harness/api.py — E4 pair diff endpoints (/pairs/{row_id}).

Covers the resolution rules (source row can be either governed or baseline),
the envelope shape of /pairs/{id}, /pairs/{id}/diff scoped lookups, and the
_classify_diff heuristic for expected-governance-marker vs unexpected diffs.
"""
from fastapi.testclient import TestClient

from main import app
from trials import Trial, TrialConfig, TurnPlan, Turn, AuditEntry


def _make_row(
    row_id: str, *, routing: str = "via_agw",
    baseline_of: str | None = None,
    last_trial_id: str | None = None,
    mcp: str = "NONE",
) -> dict:
    row: dict = {
        "row_id": row_id,
        "framework": "langchain",
        "api": "chat",
        "stream": False,
        "state": False,
        "llm": "ollama",
        "mcp": mcp,
        "routing": routing,
    }
    if baseline_of is not None:
        row["baseline_of"] = baseline_of
        row["note"] = f"Baseline (direct, no AGW) of {baseline_of}"
    if last_trial_id is not None:
        row["last_trial_id"] = last_trial_id
    return row


def _save_matrix(api_mod, rows: list[dict]) -> None:
    api_mod._save_matrix(rows)


def _save_trial(
    api_mod, *, trial_id: str, routing: str = "via_agw",
    turns=None, audit_entries=None, verdicts=None, status: str = "pass",
) -> None:
    cfg = TrialConfig(
        framework="langchain", api="chat", stream=False, state=False,
        llm="ollama", mcp="NONE", routing=routing,
    )
    trial = Trial(
        trial_id=trial_id, config=cfg, turn_plan=TurnPlan(turns=[]),
        status=status, turns=turns or [], audit_entries=audit_entries or [],
        verdicts=verdicts or {},
    )
    api_mod.STORE.save(trial)


# ── 404 / 400 / 409 paths ─────────────────────────────────────────────

def test_pairs_404_when_row_missing(tmp_data_dir, reset_api_state):
    """Unknown row_id → 404."""
    import api as api_mod
    _save_matrix(api_mod, [])

    with TestClient(app) as client:
        r = client.get("/pairs/does-not-exist")
        assert r.status_code == 404


def test_pairs_400_when_baseline_orphan(tmp_data_dir, reset_api_state):
    """Direct-routed row with no baseline_of pointer is not part of a pair."""
    import api as api_mod
    _save_matrix(api_mod, [
        _make_row("row-orphan", routing="direct"),
    ])

    with TestClient(app) as client:
        r = client.get("/pairs/row-orphan")
        assert r.status_code == 400
        assert "baseline_of" in r.json().get("detail", "")


def test_pairs_404_when_baseline_sibling_missing(tmp_data_dir, reset_api_state):
    """Governed row exists but no one has cloned it yet → 404."""
    import api as api_mod
    _save_matrix(api_mod, [
        _make_row("row-g", routing="via_agw", last_trial_id="t-g"),
    ])

    with TestClient(app) as client:
        r = client.get("/pairs/row-g")
        assert r.status_code == 404
        assert "baseline" in r.json().get("detail", "").lower()


def test_pairs_409_when_no_trial_history(tmp_data_dir, reset_api_state):
    """Pair exists but neither row has last_trial_id yet → 409."""
    import api as api_mod
    _save_matrix(api_mod, [
        _make_row("row-g", routing="via_agw"),
        _make_row("row-b", routing="direct", baseline_of="row-g"),
    ])

    with TestClient(app) as client:
        r = client.get("/pairs/row-g")
        assert r.status_code == 409
        assert "run" in r.json().get("detail", "").lower()


# ── happy path ─────────────────────────────────────────────────────────

def test_pairs_returns_both_trials_and_summary(tmp_data_dir, reset_api_state):
    """End-to-end: seed two rows + two trials, assert the envelope shape."""
    import api as api_mod

    # Governed trial: 1 user_msg turn, 2 audit entries, verdict a=pass
    g_turns = [Turn(
        turn_id="g-t0", turn_idx=0, kind="user_msg",
        started_at="2026-04-23T00:00:00+00:00",
        finished_at="2026-04-23T00:00:01+00:00",
    )]
    g_audit = [
        AuditEntry(trial_id="t-g", turn_id="g-t0", phase="llm_request",
                   cid="ib_aaa", backend="ollama", raw={}),
        AuditEntry(trial_id="t-g", turn_id="g-t0", phase="terminal",
                   cid="ib_aaa", backend="ollama", raw={}),
    ]
    g_verdicts = {"a": {"verdict": "pass", "reason": "ok"}}
    _save_trial(
        api_mod, trial_id="t-g", routing="via_agw",
        turns=g_turns, audit_entries=g_audit, verdicts=g_verdicts,
    )

    # Baseline trial: 1 user_msg turn, 0 audit entries
    b_turns = [Turn(
        turn_id="b-t0", turn_idx=0, kind="user_msg",
        started_at="2026-04-23T00:00:00+00:00",
        finished_at="2026-04-23T00:00:00.850000+00:00",
    )]
    _save_trial(
        api_mod, trial_id="t-b", routing="direct",
        turns=b_turns, audit_entries=[], verdicts={
            "a": {"verdict": "na", "reason": "baseline"},
        },
    )

    _save_matrix(api_mod, [
        _make_row("row-g", routing="via_agw", last_trial_id="t-g"),
        _make_row("row-b", routing="direct", baseline_of="row-g",
                  last_trial_id="t-b"),
    ])

    with TestClient(app) as client:
        r = client.get("/pairs/row-g")
        assert r.status_code == 200
        body = r.json()
        assert body["governed_row_id"] == "row-g"
        assert body["baseline_row_id"] == "row-b"
        # Full trial envelopes
        assert body["governed"]["trial_id"] == "t-g"
        assert body["baseline"]["trial_id"] == "t-b"
        # Diff summary
        s = body["diff_summary"]
        assert s["audit_entry_count"] == {"governed": 2, "baseline": 0}
        assert s["turn_count"] == {"governed": 1, "baseline": 1}
        # Governed took 1000ms, baseline took 850ms → +150ms overhead
        lo = s["latency_overhead_ms"]
        assert lo["n_turns"] == 1
        assert lo["median"] is not None
        assert abs(lo["median"] - 150.0) < 1.0
        # Classification: governed >0 audit + baseline 0 audit → expected entry
        assert any(
            "governed audit count" in d
            for d in s["classification"]["expected_diffs"]
        )
        assert s["classification"]["unexpected_diffs"] == []


def test_pairs_lookup_from_baseline_side_succeeds(tmp_data_dir, reset_api_state):
    """Passing the baseline row_id also resolves the pair (symmetry)."""
    import api as api_mod

    _save_trial(api_mod, trial_id="t-g", routing="via_agw")
    _save_trial(api_mod, trial_id="t-b", routing="direct")

    _save_matrix(api_mod, [
        _make_row("row-g", routing="via_agw", last_trial_id="t-g"),
        _make_row("row-b", routing="direct", baseline_of="row-g",
                  last_trial_id="t-b"),
    ])

    with TestClient(app) as client:
        r = client.get("/pairs/row-b")
        assert r.status_code == 200
        body = r.json()
        assert body["governed_row_id"] == "row-g"
        assert body["baseline_row_id"] == "row-b"


def test_pairs_diff_returns_path_scoped_diff(tmp_data_dir, reset_api_state):
    """?path=turns.0.response returns scoped governed+baseline values."""
    import api as api_mod

    g_turns = [Turn(
        turn_id="g-t0", turn_idx=0, kind="user_msg",
        response={"body": {"choices": [{"message": {"content": "hi"}}]}},
    )]
    b_turns = [Turn(
        turn_id="b-t0", turn_idx=0, kind="user_msg",
        response={"body": {"choices": [{"message": {"content": "hi (baseline)"}}]}},
    )]
    _save_trial(api_mod, trial_id="t-g", routing="via_agw", turns=g_turns)
    _save_trial(api_mod, trial_id="t-b", routing="direct", turns=b_turns)

    _save_matrix(api_mod, [
        _make_row("row-g", routing="via_agw", last_trial_id="t-g"),
        _make_row("row-b", routing="direct", baseline_of="row-g",
                  last_trial_id="t-b"),
    ])

    with TestClient(app) as client:
        r = client.get("/pairs/row-g/diff", params={"path": "turns.0.response"})
        assert r.status_code == 200
        body = r.json()
        assert body["path"] == "turns.0.response"
        # Governed response body has content "hi"; baseline has "hi (baseline)"
        assert body["governed"]["body"]["choices"][0]["message"]["content"] == "hi"
        assert body["baseline"]["body"]["choices"][0]["message"]["content"] == (
            "hi (baseline)"
        )
        # Values differ with no ib_ marker → unexpected_diff
        assert body["classification"] == "unexpected_diff"


def test_pairs_classification_marks_audit_leak_as_unexpected(
    tmp_data_dir, reset_api_state,
):
    """Baseline with non-empty audit entries → 'leaking through AGW' warning."""
    import api as api_mod

    _save_trial(
        api_mod, trial_id="t-g", routing="via_agw",
        audit_entries=[AuditEntry(
            trial_id="t-g", turn_id=None, phase="llm_request",
            cid="ib_abc", backend="ollama", raw={},
        )],
    )
    # Baseline with a stray audit entry — should trigger unexpected classification
    _save_trial(
        api_mod, trial_id="t-b", routing="direct",
        audit_entries=[AuditEntry(
            trial_id="t-b", turn_id=None, phase="llm_request",
            cid="ib_xyz", backend="ollama", raw={},
        )],
    )

    _save_matrix(api_mod, [
        _make_row("row-g", routing="via_agw", last_trial_id="t-g"),
        _make_row("row-b", routing="direct", baseline_of="row-g",
                  last_trial_id="t-b"),
    ])

    with TestClient(app) as client:
        r = client.get("/pairs/row-g")
        assert r.status_code == 200
        cls = r.json()["diff_summary"]["classification"]
        assert any(
            "leaking through AGW" in d for d in cls["unexpected_diffs"]
        )


def test_pairs_classification_marks_governance_marker_as_expected(
    tmp_data_dir, reset_api_state,
):
    """Governed body contains _ib_cid; baseline doesn't → expected_governance_marker."""
    from api import _classify_diff

    # Direct helper test — easier than round-tripping a full trial since the
    # classifier is a pure function.
    governed_body = {
        "choices": [{"message": {
            "content": "hi <!-- ib:cid=ib_abc123def456 -->",
        }}]
    }
    baseline_body = {
        "choices": [{"message": {"content": "hi"}}]
    }
    cls = _classify_diff(
        "turns.0.response.body", governed_body, baseline_body,
    )
    assert cls == "expected_governance_marker"

    # Also check identical values → noise
    assert _classify_diff("x", {"a": 1}, {"a": 1}) == "noise"
    # And plain unequal non-marker values → unexpected_diff
    assert _classify_diff("x", {"a": 1}, {"a": 2}) == "unexpected_diff"
