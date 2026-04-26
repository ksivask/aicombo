"""Tests for harness/runner.py — turn plan executor using mock adapter."""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

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
async def test_runner_honors_abort_event(tmp_data_dir):
    """Plan B T14 — when abort_event is set before the loop starts, runner
    skips every turn, transitions trial.status to 'aborted', and stamps
    verdicts with an _aborted marker.
    """
    import asyncio

    cfg = TrialConfig(
        framework="langchain", api="chat", stream=False, state=False,
        llm="ollama", mcp="NONE", routing="via_agw",
    )
    plan = TurnPlan(turns=[
        {"kind": "user_msg", "content": f"hi {i}"} for i in range(5)
    ])
    trial = Trial(
        trial_id="trial-abort-pre", config=cfg, turn_plan=plan, status="running",
    )
    store = TrialStore(tmp_data_dir / "trials")
    store.save(trial)

    # Adapter: create_trial / delete_trial must still work (abort happens
    # AFTER create_trial); drive_turn must NEVER be called because abort
    # is already set at the top of the first iteration.
    adapter = MagicMock()
    adapter.create_trial = AsyncMock(return_value={"ok": True})
    adapter.drive_turn = AsyncMock(
        side_effect=AssertionError("adapter.drive_turn called after abort"),
    )
    adapter.delete_trial = AsyncMock(return_value={"ok": True})

    ev = asyncio.Event()
    ev.set()

    await run_trial(
        trial_id="trial-abort-pre",
        store=store,
        adapter_client=adapter,
        audit_entries_provider=lambda: [],
        abort_event=ev,
    )

    loaded = store.load("trial-abort-pre")
    assert loaded.status == "aborted"
    assert loaded.verdicts.get("_aborted", {}).get("verdict") == "aborted"
    assert "before turn 0" in loaded.verdicts["_aborted"]["reason"]
    # drive_turn was NEVER called
    adapter.drive_turn.assert_not_called()
    # create_trial DID run (we abort between turns, not before the adapter
    # session is established) and delete_trial ran via the finally block.
    adapter.create_trial.assert_called_once()
    adapter.delete_trial.assert_called_once()


@pytest.mark.asyncio
async def test_runner_abort_mid_plan_preserves_completed_turns(tmp_data_dir):
    """Plan B T14 — abort set AFTER the first turn completes: turn 0 is
    persisted, turn 1 is skipped (never reached drive_turn), trial is
    aborted. Proves 'every persisted turn is a complete turn' invariant.
    """
    import asyncio

    cfg = TrialConfig(
        framework="langchain", api="chat", stream=False, state=False,
        llm="ollama", mcp="NONE", routing="via_agw",
    )
    plan = TurnPlan(turns=[
        {"kind": "user_msg", "content": "first"},
        {"kind": "user_msg", "content": "second"},
        {"kind": "user_msg", "content": "third"},
    ])
    trial = Trial(
        trial_id="trial-abort-mid", config=cfg, turn_plan=plan, status="running",
    )
    store = TrialStore(tmp_data_dir / "trials")
    store.save(trial)

    ev = asyncio.Event()

    # First drive_turn call: legit response, set the abort event
    # so the SECOND iteration trips the check at loop top.
    async def _first_then_abort(**kw):
        ev.set()
        return {
            "turn_id": "turn-0",
            "assistant_msg": "hi!",
            "tool_calls": [],
            "request_captured": {"body": {}},
            "response_captured": {
                "status": 200,
                "body": {"choices": [{"message": {"content": "hi!"}}]},
            },
        }

    adapter = MagicMock()
    adapter.create_trial = AsyncMock(return_value={"ok": True})
    adapter.drive_turn = AsyncMock(side_effect=_first_then_abort)
    adapter.delete_trial = AsyncMock(return_value={"ok": True})

    await run_trial(
        trial_id="trial-abort-mid",
        store=store,
        adapter_client=adapter,
        audit_entries_provider=lambda: [],
        abort_event=ev,
    )

    loaded = store.load("trial-abort-mid")
    assert loaded.status == "aborted"
    # Exactly one turn persisted
    assert len(loaded.turns) == 1, [t.turn_id for t in loaded.turns]
    # drive_turn called exactly once (never retried after abort)
    assert adapter.drive_turn.call_count == 1
    # _aborted reason names the turn we stopped BEFORE
    assert "before turn 1" in loaded.verdicts["_aborted"]["reason"]


