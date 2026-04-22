"""Tests for harness/runner.py — turn plan executor using mock adapter."""
import pytest
from unittest.mock import AsyncMock, MagicMock

from trials import Trial, TrialConfig, TurnPlan, AuditEntry, TrialStore
from runner import run_trial


@pytest.mark.asyncio
async def test_runner_executes_single_user_msg_turn(tmp_data_dir):
    """One user_msg turn: runner calls adapter.turn, saves result."""
    cfg = TrialConfig(
        framework="langchain", api="chat", stream=False, state=False,
        llm="ollama", mcp="NONE", routing="via_agw",
    )
    plan = TurnPlan(turns=[{"kind": "user_msg", "content": "hello"}])
    trial = Trial(trial_id="trial-x", config=cfg, turn_plan=plan, status="running")
    store = TrialStore(tmp_data_dir / "trials")
    store.save(trial)

    # Mock adapter client
    adapter = MagicMock()
    adapter.create_trial = AsyncMock(return_value={"ok": True})
    adapter.drive_turn = AsyncMock(return_value={
        "turn_id": "turn-0",
        "assistant_msg": "hi!",
        "tool_calls": [],
        "request_captured": {"body": {}},
        "response_captured": {
            "status": 200,
            "body": {"choices": [{"message": {"content": "hi!<!-- ib:cid=ib_abc123def456 -->"}}]},
        },
    })
    adapter.delete_trial = AsyncMock(return_value={"ok": True})

    # Mock audit tail — simulate one audit entry
    audit_entries = [
        AuditEntry(trial_id="trial-x", turn_id="turn-0", phase="terminal",
                   cid="ib_abc123def456", backend="ollama", raw={}),
    ]

    await run_trial(
        trial_id="trial-x",
        store=store,
        adapter_client=adapter,
        audit_entries_provider=lambda: audit_entries,
    )

    loaded = store.load("trial-x")
    assert loaded.status == "pass"
    assert len(loaded.turns) == 1
    assert loaded.verdicts["a"]["verdict"] == "pass"
    assert loaded.verdicts["b"]["verdict"] == "pass"
    adapter.create_trial.assert_called_once()
    adapter.drive_turn.assert_called_once()
    adapter.delete_trial.assert_called_once()


@pytest.mark.asyncio
async def test_runner_handles_adapter_error(tmp_data_dir):
    """Adapter raises → trial marked error."""
    cfg = TrialConfig(
        framework="langchain", api="chat", stream=False, state=False,
        llm="ollama", mcp="NONE", routing="via_agw",
    )
    plan = TurnPlan(turns=[{"kind": "user_msg", "content": "hello"}])
    trial = Trial(trial_id="t-err", config=cfg, turn_plan=plan, status="running")
    store = TrialStore(tmp_data_dir / "trials")
    store.save(trial)

    adapter = MagicMock()
    adapter.create_trial = AsyncMock(return_value={"ok": True})
    adapter.drive_turn = AsyncMock(side_effect=RuntimeError("adapter crashed"))
    adapter.delete_trial = AsyncMock(return_value={"ok": True})

    await run_trial(
        trial_id="t-err",
        store=store,
        adapter_client=adapter,
        audit_entries_provider=lambda: [],
    )

    loaded = store.load("t-err")
    assert loaded.status == "error"
    assert "adapter crashed" in (loaded.error_reason or "")
