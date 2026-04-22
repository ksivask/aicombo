# aiplay — Design

**Feature:** Cidgar Harness C — multi-framework test playground for the AGW governance pipeline.

**Goal:** Exercise cidgar (CID + GAR injection) through real agent-framework code paths across 6 frameworks, 4 LLM APIs, 5 providers, streaming on/off, server-state on/off — to verify efficacy levels (a) presence, (b) channel structure, (c) multi-turn continuity, (d) compaction resilience, (e) server-state-mode gap.

**Relation to other artifacts:** Complements Harness B (minimal scripted curl harness inside agw-gh, 23/23 passing). Harness A (auth2v production-parity) is skipped because auth2v uses native Ollama API, not OpenAI-compat chat completions.

---

## 1. Architecture

Four-layer design, all running in a single `docker-compose.yaml` on one host:

```
┌─────────────────────────────────────────────────────────┐
│                  Browser (user)                         │
│         AG-Grid matrix + detail drawer + SSE            │
└─────────────────────┬───────────────────────────────────┘
                      │ HTTP + SSE
┌─────────────────────▼───────────────────────────────────┐
│  harness-api (FastAPI)                                  │
│  - /trials, /trials/{id}/run, /trials/{id}/turn/next    │
│  - /audit/stream (SSE) — AGW audit log demuxed by       │
│    X-Harness-Trial-ID header                            │
│  - /validate (row validity checks)                      │
│  - /info (framework adapter discovery)                  │
│  - Persistence: JSON per trial under ./data/trials/     │
└──┬──────────────────────────────────────────────────────┘
   │ HTTP (drive adapter)                  │ Docker logs tail
   │                                       │ (RUST_LOG_FORMAT=json)
┌──▼──────────────────────────────────────┐│
│  Framework adapters (6 services)        ││
│  - adapter-langchain    :5001          ││
│  - adapter-langgraph    :5002          ││
│  - adapter-crewai       :5003          ││
│  - adapter-pydantic-ai  :5004          ││
│  - adapter-autogen      :5005          ││
│  - adapter-llamaindex   :5006          ││
│                                         ││
│  Each exposes: POST /trials, /trials/{id}/  ││
│  turn, /trials/{id}/compact, GET /info... ││
│                                         ││
│  All OUTBOUND traffic points at AGW:    ││
│    LLM_BASE_URL   = http://agw:8080/llm/<provider>/  ││
│    MCP_BASE_URL   = http://agw:8080/mcp/<server>     ││
│  Never points at providers or MCPs directly.         ││
└──┬────┬─────────────────────────────────┘│
   │    │   Both legs carry the correlation headers:    │
   │    │     X-Harness-Trial-ID                       │
   │    │     X-Harness-Turn-ID                        │
   │    │                                               │
   │    │ (leg 1 — agent→LLM)  (leg 2 — agent→MCP)     │
   ▼    ▼                                               │
┌─────────────────────────────────────────┐             │
│  agentgateway (cidgar branch)           │             │
│  - external image, static tag           │             │
│  - GOVERNED routes (cidgar policy):     │             │
│      /llm/ollama,claude,chatgpt,gemini  │             │
│      /mcp/weather,news,library,fetch    │             │
│  - governance fires on BOTH directions: │             │
│      LLM leg  → f2/f3 (Completions /    │             │
│                 Messages / Responses)   │             │
│      MCP leg  → f1 (tools/list schema   │             │
│                 inject), f4 (tools/call │             │
│                 strip), f5 (tool_result ├─────────────┘
│                 resource block append)  │  stderr → JSON logs
│  - mirrors auth2v's proxy pattern       │  consumed by harness
│    where AGW is the only egress path    │  via Docker SDK
└──┬──────────────────────────────────┬───┘
   │ proxied LLM call                 │ proxied MCP call
   ▼                                  ▼
┌─────────────────────────────────────┐   ┌────────────────────────┐
│  LLM providers                      │   │  MCP servers           │
│  - Ollama (host.docker.internal:11434) │  - weather (fastmcp)   │
│  - claude.ai (api.anthropic.com)    │   │  - news    (fastmcp)   │
│  - chatgpt (api.openai.com)         │   │  - library (fastmcp)   │
│  - gemini (generativelanguage...)   │   │  - fetch   (fastmcp)   │
│                                     │   │  (each listens on its  │
│                                     │   │   own port on compose  │
│                                     │   │   network — NOT exposed│
│                                     │   │   to host / adapters)  │
└─────────────────────────────────────┘   └────────────────────────┘
```

### Core design principles

1. **Cidgar correctness is verified externally, not self-reported.** The harness never asks AGW "did you inject a CID?" — it reconstructs the answer from AGW's audit log (stderr JSON) and the observed request/response bodies. AGW has no idea the harness exists.

2. **Per-framework adapters own their framework's state.** The harness holds nothing framework-specific. Adapters encapsulate `langchain.ConversationBufferMemory`, `crewai.Crew`, `llamaindex.ChatStore`, etc. — whatever is idiomatic.

3. **Correlation via headers, not timestamps.** Every adapter → AGW HTTP call carries `X-Harness-Trial-ID` + `X-Harness-Turn-ID`. AGW captures request headers in its audit entries. Harness filters audit stream by these headers. Concurrent trials are demuxed deterministically.

4. **Compose-local networking, no external services required.** Ollama runs on the host; everything else is containerized on `aiplay-network`. claude.ai, chatgpt, and gemini are reached via public HTTPS — no proxy needed.

5. **Zero AGW modifications.** The harness works against any cidgar-built AGW image. No feature flags, no conditional compilation, no admin API surface added for the harness. The static-tag discipline (auth2v pattern) enforces this — the image is whatever the user built externally.

6. **Two first-class routing modes: `via_agw` (default) and `direct` (baseline).** Each trial row picks one via the `Routing` matrix column. A single shared compose-level default (`AIPLAY_DEFAULT_ROUTING`, default `via_agw`) seeds new rows; per-row override always wins.

   - **`via_agw`** — adapters route LLM and MCP through AGW. All 5 governance hooks (f1/f2/f3/f4/f5) fire. Correlation headers propagate. Efficacy verdicts (a-e) are computed. This is the default and the primary use case.
   - **`direct`** — adapters reach LLM providers and MCP servers directly. AGW is not in the path. No governance hooks fire. Correlation headers are omitted. Efficacy verdicts short-circuit to `N/A — baseline`. Used as a comparison reference: run the same config twice (once via_agw, once direct), diff the trial JSONs to see exactly what cidgar adds/changes.

   Enforcement shifts from network-level to config-level: MCP services are resolvable from adapter containers in this topology (compose network allows it), and adapters read BOTH base URLs at startup (AGW-pointed and direct-pointed). The adapter picks per-turn based on the row's routing field. Tradeoff: loses the "impossible to bypass by misconfiguration" invariant. Gains: zero-restart A/B comparisons — the defining workflow for cidgar visibility. A **"Clone for baseline"** row action generates the paired direct-routing sibling with identical config, enabling one-click A/B.

   Why this matters: the primary question users ask is "what does cidgar actually do?" The cleanest answer is a side-by-side diff of two trial JSONs from identical turn plans — one mutated by governance, one unmutated. Making both modes first-class in the matrix column is the shortest path to that answer.

### Non-goals