@pytest.mark.asyncio
async def test_runner_records_user_msg_turn_error_on_adapter_failure(tmp_data_dir):
    """Adapter raises during user_msg → turn record preserved with turn.error.

    Regression: previously the user_msg branch had no try/except, so a
    mid-trial 5xx propagated out of the loop and only set trial-level
    error_reason — the turn record (with whatever framework_events had
    been captured before the failure) was silently discarded. Fix wraps
    the adapter call so the turn lands with .error populated and the
    loop continues (consistent with compact + force_state_ref branches).
    """
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
    # Turn record IS preserved with the error reason captured.
    assert len(loaded.turns) == 1
    assert loaded.turns[0].error is not None
    assert "adapter crashed" in loaded.turns[0].error.get("reason", "")
    # No trial-level error_reason — this isn't a runner-internal crash.
    # (Old behavior set this to "adapter crashed" via the outer except —
    # masked the turn record being silently dropped.)
    assert not loaded.error_reason
    # Verdicts ran (loop completed) — proves we didn't bail early.
    # Verdict (a) likely 'error' because audit is empty, but the
    # important thing is that compute_verdicts ran AT ALL.
    assert loaded.verdicts, "verdicts never computed → loop bailed early"


@pytest.mark.asyncio
async def test_runner_continues_subsequent_turns_after_user_msg_failure(tmp_data_dir):
    """Multi-turn plan: turn 0 fails, turn 1 also attempted (and recorded).

    Mirrors the compact + force_state_ref behavior — failure on one turn
    doesn't abort the loop. User sees the failure pattern (every turn
    failing the same way) instead of one failed turn + a black hole.
    """
    cfg = TrialConfig(
        framework="langchain", api="chat", stream=False, state=False,
        llm="ollama", mcp="NONE", routing="via_agw",
    )
    plan = TurnPlan(turns=[
        {"kind": "user_msg", "content": "first"},
        {"kind": "user_msg", "content": "second"},
        {"kind": "user_msg", "content": "third"},
    ])
    trial = Trial(trial_id="t-multi-err", config=cfg, turn_plan=plan, status="running")
    store = TrialStore(tmp_data_dir / "trials")
    store.save(trial)

    adapter = MagicMock()
    adapter.create_trial = AsyncMock(return_value={"ok": True})
    adapter.drive_turn = AsyncMock(side_effect=RuntimeError("upstream 503"))
    adapter.delete_trial = AsyncMock(return_value={"ok": True})

    await run_trial(
        trial_id="t-multi-err",
        store=store,
        adapter_client=adapter,
        audit_entries_provider=lambda: [],
    )

    loaded = store.load("t-multi-err")
    # All three turns attempted and recorded with errors.
    assert len(loaded.turns) == 3
    for i, t in enumerate(loaded.turns):
        assert t.error is not None, f"turn {i} should have error captured"
        assert "upstream 503" in t.error.get("reason", "")


# ── E22 — mcp_admin turn kind ──

@pytest.mark.asyncio
async def test_mcp_admin_set_tools_dispatches_to_admin_endpoint(
    tmp_data_dir, monkeypatch,
):
    """mcp_admin POSTs to <AGW_MCP_<NAME>>/_admin/<op> with the payload.

    Mocks httpx.AsyncClient at runner-import time so no network is
    touched. Verifies the URL composition (base + /_admin/op), the JSON
    body, and that the run completes cleanly.
    """
    cfg = TrialConfig(
        framework="langchain", api="chat", stream=False, state=False,
        llm="ollama", mcp="mutable", routing="via_agw",
    )
    plan = TurnPlan(turns=[
        {
            "kind": "mcp_admin",
            "mcp": "mutable",
            "op": "set_tools",
            "payload": {"tools": [{"name": "only_one", "description": "x"}]},
        },
    ])
    trial = Trial(
        trial_id="trial-mcpadmin", config=cfg, turn_plan=plan, status="running",
    )
    store = TrialStore(tmp_data_dir / "trials")
    store.save(trial)

    monkeypatch.setenv(
        "AGW_MCP_MUTABLE", "http://agentgateway:8080/mcp/mutable",
    )

    adapter = MagicMock()
    adapter.create_trial = AsyncMock(return_value={"ok": True})
    adapter.delete_trial = AsyncMock(return_value={"ok": True})
    # No drive_turn calls expected (mcp_admin is harness-direct).
    adapter.drive_turn = AsyncMock(
        side_effect=AssertionError("adapter.drive_turn must NOT be called"),
    )

    # Mock the httpx.AsyncClient class as imported into runner module.
    fake_resp = MagicMock(status_code=200)
    fake_resp.json.return_value = {"ok": True, "version_counter": 1}
    fake_client = MagicMock()
    fake_client.post = AsyncMock(return_value=fake_resp)
    fake_async_ctx = MagicMock()
    fake_async_ctx.__aenter__ = AsyncMock(return_value=fake_client)
    fake_async_ctx.__aexit__ = AsyncMock(return_value=False)

    with patch("runner.httpx.AsyncClient", return_value=fake_async_ctx):
        await run_trial(
            trial_id="trial-mcpadmin",
            store=store,
            adapter_client=adapter,
            audit_entries_provider=lambda: [],
        )

    # Verify the POST: URL composed from AGW_MCP_MUTABLE + /_admin/set_tools,
    # JSON body is the turn's payload.
    fake_client.post.assert_called_once()
    call_args = fake_client.post.call_args
    url = call_args.args[0] if call_args.args else call_args.kwargs.get("url")
    assert url == (
        "http://agentgateway:8080/mcp/mutable/_admin/set_tools"
    ), url
    posted_json = call_args.kwargs.get("json")
    assert posted_json == {
        "tools": [{"name": "only_one", "description": "x"}],
    }

    loaded = store.load("trial-mcpadmin")
    assert len(loaded.turns) == 1
    turn = loaded.turns[0]
    assert turn.kind == "mcp_admin"
    assert turn.error is None, turn.error
    assert turn.request["op"] == "set_tools"
    assert turn.request["mcp"] == "mutable"
    assert turn.response["status"] == 200


