"""FastAPI adapter service wrapping pydantic-ai (Agent + Model + Toolset).

Supports api in {chat, messages, responses}:
  - chat      → OpenAIChatModel (ollama/chatgpt/gemini/mock via OpenAI-compat endpoints)
  - messages  → AnthropicModel  (claude via Anthropic Messages API)
  - responses → OpenAIResponsesModel (chatgpt via OpenAI Responses API)
"""
from __future__ import annotations

import logging
import os

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

try:
    import pydantic_ai
    _PAI_VERSION = getattr(pydantic_ai, "__version__", "unknown")
except Exception:
    _PAI_VERSION = "unknown"

from framework_bridge import Trial

log = logging.getLogger("aiplay.adapter.pydantic_ai")

app = FastAPI(title="aiplay-adapter-pydantic-ai")

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
        "framework": "pydantic-ai",
        "version": _PAI_VERSION,
        "supports": {
            "apis": ["chat", "messages", "responses"],
            "mcps": ["weather", "news", "library", "fetch"],
            "agent_loop": True,
            "streaming": False,
            "state_modes": ["stateless"],
            "compact_strategies": ["drop_half", "drop_tool_calls", "summarize"],
        },
        "default_ollama_model": os.environ.get("DEFAULT_OLLAMA_MODEL", "qwen2.5:7b"),
    }


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/trials")
def create_trial(req: CreateTrialReq):
    if req.config.api not in ("chat", "messages", "responses"):
        raise HTTPException(
            400,
            f"unsupported_combination: api={req.config.api} "
            f"(pydantic-ai adapter supports chat + messages + responses)",
        )
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
            "adapter 'pydantic-ai' does not support force_state_ref "
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


@app.post("/trials/{trial_id}/reset")
async def reset_trial(trial_id: str):
    """E21 — wipe agent-side LLM history (reset_context boundary)."""
    trial = TRIALS.get(trial_id)
    if trial is None:
        raise HTTPException(404, "trial not found")
    return await trial._drive_reset()


@app.post("/trials/{trial_id}/refresh_tools")
async def refresh_tools_trial(trial_id: str):
    """E21 — force MCP tools/list re-fetch on next turn (no-op for
    pydantic-ai — MCPServerStreamableHTTP re-fetches per agent.run())."""
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
    port = int(os.environ.get("ADAPTER_PORT", "5013"))
    uvicorn.run(app, host="0.0.0.0", port=port)
