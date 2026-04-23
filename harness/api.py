"""FastAPI routes for the aiplay harness."""
from __future__ import annotations

import asyncio
import json
import os
import uuid
from collections import defaultdict, deque
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Body, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from audit_tail import AuditTail
from efficacy import compute_verdicts
from providers import get_providers
from runner import run_trial
from templates import default_turn_plan
from trials import AuditEntry, Trial, TrialConfig, TrialStore, TurnPlan
from validator import validate as validate_row

router = APIRouter()


# ── State (module-global, single-process harness) ──

DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
STORE = TrialStore(DATA_DIR / "trials")
MATRIX_PATH = DATA_DIR / "matrix.json"
AUDIT_TAIL: AuditTail | None = None
AUDIT_BUFFER_PER_TRIAL: dict[str, list[AuditEntry]] = defaultdict(list)
SSE_QUEUES: dict[str, deque] = defaultdict(lambda: deque(maxlen=100))

# Plan B T14 — cooperative abort registry. Populated when a trial starts,
# set by POST /trials/{id}/abort, checked by runner.run_trial between turns,
# cleared once the background task completes.
ABORT_EVENTS: dict[str, asyncio.Event] = {}


# ── Matrix persistence (distinct from trial JSON) ──

def _load_matrix() -> list[dict]:
    if not MATRIX_PATH.exists():
        # First-boot seed from harness/defaults.yaml
        seeded = _seed_matrix_from_defaults()
        if seeded:
            _save_matrix(seeded)
            return seeded
        return []
    with MATRIX_PATH.open() as f:
        return json.load(f).get("rows", [])


def _seed_matrix_from_defaults() -> list[dict]:
    """Load matrix_seed_rows from harness/defaults.yaml.

    Seeds only rows for adapters the harness can actually drive:
      * Plan A  — langchain, direct-mcp (NONE routing)
      * Plan B  — extends to autogen (T5), llamaindex (T6) for the
                  force_state_ref/verdict-e seed row.
    """
    try:
        import yaml
        defaults_path = Path(__file__).with_name("defaults.yaml")
        if not defaults_path.exists():
            return []
        with defaults_path.open() as f:
            data = yaml.safe_load(f) or {}
        rows_spec = data.get("matrix_seed_rows", [])
        allowed = {"langchain", "autogen", "llamaindex", "NONE"}
        seeded = []
        for idx, spec in enumerate(rows_spec):
            if spec.get("framework") not in allowed:
                continue
            seeded.append({
                "row_id": f"row-seed-{idx:02d}",
                **spec,
            })
        return seeded
    except Exception:
        return []


def _save_matrix(rows: list[dict]) -> None:
    MATRIX_PATH.parent.mkdir(parents=True, exist_ok=True)
    with MATRIX_PATH.open("w") as f:
        json.dump({"rows": rows}, f, indent=2)


# ── Models ──

class RowConfig(BaseModel):
    framework: str
    api: str
    stream: bool = False
    state: bool = False
    llm: str
    mcp: str
    routing: str = "via_agw"
    # Plan B T10 — opt-in flag that switches the row's default turn plan to
    # `with_mcp_with_compact` so verdict (d) has a compact turn to bracket.
    with_compact: bool = False
    # Plan B T11 — opt-in flag that switches the row's default turn plan to
    # `with_responses_state_force_ref` so verdict (e) has a force_state_ref
    # turn to bracket. Only meaningful for api in responses/responses+conv
    # with state=True + a supporting framework (autogen / llamaindex).
    with_force_state_ref: bool = False
    # Plan B T12 — when present, takes precedence over default_turn_plan(row).
    # Shape: {"turns": [...]} matching TurnPlan. Edited via the row drawer's
    # CodeMirror JSON editor; persisted via PATCH /matrix/row/{id}; cleared
    # via DELETE /matrix/row/{id}/turn_plan_override.
    turn_plan_override: dict | None = None
    # Plan B T13 — optional baseline-pairing metadata set by
    # POST /matrix/row/{id}/clone-baseline. `baseline_of` points at the
    # source row_id; `note` is a human-readable label rendered in the UI.
    baseline_of: str | None = None
    note: str | None = None


# ── Routes ──

@router.get("/health")
def health():
    return {"status": "ok", "version": "plan-a-mvp"}


@router.get("/info")
def info():
    return {
        "harness_version": "plan-a-mvp",
        "adapters": [
            {"framework": "langchain", "url": "http://adapter-langchain:5001"},
        ],
    }


@router.get("/providers")
def providers_endpoint():
    return {"providers": get_providers()}