- Not a performance benchmark (no timing assertions, no throughput measurements in v1).
- Not a regression suite for AGW itself (that's Harness B + unit tests).
- Not an auth integration test (auth2v owns that).
- Not a production deployment. Runs on developer machines only. No horizontal scaling story.

---

## 2. Components

Six logical components; each maps to its own directory under `/my/ws/aiplay/`.

### 2.1 `harness/` — FastAPI backend

**Purpose:** Single authoritative coordinator. Owns trial lifecycle (create, run, persist, serve), drives adapters, ingests AGW audit log, computes efficacy verdicts, serves the frontend.

**Key files:**
```
harness/
├── api.py               # FastAPI app: routes + SSE endpoints
├── trials.py            # Trial + Turn dataclasses, JSON persistence
├── runner.py            # Turn-plan executor (drives adapter → captures response → pulls audit)
├── validator.py         # validate(row) → disabled cells, forced values
├── audit_tail.py        # Docker logs subscriber, JSON parser, trial-id demuxer
├── efficacy.py          # 5 verdict functions: presence, channel_struct, continuity, resilience, state_mode_gap
├── adapters_registry.py # Map framework → adapter URL, handles /info discovery
├── templates.py         # Default turn-plan templates by config
└── main.py              # Entrypoint
```

**HTTP surface** (all JSON unless noted):
| Method + path | Purpose |
|---|---|
| `GET  /matrix` | Current rows (trial configs + last-run status) |
| `POST /matrix/row` | Create new row; returns row_id |
| `PATCH /matrix/row/{id}` | Update row config (triggers `validate`) |
| `DELETE /matrix/row/{id}` | Remove row |
| `POST /validate` | `{row_config} → {disabled_cells, forced_values, runnable}` |
| `POST /trials/{row_id}/run` | Execute full turn plan; streams progress via SSE |
| `POST /trials/{row_id}/turn/next` | Execute next un-run turn (interactive) |
| `GET  /trials/{trial_id}` | Full trial record (turns, audit, verdicts) |
| `GET  /trials/{trial_id}/stream` | SSE — live updates during execution |
| `GET  /audit/stream` | SSE — raw AGW audit entries (trial-id demuxed) |
| `GET  /info` | Enumerate discovered adapters + their capabilities |
| `GET  /providers` | Return `{providers: [{id, name, available, unavailable_reason}]}` — drives the LLM dropdown population based on key detection |
| `GET  /`, `/static/*` | Serve frontend |

**Estimated size:** ~800-1000 LOC.

**Dependencies:** `fastapi`, `uvicorn`, `httpx` (async client for adapters), `sse-starlette`, `docker` (SDK for log tailing), `pydantic` (models).

### 2.2 `frontend/` — AG-Grid + vanilla JS

**Purpose:** Matrix table UI + detail drawer. No build step. No framework.

**Key files:**
```
frontend/
├── index.html       # Single page; loads AG-Grid CDN + app.js
├── app.js           # Grid setup, SSE subscriptions, row-edit handlers
├── drawer.js        # Detail drawer: tabs (Request/Response/Audit/Verdicts/Plan)
├── validate.js      # Client-side helpers around /validate responses
├── turn-plan.js     # Turn-plan JSON editor + controls
└── style.css        # Theming, pill styles, drawer layout
```

**Grid columns:** `#`, Framework, API, Stream, State, LLM, MCP, **Routing**, Status, Verdicts (5 mini-pills), Actions.

**Routing column:** dropdown `via_agw | direct`. Default from compose env `AIPLAY_DEFAULT_ROUTING`. When `direct` is selected, the 5 verdict pills render as `—` (N/A — baseline) and the detail drawer shows a "Baseline run — cidgar not in path" banner at the top.

**Row actions (right-side action column):** `[▶ Run]  [⏭ Next turn]  [⎘ Clone for baseline]  [⏹ Abort]  [✕ Delete]`. The clone action duplicates the row with `routing=direct`, enabling one-click A/B pairing. Abort is enabled only while the row's trial is `running` — clicking cancels the in-flight trial: harness sends `DELETE /trials/{trial_id}` to the adapter (adapter aborts any in-flight HTTP to LLM/MCP, tears down framework state), persists trial with `status=aborted`, emits SSE `trial_done` with `status=aborted`.

**Interaction contract:**
- Cell edit → debounced `/validate` → update disabled/forced styling in-place
- Row Run click → `POST /trials/{id}/run` → subscribe SSE for live status
- Row click → open drawer, fetch `/trials/{trial_id}`
- Drawer turn-plan edit → PATCH trial config before Run

**Estimated size:** ~500-700 LOC total (HTML+CSS+JS), mostly event handlers.

**Dependencies:** AG-Grid Community (CDN, free), no bundler, no npm.

### 2.3 `adapters/<framework>/` — Framework adapters (6)

**Purpose:** Thin HTTP wrappers exposing the common adapter contract over each framework's native API. Each runs as its own container, its own port.

**Per-adapter shape (identical across frameworks):**
```
adapters/<framework>/
├── main.py              # FastAPI/Flask app with common endpoints
├── framework_bridge.py  # Adapter logic — how this framework handles a turn
├── compact.py           # Framework-specific compaction strategy implementations
├── requirements.txt     # langchain / crewai / autogen / etc
└── Dockerfile
```

**Common endpoints** (§4 details the contract):
- `POST /trials` — start a conversation with given config; return `trial_id`
- `POST /trials/{id}/turn` — drive one turn; return `{assistant_msg, tool_calls[], turn_id}`
- `POST /trials/{id}/compact` — mutate framework-internal history
- `POST /trials/{id}/force_state_ref` — set next turn's `previous_response_id` (responses+state only)
- `POST /trials/{id}/inject_ambient_cid` — pre-seed CID into framework state
- `GET  /trials/{id}/history` — raw framework dump for debugging
- `DELETE /trials/{id}` — cleanup
- `GET  /info` — `{framework, version, supports: {apis, streaming, state_modes, compact_strategies}}`

**Adapter egress configuration:** Each adapter reads FOUR base URLs at startup — AGW-pointed and direct-pointed for both LLM and MCP:

```
AGW_LLM_BASE_URL    = http://agentgateway:8080/llm/<provider>/
DIRECT_LLM_BASE_URL = https://api.openai.com/v1 | https://api.anthropic.com | https://generativelanguage.googleapis.com/v1beta | http://host.docker.internal:11434/v1

AGW_MCP_BASE_URL    = http://agentgateway:8080/mcp/<server>
DIRECT_MCP_BASE_URL = http://mcp-<server>:80xx/mcp
```

Per turn, the harness passes `routing: "via_agw" | "direct"` in the `/trials/{id}/turn` request. The adapter picks the matching pair and builds its LLM client / MCP client against it. API keys for paid providers are injected at the adapter (not AGW) because SDK clients need them client-side — but this is invisible to the routing choice.

**Header propagation:** correlation headers (`X-Harness-Trial-ID`, `X-Harness-Turn-ID`) are always emitted by the adapter. AGW consumes them in `via_agw` mode; providers/MCPs harmlessly ignore them in `direct` mode. No conditional header logic in adapter code.

**Framework-specific size estimates:**
| Adapter | Core framework LOC | Bridge LOC | Notes |
|---|---|---|---|
| langchain | ~50 | ~100 | ConversationBufferMemory; chat completion only |
| langgraph | ~80 | ~120 | StateGraph; chat completion only; streaming via AsyncGenerator |
| crewai | ~60 | ~150 | Crew + Agent + Task; chat + messages |
| pydantic-ai | ~70 | ~130 | Agent with message_history; chat + messages + responses |
| autogen | ~100 | ~180 | AssistantAgent + UserProxyAgent; chat + messages + responses |
| llamaindex | ~80 | ~160 | ChatStore + OpenAIResponsesAgent (or ReActAgent); chat + responses |

**Total adapter code:** ~1200 LOC across 6 services.

### 2.4 `mcp/<name>/` — MCP servers (4)

**Purpose:** Provide tools for MCP test flows (f1/f4/f5 hooks). Reused from `/my/ws/demo` with minimal adaptation.

**Imported as-is:**
```
mcp/
├── weather/    # get_weather(city) → weather report
├── news/       # get_news(topic) → news summaries
├── library/    # get_book_by_title(title, limit), search_books
└── fetch/      # fetch(url, max_length) → web content
```

**No auth, no extra security.** Each is a fastmcp server listening on its own port, exposing streamable-http transport at `/mcp`.

**Network topology:** In compose, MCP services are on `aiplay-network` and reachable from both `agentgateway` AND adapter services. Their ports are NOT mapped to the host (no external exposure). This is the trade-off for supporting the `routing: direct` baseline mode — adapters need compose-internal name resolution for MCPs.

- In `via_agw` mode: adapters resolve `agentgateway:8080` and call `/mcp/<server>`. AGW proxies to the MCP service and governance fires.
- In `direct` mode: adapters resolve `mcp-<server>:80xx` directly. AGW is not involved.

The "AGW as sole egress" property is enforced by the adapter's runtime config (routing choice), not by network topology. Principle #6 explains this trade-off.

**Key change from demo:** None in the MCP server code — same fastmcp servers. Networking adapted to permit adapter→MCP direct reach for baseline mode. demo's `mcp/auth/` is excluded.

### 2.5 `agw/` — Gateway config only

**Purpose:** Gateway config YAML for cidgar routes + governance policies. The AGW binary/image is external.

```
agw/
└── config.yaml       # all routes have cidgar governance policy blocks
                      #
                      # LLM routes (governance on f2/f3 LLM hooks):
                      #   /llm/ollama/      → host.docker.internal:11434
                      #   /llm/claude/      → api.anthropic.com:443     (ai.routes map for /v1/messages)
                      #   /llm/chatgpt/     → api.openai.com:443
                      #   /llm/gemini/      → generativelanguage.googleapis.com:443
                      #
                      # MCP routes (governance on f1/f4/f5 MCP hooks):
                      #   /mcp/weather     → mcp-weather:8001 (compose-internal)
                      #   /mcp/news        → mcp-news:8002
                      #   /mcp/library     → mcp-library:8003
                      #   /mcp/fetch       → mcp-fetch:8004
                      #
                      # Per spec §5, Anthropic routes require ai.routes mapping
                      #   "/v1/messages": messages
                      #   "/v1/messages/count_tokens": anthropicTokenCount
                      # without which AGW defaults to Completions parser and the
                      # governance walker never fires for Messages shape.
```

Referenced by compose as: `volumes: [./agw/config.yaml:/config/gateway-config.yaml:ro]`.

**No AGW source code here.** Image is `agentgateway:cidgar` (user-built externally per auth2v pattern).

### 2.6 `data/` — Runtime artifacts

**Purpose:** Persist trial runs as JSON (R3, per decision D2). Survives container restart.

```
data/
└── trials/
    ├── 8f3e1a2b-....json   # full trial record — config + turns + audit slices + verdicts
    ├── 2a4c6f9d-....json
    └── ...
```

**JSON schema per trial** (simplified):
```json
{
  "trial_id": "8f3e1a2b-...",
  "created_at": "2026-04-22T14:23:10Z",
  "config": {
    "framework": "autogen",
    "api": "responses",
    "stream": false,
    "state": false,
    "llm": "chatgpt",
    "mcp": "fetch",
    "routing": "via_agw"
  },
  "paired_trial_id": "2a4c6f9d-...",   // set when cloned-for-baseline; null otherwise
  "turn_plan": {"turns": [{"kind": "user_msg", "content": "..."}, ...]},
  "status": "pass",
  "started_at": "2026-04-21T14:23:12Z",
  "finished_at": "2026-04-21T14:23:24Z",
  "turns": [
    {
      "turn_id": "...",
      "turn_idx": 0,
      "kind": "user_msg",
      "request": {...},
      "response": {...},
      "audit_entries": [...]
    }
  ],
  "verdicts": {
    "a": {"verdict": "pass", "reason": "CID present in all 6 audit entries"},
    "b": {...}, "c": {...}, "d": {...}, "e": {...}
  }
}
```

**Size discipline:** Large tool_result bodies are truncated at 8 KB with a marker; full bodies logged to a sibling `bodies/` file if needed. Keeps per-trial JSON under ~100 KB typical.

### 2.6.1 Provider availability logic

`GET /providers` endpoint on the harness-api returns the currently-usable LLM providers, filtered by `.env` key detection:

```python
def get_providers():
    return [
        {"id": "NONE",     "name": "NONE (direct MCP only)", "available": True},
        {"id": "ollama",   "name": "Ollama (local)",         "available": True},  # no key; assumed reachable
        {"id": "claude",   "name": "claude.ai (Anthropic)",  "available": bool(os.environ.get("ANTHROPIC_API_KEY")),  "unavailable_reason": "ANTHROPIC_API_KEY not set in .env"},
        {"id": "chatgpt",  "name": "chatgpt (OpenAI)",       "available": bool(os.environ.get("OPENAI_API_KEY")),     "unavailable_reason": "OPENAI_API_KEY not set in .env"},
        {"id": "gemini",   "name": "gemini (Google)",        "available": bool(os.environ.get("GOOGLE_API_KEY")),     "unavailable_reason": "GOOGLE_API_KEY not set in .env"},
    ]
```

Called by the frontend on page load and periodically (every 30s per `PROVIDERS_REFRESH_MS`). Also triggered on Settings panel close.

Ollama's availability is currently always `True` — no key required, and harness doesn't liveness-probe Ollama at each dropdown render (too chatty). Ollama-unreachable errors surface at trial time as a regular connection failure, handled per §9.10.

### 2.7 Shared infrastructure

- `docker-compose.yaml` — declarative topology; all services on `aiplay-network` bridge
- `.env` — API keys (gitignored); `.env.example` as template
- `scripts/` — small helpers (e.g. `check-agw-image.sh` verifies the static tag exists; `reset-trials.sh` clears `data/trials/`)
- `Makefile` — `up`, `down`, `logs`, `reset` targets for convenience

---

## 3. Data flow

Walk through of a single trial, end-to-end, under `routing=via_agw` mode. Differences for `direct` mode are called out inline.

### 3.1 Trial creation

1. User edits a row in the grid. Frontend calls `PATCH /matrix/row/{id}` with the new config.
2. Harness calls `POST /validate` internally, returns updated disabled cells + forced values. Frontend re-renders the row.
3. Row exists only in matrix state (ephemeral in-memory on the harness side, persisted to `data/matrix.json` on every change). No `trial_id` yet.
4. User clicks **[▶ Run]**. Frontend calls `POST /trials/{row_id}/run`.

### 3.2 Trial start — sequential setup

Harness:
1. Generates `trial_id = uuid4()`. Stamps `created_at`, `started_at`.
2. Writes initial trial JSON to `data/trials/{trial_id}.json` with `status="running"`.
3. Picks the correct adapter based on `config.framework` (looked up via `adapters_registry`).
4. Opens two concurrent tasks:
   - Audit subscription: `audit_tail.subscribe(trial_id)` — adds this `trial_id` to the demux filter; any AGW audit line whose `X-Harness-Trial-ID` matches gets forwarded to this trial's event stream.
   - SSE stream: `/trials/{trial_id}/stream` is opened by the frontend to receive live updates.
5. Calls adapter: `POST http://adapter-<framework>:5000/trials` with:
   ```json
   {
     "trial_id": "...",
     "config": {api, llm, mcp, stream, state, routing}
   }
   ```
6. Adapter builds its framework-native state (e.g. `ConversationBufferMemory`, `StateGraph`, `Crew` instance) keyed internally by `trial_id`. Returns `{ "ok": true }`.

### 3.3 Turn loop

For each turn in `turn_plan.turns`:

**If `kind == user_msg`:**

1. Harness picks `turn_id = uuid4()`.
2. Calls `POST http://adapter-<framework>:5000/trials/{trial_id}/turn` with:
   ```json
   {
     "turn_id": "...",
     "user_msg": "What's the weather in Paris?"
   }
   ```
3. Adapter drives its framework to produce one **complete assistant turn**, which may include multiple LLM round-trips internally (model emits tool_call → adapter executes via MCP → result fed back to model → model emits continuation → repeat until framework's internal loop settles on a final text response). All internal LLM and MCP calls carry the same `X-Harness-Turn-ID` header — they're all part of this one harness-level turn. The adapter only returns when the framework's agent-loop terminates naturally (model produces non-tool-call response, or framework's max-iteration limit hit).
   - LLM base URL selected based on trial's `routing`
   - MCP base URL selected based on trial's `routing`
   - Headers `X-Harness-Trial-ID` + `X-Harness-Turn-ID` injected into the framework's LLM client (`additional_kwargs`, `default_headers`, custom `httpx.Client`, etc — per-framework)
4. Depending on configured `api`:
   - `chat completion` / `responses` / `responses+conv` → OpenAI-compat client library
   - `messages` → Anthropic library
5. The framework-internal flow emits one or more HTTP calls — all transit AGW under `via_agw` (f1/f2/f3/f4/f5 fire as applicable), or go direct under `direct`.
6. Adapter returns:
   ```json
   {
     "turn_id": "...",
     "assistant_msg": "It's 18°C and cloudy in Paris.",
     "tool_calls": [{"name": "fetch_weather", "args": {...}}],
     "request_captured": {...},   // what adapter actually sent to LLM / MCP (post-framework serialization)
     "response_captured": {...}   // raw response received
   }
   ```
7. Harness persists the turn record. Emits SSE event to frontend: `{"event": "turn_complete", "turn_idx": N, "turn_id": ...}`.

**If `kind == compact`:**

1. Harness calls `POST http://adapter-<framework>:5000/trials/{trial_id}/compact` with `{strategy: "drop_half"}`.
2. Adapter mutates framework-internal history per strategy. Returns `{ "history_len_before": N, "history_len_after": M }`.
3. Harness records the action as a pseudo-turn with `kind="compact"` (no request/response, just the before/after sizes).

**If `kind == force_state_ref`:**

1. Sets adapter's next-turn state-ref override — adapter stores `force_previous_response_id_from_turn = N` on its per-trial state.
2. Next `user_msg` turn will pull the response_id from turn N's response and pass it on the LLM call instead of the most recent.

**If `kind == inject_ambient_cid`:**

1. Adapter splices a synthetic assistant message into framework history — one with a text-marker CID embedded (`<!-- ib:cid=ib_cafebabe1234 -->`).
2. No network call. Records as pseudo-turn.

### 3.4 Audit ingestion (concurrent with turn loop)

Independent of the turn loop:

1. `audit_tail` task subscribed to `docker logs -f agentgateway` (via Docker SDK) from `trial.started_at`.
2. Each stderr line is parsed as JSON (since `RUST_LOG_FORMAT=json` is set on the AGW container).
3. Filter: `target == "agentgateway::governance"` AND request headers contain `X-Harness-Trial-ID == current trial_id`.
4. Matching entries are appended to `trial.audit_entries[]`.
5. Emitted as SSE `{"event": "audit_entry", "entry": {...}}` to frontend.

Under `routing=direct`, audit_tail produces zero entries for the trial (AGW is not in the path). The trial proceeds; verdicts short-circuit.

### 3.5 Trial completion

After the last turn plan entry:

1. Harness waits up to `trial_completion_grace` (default 3 seconds) for stragglers from audit_tail.
2. Harness calls `DELETE http://adapter-<framework>:5000/trials/{trial_id}` — adapter tears down framework state.
3. Harness runs verdict computation (§7) over `trial.turns` + `trial.audit_entries`:
   - Under `via_agw`: computes a through e.
   - Under `direct`: all five set to `{"verdict": "n/a", "reason": "baseline — cidgar not in path"}`.
4. Stamps `finished_at`, `status = pass | fail | error`.
5. Writes final trial JSON.
6. Emits SSE `{"event": "trial_done", "status": ..., "verdicts": {...}}`.
7. Unsubscribes audit_tail.

### 3.6 Interactive mode ("Run next turn")

Instead of looping, harness executes exactly one turn plan entry, persists it, and returns to idle. Next click on `[⏭ Next turn]` resumes from the next un-run turn_idx. Intermediate state survives across clicks because the adapter's per-trial state persists in the adapter process until `DELETE` is called.

If the user navigates away or closes the browser, the trial remains "partial" (adapter state retained, trial JSON flagged `status=paused`). Resumable on reopen by any client.

### 3.7 Paired A/B runs (clone-for-baseline)

1. User clicks **[⎘ Clone for baseline]** on a row with `routing=via_agw`.
2. Harness creates a new row with identical config but `routing=direct`. Links both via `paired_trial_id` on both trial JSONs once each is run.
3. Both rows can be [Run] independently; harness executes the same turn plan on both. User can then view side-by-side diff.

### 3.8 Header propagation responsibility

| Component | Action |
|---|---|
| harness-api | Generates `trial_id` + `turn_id`. Passes both to adapter in every turn call. |
| adapter | Propagates both as `X-Harness-Trial-ID` and `X-Harness-Turn-ID` headers on EVERY outbound HTTP (LLM + MCP). Mechanism is per-framework (`default_headers`, `additional_kwargs`, custom `httpx.Client`). |
| AGW (`via_agw` only) | Captures request headers in the audit entry per existing governance log.rs behavior. No changes needed — headers are already in scope for the log entry. |
| LLM providers / MCP servers | Receive the headers, ignore them. No side effects. |

Under `direct` mode, headers are still emitted by the adapter; they land on providers/MCPs which ignore them. No conditional logic needed.

### 3.9 Error paths

Per-turn failures are captured but don't abort the trial:
- Adapter error (framework exception, network fail) → `turn.error = {...}`, harness continues with next turn.
- AGW returns non-2xx → adapter records it, returns to harness as the `response_captured` with `status_code`.
- audit_tail dropout (docker SDK disconnect) → harness logs warning, attempts re-subscribe; verdicts degrade to "insufficient data" if gaps detected.

Trial-level failures (adapter unreachable, malformed turn plan) → `trial.status = "error"`, `error_reason` filled, no verdicts computed.

---

## 4. Adapter contract

The common HTTP surface every framework adapter exposes. Request and response schemas are identical across frameworks; only the implementation behind the endpoint varies.

All endpoints return `application/json` unless noted. Errors use HTTP status + `{"error": "...", "detail": "..."}` body.

### 4.1 `GET /info`

Static capabilities of this adapter. Called by harness on startup for discovery.

Request: none.

Response:
```json
{
  "framework": "autogen",
  "version": "0.4.2",
  "supports": {
    "apis": ["chat", "messages", "responses"],
    "streaming": true,
    "state_modes": ["stateless", "responses_previous_id"],
    "compact_strategies": ["drop_half", "drop_tool_calls"]
  },
  "default_ollama_model": "qwen2.5:7b-instruct"
}
```

Harness uses `supports` to fill the dropdown options and validate per-row combinations.

### 4.2 `POST /trials`

Create a new trial. Adapter allocates framework-internal state keyed by the returned `trial_id`.

Request:
```json
{
  "trial_id": "8f3e1a2b-...",
  "config": {
    "api": "responses",
    "stream": false,
    "state": false,
    "llm": "chatgpt",
    "model": "gpt-4o",
    "mcp": "fetch",
    "routing": "via_agw"
  }
}
```

Response:
```json
{ "ok": true, "trial_id": "8f3e1a2b-..." }
```

If `api` or `state_mode` isn't supported by this adapter: `400 Bad Request` with `error: "unsupported_combination"`.

### 4.3 `POST /trials/{trial_id}/turn`

Drive one user turn through the framework.

Request:
```json
{
  "turn_id": "turn-002-abc",
  "user_msg": "What's the weather in Paris?"
}
```

Response:
```json
{
  "turn_id": "turn-002-abc",
  "assistant_msg": "It's 18°C and cloudy in Paris.",
  "tool_calls": [
    {"name": "get_weather", "args": {"city": "Paris", "_ib_cid": "ib_abc123def456"}},
  ],
  "request_captured": {
    "url": "http://agentgateway:8080/llm/chatgpt/v1/responses",
    "method": "POST",
    "headers": {"X-Harness-Trial-ID": "...", "X-Harness-Turn-ID": "..."},
    "body": {...}
  },
  "response_captured": {
    "status": 200,
    "headers": {...},
    "body": {...}
  },
  "framework_events": [
    {"t": "llm_call_start", "ts": 1234567890},
    {"t": "tool_call_invoked", "name": "get_weather"},
    {"t": "llm_call_end"}
  ]
}
```

`request_captured` / `response_captured` are the full HTTP payloads as seen at the framework's HTTP boundary (captured via each framework's native hooks — langchain `Callbacks`, crewai `event_bus`, OpenAI SDK `response.http_response`, etc). These are what the harness compares against AGW audit entries.

