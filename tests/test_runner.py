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
async def test_runner_drives_force_state_ref_turn(tmp_data_dir):
    """Plan B T11 — runner resolves target_response_id from prior user_msg
    turn's envelope `_response_id`, then drives the force_state_ref turn
    through the adapter with the right body fields.
    """
    cfg = TrialConfig(
        framework="autogen", api="responses", stream=False, state=True,
        llm="chatgpt", mcp="NONE", routing="via_agw",
    )
    plan = TurnPlan(turns=[
        {"kind": "user_msg", "content": "hello"},
        {"kind": "user_msg", "content": "and more?"},
        {"kind": "force_state_ref", "lookback": 2, "content": "refer back"},
    ])
    trial = Trial(trial_id="trial-fsr", config=cfg, turn_plan=plan, status="running")
    store = TrialStore(tmp_data_dir / "trials")
    store.save(trial)

    adapter = MagicMock()
    adapter.create_trial = AsyncMock(return_value={"ok": True})
    # First 2 user_msg turns: each returns a distinct _response_id. Third
    # turn (force_state_ref) gets a fresh id. We record the drive_turn
    # kwargs to assert target_response_id is the first-turn id.
    responses = [
        {
            "turn_id": "t-user-0",
            "assistant_msg": "hi",
            "tool_calls": [],
            "request_captured": {"body": {}},
            "response_captured": {"status": 200, "body": {"id": "resp_001"}},
            "_response_id": "resp_001",
        },
        {
            "turn_id": "t-user-1",
            "assistant_msg": "yes",
            "tool_calls": [],
            "request_captured": {"body": {}},
            "response_captured": {"status": 200, "body": {"id": "resp_002"}},
            "_response_id": "resp_002",
        },
        {
            "turn_id": "t-fsr",
            "assistant_msg": "sure",
            "tool_calls": [],
            "request_captured": {"body": {}},
            "response_captured": {"status": 200, "body": {"id": "resp_003"}},
            "_response_id": "resp_003",
        },
    ]
    adapter.drive_turn = AsyncMock(side_effect=responses)
    adapter.delete_trial = AsyncMock(return_value={"ok": True})

    await run_trial(
        trial_id="trial-fsr",
        store=store,
        adapter_client=adapter,
        audit_entries_provider=lambda: [],
    )

    # Verify the force_state_ref call received target_response_id=resp_001
    # (lookback=2 → 2 user_msg turns back from end-of-user-list = idx 0).
    all_calls = adapter.drive_turn.call_args_list
    assert len(all_calls) == 3, all_calls
    fsr_call_kwargs = all_calls[-1].kwargs
    assert fsr_call_kwargs.get("turn_kind") == "force_state_ref"
    assert fsr_call_kwargs.get("target_response_id") == "resp_001"

    # Trial should have 3 turns persisted with the fsr turn not erroring.
    loaded = store.load("trial-fsr")
    assert len(loaded.turns) == 3
    fsr_turn = loaded.turns[2]
    assert fsr_turn.kind == "force_state_ref"
    assert fsr_turn.error is None, fsr_turn.error
    assert fsr_turn.request.get("target_response_id") == "resp_001"


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