@router.post("/validate")
def validate_endpoint(payload: dict = Body(...)):
    row = payload.get("row_config", {})
    id_to_env = {"chatgpt": "openai", "claude": "anthropic", "gemini": "google"}
    available = {}
    for p in get_providers():
        env_key = id_to_env.get(p["id"])
        if env_key:
            available[env_key] = p["available"]
    return validate_row(row, available_keys=available)


@router.post("/templates/preview")
def templates_preview(row: RowConfig):
    """Return the turn plan that would be used for this row config.

    Same logic as templates.default_turn_plan but exposed as an HTTP endpoint
    so the UI can preview without creating a trial. Plan B will add an edit
    path (PATCH /templates/override) to override the default per row."""
    row_dict = row.model_dump()
    plan = default_turn_plan(row_dict)
    return {"row_config": row_dict, "turn_plan": plan}


@router.get("/matrix")
def matrix_list():
    return {"rows": _load_matrix()}


@router.post("/matrix/row")
def matrix_create(row: RowConfig):
    rows = _load_matrix()
    rid = f"row-{uuid.uuid4().hex[:8]}"
    rows.append({"row_id": rid, **row.model_dump()})
    _save_matrix(rows)
    return {"row_id": rid}


@router.get("/matrix/row/{row_id}")
def matrix_get(row_id: str):
    rows = _load_matrix()
    row = next((r for r in rows if r["row_id"] == row_id), None)
    if not row:
        raise HTTPException(404, "row not found")
    return row


@router.patch("/matrix/row/{row_id}")
def matrix_update(row_id: str, updates: dict = Body(...)):
    rows = _load_matrix()
    for r in rows:
        if r["row_id"] == row_id:
            r.update(updates)
            _save_matrix(rows)
            return {"ok": True}
    raise HTTPException(404, "row not found")


@router.delete("/matrix/row/{row_id}")
def matrix_delete(row_id: str):
    rows = _load_matrix()
    rows = [r for r in rows if r["row_id"] != row_id]
    _save_matrix(rows)
    return {"ok": True}


@router.post("/matrix/row/{row_id}/clone-baseline")
def matrix_clone_baseline(row_id: str):
    """Clone a row as a 'baseline' — same config but routing=direct.

    Used for A/B comparison: the governed row goes via AGW (cidgar hooks
    fire), the baseline row hits the LLM/MCP directly (no governance).
    Verdict comparison should show pass on the governed row and na/empty
    on the baseline row — confirms the governed verdicts aren't false
    positives from non-cidgar sources.
    """
    rows = _load_matrix()
    src = next((r for r in rows if r["row_id"] == row_id), None)
    if not src:
        raise HTTPException(404, "row not found")
    if src.get("routing") == "direct":
        raise HTTPException(
            400, "row is already a direct/baseline row — nothing to clone"
        )

    new_id = f"row-{uuid.uuid4().hex[:8]}"
    clone = {
        "row_id": new_id,
        "framework": src["framework"],
        "api": src["api"],
        "stream": src.get("stream", False),
        "state": src.get("state", False),
        "llm": src["llm"],
        "mcp": src["mcp"],
        "routing": "direct",
        "with_compact": src.get("with_compact", False),
        "with_force_state_ref": src.get("with_force_state_ref", False),
        # Carry the same turn_plan_override so the comparison runs the
        # same exact prompts on both rows.
        "turn_plan_override": src.get("turn_plan_override"),
        # Pairing metadata so the UI can render an A/B link.
        "baseline_of": row_id,
        "note": f"Baseline (direct, no AGW) of {row_id}",
    }
    # Strip None turn_plan_override to keep JSON tidy when not set
    if clone["turn_plan_override"] is None:
        clone.pop("turn_plan_override")
    rows.append(clone)
    _save_matrix(rows)
    return {"row_id": new_id, "baseline_of": row_id}


@router.delete("/matrix/row/{row_id}/turn_plan_override")
def matrix_clear_override(row_id: str):
    """T12 — remove a row's saved turn_plan_override so the runner falls back
    to default_turn_plan(row). Cleaner than PATCH-ing {turn_plan_override:null}."""
    rows = _load_matrix()
    for r in rows:
        if r["row_id"] == row_id:
            r.pop("turn_plan_override", None)
            _save_matrix(rows)
            return {"ok": True}
    raise HTTPException(404, "row not found")


