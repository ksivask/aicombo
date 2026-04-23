"""Map framework → adapter URL; HTTP client for adapter endpoints."""
from __future__ import annotations

import os

import httpx

ADAPTER_URLS = {
    "langchain":   os.environ.get("ADAPTER_LANGCHAIN_URL",   "http://adapter-langchain:5001"),
    "direct-mcp":  os.environ.get("ADAPTER_DIRECT_MCP_URL",  "http://adapter-direct-mcp:5010"),
    "langgraph":   os.environ.get("ADAPTER_LANGGRAPH_URL",   "http://adapter-langgraph:5011"),
    "crewai":      os.environ.get("ADAPTER_CREWAI_URL",      "http://adapter-crewai:5012"),
    "pydantic-ai": os.environ.get("ADAPTER_PYDANTIC_AI_URL", "http://adapter-pydantic-ai:5013"),
    "autogen":     os.environ.get("ADAPTER_AUTOGEN_URL",     "http://adapter-autogen:5014"),
    "llamaindex":  os.environ.get("ADAPTER_LLAMAINDEX_URL",  "http://adapter-llamaindex:5015"),
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

    async def drive_turn(
        self,
        trial_id: str,
        turn_id: str,
        user_msg: str,
        turn_kind: str | None = None,
        target_response_id: str | None = None,
    ) -> dict:
        """Drive one turn through the adapter.

        T11 — optional `turn_kind` + `target_response_id` forward through to
        the adapter's /trials/{id}/turn endpoint. When `turn_kind` is None or
        "user_msg" the body matches the pre-T11 contract (default path).
        When `turn_kind == "force_state_ref"` the adapter is expected to set
        `previous_response_id = target_response_id` on the NEXT Responses-API
        call; supporting adapters are autogen + llamaindex, the rest return
        HTTP 400.
        """
        body: dict = {"turn_id": turn_id, "user_msg": user_msg}
        if turn_kind:
            body["turn_kind"] = turn_kind
        if target_response_id:
            body["target_response_id"] = target_response_id
        r = await self.client.post(
            f"/trials/{trial_id}/turn",
            json=body,
        )
        r.raise_for_status()
        return r.json()

    async def compact(self, trial_id: str, strategy: str) -> dict:
        """Plan B T10 — ask the adapter to mutate internal conversation history.

        Duck-typed: every framework adapter exposes this endpoint. Returns the
        adapter's {strategy, history_len_before, history_len_after, ...}
        envelope, which the runner stores as the compact turn's response body.
        """
        r = await self.client.post(
            f"/trials/{trial_id}/compact",
            json={"strategy": strategy},
        )
        r.raise_for_status()
        return r.json()

    async def delete_trial(self, trial_id: str) -> dict:
        r = await self.client.delete(f"/trials/{trial_id}")
        r.raise_for_status()
        return r.json()
