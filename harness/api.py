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


# ── Matrix persistence (distinct from trial JSON) ──

def _load_matrix() -> list[dict]:
    if not MATRIX_PATH.exists():
        return []
    with MATRIX_PATH.open() as f:
        return json.load(f).get("rows", [])


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


@router.post("/trials/{row_id}/run")
async def trial_run(row_id: str):
    rows = _load_matrix()
    row = next((r for r in rows if r["row_id"] == row_id), None)
    if not row:
        raise HTTPException(404, "row not found")

    trial_id = str(uuid.uuid4())
    cfg = TrialConfig(
        framework=row["framework"], api=row["api"],
        stream=row.get("stream", False), state=row.get("state", False),
        llm=row["llm"], mcp=row["mcp"], routing=row.get("routing", "via_agw"),
    )
    plan_dict = default_turn_plan(row)
    plan = TurnPlan(turns=plan_dict["turns"])
    trial = Trial(trial_id=trial_id, config=cfg, turn_plan=plan, status="running")
    STORE.save(trial)

    # Subscribe audit_tail → capture into buffer for this trial
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

    # Run in background (pass row_id so we can update the matrix on completion)
    asyncio.create_task(_run_trial_bg(trial_id, adapter, row_id=row_id))

    return {"trial_id": trial_id, "status": "running"}


async def _run_trial_bg(trial_id: str, adapter, row_id: str | None = None):
    def audit_provider():
        return list(AUDIT_BUFFER_PER_TRIAL.get(trial_id, []))

    await run_trial(
        trial_id=trial_id,
        store=STORE,
        adapter_client=adapter,
        audit_entries_provider=audit_provider,
    )

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