@router.post("/templates/validate")
def templates_validate(payload: dict = Body(...)):
    """T12 — validate a turn_plan dict. Returns {ok, errors}.

    Checks the shape the runner expects (turns list of dicts with kind + turn_id;
    user_msg requires text; force_state_ref requires lookback). The drawer's
    Validate button hits this before Save so users get a clear error list
    without having to trigger a trial first.
    """
    errors: list[str] = []
    plan = payload.get("turn_plan", {})
    turns = plan.get("turns")
    if not isinstance(turns, list) or not turns:
        errors.append("turns must be a non-empty list")
    else:
        for i, t in enumerate(turns):
            if not isinstance(t, dict):
                errors.append(f"turn {i}: must be object")
                continue
            if "kind" not in t:
                errors.append(f"turn {i}: missing 'kind'")
            elif t["kind"] not in ("user_msg", "compact", "force_state_ref"):
                errors.append(f"turn {i}: invalid kind '{t['kind']}'")
            if "turn_id" not in t:
                errors.append(f"turn {i}: missing 'turn_id'")
            if t.get("kind") == "user_msg" and "text" not in t:
                errors.append(f"turn {i}: user_msg requires 'text'")
            if t.get("kind") == "force_state_ref" and "lookback" not in t:
                errors.append(f"turn {i}: force_state_ref requires 'lookback' (int)")
    return {"ok": not errors, "errors": errors}


@router.delete("/matrix")
def matrix_clear():
    """Delete all matrix rows. Persists an empty list so the seed-on-first-boot
    logic doesn't reintroduce them on the next /matrix call."""
    prior = _load_matrix()
    _save_matrix([])
    return {"ok": True, "deleted_count": len(prior)}


@router.post("/trials/{row_id}/run")
async def trial_run(row_id: str):
    rows = _load_matrix()
    row = next((r for r in rows if r["row_id"] == row_id), None)
    if not row:
        raise HTTPException(404, "row not found")

    # Reject unrunnable rows server-side via the full validator, not just
    # the UI. Covers: LLM=NONE+MCP=NONE, api↔llm mismatch, adapter-not-
    # implemented (Plan A only has langchain/chat), etc.
    id_to_env = {"chatgpt": "openai", "claude": "anthropic", "gemini": "google"}
    available_keys = {}
    for p in get_providers():
        env_key = id_to_env.get(p["id"])
        if env_key:
            available_keys[env_key] = p["available"]
    v = validate_row(row, available_keys=available_keys)
    if not v.get("runnable", True):
        reasons = " | ".join(v.get("warnings", [])) or "row is not runnable"
        raise HTTPException(400, f"row not runnable: {reasons}")

    trial_id = str(uuid.uuid4())
    # When llm=NONE, the direct-mcp adapter drives the trial regardless of
    # the framework the user picked in the matrix row (that dropdown is
    # irrelevant for llm=NONE — the runner picks the adapter for us).
    framework = row["framework"]
    if row.get("llm", "NONE") == "NONE":
        framework = "direct-mcp"
    cfg = TrialConfig(
        framework=framework, api=row["api"],
        stream=row.get("stream", False), state=row.get("state", False),
        llm=row["llm"], mcp=row["mcp"], routing=row.get("routing", "via_agw"),
    )
    # T12 — prefer per-row override when the user has saved one via the drawer.
    plan_dict = row.get("turn_plan_override") or default_turn_plan(row)
    plan = TurnPlan(turns=plan_dict["turns"])
    trial = Trial(trial_id=trial_id, config=cfg, turn_plan=plan, status="running")
    STORE.save(trial)

    # Record trial start time for time-window audit correlation.
    # Header-based demux still registered as a fallback (fires only if
    # cidgar ever starts logging X-Harness-Trial-ID; currently does not).
    import time
    trial_started_mono = time.time()
    if AUDIT_TAIL is not None:
        def cb(entry: dict):
            AUDIT_BUFFER_PER_TRIAL[trial_id].append(AuditEntry(
                trial_id=trial_id, turn_id=entry.get("turn_id"),
                phase=entry.get("phase"), cid=entry.get("cid"),
                backend=entry.get("backend"), raw=entry.get("raw", {}),
                captured_at=entry.get("timestamp", ""),
            ))
        AUDIT_TAIL.subscribe(trial_id, cb)

    # Adapter client (simplified HTTP wrapper)
    from adapters_registry import AdapterClient
    adapter = AdapterClient(framework=cfg.framework)

    # T14 — register cooperative-abort event BEFORE spawning so a racing
    # POST /abort can find it even if it arrives before create_task yields.
    ABORT_EVENTS[trial_id] = asyncio.Event()

    # Run in background (pass row_id so we can update the matrix on completion)
    asyncio.create_task(_run_trial_bg(trial_id, adapter, row_id=row_id, started_mono=trial_started_mono))

    return {"trial_id": trial_id, "status": "running"}


