"""FastAPI adapter service wrapping llama_index (OpenAI LLM wrapper) plus a
direct openai SDK bypass for the Responses API.

Supports api in {chat, responses, responses+conv}:
  - chat            → llama_index.llms.openai.OpenAI (ollama/chatgpt/gemini/mock)
  - responses       → openai.AsyncOpenAI.responses.create (chatgpt), stateless
  - responses+conv  → openai.AsyncOpenAI.responses.create (chatgpt) + chained
                      previous_response_id across turns (state mode)

api=messages is NOT supported — llama_index does not ship a first-class
Anthropic Messages wrapper on the same catalog tier, and crewai /
autogen / pydantic-ai already cover that combo.

This is the SIXTH and last Plan B framework adapter.
"""
from __future__ import annotations

import logging
import os

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

try:
    import llama_index.core
    _LLAMAINDEX_CORE_VERSION = getattr(llama_index.core, "__version__", "unknown")
except Exception:
    _LLAMAINDEX_CORE_VERSION = "unknown"

try:
    import llama_index.llms.openai as _li_openai
    _LLAMAINDEX_OPENAI_VERSION = getattr(_li_openai, "__version__", "unknown")
except Exception:
    _LLAMAINDEX_OPENAI_VERSION = "unknown"

from framework_bridge import Trial

log = logging.getLogger("aiplay.adapter.llamaindex")

app = FastAPI(title="aiplay-adapter-llamaindex")

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


class ForceStateRefReq(BaseModel):
    ref_to_turn: int


class CompactReq(BaseModel):
    strategy: str = "drop_half"


SUPPORTED_APIS = ("chat", "responses", "responses+conv")


@app.get("/info")
def info():
    return {
        "framework": "llamaindex",
        "version": _LLAMAINDEX_CORE_VERSION,
        "openai_wrapper_version": _LLAMAINDEX_OPENAI_VERSION,
        "supports": {
            "apis": list(SUPPORTED_APIS),
            "mcps": ["weather", "news", "library", "fetch"],
            "agent_loop": True,
            "streaming": False,
            "state_modes": ["stateless", "responses_previous_id"],
            "compact_strategies": ["drop_half", "drop_tool_calls", "summarize"],
        },
        "default_ollama_model": os.environ.get("DEFAULT_OLLAMA_MODEL", "qwen2.5:7b"),
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
            f"(llamaindex adapter supports {', '.join(SUPPORTED_APIS)})",
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


@app.post("/trials/{trial_id}/compact")
async def compact_trial(trial_id: str, req: CompactReq):
    """Plan B T10 — mutate the framework's internal conversation history."""
    trial = TRIALS.get(trial_id)
    if trial is None:
        raise HTTPException(404, "trial not found")
    return await trial.compact(req.strategy)


@app.post("/trials/{trial_id}/force_state_ref")
async def force_state_ref(trial_id: str, req: ForceStateRefReq):
    """Override the next responses+conv turn's previous_response_id.

    The runner uses this to build a verdict-e referential test path: drive
    a few turns normally, then force the NEXT turn to reference an earlier
    response id (not the most recent one). The resulting LLM state should
    reflect the earlier branch — cidgar's state-tracking must handle this.
    """
    trial = TRIALS.get(trial_id)
    if trial is None:
        raise HTTPException(404, "trial not found")
    return trial.force_state_ref(req.ref_to_turn)


@app.delete("/trials/{trial_id}")
async def delete_trial(trial_id: str):
    trial = TRIALS.pop(trial_id, None)
    if trial is not None:
        await trial.aclose()
    return {"ok": True}


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("ADAPTER_PORT", "5015"))
    uvicorn.run(app, host="0.0.0.0", port=port)
