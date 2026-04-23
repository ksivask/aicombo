# aiplay — Plan B findings

> Output of Plan B execution (T1-T15). Records per-adapter behavior, verdict
> coverage, known issues, and operational notes discovered while building out
> the 5 new framework adapters and verdicts c/d/e on top of the Plan A MVP.

## Verdict matrix — what runs where

| Verdict | What it measures | Pass example observed | Adapters that can produce non-na |
|---|---|---|---|
| (a) Presence | CID appears in audit log per turn | langchain+ollama+weather (Plan A), crewai+ollama (T15 smoke) | all (any framework hitting AGW) |
| (b) Channel structure | All 3 channels (system / tool args / tool result) carry expected CID | langchain+ollama+weather, crewai+ollama, pydantic-ai+ollama, autogen+ollama | all |
| (c) Continuity | CID preserved across >=3 consecutive turns | all ollama rows smoked in T15 | all (multi-turn plans) |
| (d) Resilience | CID survives a compact turn | langchain+with_compact (Plan A reference) | all 7 adapters (compact fallback to drop_half) |
| (e) State-mode gap | CID survives previous_response_id chain incl. force_state_ref | autogen+responses+conv (mocked in runner test) | autogen, llamaindex (only Responses+state-supporting adapters) |
| (f) GAR richness | `_ib_gar` field present in audit | langchain+with_mcp (Plan A) | all (depends on AGW reflection, not framework) |

## Adapter capability matrix (mirror of harness/validator.py::ADAPTER_CAPABILITIES)

| Adapter | port | chat | messages | responses | responses+conv | MCP support | Notes |
|---|---|---|---|---|---|---|---|
| langchain | 5001 | yes | - | - | - | langchain-mcp-adapters | Plan A reference impl |
| direct-mcp | 5010 | - | - | - | - | fastmcp.Client | No LLM — exercises f1/f4/f5 only |
| langgraph | 5011 | yes | - | - | - | langchain-mcp-adapters | Single `graph.ainvoke` per turn |
| crewai | 5012 | yes | yes | - | - | custom BaseTool wrapping fastmcp | crewai 1.14.2 native SDK |
| pydantic-ai | 5013 | yes | yes | yes | - | MCPServerStreamableHTTP toolset | Cleanest provider injection |
| autogen | 5014 | yes | yes | yes | yes | fastmcp wrap | Bypasses autogen for Responses |
| llamaindex | 5015 | yes | - | yes | yes | fastmcp wrap | OpenAILike for non-OpenAI catalog |

## Per-adapter quirks discovered during build-out

### langchain (Plan A reference; T12 of plan-a)
- Captures the canonical `_http_exchanges` + `_capture_mcp_op_events` pattern
  that the other 6 adapters mirror.
- Per-turn header injection via a mutable dict passed as `default_headers={}` —
  closure-captured and mutated per-turn; works because langchain/openai-python
  reuses the same httpx client.

### langgraph (T2)
- `graph.ainvoke` is a single call per turn — no iterative agent loop in the
  adapter itself (langgraph runs the loop internally).
- Inherits langchain-mcp-adapters, so tools/list shape identical to langchain.

### crewai (T3)
- crewai 1.14.x ships its own SDK clients (no longer litellm by default).
  Required rebuilding BOTH `_client` (sync) AND `_async_client` (async)
  post-construction with the hooked httpx — otherwise exchanges don't land in
  the capture buffer.
- Use the `ollama/<model>` prefix on model strings (NOT `openai/...` even
  though chat-completions is the wire protocol).
- Custom BaseTool subclass wraps `fastmcp.Client` directly — no
  langchain-mcp-adapters dependency.
- Compaction strategy `drop_tool_calls` falls back to `drop_half` (the
  internal message dict schema has no tool_calls metadata to filter on).

### pydantic-ai (T4)
- pydantic-ai 1.86.0 — major API rewrite from 0.0.13. Migration notes:
  - `OpenAIModel` -> `OpenAIChatModel`
  - `mcp_servers=` -> `toolsets=`
  - `result.data` -> `result.output`
- Cleanest injection of the 6: `Provider(http_client=...)` is first-class.
- Anthropic, Responses APIs all use distinct model classes:
  `OpenAIChatModel`, `AnthropicModel`, `OpenAIResponsesModel`.
- Compact strategies all fall back to `drop_half` (ModelMessage part-walking
  is fragile across minor versions).

### autogen (T5)
- autogen-ext has no Responses client — bypassed autogen entirely for the
  Responses path; uses `openai.AsyncOpenAI(http_client=...).responses.create()`
  directly.
- `force_state_ref` requires manipulating the trial's `_forced_prev_id` field;
  agent path pokes private `agent._model_context._messages` to inject.
- AssistantAgent default tool iteration limit raised to 3
  (`max_tool_iterations=3`) — default (1) wasn't enough for multi-hop MCP
  chains.
- **Known orthogonal AGW issue**: AGW `chatgpt` route returns 503 for
  Responses payloads. Surfaced in T5 autogen+responses smoke. This is an AGW
  routing bug, NOT an adapter regression — file upstream.

### llamaindex (T6)
- `OpenAI` class rejects non-catalog model names — `OpenAILike` is the right
  class for ollama / mock / gemini (anything that speaks OpenAI-compat but
  isn't in OpenAI's official model catalog).
