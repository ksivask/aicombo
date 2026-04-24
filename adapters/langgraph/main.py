"""FastAPI adapter service wrapping langgraph (create_react_agent).

Supports api in {chat, messages, responses, responses+conv}:
  - chat            → langchain_openai.ChatOpenAI (openai-compat; any llm)
  - messages        → langchain_anthropic.ChatAnthropic (claude)
  - responses       → langchain_openai.ChatOpenAI(use_responses_api=True) (chatgpt)
  - responses+conv  → same as responses + previous_response_id threaded through
                      graph.ainvoke's config={"configurable": {...}} (state mode)

langgraph's create_react_agent(llm, tools) is LLM-agnostic — it accepts any
langchain-wrapped chat model, so api switching boils down to selecting the
right wrapper class inside Trial._build_llm. The graph shape + agent-loop
semantics are unchanged.
"""
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
    # T11 — force_state_ref in-line support. When turn_kind=="force_state_ref"
    # the adapter sets trial._forced_prev_id = target_response_id before
    # dispatching the turn; turn() consumes + clears it. Only valid for
    # api=responses+conv.
    turn_kind: str = "user_msg"
    target_response_id: str | None = None


class CompactReq(BaseModel):
    strategy: str = "drop_half"


SUPPORTED_APIS = {"chat", "messages", "responses", "responses+conv"}


@app.get("/info")
def info():
    return {
        "framework": "langgraph",
        "version": _LG_VERSION,
        "supports": {
            "apis": sorted(SUPPORTED_APIS),
            # MCP tool-calling via langchain-mcp-adapters; langgraph's
            # create_react_agent drives the LLM↔tool hop loop internally.
            "mcps": ["weather", "news", "library", "fetch"],
            "agent_loop": True,
            "streaming": False,  # Plan B v1
            "state_modes": ["stateless", "responses_previous_id"],
            "compact_strategies": ["drop_half", "drop_tool_calls", "summarize"],
        },
        "default_ollama_model": os.environ.get("DEFAULT_OLLAMA_MODEL", "qwen2.5:7b-instruct"),
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
            f"(langgraph adapter supports {', '.join(sorted(SUPPORTED_APIS))})",
        )
    try:
        TRIALS[req.trial_id] = Trial(req.trial_id, req.config.model_dump())
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True, "trial_id": req.trial_id}


@app.post("/trials/{trial_id}/turn")
async def drive_turn(trial_id: str, req: TurnReq):
    if req.turn_kind == "force_state_ref":
        trial = TRIALS.get(trial_id)
        if trial is None:
            raise HTTPException(404, "trial not found")
        if trial.config.get("api") != "responses+conv":
            raise HTTPException(
                400,
                f"force_state_ref requires api=responses+conv; "
                f"got api={trial.config.get('api')}",
            )
        if not req.target_response_id:
            raise HTTPException(400, "force_state_ref requires target_response_id")
        trial._forced_prev_id = req.target_response_id
        return await trial.turn(req.turn_id, req.user_msg)
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
