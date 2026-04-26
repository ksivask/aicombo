# aiplay — brainstorming

Cidgar Harness C playground — designs a multi-framework test harness for the AGW governance pipeline (cidgar feature) beyond what Harness B covers.

## Goal

Test cidgar efficacy across agent frameworks, LLM APIs, streaming modes, and server-state modes — combinations not reachable from Harness B's scripted curl-only setup. Exercise the governance pipeline through real framework code paths (conversation memory, message compaction, state round-trips) to expose latent gaps before hyperstate/SIB lands.

## Project context

- Sibling to `/my/ws/agw-gh` (AGW source) and `/my/ws/auth2v` (prod-parity auth stack).
- Inherits from `/my/ws/demo` — adapts MCP servers and agent entry points, strips auth.
- AGW itself runs as a locally-built docker image referenced by a static tag in aiplay's compose. **aiplay compose never triggers an AGW build.** User owns the build + tag lifecycle externally (mirrors auth2v's `docker-compose.agw.yml` pattern).
- Auth is explicitly out of scope for aiplay v1 — this is a governance-focused harness, not an auth re-test.

## Scope

### Frameworks (v1)
langchain, langgraph, crewai, pydantic-ai, autogen, llamaindex.
**Deferred to v1.1**: n8n (workflow platform, needs webhook-triggered workflow per combo).

### LLM APIs
- chat completions (OpenAI-compatible)
- responses (OpenAI Responses API)
- responses + conversation state (`previous_response_id` mode)
- messages (Anthropic)

### Toggles per test row
- streaming: T / F
- server_state: T / F (only meaningful for responses API)

### Providers (preference order)
Ollama → claude.ai → chatgpt → gemini. Skip to next when the earlier provider doesn't support the feature combination (e.g. responses API + state=T → chatgpt). Microsoft Copilot was initially proposed but dropped — copilot.microsoft.com has no public API and the 3-provider minimum (Ollama + chatgpt + claude) already covers all 4 target APIs.

### MCP servers (non-auth)
weather, news, library, fetch (from `/my/ws/demo`, fastmcp-based). Server-everything excluded per user ask.

## Efficacy levels (all in scope — a+b+c+d+e)

| Level | Check |
|---|---|
| (a) Presence | CID appears in AGW audit log for each turn |
| (b) Channel structure | All 3 channels behave per spec — framework transport doesn't drop them |
| (c) Multi-turn continuity | Turn N's CID == turn N+1's ingress-extracted CID across ≥3 turns |
| (d) Resilience | Simulated compaction/truncation still leaves at least one channel carrying CID |
| (e) Server-state-mode gap | Responses API + `previous_response_id` — exposes whether body-level CID propagation fails without history |

## Decisions made

| Dimension | Decision | Rationale |
|---|---|---|
| Location | `/my/ws/aiplay/` | Sibling to agw-gh and auth2v; decoupled from both |
| Matrix density | Minimum spanning (~7 rows default) + UI-extensible | User adds rows via UI as needed |
| UI tech | FastAPI backend + AG-Grid frontend + SSE | Matrix is first-class artifact; AG-Grid handles editable cells + live row state |
| State model | (Y) Stateful adapter — framework-internal state keyed by `trial_id` | Tests the framework's real memory management |
| Adapter contract | `POST /trials`, `POST /trials/{id}/turn`, `POST /trials/{id}/compact`, `GET /trials/{id}/history`, `DELETE /trials/{id}`, `GET /info` | Framework-agnostic surface; ~100-200 LOC per adapter; plural form matches harness REST convention |
| Log capture | L1 — docker logs tail with `RUST_LOG_FORMAT=json` | Zero AGW modifications; JSON-lines parsed per-line |
| **AGW image** | **Static-tagged image referenced via `image:` only; no `build:` key in compose. User builds + tags externally.** | **Mirrors auth2v `docker-compose.agw.yml`. Compose never builds AGW. Missing tag = explicit failure, not stale image.** |
| Correlation | `X-Harness-Trial-ID` + `X-Harness-Turn-ID` headers, adapters propagate | Filter AGW audit log by header |
| Auth | None | Out of scope for playground |

## Rejected / deferred options

