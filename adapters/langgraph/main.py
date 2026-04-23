"""FastAPI adapter service wrapping langgraph (create_react_agent)."""
from __future__ import annotations

import logging
import os

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

try:
    import langgraph
    _LG_VERSION = getattr(langgraph, "__version__", "unknown")
except Exception:
    _LG_VERSION = "unknown"

from framework_bridge import Trial

log = logging.getLogger("aiplay.adapter.langgraph")

app = FastAPI(title="aiplay-adapter-langgraph")

TRIALS: dict[str, Trial] = {}


class Config(BaseModel):
    api: str
    stream: bool = False
    state: bool = False
    llm: str
    mcp: str = "NONE"
    routing: str = "via_agw"
    model: str | None = None


class CreateTrialReq(BaseModel):
    trial_id: str
    config: Config


class TurnReq(BaseModel):
    turn_id: str
    user_msg: str
    # T11 — accept the new fields but reject force_state_ref below (this
    # adapter does not implement Responses-API state mode).
    turn_kind: str = "user_msg"
    target_response_id: str | None = None


class CompactReq(BaseModel):
    strategy: str = "drop_half"


@app.get("/info")
def info():
    return {
        "framework": "langgraph",
        "version": _LG_VERSION,
        "supports": {
            "apis": ["chat"],
            # MCP tool-calling via langchain-mcp-adapters; langgraph's
            # create_react_agent drives the LLM↔tool hop loop internally.
            "mcps": ["weather", "news", "library", "fetch"],
            "agent_loop": True,
            "streaming": False,  # Plan B v1
            "state_modes": ["stateless"],
            "compact_strategies": ["drop_half", "drop_tool_calls", "summarize"],
        },
        "default_ollama_model": os.environ.get("DEFAULT_OLLAMA_MODEL", "qwen2.5:7b-instruct"),
    }


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/trials")
def create_trial(req: CreateTrialReq):
    if req.config.api != "chat":
        raise HTTPException(400, f"unsupported_combination: api={req.config.api}")
    TRIALS[req.trial_id] = Trial(req.trial_id, req.config.model_dump())
    return {"ok": True, "trial_id": req.trial_id}


@app.post("/trials/{trial_id}/turn")
async def drive_turn(trial_id: str, req: TurnReq):
    # T11 — reject unsupported turn_kind early (before trial lookup).
    if req.turn_kind == "force_state_ref":
        raise HTTPException(
            400,
            "adapter 'langgraph' does not support force_state_ref "
            "(no Responses-API state mode)",
        )
    if req.turn_kind != "user_msg":
        raise HTTPException(400, f"unknown turn_kind: {req.turn_kind!r}")
    trial = TRIALS.get(trial_id)
    if trial is None:
        raise HTTPException(404, "trial not found")
    return await trial.turn(req.turn_id, req.user_msg)


@app.post("/trials/{trial_id}/compact")
async def compact_trial(trial_id: str, req: CompactReq):
    """Plan B T10 — mutate the framework's internal conversation history."""
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
    port = int(os.environ.get("ADAPTER_PORT", "5011"))
    uvicorn.run(app, host="0.0.0.0", port=port)
