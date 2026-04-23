"""Map framework → adapter URL; HTTP client for adapter endpoints."""
from __future__ import annotations

import os

import httpx

ADAPTER_URLS = {
    "langchain": os.environ.get("ADAPTER_LANGCHAIN_URL", "http://adapter-langchain:5001"),
    "direct-mcp": os.environ.get("ADAPTER_DIRECT_MCP_URL", "http://adapter-direct-mcp:5010"),
    "langgraph": os.environ.get("ADAPTER_LANGGRAPH_URL", "http://adapter-langgraph:5011"),
}


class AdapterClient:
    def __init__(self, framework: str):
        self.base = ADAPTER_URLS.get(framework)
        if not self.base:
            raise ValueError(f"no adapter registered for framework={framework}")
        self.client = httpx.AsyncClient(base_url=self.base, timeout=120.0)

    async def create_trial(self, trial_id: str, config) -> dict:
        r = await self.client.post("/trials", json={
            "trial_id": trial_id,
            "config": {
                "api": config.api,
                "stream": config.stream,
                "state": config.state,
                "llm": config.llm,
                "mcp": config.mcp,
                "routing": config.routing,
                "model": config.model,
            },
        })
        r.raise_for_status()
        return r.json()

    async def drive_turn(self, trial_id: str, turn_id: str, user_msg: str) -> dict:
        r = await self.client.post(
            f"/trials/{trial_id}/turn",
            json={"turn_id": turn_id, "user_msg": user_msg},
        )
        r.raise_for_status()
        return r.json()

    async def delete_trial(self, trial_id: str) -> dict:
        r = await self.client.delete(f"/trials/{trial_id}")
        r.raise_for_status()
        return r.json()
