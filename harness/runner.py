"""Drive a trial's turn plan through the adapter; capture audit entries."""
from __future__ import annotations

import asyncio
import logging
import os
import uuid
from datetime import datetime, timezone
from typing import Callable

import httpx

from trials import Trial, TrialStore, Turn, AuditEntry
from efficacy import compute_verdicts


_log = logging.getLogger(__name__)


def _pick_mcp_base_url(mcp_name: str, routing: str = "via_agw") -> str | None:
    """Resolve an MCP base URL for harness-direct calls (E22 mcp_admin).

    Mirrors the AGW_MCP_<NAME> / DIRECT_MCP_<NAME> env-var convention
    that adapters already use. Returns None if neither var is set —
    callers should treat that as "skip this turn".

    routing="via_agw"  → AGW_MCP_<NAME> (governed path, but admin route
                         in agw/config.yaml has policies: {} so the
                         admin sub-path bypasses cidgar).
    routing="direct"   → DIRECT_MCP_<NAME> (skip AGW entirely).
    """
    name = (mcp_name or "").upper()
    if not name:
        return None
    if routing == "direct":
        return os.environ.get(f"DIRECT_MCP_{name}")
    return (
        os.environ.get(f"AGW_MCP_{name}")
        or os.environ.get(f"DIRECT_MCP_{name}")
    )


