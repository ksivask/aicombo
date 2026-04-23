"""FastAPI adapter service wrapping crewai (Crew + Agent + Task).

Supports api=chat (via OpenAICompletion/OpenAICompatibleCompletion
native providers) and api=messages (via AnthropicCompletion).
"""
from __future__ import annotations

import logging
import os

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

try:
    import crewai
    _CREW_VERSION = getattr(crewai, "__version__", "unknown")
except Exception:
    _CREW_VERSION = "unknown"

from framework_bridge import Trial

log = logging.getLogger("aiplay.adapter.crewai")

app = FastAPI(title="aiplay-adapter-crewai")

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
        "framework": "crewai",
        "version": _CREW_VERSION,
        "supports": {
            "apis": ["chat", "messages"],
            "mcps": ["weather", "news", "library", "fetch"],
            "agent_loop": True,
            "streaming": False,
            "state_modes": ["stateless"],
            "compact_strategies": [],
        },
        "default_ollama_model": os.environ.get("DEFAULT_OLLAMA_MODEL", "qwen2.5:7b"),
    }


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/trials")
def create_trial(req: CreateTrialReq):
    if req.config.api not in ("chat", "messages"):
        raise HTTPException(
            400,
            f"unsupported_combination: api={req.config.api} "
            f"(crewai adapter supports chat + messages)",
        )
    try:
        TRIALS[req.trial_id] = Trial(req.trial_id, req.config.model_dump())
    except ValueError as e:
        raise HTTPException(400, str(e))
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
    port = int(os.environ.get("ADAPTER_PORT", "5012"))
    uvicorn.run(app, host="0.0.0.0", port=port)
