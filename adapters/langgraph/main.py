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
            "compact_strategies": [],
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
    trial = TRIALS.get(trial_id)
    if trial is None:
        raise HTTPException(404, "trial not found")
    return await trial.turn(req.turn_id, req.user_msg)


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