- `reuse_client=True` is mandatory; otherwise per-call teardown closes the
  shared httpx and breaks subsequent calls (first call works, second throws).
- Bypassed llamaindex for the Responses path (direct openai SDK), same as
  autogen — llamaindex's Responses integration doesn't expose client
  injection cleanly.
- Compact strategies: `drop_tool_calls` filters out `MessageRole.TOOL`;
  otherwise `drop_half` / `summarize` use the `ChatMessage` SystemMessage
  marker pattern.

### direct-mcp (Plan A)
- No LLM — purely exercises f1 (tools/list schema injection), f4 (tools/call
  CID strip), f5 (tool_result resource block append).
- `compact` is a documented no-op.

## Cross-cutting patterns (every adapter inherits these)

1. **httpx-shared event-hook capture** — every adapter reuses the same
   `_http_exchanges` + `_mark_exchange_start` + `_capture_mcp_op_events`
   triple. New adapters in v1.1 (e.g. n8n) should inherit this pattern.
2. **Per-turn header injection via mutable dict** — `default_headers={}` +
   closure-captured update on each turn. Survives across most frameworks
   because they all expose the underlying httpx client one way or another.
3. **Audit demux** — header-based correlation (when AGW logs request headers,
   which it doesn't yet for all routes) + time-window fallback
   (`captured_at` lexicographic compare). Both fire; dedupe via
   (phase, cid, captured_at).
4. **Cooperative abort** — runner checks `abort_event.is_set()` between
   turns; never `task.cancel()` mid-await (would corrupt framework state,
   especially MCP sessions).

## Known issues / open

- **AGW chatgpt route returns 503 on Responses payloads** — surfaced in T5
  autogen+responses smoke. Orthogonal AGW routing bug; not an adapter
  regression. Should be filed against AGW.
- **OpenAI quota-exhausted** observed in T11 live verify of `force_state_ref`.
  Test infrastructure works correctly; the verdict-(e) path can't be exercised
  against live OpenAI Responses without quota. Mock-backed runner test verifies
  the wiring end-to-end.
- **Stored trial verdicts are frozen at trial-run time** — no recompute
  endpoint. Re-running a trial for a new verdict is the workaround.
- **pytest UnraisableExceptionWarning** (asyncio subprocess cleanup, 1x per
  suite run) — cosmetic, does not affect the 117/117 pass count.

## End-of-Plan-B verification (T15)

### Test counts
- Plan A end of: 41 pytest
- Plan B T1-T15: **117 pytest, 117 pass, 6.10s wall (local)**

### Docker-compose healthcheck (T15.2)
After `docker compose down && build && up`:

| Service | Port | `/info` / `/health` |
|---|---|---|
| adapter-langchain | 5001 | 200 OK, framework=langchain 1.2.15 |
| adapter-direct-mcp | 5010 | 200 OK, framework=direct-mcp 1.0 |
| adapter-langgraph | 5011 | 200 OK, framework=langgraph |
| adapter-crewai | 5012 | 200 OK, framework=crewai 1.14.2 |
| adapter-pydantic-ai | 5013 | 200 OK, framework=pydantic-ai 1.86.0 |
| adapter-autogen | 5014 | 200 OK, framework=autogen 0.7.5 |
| adapter-llamaindex | 5015 | 200 OK, framework=llamaindex 0.14.21 |
| mcp-weather | 8001 | 200 OK |
| mcp-news | 8002 | 200 OK |
| mcp-library | 8003 | 200 OK |
| mcp-fetch | 8004 | 200 OK |
| harness-api | 8000 | 200 OK, `/matrix` returns 10 seeded rows |

All 7 adapters + 4 MCP + harness-api healthy after rebuild.

### Live trial smoke (T15.3)
3 ollama+via_agw rows kicked off against running compose stack:

| row_id | framework / api / mcp | status | turns | a | b | c | d | e | f |
|---|---|---|---|---|---|---|---|---|---|
| row-d82852fb | crewai/chat/NONE | pass | 3 | pass | pass | pass | na | na | na |
| row-9b7d7a90 | pydantic-ai/chat/NONE | pass | 3 | pass | pass | pass | na | na | na |
| row-d68d04b9 | autogen/chat/NONE | pass | 3 | pass | pass | pass | na | na | na |

(d=na because no compact turn in the default plan; e=na because api=chat not
responses; f=na because no tool_calls — MCP=NONE rows.)

### Provider availability during T15
- ollama: available (host)
- mock: available (compose-internal)
- claude / chatgpt: keys present in `.env`
- gemini: `GOOGLE_API_KEY not set in .env` (unavailable)

## Pointers

- [docs/design.md](design.md) — original design (Plan A scope)
- [docs/plans/2026-04-22-aiplay-v1-plan-a-mvp.md](plans/2026-04-22-aiplay-v1-plan-a-mvp.md) — Plan A (16 tasks)
- [docs/plans/2026-04-23-aiplay-plan-b.md](plans/2026-04-23-aiplay-plan-b.md) — Plan B (15 tasks)
- [docs/enhancements.md](enhancements.md) — post-Plan-B brainstorm parking lot (E1 OTel, E2/E3 alt markers)
- [docs/brainstorming.md](brainstorming.md) — decision log
- [docs/memory-log.md](memory-log.md) — parked items