- **n8n**: deferred to v1.1. Workflow platform, not a Python library — needs webhook-triggered workflow per config combo.
- **Streamlit UI (option A)**: rejected. Whole-app rerun model fights live log streaming.
- **FastHTML UI (option C)**: rejected. HTMX viable but lacks AG-Grid-class editable grid.
- **Stateless puppet adapter (option X)**: rejected. Harness-owned state bypasses what efficacy (c) probes.
- **Hybrid X+Y adapter (option Z)**: rejected for v1. Over-engineered.
- **L3 OTel exporter log capture**: rejected. Production-realistic but heavy.
- **L4 sidecar log proxy**: rejected. Extra container for marginal benefit.
- **Dense matrix (30-40 tests)**: rejected. High maintenance cost, marginal coverage gain.
- **ollama-pull.sh auto-bootstrap**: skipped per user ask. Ollama pre-running on host.
- **server-everything MCP**: skipped per user ask.
- **agw-gh repo as host**: rejected. Harness decoupled from AGW source for reuse.
- **auth2v repo as host**: rejected. Auth2v uses native Ollama API (not chat completions).
- **Original G1/G4 "rebuild script"**: rejected. Per user ask, aiplay compose never builds AGW — just references pre-built tag.

## Test matrix — default seeded rows

Minimum spanning cover of all axes:

| # | Framework | API | Stream | State | LLM Provider | MCP |
|---|---|---|---|---|---|---|
| 1 | langchain | chat completion | F | F | Ollama | NONE |
| 2 | langgraph | chat completion | T | F | Ollama | weather |
| 3 | crewai | messages | F | F | claude.ai | library |
| 4 | pydantic-ai | messages | T | F | claude.ai | news |
| 5 | autogen | responses | F | F | chatgpt | fetch |
| 6 | llamaindex | responses | T | T | chatgpt | weather |
| 7 | (any) | chat completion | F | F | NONE | news |

Row 7 probes the "direct MCP, no LLM" flow (f1/f4/f5 isolation). An "LLM only, no MCP" variant (f2/f3 isolation) falls out when MCP=NONE is selected on any row.

User can add rows via UI's "Add row" button.

## Invalid combination enforcement

Backend exposes `validate(row) → { disabledCells, forcedValues, runnable }`. UI calls on every cell edit.

| API | Stream | State | Providers |
|---|---|---|---|
| chat completion | T, F | **F only** (disabled) | Ollama, claude.ai, chatgpt, gemini |
| responses | T, F | T, F | chatgpt (only) |
| responses + conversation | T, F | **T only** (disabled, forced T) | chatgpt |
| messages | T, F | **F only** (disabled) | claude.ai |

Additional rules:
- LLM=NONE → API / provider / stream / state all become N/A, disabled
- LLM=NONE AND MCP=NONE → row invalid, Run button greyed
- Changing API dropdown auto-resets state + provider to defaults-valid-for-that-API

## Reuse inventory from /my/ws/demo

**Reuse as-is or with minor adaptation:**
- `mcp/weather/`, `mcp/news/`, `mcp/library/`, `mcp/fetch/` — fastmcp servers
- `agents/langgraph/main.py`, `agents/crewai/main.py`, `agents/autogen/main.py` — templates for adapter contract
- `gateway-config.yaml` — adapt for governance routes
- `multi-agent-compose.yaml` — adapt for aiplay services

**Skip:**
- `mcp/auth/`, `agents/auth/`, `keycloak/`, `test_auth_integration.sh` — auth, out of scope
- `frontend/` — replace with new AG-Grid UI
- `ollama-pull.sh` — assume Ollama pre-running
- `mcp/everything/` — server-everything excluded per user ask

## Nomenclature (terms locked)

| Term | Domain | Meaning |
|---|---|---|
| `cid` | cidgar governance | The 12-hex-char correlator injected into message bodies (spec §4.1) |
| `trial_id` | harness-C | UUID for one test-row invocation; key for adapter state |
| `turn_id` | harness-C | Per-turn UUID within a trial; AGW audit log correlation |
| `session_id` | MCP transport | `Mcp-Session-Id` header; MCP-layer concept, unrelated |
| `conv_id` | **DO NOT USE** | Forbidden — collides with cidgar's `cid` |

## P2 decisions (locked)

