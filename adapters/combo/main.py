"""FastAPI adapter service wrapping the combo (multi-LLM-same-CID) bridge.

Endpoint shape mirrors every other adapter (langchain, langgraph, ...)
so harness/adapters_registry.py talks to combo identically:
  POST   /trials                    create
  POST   /trials/{id}/turn          drive one turn (round-robin LLM)
  POST   /trials/{id}/compact       no-op (parity)
  POST   /trials/{id}/reset         wipe canonical history (E21)
  POST   /trials/{id}/refresh_tools no-op (no MCP in first cut)
  DELETE /trials/{id}               cleanup
  GET    /info                      framework metadata
  GET    /health                    liveness

First-cut API coverage: chat (openai-shape providers) + messages (claude
via anthropic SDK). NO MCP, NO tool calling, NO streaming, NO responses
APIs — see framework_bridge.py preamble for deferral notes (E24a/b/c).
"""
from __future__ import annotations

import logging
import os

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from framework_bridge import Trial

log = logging.getLogger("aiplay.adapter.combo")

app = FastAPI(title="aiplay-adapter-combo")

TRIALS: dict[str, Trial] = {}

SUPPORTED_APIS = ("chat", "messages")


class Config(BaseModel):
    """Per-trial config sent by the harness.

    `llm` is intentionally typed as `str | list[str]` so combo can accept
    BOTH the single-string legacy form (round-robin of length 1) AND the
    list form that is the whole point of this adapter (E23 schema).
    `model` mirrors the same shape; validator enforces 1:1 length match
    when both are lists.
    """
    api: str
    stream: bool = False
    state: bool = False
    llm: str | list[str]
    mcp: str | list[str] = "NONE"
    routing: str = "via_agw"
    model: str | list[str] | None = None


class CreateTrialReq(BaseModel):
    trial_id: str
    config: Config


class TurnReq(BaseModel):
    turn_id: str
    user_msg: str
    # T11 fields are accepted for endpoint-shape parity, but combo doesn't
    # implement Responses-API state so anything other than "user_msg"
    # returns 400 below. (force_state_ref defers to E24a alongside the
    # responses / responses+conv API support.)
    turn_kind: str = "user_msg"
    target_response_id: str | None = None


class CompactReq(BaseModel):
    strategy: str = "drop_half"


@app.get("/info")
def info():
    return {
        "framework": "combo",
        "version": "0.1",
        "supports": {
            "apis": list(SUPPORTED_APIS),
            "mcps": [],  # No MCP integration in first cut.
            "agent_loop": False,  # No tool calling yet.
            "streaming": False,
            "state_modes": ["stateless"],
            "compact_strategies": [],  # parity stub only — see compact()
            "multi_llm": True,  # The whole point of this adapter.
        },
    }


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/trials")
def create_trial(req: CreateTrialReq):
    if req.config.api not in SUPPORTED_APIS:
        raise HTTPException(
            400,
            f"unsupported_combination: api={req.config.api} "
            f"(combo adapter first-cut supports {', '.join(SUPPORTED_APIS)})",
        )
    try:
        TRIALS[req.trial_id] = Trial(req.trial_id, req.config.model_dump())
    except ValueError as e:
        # Trial.__init__ raises ValueError on misconfig (empty llm list,
        # missing env var for cloud provider, unsupported llm name).
        raise HTTPException(400, str(e))
    return {"ok": True, "trial_id": req.trial_id}


@app.post("/trials/{trial_id}/turn")
async def drive_turn(trial_id: str, req: TurnReq):
    trial = TRIALS.get(trial_id)
    if trial is None:
        raise HTTPException(404, "trial not found")
    if req.turn_kind == "force_state_ref":
        raise HTTPException(
            400,
            "adapter 'combo' does not support force_state_ref in the first "
            "cut (no Responses API yet — defer to E24a)",
        )
    if req.turn_kind != "user_msg":
        raise HTTPException(400, f"unknown turn_kind: {req.turn_kind!r}")
    return await trial.turn(req.turn_id, req.user_msg)


@app.post("/trials/{trial_id}/compact")
async def compact_trial(trial_id: str, req: CompactReq):
    trial = TRIALS.get(trial_id)
    if trial is None:
        raise HTTPException(404, "trial not found")
    return await trial.compact(req.strategy)


@app.post("/trials/{trial_id}/reset")
async def reset_trial(trial_id: str):
    trial = TRIALS.get(trial_id)
    if trial is None:
        raise HTTPException(404, "trial not found")
    return await trial._drive_reset()


@app.post("/trials/{trial_id}/refresh_tools")
async def refresh_tools_trial(trial_id: str):
    trial = TRIALS.get(trial_id)
    if trial is None:
        raise HTTPException(404, "trial not found")
    return await trial._drive_refresh_tools()


@app.delete("/trials/{trial_id}")
async def delete_trial(trial_id: str):
    trial = TRIALS.pop(trial_id, None)
    if trial is not None:
        await trial.aclose()
    return {"ok": True}


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("ADAPTER_PORT", "5008"))
    uvicorn.run(app, host="0.0.0.0", port=port)
