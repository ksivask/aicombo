"""Mock OpenAI-compat chat-completions server for cidgar multi-choice testing.

Why: Ollama ignores the `n` parameter and always returns 1 choice. To
verify cidgar injects the C2 text marker into EACH choice (completions_shape
§105 iterates `choices.iter_mut()`), we need an upstream that honors `n>1`.

Endpoint: POST /v1/chat/completions  (OpenAI-compat)

Response shape: standard OpenAI chat.completion with N choices, each with
deterministic content so tests can assert. When `n` is absent → 1 choice.

Run: python main.py  (listens on 0.0.0.0:8000 inside container)
"""
from __future__ import annotations

import json
import os
import time
import uuid

from fastapi import FastAPI, Request
from pydantic import BaseModel

app = FastAPI(title="aiplay-mock-llm")


@app.get("/health")
def health():
    return {"status": "ok", "role": "mock-llm"}


@app.get("/v1/models")
def models():
    return {
        "object": "list",
        "data": [
            {"id": "mock-multichoice", "object": "model", "created": int(time.time()), "owned_by": "aiplay"},
        ],
    }


@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    body = await request.json()
    n = int(body.get("n", 1))
    messages = body.get("messages") or []
    last_user = next((m.get("content", "") for m in reversed(messages) if m.get("role") == "user"), "")

    # Deterministic per-choice content so tests can identify each choice.
    # Multi-line body helps verify cidgar's C2 marker is appended PER CHOICE
    # (completions_shape.rs:165 iterates choices and appends to each .content).
    choices = []
    for idx in range(n):
        choices.append({
            "index": idx,
            "message": {
                "role": "assistant",
                "content": f"[mock response {idx + 1} of {n}] echo of your question: {last_user[:80]}",
            },
            "finish_reason": "stop",
        })

    return {
        "id": f"mockcmpl-{uuid.uuid4().hex[:8]}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": body.get("model", "mock-multichoice"),
        "system_fingerprint": "fp_mock",
        "choices": choices,
        "usage": {
            "prompt_tokens": sum(len(m.get("content", "")) for m in messages),
            "completion_tokens": 20 * n,
            "total_tokens": sum(len(m.get("content", "")) for m in messages) + 20 * n,
        },
    }


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("MOCK_LLM_PORT", "8000"))
    uvicorn.run(app, host="0.0.0.0", port=port)
