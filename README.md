# aiplay — cidgar Harness C

Multi-framework test harness for AGW's cidgar governance pipeline. Drives **7 framework adapters** through 4 LLM APIs against 4 MCP servers, computes **6 verdicts (a–f)** on the captured cidgar audit stream.

Complementary to Harness B (scripted curl harness inside `agw-gh`, 23/23 green) and AGW's Rust unit/integration suite.

## What this is

A playground for verifying that cidgar's correlator (CID) survives across realistic agent code paths — multi-turn chat, message compaction, server-state Responses chains — that Harness B's curl scripts can't reach. Each row in the matrix UI = one `(framework × API × LLM × MCP × stream × state × routing)` trial.

- 7 framework adapter services
- 4 MCP servers (weather, news, library, fetch) — no auth
- 1 FastAPI harness backend + AG-Grid UI on port 8000
- 1 AGW instance (externally-built cidgar image)

Toggle `routing=via_agw` (cidgar enabled) vs `routing=direct` (baseline) for A/B comparisons.

## What this is NOT

- Not an AGW regression suite (that's Harness B + Rust tests).
- Not an auth integration test (auth2v owns that).
- Not a performance benchmark.
- Not production-hosted; dev tool only.

## Prereqs

- Docker + docker compose
- Ollama running on the host (default model: `qwen2.5:7b-instruct`, tool-capable)
- Pre-built `agentgateway:cidgar` image from the cidgar branch of `agw-gh` — **aiplay does NOT build AGW**, see "Building AGW image" below
- Optional: `OPENAI_API_KEY` / `ANTHROPIC_API_KEY` / `GOOGLE_API_KEY` in `.env` for non-ollama trials

## Quickstart

```bash
cd /my/ws/aiplay

# 1. Build the AGW image externally (from agw-gh worktree)
cd /mnt/share/ws/agw-gh/.worktrees/cidgar
docker build -t agentgateway:cidgar .
cd /my/ws/aiplay

# 2. Verify the image is available locally
make check-agw

# 3. Copy .env template and fill in keys (optional providers)
cp .env.example .env
$EDITOR .env

# 4. Pull an Ollama model (on the host, not inside compose)
ollama pull qwen2.5:7b-instruct

# 5. Bring up the stack
make up          # docker compose up -d

# 6. Open the UI
open http://localhost:8000/
```

```bash
make logs        # tail all services
make down        # docker compose down
make reset       # nuke data/ + restart
```

## The 7-adapter matrix

Each adapter is a FastAPI service exposing `/info` + `/run`. The harness picks the right adapter per matrix row based on `framework × api` support:

| Adapter | port | chat | messages | responses | responses+conv | MCP integration |
|---|---|---|---|---|---|---|
| langchain | 5001 | ✓ | – | – | – | langchain-mcp-adapters |
| direct-mcp | 5010 | – | – | – | – | fastmcp (no LLM) |
| langgraph | 5011 | ✓ | – | – | – | langchain-mcp-adapters |
| crewai | 5012 | ✓ | ✓ | – | – | custom BaseTool wrapping fastmcp |
| pydantic-ai | 5013 | ✓ | ✓ | ✓ | – | MCPServerStreamableHTTP toolset |
| autogen | 5014 | ✓ | ✓ | ✓ | ✓ | fastmcp wrap |
| llamaindex | 5015 | ✓ | – | ✓ | ✓ | fastmcp wrap |

`ADAPTER_CAPABILITIES` in `harness/validator.py` drives the dropdown-filtering and run-button-enabled logic in the UI.

## Verdicts (a–f)

| | What it measures |
|---|---|
| **(a) Presence** | CID appears in AGW audit log for each turn |
| **(b) Channel structure** | All 3 channels (system prompt / tool args / tool result) behave per spec |
| **(c) Continuity** | CID preserved across ≥3 consecutive turns |
| **(d) Resilience** | CID survives a `compact` turn (drop_half / drop_tool_calls / summarize) |
| **(e) State-mode gap** | CID survives a `force_state_ref` jump in a Responses+conv chain |
| **(f) GAR richness** | `_ib_gar` reflection arrives intact in the audit log |

Each verdict returns `pass` / `fail` / `na` with a reason string. `na` means "this verdict doesn't apply to this trial configuration" (e.g. (e) on an `api=chat` row).

## Running tests

### Via UI (primary workflow)

1. Open http://localhost:8000/
2. Matrix grid shows seeded rows. Click **[▶]** on any row to run its default turn plan.
3. Click the row (not the button) to open the detail drawer.
4. Drawer tabs:
   - **Turn Plan** — CodeMirror JSON editor, override per row, reset to default
   - **Turns** — per-turn request/response/audit cards (live-streaming via SSE)
   - **Verdicts** — a/b/c/d/e/f pass-fail cards with reason strings
   - **Raw JSON** — full trial record
5. **🔀 Baseline** on any AGW-routed row → creates a sibling `routing=direct` row for A/B comparison.
6. **⏹ Stop** on a running trial → the current turn finishes naturally; subsequent turns are skipped; verdicts compute on completed turns.

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

# Provider availability (reads .env)
curl http://localhost:8000/providers
```

Full API reference: `docs/design.md` §2.1 + §4.

## Turn kinds

| Kind | What it does |
|---|---|
| `user_msg` | Single user message, adapter handles agent loop if MCP present |
| `compact` | Simulates history truncation between turns (drop_half / drop_tool_calls / summarize) |
| `force_state_ref` | Overrides next turn's `previous_response_id` (exercises verdict e) |

> `inject_ambient_cid` appears in `docs/design.md` but is deferred — not
> implemented in v1. The runner's default branch records
> `{"reason": "turn kind 'inject_ambient_cid' not implemented"}` if a
> template carries it.

## Building AGW image

aiplay's compose references `agentgateway:cidgar` as a static tag — it never rebuilds AGW itself.

```bash
cd /mnt/share/ws/agw-gh/.worktrees/cidgar
docker build -t agentgateway:cidgar .
# back in aiplay
cd /my/ws/aiplay
docker compose restart agentgateway
```

The Makefile's `check-agw` target verifies the image exists before `up`.

## API keys

Keys are **optional** — you can run the whole matrix against Ollama + mock-llm without any paid provider. Free tiers exist for all three paid providers.

| Provider | `.env` var | Free tier |
|---|---|---|
| OpenAI | `OPENAI_API_KEY` | intro credits, then pay-per-token |
| Anthropic | `ANTHROPIC_API_KEY` | $5 signup credit (thousands of Haiku turns) |
| Google AI Studio (Gemini) | `GOOGLE_API_KEY` | generous Flash rate limits |
| Ollama | none | fully local |

See the "Getting API keys" sections in `docs/design.md` for step-by-step signup instructions.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `agentgateway:cidgar missing` | Build the AGW image first (see above) |
| `provider_key_missing` | Check `.env` has the key, then `docker compose restart adapter-<framework>` |
| AGW audit log empty | Check `RUST_LOG_FORMAT=json`, policy present on route, correct ai.routes map |
| Stack won't come up | `docker compose logs agentgateway` then `harness-api`; verify all 13 services are up via `docker compose ps` |
| Ollama unreachable | `docker network inspect aiplay_net \| grep Gateway`; update `agw/config.yaml` `hostOverride` |
| chatgpt + responses returns 503 | Known AGW routing bug (see `docs/findings-plan-b.md`) — unrelated to adapter code |

## Layout

```
/my/ws/aiplay/
├── README.md                   # this file
├── docker-compose.yaml         # topology
├── Makefile                    # up, down, logs, reset, check-agw
├── .env / .env.example         # provider keys
├── agw/config.yaml             # AGW routes + cidgar policy
├── adapters/                   # 7 framework adapters
│   ├── langchain/        (5001)
│   ├── direct-mcp/       (5010)
│   ├── langgraph/        (5011)
│   ├── crewai/           (5012)
│   ├── pydantic_ai/      (5013)
│   ├── autogen/          (5014)
│   └── llamaindex/       (5015)
├── harness/                    # FastAPI backend
│   ├── main.py / api.py / runner.py / efficacy.py / validator.py / audit_tail.py
│   └── defaults.yaml           # matrix seed + turn templates
├── mcp/                        # 4 MCP services (weather, news, library, fetch)
├── mock_llm/                   # in-compose deterministic LLM
├── frontend/                   # plain HTML + AG-Grid + CodeMirror (no bundler)
├── data/                       # gitignored: trials/, matrix.json, settings.json
├── tests/                      # 117 pytest
└── docs/
    ├── design.md               # original design (Plan A scope)
    ├── findings-plan-b.md      # per-adapter quirks + verdict matrix (Plan B output)
    ├── enhancements.md         # open brainstorm topics (OTel, marker alternatives)
    ├── brainstorming.md        # running decision log
    ├── memory-log.md           # parked items / deferred tasks
    └── plans/                  # Plan A + Plan B implementation plans
```

## Further reading

- **`docs/design.md`** — full architectural design
- **`docs/findings-plan-b.md`** — per-adapter quirks, verdict coverage, known issues surfaced in T1-T14
- **`docs/plans/2026-04-22-aiplay-v1-plan-a-mvp.md`** — Plan A (16 tasks, MVP)
- **`docs/plans/2026-04-23-aiplay-plan-b.md`** — Plan B (15 tasks, 6 new adapters + verdicts c/d/e)
- **AGW cidgar spec** — `/mnt/share/ws/agw-gh/docs/agw-governance-spec.md`
- **Harness B findings** — `/mnt/share/ws/agw-gh/docs/2026-04-20-governance-harness-b-findings.md`

## License

Private / internal playground. Not for distribution.