async def run_trial(
    trial_id: str,
    store: TrialStore,
    adapter_client,
    audit_entries_provider: Callable[[], list[AuditEntry]],
    abort_event: "asyncio.Event | None" = None,
) -> None:
    """Execute a trial's turn plan end to end.

    adapter_client must expose: create_trial, drive_turn, delete_trial (async).
    audit_entries_provider returns the list of audit entries captured
    so far (used at verdict time; for production wiring, it's audit_tail's
    subscriber-side buffer).

    T14 — abort_event, when set, causes the runner to stop iterating the
    turn plan at the TOP of the next loop iteration. The currently-
    executing turn finishes naturally so no HTTP-mid-call corruption /
    framework state poisoning occurs. The trial transitions to
    status=aborted; verdicts are computed best-effort on whatever turns
    completed and stamped with an `_aborted` marker so the UI can render
    "aborted before turn N" alongside the partial verdicts.
    """
    trial = store.load(trial_id)
    trial.started_at = datetime.now(timezone.utc).isoformat()
    trial.status = "running"
    store.save(trial)

    try:
        await adapter_client.create_trial(trial_id=trial_id, config=trial.config)

        for idx, turn_spec in enumerate(trial.turn_plan.turns):
            # T14 — cooperative abort check at the top of every turn. We
            # intentionally skip checking mid-turn: abandoning an HTTP call
            # in flight would leave the framework adapter in an undefined
            # state. The invariant "every persisted turn is a complete turn"
            # matters for verdict computation.
            if abort_event is not None and abort_event.is_set():
                trial = store.load(trial_id)
                trial.status = "aborted"
                trial.finished_at = datetime.now(timezone.utc).isoformat()
                # Best-effort partial verdicts — if compute_verdicts raises
                # mid-abort we still persist status=aborted.
                try:
                    partial = compute_verdicts(trial)
                    trial.verdicts = {
                        k: {"verdict": v.verdict, "reason": v.reason}
                        for k, v in partial.items()
                    }
                except Exception as ve:
                    trial.verdicts = trial.verdicts or {}
                    trial.verdicts["_verdict_error"] = {
                        "verdict": "error",
                        "reason": f"compute_verdicts raised during abort: {ve}",
                    }
                trial.verdicts["_aborted"] = {
                    "verdict": "aborted",
                    "reason": (
                        f"aborted before turn {idx} of "
                        f"{len(trial.turn_plan.turns)}"
                    ),
                }
                store.save(trial)
                return
            kind = turn_spec.get("kind", "user_msg")
            turn_id = f"turn-{idx:03d}-{uuid.uuid4().hex[:8]}"
            turn = Turn(
                turn_id=turn_id, turn_idx=idx, kind=kind,
                started_at=datetime.now(timezone.utc).isoformat(),
            )

            if kind == "user_msg":
                # Wrap the adapter call so a mid-trial 5xx persists as
                # turn.error instead of propagating out of the for-loop and
                # silently discarding the turn record + the rest of the
                # plan's turns + the audit/verdict tail. Mirrors the
                # try/except already used by the compact + force_state_ref
                # branches below.
                try:
                    resp = await adapter_client.drive_turn(
                        trial_id=trial_id,
                        turn_id=turn_id,
                        user_msg=turn_spec.get("content", ""),
                    )
                    # Adapter may echo back a canonical turn_id — prefer that
                    # so audit entries (which use the adapter-side id) align.
                    if resp.get("turn_id"):
                        turn.turn_id = resp["turn_id"]
                    turn.request = resp.get("request_captured", {})
                    turn.response = resp.get("response_captured", {}) or {}
                    # T11 — also surface the Responses-API response id at the
                    # turn.response level so a subsequent force_state_ref
                    # turn can lookup a target_response_id from this turn.
                    if resp.get("_response_id"):
                        turn.response["_response_id"] = resp["_response_id"]
                    turn.framework_events = resp.get("framework_events", [])
                except Exception as e:
                    turn.error = {"reason": f"user_msg turn failed: {e}"}
            elif kind == "compact":
                # Plan B T10 — ask the adapter to mutate its internal history
                # per the requested strategy. No LLM call; no audit entry
                # expected. Verdict (d) reads this turn's position in the
                # turn list to bracket pre/post CID windows.
                strategy = turn_spec.get("strategy", "drop_half")
                try:
                    compact_resp = await adapter_client.compact(
                        trial_id=trial_id, strategy=strategy,
                    )
                    turn.request = {"strategy": strategy}
                    turn.response = {"body": compact_resp}
                except Exception as e:
                    turn.error = {"reason": f"compact failed: {e}"}
            elif kind == "force_state_ref":
                # Plan B T11 — force a Responses-API state-mode jump by
                # telling the adapter to use previous_response_id from an
                # EARLIER turn (N-lookback) instead of the immediate prior.
                # Verdict (e) then reads whether the CID survived that jump.
                lookback = int(turn_spec.get("lookback", 2))
                text = turn_spec.get(
                    "content", turn_spec.get("text", "What did we discuss earlier?"),
                )
                # Find a completed user_msg turn at N-lookback that has a
                # captured _response_id (set by the supporting adapters'
                # _build_turn_response).
                completed_user = [
                    t for t in trial.turns
                    if t.kind == "user_msg" and (t.response or {}).get("_response_id")
                ]
                target_response_id = None
                if len(completed_user) >= lookback:
                    target_response_id = completed_user[-lookback].response.get(
                        "_response_id",
                    )
                elif completed_user:
                    # Fallback: use the oldest available id (still exercises a
                    # non-immediate-prior reference even if the plan was too
                    # short for the requested lookback).
                    target_response_id = completed_user[0].response.get(
                        "_response_id",
                    )
                if not target_response_id:
                    turn.error = {
                        "reason": (
                            f"force_state_ref: no prior user_msg turn carries "
                            f"_response_id (lookback={lookback}); supporting "
                            f"adapters are autogen + llamaindex on "
                            f"api=responses with state=True"
                        ),
                    }
                else:
                    try:
                        turn_resp = await adapter_client.drive_turn(
                            trial_id=trial_id,
                            turn_id=turn_id,
                            user_msg=text,
                            turn_kind="force_state_ref",
                            target_response_id=target_response_id,
                        )
                        if turn_resp.get("turn_id"):
                            turn.turn_id = turn_resp["turn_id"]
                        turn.request = {
                            "user_msg": text,
                            "turn_kind": "force_state_ref",
                            "target_response_id": target_response_id,
                            "lookback": lookback,
                        }
                        # Preserve the same shape as user_msg turns so verdict
                        # (e) can use the existing time-window helpers.
                        turn.response = turn_resp.get("response_captured", {}) or {}
                        # Also keep the top-level envelope's _response_id so
                        # chained force_state_ref turns can target this one.
                        if turn_resp.get("_response_id"):
                            turn.response["_response_id"] = turn_resp["_response_id"]
                        turn.framework_events = turn_resp.get("framework_events", [])
                    except Exception as e:
                        turn.error = {
                            "reason": f"force_state_ref turn failed: {e}",
                        }
            elif kind == "mcp_admin":
                # E22 — harness-direct admin call against an MCP server's
                # /_admin/* endpoint. Adapter-naive: the agent never sees
                # this happen, which is the WHOLE POINT for E20's
                # snapshot-correlation tests (mutation drives the agent
                # to discover a new tools/list on the next fetch).
                #
                # Resolution order for the target MCP name:
                #   turn_spec.mcp  >  trial.config.mcp
                # Resolution order for the op + payload: turn_spec only
                # (these are mutation parameters, not trial config).
                mcp_name = turn_spec.get("mcp") or trial.config.mcp
                op = turn_spec.get("op")
                payload = turn_spec.get("payload") or {}
                base = _pick_mcp_base_url(
                    mcp_name, routing=trial.config.routing,
                )
                turn.request = {
                    "mcp": mcp_name,
                    "op": op,
                    "payload": payload,
                }
                if not op:
                    turn.error = {
                        "reason": "mcp_admin: missing required field 'op'",
                    }
                elif base is None:
                    # No env var configured for this MCP — likely a
                    # non-mutable MCP that has no admin endpoint at all.
                    # Log + skip rather than fail the trial.
                    _log.info(
                        "mcp_admin called for mcp=%s which has no MCP base "
                        "URL env var set; skipped (turn=%s)",
                        mcp_name, turn_id,
                    )
                    turn.response = {"skipped": True, "reason": "no_base_url"}
                else:
                    url = f"{base}/_admin/{op}"
                    try:
                        async with httpx.AsyncClient(timeout=5.0) as c:
                            resp = await c.post(url, json=payload)
                        body: dict | str
                        try:
                            body = resp.json()
                        except Exception:
                            body = resp.text
                        # 404 on /_admin/* = MCP doesn't expose the admin
                        # surface (e.g. weather/news). Treat as a no-op
                        # rather than an error so trial scripts can use
                        # mcp_admin defensively without per-MCP guards.
                        if resp.status_code == 404:
                            _log.info(
                                "mcp_admin: %s returned 404 (no _admin "
                                "endpoint); skipped (turn=%s)",
                                url, turn_id,
                            )
                            turn.response = {
                                "skipped": True,
                                "reason": "admin_not_found",
                                "status": 404,
                                "url": url,
                            }
                        else:
                            turn.response = {
                                "status": resp.status_code,
                                "url": url,
                                "body": body,
                            }
                            if resp.status_code >= 400:
                                turn.error = {
                                    "reason": (
                                        f"mcp_admin {op} returned "
                                        f"{resp.status_code}"
                                    ),
                                }
                    except httpx.HTTPError as e:
                        turn.error = {
                            "reason": f"mcp_admin transport error: {e}",
                        }
            elif kind == "reset_context":
                # E21 — wipe agent-side LLM history. AGW is stateless — no
                # AGW signal needed; the next request lands without prior
                # CID evidence and AGW naturally mints a fresh CID via
                # Cid::generate(). Per-API state matrix is in the
                # adapter's Trial._drive_reset() (framework_bridge.py).
                #
                # Boundary turns are STILL recorded as turns so verdict_c
                # can find them and bracket segments correctly.
                try:
                    res = await adapter_client.reset_context(trial_id=trial_id)
                    turn.request = {"kind": "reset_context"}
                    turn.response = {"body": res}
                except Exception as e:
                    turn.error = {"reason": f"reset_context failed: {e}"}
            elif kind == "refresh_tools":
                # E21 — force MCP tools/list re-fetch. Framework-specific;
                # some adapters cache the toolset and need explicit
                # invalidation, others re-fetch per call. Safe to call
                # when mcp=NONE (adapter returns skipped sentinel).
                try:
                    res = await adapter_client.refresh_tools(trial_id=trial_id)
                    turn.request = {"kind": "refresh_tools"}
                    turn.response = {"body": res}
                except Exception as e:
                    turn.error = {"reason": f"refresh_tools failed: {e}"}
            else:
                # Catch-all: any unknown / deferred kind (e.g. the
                # design-doc-only `inject_ambient_cid`) lands here so the
                # trial records an explicit error rather than silently
                # succeeding. Implement new kinds above this branch.
                turn.error = {"reason": f"turn kind {kind!r} not implemented"}

            turn.finished_at = datetime.now(timezone.utc).isoformat()
            # Keep the in-memory trial.turns list in sync with the persisted
            # one so subsequent turn iterations (e.g. force_state_ref looking
            # up a prior turn's _response_id via `trial.turns`) can see what
            # came before without round-tripping through the store.
            trial.turns.append(turn)
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

        # Trial-level status semantics:
        #   pass  — all verdicts passed
        #   fail  — trial ran to completion but ≥1 verdict didn't pass
        #           (whether verdict=="fail" OR verdict=="error" — both mean
        #           "didn't meet the bar"; per-verdict cells still show the
        #           distinction between definitively-failed vs
        #           computation-errored)
        #   error — reserved for run-level exceptions (see except block
        #           below). NEVER set here.
        any_not_pass = any(
            v.verdict in ("fail", "error") for v in verdicts_out.values()
        )
        trial.status = "fail" if any_not_pass else "pass"
        trial.finished_at = datetime.now(timezone.utc).isoformat()
        store.save(trial)

    except Exception as e:
        # Run-level exception — adapter raised, network died, etc.
        # Distinct from "ran cleanly but a verdict failed" (status=fail).
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