- **D1 — API key management**: **K1** — `/my/ws/aiplay/.env` (gitignored) + `.env.example` committed. Per-service `env_file:` in compose. No K5 UI runtime injection in v1 (keep simple). **Addendum**: `GET /providers` endpoint detects which keys are set and returns availability; LLM dropdown excludes unavailable providers at render time (greyed with reason tooltip). Adapter-layer `provider_key_missing` remains as defense-in-depth.
- **D2 — Test-result persistence**: **R3** — JSON file per trial at `./data/trials/<trial_id>.json`. No SQLite. Simpler, grep-friendly, no schema migrations. UI reads the JSON directly.
- **D3 — Concurrency**: **C1 serial default** with `MAX_CONCURRENT_TRIALS` env flag for future C2 upgrade.
- **D4 — UI layout**: AG-Grid top + detail drawer below (request/response/audit/verdicts). Approved.
- **D5 — Default Ollama model**: `qwen2.5:7b-instruct`.

## Turn plan specification

Each row has a **turn plan** — ordered list of turn specs. Default plans auto-populated from row config. User edits via JSON editor in drawer before Run.

### Turn plan JSON schema

```json
{
  "turns": [
    {"kind": "user_msg",        "content": "Hello, what tools do you have?"},
    {"kind": "user_msg",        "content": "What's the weather in Paris?"},
    {"kind": "compact",         "strategy": "drop_half"},
    {"kind": "user_msg",        "content": "And in London?"},
    {"kind": "force_state_ref", "ref_to_turn": 1},
    {"kind": "inject_ambient_cid", "cid": "ib_cafebabe1234"}
  ]
}
```

### Turn kinds

| Kind | Parameters | Effect |
|---|---|---|
| `user_msg` | `content: string` | Sends next user message, receives one assistant turn back |
| `compact` | `strategy: "drop_half" \| "summarize" \| "drop_tool_calls"` | Harness tells adapter to mutate framework-internal history |
| `force_state_ref` | `ref_to_turn: int` | (responses+state only) forces next turn to use `previous_response_id` pointing to turn N |
| `inject_ambient_cid` | `cid: string` | Pre-seeds a CID into framework state (edge case testing) |

### Default templates (by config)

| Config | Default turn plan |
|---|---|
| No MCP, any API, state=F | 3 text turns (seed → follow-up → summary) |
| With MCP (weather), state=F | t1 "hello, what tools?" (probes f1) • t2 "weather in Paris?" (probes C1) • t3 "and London?" (probes continuity) |
| With MCP, efficacy (d) on | above + `compact` between t2-t3 |
| responses + state=T | t1 seed • t2 tool call via previous_response_id • t3 follow-up via previous_response_id (probes (e)) |
| LLM=NONE + MCP | t1 direct `tools/list` • t2 direct `tools/call` |

### Execution controls

Row drawer has a **"Turn Plan"** tab alongside request/response/audit tabs. Buttons:

- **[Reset to default]** — regenerate template from current row config
- **[Add turn]** — append blank `user_msg` slot
- **[Run full plan]** — execute all turns sequentially
- **[Run next turn only]** — execute only the next un-run turn (interactive, useful for debugging)

Turn cap: **10 per trial** (configurable via settings gear) — prevents runaway token spend.

Once execution starts, turn cards appear in drawer in order. Each card: sent content → request body (expandable) → response body (expandable) → audit entries for that turn → per-turn efficacy contribution.

### Default seeded rows — turn plans

| Row | Framework/config | Default turns |
|---|---|---|
| 1 | langchain / chat / NONE MCP | 3 text turns (joke seed → follow-up → summary) |
| 2 | langgraph / chat / weather | 3 (hello+tools → Paris weather → London weather) |
| 3 | crewai / messages / library | 3 (book query text → search tool → more results) |
| 4 | pydantic-ai / messages-stream / news | 3 (news topic text → news tool → follow-up) |
| 5 | autogen / responses / fetch | 3 (tools? → fetch example.com → fetch httpbin) |
| 6 | llamaindex / resp+conv / weather | 3-4 using previous_response_id mode |
| 7 | NONE LLM / news | 2 (direct tools/list → direct tools/call) |

## Next steps