@pytest.mark.asyncio
async def test_mcp_admin_against_non_mutable_mcp_is_no_op(
    tmp_data_dir, monkeypatch,
):
    """mcp_admin against an MCP whose admin endpoint 404s logs + skips.

    Non-mutable MCPs (weather/news/library/fetch) don't expose /_admin/*.
    The harness should treat 404 as "skipped" rather than as a
    trial-failing error so trial scripts can use mcp_admin defensively.
    """
    cfg = TrialConfig(
        framework="langchain", api="chat", stream=False, state=False,
        llm="ollama", mcp="weather", routing="via_agw",
    )
    plan = TurnPlan(turns=[
        {
            "kind": "mcp_admin",
            "mcp": "weather",
            "op": "set_tools",
            "payload": {"tools": []},
        },
    ])
    trial = Trial(
        trial_id="trial-mcpadmin-noop", config=cfg, turn_plan=plan,
        status="running",
    )
    store = TrialStore(tmp_data_dir / "trials")
    store.save(trial)

    monkeypatch.setenv(
        "AGW_MCP_WEATHER", "http://agentgateway:8080/mcp/weather",
    )

    adapter = MagicMock()
    adapter.create_trial = AsyncMock(return_value={"ok": True})
    adapter.delete_trial = AsyncMock(return_value={"ok": True})
    adapter.drive_turn = AsyncMock()

    # Mock httpx to return 404 on the admin POST (no /_admin endpoint
    # on the weather MCP).
    fake_resp = MagicMock(status_code=404)
    fake_resp.json.side_effect = ValueError("not json")
    fake_resp.text = "Not Found"
    fake_client = MagicMock()
    fake_client.post = AsyncMock(return_value=fake_resp)
    fake_async_ctx = MagicMock()
    fake_async_ctx.__aenter__ = AsyncMock(return_value=fake_client)
    fake_async_ctx.__aexit__ = AsyncMock(return_value=False)

    with patch("runner.httpx.AsyncClient", return_value=fake_async_ctx):
        await run_trial(
            trial_id="trial-mcpadmin-noop",
            store=store,
            adapter_client=adapter,
            audit_entries_provider=lambda: [],
        )

    loaded = store.load("trial-mcpadmin-noop")
    turn = loaded.turns[0]
    # 404 → skipped sentinel; turn is NOT marked as an error so the
    # trial doesn't fail just because the chosen MCP lacks _admin.
    assert turn.error is None, turn.error
    assert turn.response.get("skipped") is True
    assert turn.response.get("reason") == "admin_not_found"
    assert turn.response.get("status") == 404


@pytest.mark.asyncio
async def test_mcp_admin_with_no_base_url_skips_with_log(
    tmp_data_dir, monkeypatch,
):
    """mcp_admin against an MCP with no AGW_MCP_<NAME> env logs + skips.

    Defensive path for trial scripts that reference MCPs the harness
    isn't configured for. Distinct from the 404 path (the env var is
    missing entirely) so the audit trail can distinguish "no URL" from
    "URL but no admin endpoint".
    """
    cfg = TrialConfig(
        framework="langchain", api="chat", stream=False, state=False,
        llm="ollama", mcp="library", routing="via_agw",
    )
    plan = TurnPlan(turns=[
        {
            "kind": "mcp_admin", "mcp": "library", "op": "set_tools",
            "payload": {},
        },
    ])
    trial = Trial(
        trial_id="trial-mcpadmin-nourl", config=cfg, turn_plan=plan,
        status="running",
    )
    store = TrialStore(tmp_data_dir / "trials")
    store.save(trial)

    # Make sure neither env var is set so _pick_mcp_base_url returns None.
    monkeypatch.delenv("AGW_MCP_LIBRARY", raising=False)
    monkeypatch.delenv("DIRECT_MCP_LIBRARY", raising=False)

    adapter = MagicMock()
    adapter.create_trial = AsyncMock(return_value={"ok": True})
    adapter.delete_trial = AsyncMock(return_value={"ok": True})
    adapter.drive_turn = AsyncMock()

    await run_trial(
        trial_id="trial-mcpadmin-nourl",
        store=store,
        adapter_client=adapter,
        audit_entries_provider=lambda: [],
    )

    loaded = store.load("trial-mcpadmin-nourl")
    turn = loaded.turns[0]
    assert turn.error is None, turn.error
    assert turn.response.get("skipped") is True
    assert turn.response.get("reason") == "no_base_url"
