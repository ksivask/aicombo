"""Drive a trial's turn plan through the adapter; capture audit entries."""
from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timezone
from typing import Callable

from trials import Trial, TrialStore, Turn, AuditEntry
from efficacy import compute_verdicts


async def run_trial(
    trial_id: str,
    store: TrialStore,
    adapter_client,
    audit_entries_provider: Callable[[], list[AuditEntry]],
) -> None:
    """Execute a trial's turn plan end to end.

    adapter_client must expose: create_trial, drive_turn, delete_trial (async).
    audit_entries_provider returns the list of audit entries captured
    so far (used at verdict time; for production wiring, it's audit_tail's
    subscriber-side buffer).
    """
    trial = store.load(trial_id)
    trial.started_at = datetime.now(timezone.utc).isoformat()
    trial.status = "running"
    store.save(trial)

    try:
        await adapter_client.create_trial(trial_id=trial_id, config=trial.config)

        for idx, turn_spec in enumerate(trial.turn_plan.turns):
            kind = turn_spec.get("kind", "user_msg")
            turn_id = f"turn-{idx:03d}-{uuid.uuid4().hex[:8]}"
            turn = Turn(
                turn_id=turn_id, turn_idx=idx, kind=kind,
                started_at=datetime.now(timezone.utc).isoformat(),
            )

            if kind == "user_msg":
                resp = await adapter_client.drive_turn(
                    trial_id=trial_id,
                    turn_id=turn_id,
                    user_msg=turn_spec.get("content", ""),
                )
                # Adapter may echo back a canonical turn_id — prefer that so
                # audit entries (which use the adapter-side id) align.
                if resp.get("turn_id"):
                    turn.turn_id = resp["turn_id"]
                turn.request = resp.get("request_captured", {})
                turn.response = resp.get("response_captured", {})
                turn.framework_events = resp.get("framework_events", [])
            else:
                # Plan A supports only user_msg; others are documented in design as Plan B.
                turn.error = {"reason": f"turn kind {kind!r} not implemented in Plan A"}

            turn.finished_at = datetime.now(timezone.utc).isoformat()
            store.append_turn(trial_id, turn)

        # Grace period for audit log stragglers
        await asyncio.sleep(0.3)

        # Pull audit entries collected by audit_tail (via provider)
        audits = audit_entries_provider()
        for a in audits:
            store.append_audit(trial_id, a)

        trial = store.load(trial_id)
        verdicts_out = compute_verdicts(trial)

        # Persist verdicts (convert Verdict dataclass → plain dict)
        trial.verdicts = {k: {"verdict": v.verdict, "reason": v.reason}
                          for k, v in verdicts_out.items()}

        any_fail = any(v.verdict == "fail" for v in verdicts_out.values())
        any_error = any(v.verdict == "error" for v in verdicts_out.values())
        trial.status = (
            "error" if any_error
            else "fail" if any_fail
            else "pass"
        )
        trial.finished_at = datetime.now(timezone.utc).isoformat()
        store.save(trial)

    except Exception as e:
        trial = store.load(trial_id)
        trial.status = "error"
        trial.error_reason = str(e)
        trial.finished_at = datetime.now(timezone.utc).isoformat()
        store.save(trial)
    finally:
        try:
            await adapter_client.delete_trial(trial_id=trial_id)
        except Exception:
            pass
