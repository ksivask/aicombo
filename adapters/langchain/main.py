"""FastAPI adapter service wrapping langchain."""
from __future__ import annotations

import logging
import os

from fastapi import Body, FastAPI, HTTPException
from pydantic import BaseModel

import langchain
from framework_bridge import Trial

log = logging.getLogger("aiplay.adapter.langchain")

app = FastAPI(title="aiplay-adapter-langchain")

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
        "framework": "langchain",
        "version": getattr(langchain, "__version__", "unknown"),
        "supports": {
            "apis": ["chat"],
            # MCP tool-calling supported via langchain-mcp-adapters; the
            # adapter binds tools from /mcp/<name>/tools/list to ChatOpenAI
            # and runs an iterative agent loop (max 3 LLM hops per turn).
            "mcps": ["weather", "news", "library", "fetch"],
            "agent_loop": True,
            "streaming": False,  # Plan A
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
    # T11 — the validator should never schedule force_state_ref for this
    # adapter. Reject early (before trial lookup) so the harness gets a
    # clear 400 even for a probe-style request with a bogus trial_id.
    if req.turn_kind == "force_state_ref":
        raise HTTPException(
            400,
            "adapter 'langchain' does not support force_state_ref "
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
    port = int(os.environ.get("ADAPTER_PORT", "5001"))
    uvicorn.run(app, host="0.0.0.0", port=port)
