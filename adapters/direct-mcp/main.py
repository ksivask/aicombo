"""FastAPI adapter service wrapping direct MCP tool calls (no LLM).

Plan A v1: deterministic keyword-based routing. Same HTTP contract as
adapters/langchain/main.py — harness/adapters_registry.py treats them
interchangeably.
"""
from __future__ import annotations

import logging
import os

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from framework_bridge import Trial

log = logging.getLogger("aiplay.adapter.direct-mcp")

app = FastAPI(title="aiplay-adapter-direct-mcp")

TRIALS: dict[str, Trial] = {}


class Config(BaseModel):
    api: str = "NONE"
    stream: bool = False
    state: bool = False
    llm: str = "NONE"
    mcp: str
    routing: str = "via_agw"
    model: str | None = None


class CreateTrialReq(BaseModel):
    trial_id: str
    config: Config


class TurnReq(BaseModel):
    turn_id: str
    user_msg: str
    # T11 — accept the new fields but reject force_state_ref below (this
    # adapter does not talk to an LLM, so Responses-API state is n/a).
    turn_kind: str = "user_msg"
    target_response_id: str | None = None


class CompactReq(BaseModel):
    strategy: str = "drop_half"


@app.get("/info")
def info():
    return {
        "framework": "direct-mcp",
        "version": "1.0",
        "supports": {
            "apis": [],
            "mcps": ["weather", "news", "library", "fetch"],
            "streaming": False,
            "state_modes": ["stateless"],
            "compact_strategies": ["noop"],
        },
    }


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/trials")
def create_trial(req: CreateTrialReq):
    if req.config.mcp == "NONE":
        raise HTTPException(400, "unsupported_combination: direct-mcp adapter requires a concrete MCP")
    try:
        TRIALS[req.trial_id] = Trial(req.trial_id, req.config.model_dump())
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True, "trial_id": req.trial_id}


@app.post("/trials/{trial_id}/turn")
async def drive_turn(trial_id: str, req: TurnReq):
    # T11 — reject unsupported turn_kind early (before trial lookup).
    if req.turn_kind == "force_state_ref":
        raise HTTPException(
            400,
            "adapter 'direct-mcp' does not support force_state_ref "
            "(no LLM / no Responses-API)",
        )
    if req.turn_kind != "user_msg":
        raise HTTPException(400, f"unknown turn_kind: {req.turn_kind!r}")
    trial = TRIALS.get(trial_id)
    if trial is None:
        raise HTTPException(404, "trial not found")
    return await trial.turn(req.turn_id, req.user_msg)


@app.post("/trials/{trial_id}/compact")
async def compact_trial(trial_id: str, req: CompactReq):
    """Plan B T10 — no-op for direct-mcp (no LLM history to compact).

    Kept on the endpoint surface for contract parity with the other six
    adapters, so the runner can issue a uniform compact call regardless of
    framework. Returns an informative envelope.
    """
    trial = TRIALS.get(trial_id)
    if trial is None:
        raise HTTPException(404, "trial not found")
    return await trial.compact(req.strategy)


@app.delete("/trials/{trial_id}")
async def delete_trial(trial_id: str):
    trial = TRIALS.pop(trial_id, None)
    if trial is not None:
        await trial.aclose()
    return {"ok": True}


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("ADAPTER_PORT", "5010"))
    uvicorn.run(app, host="0.0.0.0", port=port)
