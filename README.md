# aiplay — Cidgar Harness C Playground

Multi-framework test harness for the AGW governance pipeline (cidgar). Drives 6 agent frameworks × 4 LLM APIs × streaming/state combinations to verify cidgar efficacy (a-e) via a UI-driven test matrix.

Complementary to Harness B (scripted curl harness inside `agw-gh`, 23/23 green).

## What this is

- A docker-compose stack with:
  - 6 framework adapter services (langchain, langgraph, crewai, pydantic-ai, autogen, llamaindex)
  - 4 MCP servers (weather, news, library, fetch) — no auth
  - 1 FastAPI harness backend + AG-Grid UI
  - 1 AGW instance (externally-built cidgar image)
- A spreadsheet-like UI where each row is a test combination. Edit cells (framework/API/streaming/state/LLM/MCP/routing), click Run, see live audit logs and pass/fail verdicts.
- Toggle between `routing=via_agw` (cidgar enabled) and `routing=direct` (baseline) for A/B comparisons.

## What this is NOT

- Not an AGW regression suite (that's Harness B + Rust unit tests).
- Not an auth integration test (auth2v owns that).
- Not a performance benchmark.
- Not production-hosted; dev tool only.

## Plan A — exhaustive support matrix

Plan A is the MVP: **one framework adapter (langchain) + one "no-LLM" adapter (direct-mcp)**, chat-completions API only, verdicts (a)+(b). Everything else is Plan B.

### Runnable row configs

9 distinct shapes × 2 routings (`via_agw` / `direct`) = 18 configs.

| # | framework | api | llm | mcp | cidgar hooks exercised | prerequisite |
|---|---|---|---|---|---|---|
| 1 | langchain | chat | ollama | NONE | f2+f3 (C2 marker) | Ollama on host |
| 2 | langchain | chat | ollama | weather/news/library/fetch | **f1+f2+f3+f4+f5** (full agent loop) | Ollama, tool-capable model |
| 3 | langchain | chat | mock | NONE | f2+f3 with mock LLM | none (compose-internal) |
| 4 | langchain | chat | mock | weather/news/library/fetch | f1+f2+f3+f4+f5 with mock | none |
| 5 | langchain | chat | chatgpt | NONE | f2+f3 via OpenAI | `OPENAI_API_KEY` |
| 6 | langchain | chat | chatgpt | weather/news/library/fetch | f1+f2+f3+f4+f5 via OpenAI | `OPENAI_API_KEY` |
| 7 | langchain | chat | gemini | NONE | f2+f3 via Google | `GOOGLE_API_KEY` |
| 8 | langchain | chat | gemini | weather/news/library/fetch | f1+f2+f3+f4+f5 via Google | `GOOGLE_API_KEY` |
| 9 | (any) | (any) | NONE | weather/news/library/fetch | f1+f4+f5 (direct-MCP deterministic routing, no LLM) | none |

### What the validator BLOCKS in Plan A (Run button disabled, row greyed, HTTP 400 if forced via API)

| Config | Blocked because |
|---|---|
| `llm=NONE + mcp=NONE` | Nothing to exercise |
| `api=chat + llm=claude` | Anthropic has no chat-completions endpoint |
| `api=responses + llm=anyone` | No Plan A adapter implements Responses API (Plan B: autogen/llamaindex) |
| `api=responses+conv + llm=anyone` | Same — Responses API adapter needed |
| `api=messages + llm=claude` | No Plan A adapter implements Messages API (Plan B: crewai/pydantic-ai) |
| `api=messages + llm=anyone else` | Messages is claude-only AND no adapter |
| `framework=langgraph/crewai/pydantic-ai/autogen/llamaindex` | Those adapters don't exist yet (Plan B) |

### Verdicts

| Level | Plan A |
|---|---|
| (a) Presence — CID appears in audit log per turn | ✅ computed |
| (b) Channel structure — response bodies carry CID across all channels | ✅ computed (scans agent-loop intermediates too) |
| (c) Multi-turn continuity | ⏸ `na — deferred to Plan B` |
| (d) Compaction resilience | ⏸ `na — deferred to Plan B` |
| (e) Server-state-mode gap | ⏸ `na — deferred to Plan B` |

### UI features

| Feature | Plan A |
|---|---|
| Matrix grid with cell editing | ✅ |
| Validator-driven disable/force (state, LLM dropdown filtering by API, etc.) | ✅ |
| Add Row / Delete / Delete All (click-twice-to-confirm) | ✅ |
| ▶ Run per row + Run All | ✅ |
| Verdict cell → opens trial in new tab (available from row creation) | ✅ |
| Live-streaming turn cards during run (SSE + polling fallback) | ✅ |
| Turn-plan preview (📋 button → read-only modal) | ✅ |
| Settings modal showing provider availability | ✅ |
| Per-turn HTTP request/response capture (real wire bytes via httpx event hooks) | ✅ |
| Agent-loop multi-step cards (langchain + MCP) | ✅ |
| AGW audit log per-turn with color-coded phase badges | ✅ |
| Turn-plan editor (edit turns before Run) | ⏸ Plan B (CodeMirror) |
| Clone-for-baseline action | ⏸ Plan B |
| Abort running trial | ⏸ Plan B (⏸ button rendered disabled) |

### Turn kinds

| Kind | Plan A |
|---|---|
| `user_msg` — single user message, adapter handles agent loop if MCP present | ✅ |
| `compact` — simulate history truncation between turns | ⏸ Plan B |
| `force_state_ref` — override next turn's `previous_response_id` | ⏸ Plan B |
| `inject_ambient_cid` — pre-seed a CID into framework state | ⏸ Plan B |

### Stream / State reality check

- **Stream toggle**: validator lets you set `stream=true`, but the langchain adapter doesn't pass it to ChatOpenAI in Plan A. No effect. Plan B wires it through.
- **State toggle**: only editable when `api=responses + llm=chatgpt`. That combo is BLOCKED in Plan A (no Responses adapter). So **no runnable Plan A row has state=true**. Column is operationally F-only in Plan A.

### Providers detected from `.env`

| Provider | Detected from | Plan A runnable? |
|---|---|---|
| NONE | always | ✅ (direct-MCP rows) |
| ollama | always | ✅ |
| mock | always | ✅ |
| chatgpt | `OPENAI_API_KEY` set | ✅ if key set |
| claude | `ANTHROPIC_API_KEY` set | ❌ no chat-API support; Plan B adds messages |
| gemini | `GOOGLE_API_KEY` set | ✅ if key set |

### TL;DR

**Plan A only exercises `api=chat` via langchain.** For each chat-capable provider (ollama/mock/chatgpt/gemini), pair with any MCP (or NONE), pick routing, and you get real cidgar traffic. For `llm=NONE`, direct-mcp adapter handles it. Anything else (responses, messages, other frameworks, state=true, compaction, abort, turn-plan editing, verdicts c/d/e) is Plan B.

If a row is greyed out in the UI, the Run button tooltip tells you which rule blocks it.

## Requirements

- Docker + docker compose
- Ollama running on the host at `192.168.64.1:11434` (or wherever `host.docker.internal` resolves) with a tool-capable model, e.g. `qwen2.5:7b-instruct`
- A pre-built `agentgateway:cidgar` image from the cidgar branch of `/my/ws/agw-gh` (aiplay does NOT build AGW — see "Building AGW image" below)
- API keys for the paid LLM providers you want to test (OpenAI, Anthropic, Google — see "Getting API keys" below)

## Quick start

```bash
cd /my/ws/aiplay

# 1. Build the AGW image externally (from agw-gh worktree)
cd /mnt/share/ws/agw-gh/.worktrees/cidgar
docker build -t agentgateway:cidgar .   # or `make docker`
cd /my/ws/aiplay

# 2. Verify the image is locally available
make check-agw

# 3. Copy .env template and fill in keys (see "Getting API keys")
cp .env.example .env
$EDITOR .env

# 4. Pull an Ollama model (on the host, not in compose)
ollama pull qwen2.5:7b-instruct

# 5. Bring up the stack
make up-safe
# or: docker compose up -d

# 6. Open the UI
open http://localhost:8000
```

## Building AGW image

aiplay's compose references `agentgateway:cidgar` as a static tag — it never rebuilds AGW itself. This mirrors auth2v's `docker-compose.agw.yml` pattern: you own the image lifecycle externally.

To rebuild with new cidgar changes:

```bash
cd /mnt/share/ws/agw-gh/.worktrees/cidgar
# apply your changes, commit, etc.
docker build -t agentgateway:cidgar .
# optional: tag with SHA for pinning
docker tag agentgateway:cidgar agentgateway:cidgar-$(git rev-parse --short HEAD)
# back in aiplay
cd /my/ws/aiplay
docker compose restart agentgateway
```

The Makefile's `check-agw` target verifies the image exists before `up`:

```bash
$ make check-agw
✅ agentgateway:cidgar found

# or if missing:
❌ agentgateway:cidgar missing — build from agw-gh worktree first
```

## Getting API keys

You need keys for whichever providers you want to test. Free tiers exist for all three paid providers listed below.

### OpenAI (`OPENAI_API_KEY`)

Used for: `chatgpt` provider in the matrix (chat completions, Responses API, Responses+conversation state).

1. Sign up / log in at https://platform.openai.com/
2. Add payment method (required even for free credits)
3. Navigate to https://platform.openai.com/api-keys
4. Click **Create new secret key** — name it `aiplay-harness-c` or similar
5. Copy the key (shown only once); paste into `.env` as:
   ```
   OPENAI_API_KEY=sk-proj-...
   ```
6. Set usage limits at https://platform.openai.com/account/limits to cap spend
7. Free tier / introductory credits cover light testing; ongoing use is pay-per-token

Test the key:
```bash
curl https://api.openai.com/v1/models -H "Authorization: Bearer $OPENAI_API_KEY"
```

### Anthropic (`ANTHROPIC_API_KEY`)

Used for: `claude.ai` provider (Messages API).

1. Sign up at https://console.anthropic.com/
2. Add payment method; Anthropic grants $5 free credits on signup (sufficient for thousands of test turns at Haiku pricing)
3. Go to https://console.anthropic.com/settings/keys
4. Click **Create Key** — name it `aiplay-harness-c`
5. Copy the key; paste into `.env`:
   ```
   ANTHROPIC_API_KEY=sk-ant-api03-...
   ```
6. Optional: set workspace-level spend limit in Console settings

Test the key:
```bash
curl https://api.anthropic.com/v1/models -H "x-api-key: $ANTHROPIC_API_KEY" -H "anthropic-version: 2023-06-01"
```

### Google AI Studio (`GOOGLE_API_KEY`) — for Gemini

Used for: `gemini` provider (chat completions via OpenAI-compat endpoint).

1. Go to https://aistudio.google.com/
2. Sign in with a Google account
3. Click **Get API key** (top-right or in left sidebar)
4. Create a new key in a new or existing Google Cloud project
5. Copy the key; paste into `.env`:
   ```
   GOOGLE_API_KEY=AIza...
   ```
6. Free tier: generous rate limits for Gemini Flash models; pay-as-you-go beyond that

Test the key:
```bash
curl "https://generativelanguage.googleapis.com/v1beta/models?key=$GOOGLE_API_KEY"
```

### Ollama (no key needed)

Used for: `ollama` provider (chat completions).

Runs on the host, not in compose. Pull the default model:

```bash
ollama pull qwen2.5:7b-instruct
```

Verify reachability from docker:

```bash
curl http://192.168.64.1:11434/api/tags   # replace with your docker host gateway
```

## Running tests

### Via UI (primary workflow)

1. Open http://localhost:8000
2. Matrix grid shows 7 default seeded rows (one per framework)
3. Click **[▶]** on any row to run its default turn plan
4. Click the row (not the button) to open the detail drawer
5. Drawer tabs: **Turn Plan** (edit before run) / **Turns** (per-turn request/response/audit) / **Verdicts** (a/b/c/d/e pass-fail cards) / **Raw JSON**
6. Use **[⎘ Clone for baseline]** to create a paired `routing=direct` sibling row for A/B comparison

### Via API (for scripting or CI)

```bash
# List rows
curl http://localhost:8000/matrix

# Create a row
curl -X POST http://localhost:8000/matrix/row \
  -H "Content-Type: application/json" \
  -d '{"framework":"langchain","api":"chat","stream":false,"state":false,"llm":"ollama","mcp":"NONE","routing":"via_agw"}'

# Run the row's default turn plan
curl -X POST http://localhost:8000/trials/{row_id}/run

# Fetch trial result
curl http://localhost:8000/trials/{trial_id}
```

Full API reference: `docs/design.md` §2.1 + §4.

## Troubleshooting

### `agentgateway:cidgar missing`

Build the image first — see "Building AGW image" above.

### `provider_key_missing`

Check `.env` has the required key set, then:

```bash
docker compose restart adapter-<framework>
```

### AGW audit log is empty

Check:

1. `RUST_LOG_FORMAT=json` is set on AGW container (compose env)
2. Governance policy is present on the route you're hitting (`agw/config.yaml`)
3. For Anthropic routes: `ai.routes` map is present (see `docs/design.md` §8.3 for the full sketch and the known footgun)

### Stack won't come up

```bash
docker compose logs agentgateway     # check for config YAML errors
docker compose logs harness-api      # check for startup issues
docker compose ps                    # verify all 12 services are running
```

### Ollama unreachable from compose

Docker host IP varies by environment. Inspect your setup:

```bash
docker network inspect aiplay_net | grep Gateway
```

Update `agw/config.yaml` if the `hostOverride` needs to change from `host.docker.internal:11434` to an explicit IP.

## Project structure

```
/my/ws/aiplay/
├── README.md                       # this file
├── docker-compose.yaml             # topology
├── Makefile                        # up, down, logs, reset, check-agw
├── .env                            # secrets (gitignored)
├── .env.example                    # template
├── .gitignore
├── agw/
│   └── config.yaml                 # AGW routes + governance policies
├── harness/
│   ├── Dockerfile
│   ├── main.py / api.py / ...      # FastAPI backend
│   └── defaults.yaml               # matrix seed + turn templates
├── frontend/
│   ├── index.html                  # single-page app
│   └── app.js / drawer.js / ...    # AG-Grid + SSE
├── adapters/
│   ├── langchain/
│   ├── langgraph/
│   ├── crewai/
│   ├── pydantic_ai/
│   ├── autogen/
│   └── llamaindex/
├── mcp/
│   ├── weather/
│   ├── news/
│   ├── library/
│   └── fetch/
├── data/
│   ├── trials/                     # per-trial JSON records (gitignored)
│   ├── matrix.json                 # persisted grid state
│   └── settings.json               # UI settings (gitignored)
└── docs/
    ├── design.md                   # full design doc
    ├── brainstorming.md            # decision log
    ├── conversation-log.md
    └── memory-log.md
```

## Further reading

- **`docs/design.md`** — full architectural design (10 sections, ~2000 lines)
- **`docs/brainstorming.md`** — decision log with rationale for every major choice
- **`docs/memory-log.md`** — deferred items + known limitations
- **AGW cidgar spec** — `/mnt/share/ws/agw-gh/docs/agw-governance-spec.md` (on `ibfork/docs` branch)
- **Harness B findings** — `/mnt/share/ws/agw-gh/docs/2026-04-20-governance-harness-b-findings.md`

## License

Private / internal playground. Not for distribution.