If streaming is on (`stream=true`): adapter collects the full response internally, assembles the final message, returns normally. From the harness's perspective, streaming is invisible at the contract level — the SSE-streaming behavior happens inside the adapter and is captured in `framework_events`.

If the underlying call fails: `500` with `error: "framework_error", detail: "...", partial_state: {...}`.

### 4.4 `POST /trials/{trial_id}/compact`

Mutate framework-internal history for efficacy (d) testing.

Request:
```json
{ "strategy": "drop_half" }
```

Supported strategies:
- `drop_half` — delete the oldest 50% of messages in framework memory (preserving the system prompt if any)
- `summarize` — replace oldest N messages with a one-line summary marker (framework-specific; fallback to `drop_half` if framework doesn't support summarization natively)
- `drop_tool_calls` — delete all messages whose role is `tool` or contain `tool_calls`, keeping user+assistant text only

Response:
```json
{
  "strategy": "drop_half",
  "history_len_before": 8,
  "history_len_after": 4,
  "dropped_message_indices": [0, 1, 2, 3]
}
```

### 4.5 `POST /trials/{trial_id}/force_state_ref`

Responses-API-and-state mode only. Forces the NEXT turn's `previous_response_id` parameter to be pulled from turn N instead of the latest response.

Request:
```json
{ "ref_to_turn": 0 }
```

Response:
```json
{ "ok": true, "previous_response_id": "resp_abc123", "resolved_from_turn": 0 }
```

If the adapter's current `api` isn't `responses` with `state=true`: `400 Bad Request`.

### 4.6 `POST /trials/{trial_id}/inject_ambient_cid`

Pre-seed a CID into framework state as if a prior turn carried it. Used to test cleanup behavior when an LLM emits a stale CID.

Request:
```json
{ "cid": "ib_cafebabe1234", "location": "text_marker" }
```

`location` options:
- `text_marker` — splice `<!-- ib:cid=ib_cafebabe1234 -->` into the last assistant message
- `tool_use_input` — if last assistant had a tool_use, add `_ib_cid` to its input
- `resource_block` — if last message had a tool_result, append a `gateway-meta://conv/ib_cafebabe1234` resource block

Response: `{ "ok": true, "inserted_at_message_index": 3 }`

### 4.7 `GET /trials/{trial_id}/history`

Return the raw framework-internal state for debugging. Best-effort serialization.

Response: framework-specific dump, not normalized. Example for langchain:
```json
{
  "framework": "langchain",
  "memory_type": "ConversationBufferMemory",
  "messages": [
    {"role": "human", "content": "..."},
    {"role": "ai", "content": "..."}
  ]
}
```

### 4.8 `DELETE /trials/{trial_id}`

Tear down framework state. Called by harness at trial completion.

Response: `{ "ok": true }`. Idempotent — deleting an unknown trial_id returns `{ "ok": true, "already_gone": true }` (not a 404).

### 4.9 Header propagation requirement

Every outbound LLM and MCP HTTP call made during `POST /trials/{trial_id}/turn` processing MUST carry:
```
X-Harness-Trial-ID: <trial_id>
X-Harness-Turn-ID: <turn_id>
```

Mechanism is per-framework; see §4.10. Failure to propagate = broken correlation = uncomputable efficacy verdicts. This is the single most important adapter invariant.

### 4.10 Per-framework implementation notes

| Framework | Memory/state mechanism | Streaming approach | Compaction | Header propagation |
|---|---|---|---|---|
| **langchain** | `ConversationBufferMemory` (list of `HumanMessage` / `AIMessage`) stored on adapter-side dict keyed by `trial_id` | Not tested in v1 (chat only, stream=F baseline) | `memory.chat_memory.messages = messages[N/2:]` | `ChatOpenAI(default_headers={...}, openai_api_base=...)` |
| **langgraph** | `StateGraph` with custom state dict; checkpoint via `MemorySaver` keyed by `thread_id = trial_id` | `astream(...)` AsyncGenerator; adapter collects chunks | Manipulate checkpoint state directly | `ChatOpenAI(default_headers=...)` passed into graph node |
| **crewai** | `Crew` instance per trial; `crew.memory.storage` for history | Not tested in v1 (framework-internal streaming less exposed) | `crew.memory.clear_partial(ratio=0.5)` or direct storage-backend mutation | `LLM(base_url=..., additional_headers=...)` at Crew init |
| **pydantic-ai** | `Agent.run()` with `message_history: list[ModelMessage]` slot; history owned in adapter dict | `agent.run_stream()` | Slice the `message_history` list | Custom `httpx.AsyncClient(headers={...})` passed into `Model` constructor |
| **autogen** | `AssistantAgent.chat_messages[recipient]` list per trial | `a_generate_reply(stream=True)` | Slice `chat_messages` directly | `LLMConfig(config_list=[{"base_url":..., "default_headers":...}])` |
| **llamaindex** | `ChatMemoryBuffer` or `ChatStore` keyed by `chat_store_key = trial_id` | `astream_chat()` | `memory.get() -> slice -> memory.set()` | `OpenAI(default_headers=..., api_base=...)` at LLM construction |

### 4.11 API support per framework (v1 seeded matrix)

| Framework | chat | messages | responses | responses+conv |
|---|---|---|---|---|
| langchain | ✅ | via langchain-anthropic | ❌ (no native responses support) | ❌ |
| langgraph | ✅ | via langchain-anthropic nodes | ❌ | ❌ |
| crewai | ✅ | ✅ (via litellm passthrough) | partial | ❌ |
| pydantic-ai | ✅ | ✅ | ✅ (`OpenAIResponsesModel`) | ✅ |
| autogen | ✅ | ✅ | ✅ | ✅ |
| llamaindex | ✅ | ✅ (`Anthropic`) | ✅ (`OpenAIResponses`) | ✅ (`ChatStore`) |

Seeded matrix rows are chosen to align with each framework's strongest API support:
- langchain + chat (its bread-and-butter)
- langgraph + chat + streaming (its streaming is well-tested)
- crewai + messages (Anthropic is well-supported via litellm)
- pydantic-ai + messages streaming (exercises Anthropic streaming path)
- autogen + responses (its newer `AssistantAgent` supports Responses API natively)
- llamaindex + responses + conv state (its `OpenAIResponses` integration is responses-first)

If a framework doesn't support a combination the user selects via matrix edit, `/trials` returns `400` with `error: "unsupported_combination"` and the UI shows a warning in the row status cell.

### 4.12 Adapter lifecycle

Each adapter is a long-running HTTP service. On container start:
1. Parse env vars: base URLs (AGW + direct) + API keys.
2. Start HTTP server on port `500X`.
3. Initialize `trials: dict[str, FrameworkState]` as empty dict.
4. Serve requests until SIGTERM.

No startup dependency on the harness or AGW — adapters can start before AGW is ready. First `POST /trials` that hits the adapter will construct framework state on demand.

If the adapter crashes mid-trial: trial state is lost; harness detects on next turn call (HTTP error from adapter) and marks trial `status=error`. Restart recovery is out of scope for v1.

---

## 5. UI

Single-page app served from the harness FastAPI. Three major regions:

1. **Toolbar** — top strip with `[+ Add Row]`, `[▶ Run All]`, `[▶ Run All (via_agw)]`, `[▶ Run All (direct)]`, `[⚙ Settings]` gear.
2. **Matrix grid** — AG-Grid Community edition; editable cells, inline status + verdict pills.
3. **Drawer** — expands below the grid on row click; 4 tabs.

### 5.1 Grid column specifications

| # | Header | Type | Cell editor | Cell renderer | Notes |
|---|---|---|---|---|---|
| 0 | `#` | readonly int | — | sequential number | auto-assigned |
| 1 | Framework | string | `agSelectCellEditor` | text | options from `/info` discovery across all 6 adapters |
| 2 | API | string | `agSelectCellEditor` | text | options filtered by selected framework's `supports.apis` |
| 3 | Stream | bool | `agCheckboxCellEditor` | ☐/☑/▒▒ | ▒▒ renders when API doesn't permit this toggle |
| 4 | State | bool | `agCheckboxCellEditor` | ☐/☑/▒▒/▪▪ | ▪▪ renders when API forces `true` (responses+conv) |
| 5 | LLM | string | `agSelectCellEditor` | text | options: NONE / Ollama / claude.ai / chatgpt / gemini |
| 6 | MCP | string | `agSelectCellEditor` | text | options: NONE / weather / news / library / fetch |
| 7 | Routing | string | `agSelectCellEditor` | text-with-badge | `via_agw` (default) or `direct`; direct rows get a yellow "baseline" badge |
| 8 | Status | enum | readonly | pill — ○ idle, ▶ running, ● pass, ✗ fail, ⚠ error, ⏸ paused | updated via SSE |
| 9-13 | a/b/c/d/e | enum | readonly | 5 mini-pills — ✓ pass, ✗ fail, — na (baseline or inapplicable) | updated via SSE |
| 14 | Actions | — | — | 4 buttons: `[▶]` `[⏭]` `[⎘]` `[✕]` | run / next-turn / clone-for-baseline / delete |

Column widths: sticky left group (#/Framework/API) + scrollable middle + sticky right group (Status/verdicts/Actions). Grid pins left + pins right with AG-Grid's `pinned` column option.

### 5.2 Cell validation UX

Every cell edit triggers a debounced call to `POST /validate`:

```json
Request: { "row_config": {framework: "autogen", api: "responses", stream: true, ...} }
Response: {
  "disabled_cells": ["state"],
  "forced_values": {"state": false},
  "disabled_dropdown_options": {
    "llm": [
      {"id": "chatgpt", "reason": "OPENAI_API_KEY not set in .env"},
      {"id": "gemini",  "reason": "GOOGLE_API_KEY not set in .env"}
    ]
  },
  "runnable": true,
  "warnings": []
}
```

Frontend applies changes via AG-Grid's `cellClassRules`:
- `disabled_cells` → `disabled` class → greyed bg, no click, ▒▒ visual
- `forced_values` → `forced` class → locked icon + tooltip "API requires state=true"
- `disabled_dropdown_options` → affected options in the dropdown render greyed + non-clickable with tooltip carrying the reason
- `runnable=false` → Run button greyed + tooltip showing reason

**Provider availability discovery:** On page load, frontend calls `GET /providers` once to populate the LLM dropdown. Each option carries `{id, name, available, unavailable_reason}`. Unavailable providers are rendered but disabled:

```
LLM ▾
  NONE
  Ollama             (always available; no key needed)
  claude.ai
  chatgpt            ⚠ OPENAI_API_KEY not set in .env
  gemini             ⚠ GOOGLE_API_KEY not set in .env
```

Clicking a disabled option is a no-op; hovering shows the reason tooltip. The dropdown is refetched when the Settings panel is reopened (in case user updated `.env` + restarted adapters).

Invalid edits (e.g. user somehow selects state=T while API=chat, OR picks a provider with missing key) get rolled back visually with a brief red flash + toast:
- `"chat completion doesn't support server state — resetting to F"`
- `"OPENAI_API_KEY not set; cannot select chatgpt. Edit .env and restart."`

### 5.3 Drawer — 4 tabs

Drawer opens below grid on row click, takes ~60% of viewport height. Close via `[✕]` or Esc.

**Tab 1 — Turn Plan** (editable before/between runs):
- JSON editor (CodeMirror, lightweight) with syntax highlighting + schema validation
- Buttons: `[Reset to default]`, `[+ Add user_msg turn]`, `[+ Add compact]`, `[+ Add force_state_ref]`, `[+ Add inject_ambient_cid]`
- Below editor: `[▶ Run full plan]`, `[⏭ Run next turn only]`
- Turn cap indicator: `3 / 10 turns`

**Tab 2 — Turns** (populated as trial executes):
- Vertical timeline of turn cards, one per executed turn
- Each card: turn_idx, kind, timestamp, expand toggle
- Expanded view per card: 4 collapsed sections
  - **Request** — method + URL + headers + body (JSON pretty-printed)
  - **Response** — status + headers + body
  - **AGW audit entries** — list of entries filtered by this turn's turn_id (only for via_agw)
  - **Per-turn contribution** — which efficacy level(s) this turn contributes to, with pass/fail

**Tab 3 — Verdicts** (populated at trial end):
- 5 stacked cards, one per efficacy level (a/b/c/d/e)
- Each card: level letter + name + verdict pill (✓/✗/—) + one-line reason
- For baseline (`routing=direct`) trials: banner at top of tab "Baseline run — cidgar not in path; verdicts N/A"

**Tab 4 — Raw JSON** (debugging):
- Full trial JSON dump, copy-to-clipboard button
- Shows `paired_trial_id` as link if paired (clicking opens paired trial's drawer)

### 5.4 SSE wiring

Single persistent SSE connection per open drawer at `GET /trials/{trial_id}/stream`. Event types:

| Event | Payload | Frontend action |
|---|---|---|
| `trial_started` | `{trial_id, started_at}` | Update row status pill to ▶, clear prior verdicts |
| `turn_started` | `{turn_idx, turn_id, kind, content?}` | Add new turn card to Tab 2, empty body placeholders |
| `turn_complete` | `{turn_idx, turn_id, captured: {...}}` | Fill in request/response/framework_events on turn card |
| `audit_entry` | `{turn_id, entry: {...}}` | Append to matching turn card's audit section |
| `trial_done` | `{status, verdicts}` | Update row pills, populate Tab 3 verdicts |
| `error` | `{stage, message}` | Set status to ⚠, show banner at top of drawer |
| `keepalive` | `{}` | Reset client-side timeout |

Matrix row status cell listens to a separate lightweight `GET /matrix/stream` for cross-row updates (one trial done → update that row's status even if drawer isn't open).

### 5.5 Keyboard shortcuts

- `Cmd/Ctrl + Enter` on focused row → Run
- `Esc` closes drawer
- `Cmd/Ctrl + ,` opens Settings panel
- `n` → add new row (when grid has focus)
- `Cmd/Ctrl + D` clone-for-baseline on focused row

### 5.6 Settings panel (gear icon)

Modal dialog with:
- `MAX_CONCURRENT_TRIALS` — slider 1-10
- `AIPLAY_DEFAULT_ROUTING` — radio `via_agw | direct`
- `Default Ollama model` — text input
- `Turn cap per trial` — slider 1-20 (default 10)
- `Log tail buffer size` — slider (bytes of AGW stderr to buffer)
- `API keys status` — read-only panel showing which keys are populated in `.env` (present/absent only; never values). For each absent key: one-click link to `.env.example` location + reminder text "edit .env and `docker compose restart adapter-*`". Closing this panel re-runs `/providers` discovery so the LLM dropdown reflects current state.

Changes to settings are live (affect new trials); no restart.

### 5.7 Error surfaces

All user-visible errors flow through three channels:
1. **Toast** (top-right, auto-dismiss 5s) — transient UI errors (invalid edit, save failed)
2. **Row status pill** — trial-level errors (⚠ with hover tooltip)
3. **Drawer banner** — in-drawer errors (red band at top of drawer with actionable CTA)

No console-only errors; everything is user-visible.

### 5.8 Responsive behavior

Minimum supported viewport: 1280×720 (dev-laptop common). Below that, grid becomes horizontal-scroll.

No mobile UX. This is a dev tool.

### 5.9 Accessibility

- All action buttons have `aria-label` text
- Keyboard nav through grid cells (standard AG-Grid)
- Color-coded verdicts also use distinct glyphs (✓/✗/—) so colorblind-safe

---

## 6. Compose topology

Single `docker-compose.yaml` at the project root. 12 services on one bridge network. Ollama runs externally on the host.

### 6.1 Services overview

| Service | Image | Port (host) | Port (internal) | Notes |
|---|---|---|---|---|
| `harness-api` | built from `./harness/` | **8000** | 8000 | UI + API; only host-exposed service users interact with |
| `agentgateway` | `agentgateway:cidgar` (external) | 8080, 15000 | 8080, 15000 | host-exposed for debugging/admin; adapters reach via compose DNS |
| `adapter-langchain` | built from `./adapters/langchain/` | — | 5001 | internal only |
| `adapter-langgraph` | built from `./adapters/langgraph/` | — | 5002 | internal only |
| `adapter-crewai` | built from `./adapters/crewai/` | — | 5003 | internal only |
| `adapter-pydantic-ai` | built from `./adapters/pydantic_ai/` | — | 5004 | internal only |
| `adapter-autogen` | built from `./adapters/autogen/` | — | 5005 | internal only |
| `adapter-llamaindex` | built from `./adapters/llamaindex/` | — | 5006 | internal only |
| `mcp-weather` | built from `./mcp/weather/` | — | 8001 | internal only; reachable from adapters (direct mode) and AGW (via_agw) |
| `mcp-news` | built from `./mcp/news/` | — | 8002 | internal only |
| `mcp-library` | built from `./mcp/library/` | — | 8003 | internal only |
| `mcp-fetch` | built from `./mcp/fetch/` | — | 8004 | internal only |

External: Ollama on `host.docker.internal:11434` (user manages separately per decision D5).

### 6.2 Network

Single bridge: `aiplay-network`. All 12 services on it. No split between adapters and MCPs (needed so `routing=direct` works, per §2.4).

`extra_hosts: ["host.docker.internal:host-gateway"]` on services that need Ollama (agentgateway for `via_agw`, every adapter for `direct`).

### 6.3 Full compose sketch

```yaml
# /my/ws/aiplay/docker-compose.yaml
networks:
  aiplay-network:
    driver: bridge
    name: aiplay_net

services:

  harness-api:
    build: ./harness
    ports:
      - "8000:8000"
    environment:
      - AIPLAY_DEFAULT_ROUTING=${AIPLAY_DEFAULT_ROUTING:-via_agw}
      - MAX_CONCURRENT_TRIALS=${MAX_CONCURRENT_TRIALS:-1}
      - TURN_CAP=${TURN_CAP:-10}
      - AGW_CONTAINER_NAME=agentgateway
      - AGW_ADMIN_URL=http://agentgateway:15000
      - DOCKER_SOCKET=/var/run/docker.sock
    volumes:
      - ./data:/data
      - /var/run/docker.sock:/var/run/docker.sock:ro   # for docker logs tail
      - ./frontend:/app/frontend:ro
    depends_on:
      agentgateway: {condition: service_started}
    networks: [aiplay-network]
    restart: unless-stopped

  agentgateway:
    image: agentgateway:cidgar          # user-built externally; auth2v pattern
    command: ["-f", "/config/gateway-config.yaml"]
    ports:
      - "8080:8080"
      - "15000:15000"
    environment:
      - RUST_LOG=info,agentgateway::governance=debug
      - RUST_LOG_FORMAT=json              # critical for L1 log capture
      - ADMIN_ADDR=0.0.0.0:15000
    extra_hosts:
      - "host.docker.internal:host-gateway"
    volumes:
      - ./agw/config.yaml:/config/gateway-config.yaml:ro
    networks: [aiplay-network]
    restart: unless-stopped

  adapter-langchain:
    build: ./adapters/langchain
    environment:
      - ADAPTER_PORT=5001
      - AGW_LLM_BASE_URL_OLLAMA=http://agentgateway:8080/llm/ollama/v1
      - AGW_LLM_BASE_URL_OPENAI=http://agentgateway:8080/llm/chatgpt/v1
      - AGW_LLM_BASE_URL_ANTHROPIC=http://agentgateway:8080/llm/claude
      - AGW_MCP_BASE_URL=http://agentgateway:8080/mcp
      - DIRECT_LLM_BASE_URL_OLLAMA=http://host.docker.internal:11434/v1
      - DIRECT_LLM_BASE_URL_OPENAI=https://api.openai.com/v1
      - DIRECT_LLM_BASE_URL_ANTHROPIC=https://api.anthropic.com
      - DIRECT_MCP_WEATHER=http://mcp-weather:8001/mcp
      - DIRECT_MCP_NEWS=http://mcp-news:8002/mcp
      - DIRECT_MCP_LIBRARY=http://mcp-library:8003/mcp
      - DIRECT_MCP_FETCH=http://mcp-fetch:8004/mcp
    env_file: [.env]                      # secrets: OPENAI_API_KEY, ANTHROPIC_API_KEY, GOOGLE_API_KEY
    extra_hosts:
      - "host.docker.internal:host-gateway"
    depends_on:
      agentgateway: {condition: service_started}
    networks: [aiplay-network]

  # adapter-langgraph, adapter-crewai, adapter-pydantic-ai, adapter-autogen, adapter-llamaindex
  # follow the same shape with ADAPTER_PORT=5002..5006 and matching build context.
  # Full expansion omitted for brevity.

  mcp-weather:
    build: ./mcp/weather
    environment:
      - MCP_PORT=8001
    networks: [aiplay-network]
    restart: unless-stopped

  # mcp-news (8002), mcp-library (8003), mcp-fetch (8004) follow the same shape.
```

### 6.4 Env var layering

Three layers:
1. **`.env`** (gitignored, at repo root) — secrets only: `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `GOOGLE_API_KEY`. Loaded by `env_file: [.env]` on adapter services that need LLM keys.
2. **`.env.example`** (committed) — placeholders for the above; checked into git as the template.
3. **Compose env inline** — non-secret config (base URLs, port numbers, defaults) inlined in compose under `environment:`.

Harness-api does NOT read `.env` directly — it has no LLM keys. All keys live on adapters.

### 6.5 Volumes

- `./data:/data` on harness-api — trial JSON persistence (R3 decision)
- `/var/run/docker.sock:/var/run/docker.sock:ro` on harness-api — for Docker SDK log tailing (L1 decision)
- `./agw/config.yaml:/config/gateway-config.yaml:ro` on agentgateway — gateway routes + governance
- `./frontend:/app/frontend:ro` on harness-api — served as static files

No volumes on adapter or MCP services; their state is ephemeral (framework memory in RAM).

### 6.6 Service dependencies & startup

Startup order (enforced by `depends_on` + healthchecks):
1. MCP servers start first (no dependencies)
2. `agentgateway` starts (depends on MCPs being addressable on network; no health check since AGW comes up fast)
3. All 6 adapters start (`depends_on: agentgateway`)
4. `harness-api` starts last (`depends_on: agentgateway`)

Adapters do NOT depend on MCPs directly — if an MCP is down, the failure surfaces at trial time (clean error) rather than blocking service startup.

Healthchecks (optional v1, wiring in v1.1):
- `agentgateway` — `curl -f http://localhost:15000/ready`
- `harness-api` — `curl -f http://localhost:8000/health`
- Adapter — `curl -f http://localhost:500X/info`
- MCP — no healthcheck (fastmcp doesn't expose one; first tool call probes liveness)

### 6.7 Makefile helpers

```makefile
# /my/ws/aiplay/Makefile
.PHONY: up down logs reset check-agw

up:
	docker compose up -d

down:
	docker compose down

logs:
	docker compose logs -f agentgateway harness-api

reset:
	rm -rf ./data/trials/*
	@echo "Cleared trial JSONs."

check-agw:
	@docker image inspect agentgateway:cidgar > /dev/null 2>&1 && echo "✅ agentgateway:cidgar found" \
		|| (echo "❌ agentgateway:cidgar missing — build from agw-gh worktree first" && exit 1)

up-safe: check-agw up
	@echo "Stack up. UI at http://localhost:8000"

rotate-keys:
	docker compose restart $(shell docker compose ps --services | grep ^adapter-)
	@echo "Adapters restarted; new .env keys picked up."
```

`make up-safe` fails fast with a clear message if the AGW image isn't built — mirrors the "no `build:` key" discipline from D6.

### 6.8 Teardown

```bash
docker compose down              # stops + removes containers, keeps network, keeps volumes
docker compose down -v           # +removes volumes (trial JSONs lost unless data/ is bind-mounted)
docker compose down --rmi local  # +removes built images (force rebuild next up)
```

`./data/trials/` is a bind mount to a host dir, so trial history persists across `docker compose down` by default. Only `rm -rf ./data` or explicit `make reset` clears it.

---

## 7. Efficacy verdict computation

Five verdict functions. Each consumes the trial record (turns + audit_entries) and returns `{verdict, reason}`. The full verdict set is computed once at trial completion (§3.5) and written into the trial JSON.

### 7.1 Verdict output schema

```json
{
  "a": {"verdict": "pass",    "reason": "CID ib_abc123def456 present in all 3 turns"},
  "b": {"verdict": "fail",    "reason": "Turn 2 tool_use missing _ib_cid (expected C1 per spec §4.2)"},
  "c": {"verdict": "pass",    "reason": "CID carried across turns 0→1→2 via C1 and C2"},
  "d": {"verdict": "na",      "reason": "no compact turn in plan"},
  "e": {"verdict": "fail",    "reason": "previous_response_id mode: turn 2 CID differs from turn 1 (body-level propagation broken per spec §12.1)"}
}
```

Verdict enum: `pass | fail | na | error`. `na` = not applicable for this trial config. `error` = couldn't evaluate (missing data, AGW not logging, etc). For `status=aborted` trials, all 5 verdicts return `{verdict: "na", reason: "trial aborted before completion"}`.

### 7.2 (a) Presence

**Applicable:** all `via_agw` trials with at least one `user_msg` turn.

**Algorithm:**
```
for each user_msg turn in trial.turns:
    matching_audit = [e for e in trial.audit_entries if e.turn_id == turn.turn_id and e.cid is not None]
    if len(matching_audit) == 0:
        return fail(f"Turn {turn.idx} has no audit entry with a CID")
return pass(f"CID present in all {n} turns; unique CIDs: {set_of_cids}")
```

**Edge case:** if `trial.audit_entries` is empty across all turns, returns `error` with reason "no AGW audit entries captured — check governance policy on route + RUST_LOG_FORMAT=json".

### 7.3 (b) Channel structure

**Applicable:** all `via_agw` trials.

**Algorithm:**

For each user_msg turn, determine *expected* channels based on what happened:

| Response contains | Expected channels | Verify |
|---|---|---|
| `tool_calls[]` / `tool_use` blocks | C1 | `_ib_cid` in every tool_call's args/input matches audit `cid` |
| `text` / `content` string output | C2 | `<!-- ib:cid=... -->` marker present matches audit `cid` |
| MCP `tool_result` blocks (ingress from prior tool exec) | C3 | `gateway-meta://conv/<cid>` resource block matches audit `cid` |

```
issues = []
for turn in user_msg_turns:
    expected = detect_expected_channels(turn.response_captured)
    audit_cid = turn.audit_cid()
    if "C1" in expected:
        if not channel1_in_response(turn.response_captured, audit_cid):
            issues.append(f"Turn {turn.idx}: C1 missing in tool_use/tool_calls")
    if "C2" in expected:
        if not channel2_in_response(turn.response_captured, audit_cid):
            issues.append(f"Turn {turn.idx}: C2 text marker missing")
    if "C3" in expected:
        if not channel3_in_response(turn.response_captured, audit_cid):
            issues.append(f"Turn {turn.idx}: C3 resource block missing")

return pass_or_fail(issues)
```

Shape-aware — uses Messages vs Completions body shape based on `trial.config.api`:
- `chat` / `responses` → OpenAI-shape parse (`choices[].message.tool_calls`, `choices[].message.content`)
- `messages` → Anthropic-shape parse (`content[].type == tool_use|text`)

**Edge case:** if streaming was on and adapter didn't fully reassemble the response (e.g., partial stream error), returns `error` with reason "incomplete response body — cannot verify channels".

### 7.4 (c) Multi-turn continuity

**Applicable:** `via_agw` trials with ≥2 `user_msg` turns.

**Algorithm:**
```
prior_cid = None
for idx, turn in enumerate(user_msg_turns):
    if idx == 0:
        prior_cid = turn.audit_cid()    # first turn establishes CID
        continue
    # for turns ≥ 2, ingress audit entry's cid should equal prior turn's cid
    ingress_cid = turn.audit_cid()      # extracted on /llm_request or /tools_list phase
    if ingress_cid != prior_cid:
        return fail(f"Turn {idx}: ingress cid={ingress_cid} but prior turn response cid={prior_cid}")
    prior_cid = ingress_cid
return pass(f"CID {prior_cid} carried across {n} turns")
```

**Why this works:** the ingress phase reflects what AGW *extracted* from the incoming request (spec §4.3 "last-wins CID"). If the framework preserved the CID in history across channels, extraction picks it up and audit shows the same CID. If the framework dropped all channels, AGW generates a fresh CID and the audit shows a different value — that's the fail signal.

**Edge case:** if a turn has no audit entry at all, returns `error` with reason "turn {idx} has no ingress audit entry — cannot assess continuity".

### 7.5 (d) Compaction resilience

**Applicable:** `via_agw` trials whose turn plan contains a `compact` turn.

**Algorithm:**
```
compact_idx = find_compact_turn_idx(trial.turn_plan)
pre_compact_cid = last user_msg turn before compact_idx → audit_cid()
post_compact_turn = first user_msg turn after compact_idx
post_compact_cid = post_compact_turn.audit_cid()

if post_compact_cid == pre_compact_cid:
    return pass(f"CID {pre_compact_cid} survived {compact_strategy} compaction via {detected_channel}")
else:
    surviving_channels = detect_which_channels_preserved_cid(post_compact_turn, pre_compact_cid)
    if not surviving_channels:
        return fail(f"CID lost after {compact_strategy} compaction — new session CID {post_compact_cid}")
    else:
        return fail(f"CID mismatch despite channels {surviving_channels} carrying prior cid {pre_compact_cid}")
```

The second fail branch is weird — it means the channels physically carry the right CID but AGW extracted something else. Would indicate an AGW bug. Included for defense-in-depth.

**Edge case:** if trial has a compact turn but no user_msg after it, returns `na` with reason "compact turn has no following user_msg to test against".

### 7.6 (e) Server-state-mode gap

**Applicable:** `via_agw` trials with `config.api == "responses"` AND `config.state == true`.

**Algorithm:**
```
# In state=true mode, client uses previous_response_id instead of sending history[]
# CID propagation can only work via:
#   - response_id ↔ cid server-side mapping (not implemented — stateless v1)
#   - header-based passthrough X-IB-CID (spec §14.5 future item — not implemented v1)
#   - model echoes CID in its tool_use args (depends on model compliance)

# So the test is: does CID actually propagate?
for idx, turn in enumerate(user_msg_turns[1:], start=1):
    ingress_cid = turn.audit_cid()
    prior_cid = user_msg_turns[idx-1].audit_cid()
    if ingress_cid != prior_cid:
        return fail(f"Turn {idx}: state-mode ingress cid={ingress_cid} diverged from prior {prior_cid}; body-level propagation broken (spec §12.1)")
return pass(f"CID preserved across {n} turns in state-mode — unexpected in v1; investigate")
```

**Expected outcome in v1:** this verdict fails consistently, documenting the known gap. That's the *useful* result — aiplay becomes the evidence for promoting spec §14.5 to v1.1.

**Not a bug in aiplay; it's an observation about AGW v1.**

### 7.7 Applicability matrix

| Verdict | via_agw | direct (baseline) | NONE LLM + MCP | any API | ≥3 turns | has compact | api=responses+state=T |
|---|---|---|---|---|---|---|---|
| (a) | ✅ | ▒ na | ✅ (MCP only) | ✅ | ≥1 turn | ✅ | ✅ |
| (b) | ✅ | ▒ na | ✅ | ✅ | ≥1 turn | ✅ | ✅ |
| (c) | ✅ | ▒ na | ✅ | ✅ | **required** | ✅ | ✅ |
| (d) | ✅ | ▒ na | ✅ | ✅ | ≥2 user_msg | **required** | ✅ |
| (e) | ✅ | ▒ na | ✅ | ✅ | ≥2 user_msg | ✅ | **required** |

Rows marked ▒ na short-circuit to `{verdict: "na", reason: "baseline — cidgar not in path"}` without running any logic.

### 7.8 Verdict pill UI

| Pill | Meaning | Color | Glyph |
|---|---|---|---|
| `✓` | pass | green | ✓ |
| `✗` | fail | red | ✗ |
| `—` | na (inapplicable or baseline) | grey | — |
| `⚠` | error (couldn't evaluate) | amber | ⚠ |

Hover tooltip shows the `reason` field for all non-pass verdicts.

### 7.9 Computation performance

Verdict computation is O(N) over turns + O(M) over audit entries, where N ≤ 10 (turn cap) and M typically ≤ 50 per trial. Runs in milliseconds. No caching needed.

---

## 8. Configuration

Four configuration artifacts, each with a specific home and lifecycle.

### 8.1 `.env` (secrets, gitignored)

Loaded by `env_file: [.env]` on adapter services. Paid-provider keys only.

```bash
# /my/ws/aiplay/.env  (gitignored)
OPENAI_API_KEY=sk-proj-...
ANTHROPIC_API_KEY=sk-ant-...
GOOGLE_API_KEY=...
```

Empty values are allowed; adapters check per-turn. If a trial requires a key that's empty, the adapter returns `500` with `error: "provider_key_missing", detail: "ANTHROPIC_API_KEY not set"` and the harness surfaces a clear UI error.

### 8.2 `.env.example` (committed template)

Ships with the repo as documentation of required keys:

```bash
# /my/ws/aiplay/.env.example
# Copy to .env and fill in. Do NOT commit .env.
OPENAI_API_KEY=
ANTHROPIC_API_KEY=
GOOGLE_API_KEY=
```

`.gitignore` entry: `.env`.

### 8.3 `agw/config.yaml` — gateway routes + governance policies

The single most important config file. Every route pattern that should be cidgar-governed needs:
1. A governance policy block
2. For Anthropic routes: an `ai.routes` mapping (per cidgar spec §5 — the Harness B gotcha at commit `9d7b882e`)

Full sketch:

```yaml
# /my/ws/aiplay/agw/config.yaml
binds:
  - port: 8080
    listeners:
      - protocol: HTTP
        routes:

          # ── LLM routes ──
          # Each exposes the provider's API under /llm/<provider>/*
          # All are governed (cidgar policy block present).

          - name: llm-ollama
            matches:
              - path:
                  pathPrefix: /llm/ollama/
            policies:
              urlRewrite:
                path:
                  prefix: /
              governance:
                kind: cid_gar
                log_level: debug
                cid:
                  generator: uuid4_12
                  header_passthrough: true
                gar:
                  schema_required: true
                channels:
                  text_marker: true
                  resource_block: true
            backends:
              - ai:
                  name: ollama
                  provider:
                    openAI: {}
                  hostOverride: "host.docker.internal:11434"

          - name: llm-chatgpt
            matches:
              - path:
                  pathPrefix: /llm/chatgpt/
            policies:
              urlRewrite:
                path:
                  prefix: /
              governance: {kind: cid_gar, log_level: debug}
            backends:
              - ai:
                  name: openai
                  provider:
                    openAI: {}
                  hostOverride: "api.openai.com:443"
                  # API key is set by the adapter's SDK; no backendAuth here

          - name: llm-claude
            matches:
              - path:
                  pathPrefix: /llm/claude/
            policies:
              urlRewrite:
                path:
                  prefix: /
              # CRITICAL: ai.routes required for Messages-shape dispatch
              # Without this AGW defaults to Completions parser and
              # governance walker never fires for Messages.
              ai:
                routes:
                  "/v1/messages": messages
                  "/v1/messages/count_tokens": anthropicTokenCount
              governance: {kind: cid_gar, log_level: debug}
            backends:
              - ai:
                  name: anthropic
                  provider:
                    anthropic: {}
                  hostOverride: "api.anthropic.com:443"

          - name: llm-gemini
            matches:
              - path:
                  pathPrefix: /llm/gemini/
            policies:
              urlRewrite:
                path:
                  prefix: /
              governance: {kind: cid_gar, log_level: debug}
            backends:
              - ai:
                  name: gemini
                  provider:
                    openAI: {}
                  hostOverride: "generativelanguage.googleapis.com:443"
                  # Gemini exposes an OpenAI-compat endpoint at /v1beta/openai

          # ── MCP routes ──
          # Each governed MCP server exposed under /mcp/<name>
          # prefixMode: always to preserve tool-name clarity across mux

          - name: mcp-weather
            matches:
              - path:
                  pathPrefix: /mcp/weather
            policies:
              urlRewrite:
                path:
                  prefix: /mcp
              governance: {kind: cid_gar, log_level: debug}
            backends:
              - mcp:
                  prefixMode: always
                  targets:
                    - name: weather
                      mcp:
                        host: http://mcp-weather:8001/mcp

          # mcp-news, mcp-library, mcp-fetch follow same shape
          # (targets: mcp-news:8002, mcp-library:8003, mcp-fetch:8004)

          # ── No-governance controls (optional) ──
          # Uncomment to add baseline-style routes with same topology minus governance
          # Useful as negative controls when routing=via_agw but trial wants
          # to exercise a specific route without governance mutation.
          #
          # - name: mcp-weather-nogov
          #   matches: [{path: {pathPrefix: /mcp/weather-nogov}}]
          #   policies: { urlRewrite: {path: {prefix: /mcp}} }
          #   backends: [{mcp: {targets: [{name: weather, mcp: {host: http://mcp-weather:8001/mcp}}]}}]
```

### 8.4 `harness/defaults.yaml` — matrix seed + turn templates

Loaded by harness-api on first boot to populate the grid with the minimum-spanning rows (§brainstorming.md). Also contains default turn-plan templates per config pattern.

```yaml
# /my/ws/aiplay/harness/defaults.yaml

matrix_seed_rows:
  - {framework: langchain,    api: chat,      stream: false, state: false, llm: ollama,    mcp: NONE,    routing: via_agw}
  - {framework: langgraph,    api: chat,      stream: true,  state: false, llm: ollama,    mcp: weather, routing: via_agw}
  - {framework: crewai,       api: messages,  stream: false, state: false, llm: claude,    mcp: library, routing: via_agw}
  - {framework: pydantic-ai,  api: messages,  stream: true,  state: false, llm: claude,    mcp: news,    routing: via_agw}
  - {framework: autogen,      api: responses, stream: false, state: false, llm: chatgpt,   mcp: fetch,   routing: via_agw}
  - {framework: llamaindex,   api: responses+conv, stream: true, state: true, llm: chatgpt, mcp: weather, routing: via_agw}
  - {framework: NONE,         api: NONE,      stream: false, state: false, llm: NONE,      mcp: news,    routing: via_agw}

turn_plan_templates:
  # Key format: <mcp_present>_<has_compact>_<has_state_ref>
  # Harness picks template based on row config + efficacy toggles

  no_mcp_no_compact_no_stateref:
    turns:
      - {kind: user_msg, content: "Hello, tell me a short one-line fact about testing."}
      - {kind: user_msg, content: "Can you elaborate on that?"}
      - {kind: user_msg, content: "Summarize what you just told me."}

  with_mcp_no_compact_no_stateref:
    turns:
      - {kind: user_msg, content: "Hello, what tools do you have available?"}
      - {kind: user_msg, content: "{mcp_specific_query}"}    # substituted based on MCP server
      - {kind: user_msg, content: "{mcp_followup_query}"}

  with_mcp_with_compact_no_stateref:
    turns:
      - {kind: user_msg, content: "Hello, what tools do you have available?"}
      - {kind: user_msg, content: "{mcp_specific_query}"}
      - {kind: compact,  strategy: drop_half}
      - {kind: user_msg, content: "{mcp_followup_query}"}

  responses_stateful:
    turns:
      - {kind: user_msg, content: "What tools can you call?"}
      - {kind: user_msg, content: "{mcp_specific_query}"}
      - {kind: user_msg, content: "{mcp_followup_query}"}
      # state mode uses previous_response_id implicitly — no force_state_ref unless efficacy (e) wants a specific probe

  direct_mcp_no_llm:
    turns:
      - {kind: direct_mcp_tools_list, target: "{mcp}"}
      - {kind: direct_mcp_tools_call, target: "{mcp}", tool: "{first_tool}", args: "{inferred}"}

mcp_query_substitutions:
  weather:
    mcp_specific_query: "What's the weather in Paris?"
    mcp_followup_query: "And in London?"
  news:
    mcp_specific_query: "Give me news about AI."
    mcp_followup_query: "Any news about space exploration?"
  library:
    mcp_specific_query: "Find books with 'Clean Code' in the title."
    mcp_followup_query: "Show me more results."
  fetch:
    mcp_specific_query: "Fetch https://example.com and summarize it."
    mcp_followup_query: "Fetch https://httpbin.org/uuid and tell me what it returns."
```

### 8.5 `harness/settings.json` — runtime-mutable settings

Written by harness-api when user changes Settings panel values (§5.6). Lives at `./data/settings.json`.

```json
{
  "max_concurrent_trials": 1,
  "default_routing": "via_agw",
  "default_ollama_model": "qwen2.5:7b-instruct",
  "turn_cap": 10,
  "log_tail_buffer_bytes": 1048576,
  "api_keys_detected": {
    "openai": true,
    "anthropic": true,
    "google": false
  }
}
```

Updated on GET request from frontend (api_keys_detected is refreshed by reading env at query time); written on Settings panel save.

### 8.6 Frontend config

Minimal. A single constant in `frontend/config.js`:

```javascript
export const API_BASE = "";              // same-origin; harness serves the frontend
export const SSE_RETRY_MS = 3000;
export const VALIDATE_DEBOUNCE_MS = 150;
export const PROVIDERS_REFRESH_MS = 30000;   // periodic refresh of /providers for key-change detection
export const MAX_ROWS = 50;              // UI display limit; not a hard backend limit
```

No build step; loaded as a module.

### 8.7 Config file summary

| Path | Purpose | Committed? | Reloaded at runtime? |
|---|---|---|---|
| `.env` | Secrets | **NO** (gitignored) | Compose restart required |
| `.env.example` | Secret template | yes | n/a |
| `agw/config.yaml` | Gateway routes + governance | yes | AGW hot-reload if supported; else restart |
| `harness/defaults.yaml` | Matrix seed + turn templates | yes | Restart harness-api to re-seed new installs; existing trials unaffected |
| `data/settings.json` | User-tunable runtime settings | **NO** (gitignored) | Live — read per request |
| `frontend/config.js` | Frontend constants | yes | Browser refresh |
| `docker-compose.yaml` | Service topology | yes | Compose restart |
| `Makefile` | Workflow helpers | yes | n/a |

All configs are plain text, editable by hand, no generation step. `harness/defaults.yaml` is the only file a contributor might meaningfully hand-edit to extend seeded rows.

---

## 9. Failure modes

How the system behaves when things go wrong. Each failure has a detection point and a recovery posture.

### 9.1 Adapter crash mid-trial

**Detection:** Harness's `POST /trials/{id}/turn` returns connection-refused or 5xx.

**Posture:** Trial marked `status=error` with `error_reason="adapter_crash"`. Turn partial-state saved up to the last successful turn. Trial JSON preserved.

**Recovery:** User restarts the failed adapter container (`docker compose restart adapter-<framework>`). Trial is NOT automatically retried — user must re-click Run or start a new trial.

### 9.2 AGW crash / restart

**Detection:** Harness's log-tail subscriber sees Docker SDK disconnect. In-flight LLM/MCP calls receive connection errors.

**Posture:**
- Ongoing turns fail (adapter returns 5xx to harness).
- Log-tail subscriber enters reconnect loop with exponential backoff (2s, 4s, 8s, max 30s).
- On reconnect, resume log tail from current head (losing any logs emitted during outage — acceptable).
- UI shows a red banner at top: "AGW disconnected — trials cannot proceed until reconnect".

**Recovery:** When AGW comes back, in-flight trials stay `status=error`; new trials can run immediately.

### 9.3 Log-tail dropout (Docker SDK hang)

**Detection:** No log lines received for > 60 seconds while a trial is running.

**Posture:**
- Log-tail task marked stale; force-disconnect + reconnect.
- Trial continues in parallel; audit entries captured after reconnect backfill into `trial.audit_entries` with `{captured_after_gap: true}` flag.
- Verdicts (§7) flag the gap: if the missing window overlaps a turn's expected audit, verdict returns `error` with `"audit gap during turn {idx}"`.

### 9.4 Streaming error mid-response

**Detection:** Adapter's LLM client raises during stream consumption (connection drop, malformed SSE chunk, etc).

**Posture:**
- Adapter captures partial message in `response_captured.partial_body`.
- Returns turn result with `partial=true, error=...`.
- Harness persists; next turn in plan proceeds (adapter framework state may be inconsistent — turn-by-turn continuity tests may fail, which is the correct verdict).
- Verdict (b) returns `error` with "incomplete response" for this turn.

### 9.5 Network partition (e.g., host loses DNS)

**Detection:** Any HTTPS call to public providers times out.

**Posture:** Adapter returns 5xx. Trial marked `status=error`. Harness surfaces: "Network error reaching {provider}; check connectivity and `.env` settings".

### 9.6 Partial trial resumption

**Scenario:** User starts a trial, closes browser. Adapter state retained. Another browser later opens the trial.

**Posture:**
- Drawer shows current `status` (`running` or `paused` if last turn completed but not continued).
- SSE stream re-established on drawer open.
- If `status=running`, live updates resume from current state.
- If `status=paused` (interactive mode between turns), "Next turn" button is the continuation path.

**No time limit.** Adapter state persists until container restart or explicit `DELETE /trials/{id}`. Trial JSON on disk persists forever.

### 9.7 Concurrent trial race conditions

**Scenario:** `MAX_CONCURRENT_TRIALS > 1`; two trials run simultaneously, both logging through AGW.

**Posture:**
- Audit demuxer keys ENTIRELY on `X-Harness-Trial-ID` header — no time-window heuristics.
- AGW stderr is serial; harness consumes line-by-line and routes by trial-ID.
- Guaranteed no cross-contamination as long as headers propagate correctly (§4.9 invariant).
- Adapter state is per-trial-dict-entry; no shared mutable state between trials.

**Known limit:** If an adapter's framework has process-global state (some crewai singleton patterns, langchain global callbacks), concurrent trials could conflict. Audit: v1 frameworks seeded don't exhibit this, but flagged for v1.1 testing.

### 9.8 Invalid `agw/config.yaml`

**Detection:** AGW startup fails.

**Posture:**
- Container exits with error to stderr.
- Harness's `depends_on: agentgateway` keeps harness-api from starting (compose blocks).
- User sees the error via `docker compose logs agentgateway`.
- `make up-safe` won't help here (it only checks image existence, not config validity).

**Recovery:** Edit config, `docker compose up agentgateway` to test, then full stack.

### 9.9a API key rotation mid-session

**Scenario:** User updates `.env` while the stack is running — e.g., adds a missing key or rotates an existing one.

**Posture:**
- Adapter services read env at process start; they don't see the new key until restart.
- `docker compose restart adapter-<framework>` picks up new env within ~2 seconds.
- Harness-api reads env on each `GET /providers` call — refresh happens at the next frontend poll (≤30s) or immediately on Settings panel close.
- If the user tries to run a trial after updating `.env` but before restarting adapters: trial fails per §9.9 (`provider_key_missing`), harness surfaces actionable message.

**Recommended flow:** edit `.env` → `docker compose restart adapter-*` (or Makefile: `make rotate-keys`) → refresh browser.

### 9.9 Missing API key

**First line of defense (UI):** LLM dropdown excludes providers without keys (§5.2). User cannot select `chatgpt` if `OPENAI_API_KEY` is empty — the option is greyed with reason tooltip. This prevents ~100% of "missing key" errors from ever reaching adapter services.

**Second line of defense (adapter — defense in depth):**

**Detection:** Adapter receives trial with `llm=chatgpt` but `OPENAI_API_KEY` env is empty. Only possible if key was present at UI-dropdown-load time but got removed before run, OR compose env got restarted without updating the harness.

**Posture:**
- Adapter returns `500 {"error": "provider_key_missing", "provider": "openai"}` on `POST /trials` or first `turn`.
- Harness surfaces in row status: `⚠ provider_key_missing` + tooltip "Set OPENAI_API_KEY in .env and restart".
- Harness also triggers a silent refresh of `/providers` — next dropdown render excludes the now-unavailable provider.
- Other trials unaffected.

### 9.10 MCP server down

**Detection:** Adapter (or AGW in via_agw mode) receives connection-refused on MCP call.

**Posture:**
- Tool call fails mid-turn.
- Framework either (a) retries per framework policy or (b) returns an error message to the LLM, which may say "I couldn't reach the tool".
- Turn completes with `tool_calls: []` and an error-ish assistant message.
- Verdict (b) flags missing C3 if expected.
- Trial proceeds (doesn't abort on MCP failures — realistic behavior).

### 9.11 Provider rate limiting

**Detection:** 429 response from provider.

**Posture:**
- Adapter passes through as turn error `{error: "rate_limited", retry_after: <seconds>}`.
- Harness shows in row status + drawer banner.
- No automatic retry (v1). User manually re-runs.

### 9.12 Disk full

**Detection:** Writing `data/trials/*.json` fails.

**Posture:**
- Harness logs error, returns 500 for the Run request.
- UI toast: "Disk full — cannot persist trial".
- Previously-written trials safe. No corruption.

### 9.13 Frontend-backend version mismatch

**Scenario:** User updates harness-api but browser has old `frontend/app.js` cached.

**Posture:**
- API responses with new fields render as `undefined` in frontend, potentially broken.
- Mitigation: harness-api sends `ETag` on frontend assets; force cache bust on version bump.
- Also: `/version` endpoint + version comparison in `app.js`; mismatch → banner "Reload page for updated UI".

### 9.14 Summary: failure → surface → user action table

| Failure | User surface | Action |
|---|---|---|
| Adapter crash | Row status ⚠, drawer banner | Restart adapter |
| AGW crash | Top-bar banner, row pills ⚠ | Wait for reconnect, or restart AGW |
| Log-tail gap | Drawer banner "audit gap at turn N" | Re-run affected trial if verdict critical |
| Streaming error | Per-turn card shows `partial=true` | Inspect partial body; re-run trial |
| Network partition | Error toast per attempt | Check connectivity |
| Browser close mid-trial | n/a | Reopen; trial resumes automatically |
| Invalid AGW config | `make up-safe` error OR AGW container exit loop | Fix YAML; restart |
| Missing API key | Row status ⚠ `provider_key_missing` | Set in `.env`; `docker compose restart adapter-<framework>` |
| MCP server down | Turn completes with tool error | Restart MCP; re-run trial |
| Rate limit 429 | Row status ⚠ `rate_limited`; banner with retry-after | Wait / switch provider |
| Disk full | Run fails with toast | Free disk, retry |
| Version mismatch | Banner "Reload page" | Force browser refresh |
| API key rotation | Next trial reports `provider_key_missing` | `docker compose restart adapter-*` + refresh browser |
| User aborts trial | Row status ⏹ aborted; verdicts all `na` | n/a — intentional |

---

## 10. Future work

Explicitly out of v1 scope; documented here for the next design cycle.

### 10.1 v1.1 candidates

- **n8n framework integration** — workflow-authored per (API × stream × state); webhook-triggered; requires one `nodes.json` per combo. Deferred from v1 per user ask.
- **Native Ollama API shape** — `/api/chat` and `/api/generate` endpoints (auth2v uses these). Would exercise the cidgar gap identified during Harness B (native-Ollama shape walker is unimplemented).
- **Healthchecks** — formal Docker healthcheck stanzas for ordered startup (v1 uses `depends_on: service_started` only, which doesn't wait for readiness).
- **SQLite persistence (R2)** — alternative to R3 JSON files if query load grows (filter-by-status across 100s of trials, diff queries, cross-trial analytics). Swap at the persistence layer; rest of harness unchanged.
- **Concurrent trials (C2)** — bump `MAX_CONCURRENT_TRIALS` default from 1 to 3-5 after adapter reentrancy validated for each framework.
- **Spec §14.5 header-based CID passthrough** — if verdict (e) consistently fails as expected, aiplay becomes the forcing function for promoting §14.5 from "future item" to v1.1 spec addition. Adds `X-IB-CID` request header bypass at AGW.
- **Cross-trial diff view** — side-by-side JSON comparison of paired (via_agw + direct) trials with channel-by-channel visual highlights. v1 lets user open both trials' drawers separately; v1.1 adds a dedicated diff tab.

### 10.2 Deferred for v1.2+

- **Compaction strategy expansion** — `summarize` and `drop_tool_calls` are declared but implementations vary per framework. v1 may have fallback-to-drop_half for some; v1.2 formalizes each per framework.
- **Streaming tool-call verification** — streaming +  tool_calls is a known weak point (governance bypasses streaming per spec). v1 captures but doesn't assert anything useful; v1.2 could add specific stream-aware verdicts.
- **Resilience beyond compaction** — simulate context-window overflow, mid-turn token limits, model-level guardrail refusals. Each is its own turn-plan kind.
- **Multi-provider trial (fallback chains)** — single trial that uses provider A for turn 1 and provider B for turn 2 (tests the "preference order" itself). Interesting for proving CID survives across provider switches.
- **Headless mode / CI integration** — run the whole matrix via CLI without UI for CI/nightly. `aiplay run-all --output=junit.xml`.
- **OTel tracing export** — AGW already has tracing infrastructure; export spans to a local Jaeger/Tempo for trace-level correlation alongside the audit JSON. Makes "what happened per turn" deeply queryable.

### 10.3 Out of scope indefinitely

- **Authentication integration** — Keycloak, OAuth, mTLS. Harness C explicitly non-auth; auth2v owns that testing.
- **Multi-tenancy at the harness level** — one user, one machine. No per-user spaces.
- **Production deployment** — not a product, not hosted; dev tool only.
- **Performance benchmarking** — out of scope; request tracking captures timing but verdicts don't assert performance.
- **Microsoft Copilot (copilot.microsoft.com)** — dropped from scope during design (user ask). No public API; browser automation would be fragile and doesn't map to cidgar's MCP tool-calling hooks. If Microsoft ever exposes a proper API, revisit; documented in `docs/memory-log.md` for context.

---

## Appendix — Design doc changelog

- 2026-04-22 — Initial draft (sections 1-10 complete).

## Appendix — Approved-with-revision checkpoints

- Section 1 — Architecture
  - Added Principle #6 (sole egress) then revised to two-mode (via_agw default + direct baseline) per user
  - Diagram revised to show AGW as gateway for BOTH LLM and MCP legs
- Section 2 — Components
  - Component boundaries unchanged
  - Adapter egress updated to reflect dual base URLs (AGW + direct) per routing mode
  - MCP network isolation replaced with dual-reachability (required for direct routing mode)
  - AGW config shown with LLM + MCP routes for clarity
- Renaming pass — all `/conv/*` → `/trials/*` (plural REST convention)
- Section 7 — Efficacy verdicts (approved as-is)
- Section 8 — Configuration
  - Microsoft Copilot clarification (rounds 1-3): initially interpreted as Azure OpenAI; user clarified (round 2) it means consumer https://copilot.microsoft.com/; user then decided (round 3) to drop the provider entirely since Ollama + chatgpt + claude cover all 4 target APIs. All ms-copilot references removed from design.md, README.md, brainstorming.md; memory-log.md retains a "removed from scope" entry for context.
- Provider availability UX (post-§10): LLM dropdown dynamically reflects `.env` key detection via `GET /providers`. Providers without keys appear greyed with reason tooltip and cannot be selected. First-line defense at UI layer; adapter-layer `provider_key_missing` remains as defense-in-depth. Settings panel close refreshes the dropdown state.
- Self-review (2026-04-22): fixed stale §7-TBD reference; moved Microsoft Copilot from §10.1 v1.1 candidates to §10.3 out-of-scope; clarified §3.3 user_msg turn semantics (one harness turn = full framework-internal agent loop, all internal calls share X-Harness-Turn-ID); added Abort action + aborted trial status flow; added §9.9a API key rotation failure mode + Makefile `rotate-keys` target.
