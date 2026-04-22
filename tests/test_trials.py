"""Tests for harness/trials.py — Trial/Turn dataclasses + JSON persistence."""
from pathlib import Path

import pytest

from trials import Trial, Turn, AuditEntry, TrialStore, TrialConfig, TurnPlan


def test_trial_round_trip(tmp_data_dir: Path):
    """Create a Trial, save, load — fields preserved."""
    cfg = TrialConfig(
        framework="langchain", api="chat",
        stream=False, state=False,
        llm="ollama", mcp="weather",
        routing="via_agw",
    )
    plan = TurnPlan(turns=[{"kind": "user_msg", "content": "hi"}])
    trial = Trial(
        trial_id="test-trial-001",
        config=cfg,
        turn_plan=plan,
        status="running",
    )
    store = TrialStore(tmp_data_dir / "trials")
    store.save(trial)

    loaded = store.load("test-trial-001")
    assert loaded.trial_id == "test-trial-001"
    assert loaded.config.framework == "langchain"
    assert loaded.status == "running"
    assert loaded.turn_plan.turns[0]["content"] == "hi"


def test_trial_append_turn(tmp_data_dir: Path):
    """Appending a turn mutates + persists without clobber."""
    cfg = TrialConfig(
        framework="langchain", api="chat", stream=False, state=False,
        llm="ollama", mcp="NONE", routing="via_agw",
    )
    trial = Trial(trial_id="t2", config=cfg, turn_plan=TurnPlan(turns=[]))
    store = TrialStore(tmp_data_dir / "trials")
    store.save(trial)

    turn = Turn(turn_id="turn-001", turn_idx=0, kind="user_msg",
                request={"body": {"x": 1}}, response={"body": {"y": 2}})
    store.append_turn("t2", turn)

    loaded = store.load("t2")
    assert len(loaded.turns) == 1
    assert loaded.turns[0].turn_id == "turn-001"
    assert loaded.turns[0].request["body"]["x"] == 1


def test_trial_append_audit(tmp_data_dir: Path):
    """Audit entries accumulate per trial."""
    cfg = TrialConfig(
        framework="langchain", api="chat", stream=False, state=False,
        llm="ollama", mcp="NONE", routing="via_agw",
    )
    trial = Trial(trial_id="t3", config=cfg, turn_plan=TurnPlan(turns=[]))
    store = TrialStore(tmp_data_dir / "trials")
    store.save(trial)

    entry = AuditEntry(
        trial_id="t3", turn_id="turn-001",
        phase="llm_request", cid="ib_abc123def456",
        backend="ollama", raw={"body": {}},
    )
    store.append_audit("t3", entry)

    loaded = store.load("t3")
    assert len(loaded.audit_entries) == 1
    assert loaded.audit_entries[0].cid == "ib_abc123def456"


def test_trial_list(tmp_data_dir: Path):
    """List all trials; sorts by created_at desc."""
    store = TrialStore(tmp_data_dir / "trials")
    cfg = TrialConfig(
        framework="langchain", api="chat", stream=False, state=False,
        llm="ollama", mcp="NONE", routing="via_agw",
    )
    store.save(Trial(trial_id="older", config=cfg, turn_plan=TurnPlan(turns=[])))
    store.save(Trial(trial_id="newer", config=cfg, turn_plan=TurnPlan(turns=[])))

    all_trials = store.list_all()
    assert len(all_trials) == 2
    ids = {t.trial_id for t in all_trials}
    assert ids == {"older", "newer"}


def test_trial_load_missing(tmp_data_dir: Path):
    """Loading a missing trial raises FileNotFoundError."""
    store = TrialStore(tmp_data_dir / "trials")
    with pytest.raises(FileNotFoundError):
        store.load("nonexistent")