async def _run_trial_bg(trial_id: str, adapter, row_id: str | None = None, started_mono: float | None = None):
    def audit_provider():
        """Combine (a) header-demuxed entries (future) + (b) time-window entries.
        Plan A relies on (b) since cidgar governance log omits request headers.
        """
        demuxed = list(AUDIT_BUFFER_PER_TRIAL.get(trial_id, []))
        if AUDIT_TAIL is not None and started_mono is not None:
            window = AUDIT_TAIL.entries_since(started_mono)
            # Dedupe against demuxed (header-matched) entries by phase+cid+timestamp
            seen = {(e.phase, e.cid, e.captured_at) for e in demuxed}
            for e in window:
                key = (e.get("phase"), e.get("cid"), e.get("timestamp"))
                if key in seen:
                    continue
                demuxed.append(AuditEntry(
                    trial_id=trial_id, turn_id=e.get("turn_id"),
                    phase=e.get("phase"), cid=e.get("cid"),
                    backend=e.get("backend"), raw=e.get("raw", {}),
                    captured_at=e.get("timestamp", ""),
                ))
        return demuxed

    await run_trial(
        trial_id=trial_id,
        store=STORE,
        adapter_client=adapter,
        audit_entries_provider=audit_provider,
        # T14 — may be None if the event was already cleaned up by a sibling
        # path, but in practice it's registered in trial_run before this bg
        # task is scheduled.
        abort_event=ABORT_EVENTS.get(trial_id),
    )

    # T14 — release the abort event once the trial is fully done (any terminal
    # status). A second /abort hit after this point will now short-circuit to
    # the "already finished" branch instead of silently setting a stale event.
    ABORT_EVENTS.pop(trial_id, None)

    if AUDIT_TAIL is not None:
        AUDIT_TAIL.unsubscribe(trial_id)

    # Reconcile matrix row with final trial state so the UI can recover
    # even if an SSE client disconnected mid-trial.
    if row_id:
        try:
            final = STORE.load(trial_id)
            rows = _load_matrix()
            for r in rows:
                if r["row_id"] == row_id:
                    r["status"] = final.status
                    r["verdicts"] = final.verdicts or {}
                    r["last_trial_id"] = trial_id
                    _save_matrix(rows)
                    break
        except Exception:
            pass


@router.get("/trials/{trial_id}")
def trial_get(trial_id: str):
    try:
        trial = STORE.load(trial_id)
        from trials import _to_jsonable
        return _to_jsonable(trial)
    except FileNotFoundError:
        raise HTTPException(404, "trial not found")


@router.post("/trials/{trial_id}/abort")
async def trial_abort(trial_id: str):
    """Request cooperative abort of a running trial.

    The currently-executing turn finishes naturally (so no HTTP-mid-call
    corruption / framework state poisoning); subsequent turns are skipped.
    The trial transitions to status=aborted and verdicts compute on whatever
    turns completed.

    Returns:
      - 404 when the trial does not exist (no event registered AND no trial
        JSON on disk).
      - {ok: False, reason, status} when the trial already finished (no
        event in the registry, but the trial JSON exists with a terminal
        status). Idempotent — a second abort on a completed trial is a
        no-op rather than an error.
      - {ok: True, abort_requested: True, trial_id} on success.
    """
    ev = ABORT_EVENTS.get(trial_id)
    if ev is None:
        try:
            trial = STORE.load(trial_id)
        except FileNotFoundError:
            raise HTTPException(404, "trial not found")
        return {
            "ok": False,
            "reason": f"trial is not running (status={trial.status})",
            "status": trial.status,
            "trial_id": trial_id,
        }
    ev.set()
    return {"ok": True, "abort_requested": True, "trial_id": trial_id}


@router.get("/trials/{trial_id}/stream")
async def trial_stream(trial_id: str):
    async def event_gen():
        while True:
            await asyncio.sleep(1.0)
            try:
                trial = STORE.load(trial_id)
                yield f"data: {json.dumps({'event': 'status', 'status': trial.status})}\n\n"
                if trial.status in ("pass", "fail", "error", "aborted"):
                    yield f"data: {json.dumps({'event': 'trial_done', 'status': trial.status})}\n\n"
                    break
            except FileNotFoundError:
                yield f"data: {json.dumps({'event': 'error', 'message': 'trial missing'})}\n\n"
                break
    return StreamingResponse(event_gen(), media_type="text/event-stream")


@router.get("/audit/stream")
async def audit_stream():
    """Raw AGW audit stream — all trials."""
    async def gen():
        while True:
            await asyncio.sleep(2.0)
            yield f": keepalive\n\n"
    return StreamingResponse(gen(), media_type="text/event-stream")