1. Close out D1-D5 with user.
2. Draft design doc at `/my/ws/aiplay/docs/design.md` section by section.
3. Writing-plans skill → `/my/ws/aiplay/docs/plans/<date>-aiplay-v1-plan.md`.
4. Subagent-driven implementation.

## AGW review-fix bundle — B-NEW-3 / I-NEW-4 / M-NEW-2 / M-NEW-5 (2026-04-23)

### B-NEW-3: mcp_marker_kind gating clarity
- **Problem:** `on_tool_call_resp` gates BOTH resource block AND text marker by `cfg.channels.resource_block`. Operator who sets `resource_block: false, mcp_marker_kind: text` silently gets nothing.
- **Decision:** Pin gating with regression test + add spec doc explaining the historical name (`resource_block` = master "emit MCP marker" toggle). Future E17 may rename to `enable_mcp_markers`.
- **Why not flip the gate:** that would change semantics. Better to document the contract and tighten the test net.

### I-NEW-4: Raw fallback warn throttle
- **Alternatives considered:**
  - 1-in-N sample (e.g., AtomicU64 counter, log if `n.fetch_add(1) % 10 == 0`)
  - First-seen warn → subsequent debug (AtomicBool flag)
- **Decision:** First-seen warn + debug. Operators want to KNOW fallback happened once; the volume from langchain stateless multi-turn is the problem, not the existence. 1-in-N pattern would still log periodically and add operator confusion.

### M-NEW-2: Bedrock Raw conversion error test
- **Problem:** `bedrock.rs:1611-1619` returns `UnsupportedConversion` for `InputCompat::Raw`. No regression test.
- **Decision:** Add test pinning the clean-error path so a future refactor doesn't silently break. Use the same body shape that triggers Raw fallback in existing `responses.rs::tests`.

### M-NEW-5: Byte-equality round-trip for number formats
- **Concern:** serde_json::Value normalizes some number formats (`1.0` → `1`, large integers may overflow to f64). For OpenAI byte-passthrough this could cause upstream sensitivity issues.
- **Decision:** Add test that asserts round-trip equality on a body with `temperature: 1.0` and large `max_tokens`. If FAILS, that's a real finding worth surfacing.

## aiplay review-fix bundle — B-NEW-1 / B-NEW-2 (2026-04-23)

### B-NEW-1: autogen + llamaindex `_compact_responses()` misleading +conv note
- **Problem:** Both adapters fell through to chain-trim logic for `api=responses+conv` mode and emitted `note: "compacted _response_history chain instead"` — false. In +conv mode `_response_history` is intentionally empty (continuity lives server-side in the OpenAI Conversations container).
- **Decision:** Mirror langchain's existing +conv early-return branch. Honest no-op with note referencing the conversation container.
- **Tests:** 6 new regression tests (3 strategies × 2 adapters). Pytest 224 → 230.

### B-NEW-2: `_ensure_conversation_id()` no asyncio.Lock (4 adapters)
- **Concern:** Two concurrent +conv turns on the same Trial could race the `if-None` mint check and BOTH issue `POST /v1/conversations`, leaking one container.
- **Decision:** Defensive comment naming E18 (concurrent trials) as the future trigger for asyncio.Lock protection. Single-trial-at-a-time runner invariant is the current safety net. No behavior change.

### Deferred from this bundle
- **I-NEW-2:** autogen `force_state_ref()` dead-code verification — Subagent A hit rate limit before tracing the call graph. Method exists at `framework_bridge.py:668` and IS the setter for `_forced_prev_id` (used at line 614 during turn dispatch). Question is whether runner.py wiring actually invokes it with the right type. Follow-up needed.
- **I-NEW-1:** NOTE registry derive from `GET /info` — was Subagent C scope, never dispatched (rate limit).
- **I-NEW-3:** `tests/test_note_registry.py` (5 spot-checks) — was Subagent C scope, never dispatched.

### Bundle commits
- `0049279` (aiplay/main) — aiplay B-NEW-1 + B-NEW-2
- `ae54489b` (agw cidgar/feat/cidgar) — AGW B-NEW-3 + I-NEW-4 + M-NEW-2 + M-NEW-5
- `125abcf6` (agw docs/ibfork/docs) — spec §14.6 gating note + CHG-242/243 ledger
