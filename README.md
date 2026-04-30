# aiplay — cidgar Harness C

Multi-framework test harness for AGW's cidgar governance pipeline. Drives **8 framework adapters** through 4 LLM APIs against 5 MCP servers, computes **6 verdicts (a–f)** on the captured cidgar audit stream.

Complementary to Harness B (scripted curl harness inside `agw-gh`, 23/23 green) and AGW's Rust unit/integration suite.

## What this is

A playground for verifying that cidgar's correlator (CID) survives across realistic agent code paths — multi-turn chat, message compaction, server-state Responses chains, multi-LLM round-robin — that Harness B's curl scripts can't reach. Each row in the matrix UI = one `(framework × API × LLM × MCP × stream × state × routing)` trial.

- 8 framework adapter services (incl. `combo` for multi-LLM / multi-MCP fan-out)
- 5 MCP servers (weather, news, library, fetch, mutable) — no auth
- 1 FastAPI harness backend + AG-Grid UI on port 8000
- 1 AGW instance (pre-built `ghcr.io/agentgateway/agentgateway:v1.0.1-cidgar` image)

Toggle `routing=via_agw` (cidgar enabled) vs `routing=direct` (baseline) for A/B comparisons.

## What this is NOT

- Not an AGW regression suite (that's Harness B + Rust tests).
- Not an auth integration test (auth2v owns that).
- Not a performance benchmark.
- Not production-hosted; dev tool only.

## Prereqs

- Docker + docker compose
- Ollama running on the host (default model: `qwen2.5:7b`, tool-capable; override via `DEFAULT_OLLAMA_MODEL` in `.env`)
- Network access to `ghcr.io` so compose can pull the cidgar AGW image (`ghcr.io/agentgateway/agentgateway:v1.0.1-cidgar`) on first `up` — aiplay does NOT build AGW
- Optional: `OPENAI_API_KEY` / `ANTHROPIC_API_KEY` / `GOOGLE_API_KEY` in `.env` for non-ollama trials
- Optional: `OLLAMA_API_KEY` for Ollama Cloud (`*-cloud`) models routed via a signed-in `ollama serve`

## Quickstart

```bash
cd /my/ws/aiplay

# 1. Copy .env template and fill in keys (optional providers)
cp .env.example .env
$EDITOR .env

# 2. Pull an Ollama model (on the host, not inside compose)
ollama pull qwen2.5:7b

# 3. Bring up the stack (compose pulls the AGW image on first run)
make up          # docker compose up -d

# 4. Open the UI
open http://localhost:8000/
```

```bash
make logs        # tail all services
make down        # docker compose down
make reset       # nuke data/ + restart
```

## The 8-adapter matrix

Each adapter is a FastAPI service exposing `/info` + the `/trials/...` lifecycle. The harness picks the right adapter per matrix row based on `framework × api` support:

| Adapter | port | chat | messages | responses | responses+conv | MCP integration |
|---|---|---|---|---|---|---|
| langchain | 5001 | ✓ | ✓ | ✓ | ✓ | langchain-mcp-adapters |
| direct-mcp | 5010 | – | – | – | – | fastmcp (no LLM) |
| langgraph | 5011 | ✓ | ✓ | ✓ | ✓ | langchain-mcp-adapters |
| crewai | 5012 | ✓ | ✓ | – | – | custom BaseTool wrapping fastmcp |
| pydantic-ai | 5013 | ✓ | ✓ | ✓ | – | MCPServerStreamableHTTP toolset |
| autogen | 5014 | ✓ | ✓ | ✓ | ✓ | fastmcp wrap |
| llamaindex | 5015 | ✓ | – | ✓ | ✓ | fastmcp wrap |
| combo | 5008 | ✓ | ✓ | – | – | multi-MCP fan-out (E24a) |

`combo` is the only adapter that accepts list-form `llm` (round-robin per turn) and list-form `mcp` (intra-turn fan-out across multiple MCP servers, with same-CID propagation). See `MULTI_LLM_FRAMEWORKS` / `MULTI_MCP_FRAMEWORKS` in `harness/validator.py`.

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
3. Click a row's trial-id link to open `/trial.html?id=<trial>` in a new tab. The matrix row's **Turn Plan** opens in a side drawer (CodeMirror JSON editor) for per-row overrides.
4. Trial-detail tabs (`/trial.html`):
   - **Turns** — per-turn request/response/audit cards (live-streaming via SSE)
   - **Turn Plan** — read-only view of the plan that ran
   - **Verdicts** — a/b/c/d/e/f pass-fail cards with reason strings
   - **Note** — free-text annotation persisted with the trial
   - **CID flow** / **CID flow (interactive)** — per-turn channel timeline (channels 1/2/3 + audit phases)
   - **Services** — live `/info` snapshot for each adapter/MCP/AGW touched by the trial
   - **Raw JSON** — full trial record
5. **🔀 Baseline** on any AGW-routed row → creates a sibling `routing=direct` row for A/B comparison; `/pairs.html?row=<id>` renders the diff.
6. **⏹ Stop** on a running trial → the current turn finishes naturally; subsequent turns are skipped; verdicts compute on completed turns.
7. **ℹ About** in the toolbar opens the framework × API support matrix.

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

# Abort an in-flight trial (cooperative — finishes the current turn)
curl -X POST http://localhost:8000/trials/{trial_id}/abort

# Recompute verdicts on a stored trial without re-running it
curl -X POST http://localhost:8000/trials/{trial_id}/recompute_verdicts

# Pair (governed + last baseline) for a row, with diff summary
curl http://localhost:8000/pairs/{row_id}
curl 'http://localhost:8000/pairs/{row_id}/diff?path=turns.0.response'

# Provider availability (reads .env) + per-provider model catalog
curl http://localhost:8000/providers
curl http://localhost:8000/providers/chatgpt/models
```

Full API reference: `docs/design.md` §2.1 + §4.

## Turn kinds

| Kind | What it does |
|---|---|
| `user_msg` | Single user message, adapter handles agent loop if MCP present |
| `compact` | Simulates history truncation between turns (drop_half / drop_tool_calls / summarize) |
| `force_state_ref` | Overrides next turn's `previous_response_id` (exercises verdict e) |
| `mcp_admin` | Out-of-band POST to the mutable MCP (e.g. add/remove/rename a tool) between turns — exercises tool-list churn |
| `reset_context` | Wipes the adapter trial's canonical history without tearing down the trial — used by combo's E21 reset path |
| `refresh_tools` | Re-fetches `tools/list` from MCP servers (e.g. after an `mcp_admin` mutation) |

> `inject_ambient_cid` appears in `docs/design.md` but is deferred — not
> implemented in v1. The runner's default branch records
> `{"reason": "turn kind 'inject_ambient_cid' not implemented"}` if a
> template carries it.

## AGW image

aiplay's compose pins `ghcr.io/agentgateway/agentgateway:v1.0.1-cidgar`. `docker compose up` pulls it on first run; subsequent `up`s use the cached layer. To force a re-pull (e.g. after the upstream tag is updated):

```bash
docker compose pull agentgateway
docker compose up -d agentgateway
```

The Makefile still ships a `check-agw` target that inspects a local `agentgateway:cidgar` tag — it is a leftover from when AGW was built externally and is **not required** for the current ghcr-based flow.

## API keys

Keys are **optional** — you can run the whole matrix against Ollama + mock-llm without any paid provider. Free tiers exist for all three paid providers.

| Provider | `.env` var | Free tier |
|---|---|---|
| OpenAI | `OPENAI_API_KEY` | intro credits, then pay-per-token |
| Anthropic | `ANTHROPIC_API_KEY` | $5 signup credit (thousands of Haiku turns) |
| Google AI Studio (Gemini) | `GOOGLE_API_KEY` | generous Flash rate limits |
| Ollama (local) | none | fully local |
| Ollama Cloud | `OLLAMA_API_KEY` | optional bearer for `*-cloud` tags routed through a signed-in `ollama serve` |

Per-provider default models and the model-catalog override (`CHATGPT_MODELS`, `CLAUDE_MODELS`, `GEMINI_MODELS`, `OLLAMA_MODELS`) are documented inline in `.env.example`. See the "Getting API keys" sections in `docs/design.md` for step-by-step signup instructions.

## Troubleshooting

| Symptom | Fix |
|---|---|
| AGW image pull fails | `docker login ghcr.io` if you hit anonymous-pull rate limits, then `docker compose pull agentgateway` |
| `provider_key_missing` | Check `.env` has the key, then `make rotate-keys` (or `docker compose restart adapter-<framework>`) |
| AGW audit log empty | Check `RUST_LOG_FORMAT=json`, policy present on route, correct ai.routes map |
| Stack won't come up | `docker compose logs agentgateway` then `harness-api`; verify all 16 services are up via `docker compose ps` |
| Ollama unreachable | `docker network inspect aiplay_net \| grep Gateway`; adjust `HOST_DOCKER_INTERNAL_IP` in `.env` and/or `agw/config.yaml` `hostOverride` |
| chatgpt + responses returns 503 | Known AGW routing bug (see `docs/findings-plan-b.md`) — unrelated to adapter code |

## Layout

```
/my/ws/aiplay/
├── README.md                   # this file
├── docker-compose.yaml         # topology (16 services)
├── Makefile                    # up, down, logs, reset, rotate-keys, test
├── .env / .env.example         # provider keys + per-provider default models
├── agw/config.yaml             # AGW routes + cidgar policy
├── adapters/                   # 8 framework adapters
│   ├── langchain/        (5001)
│   ├── combo/            (5008)   # multi-LLM round-robin + multi-MCP fan-out
│   ├── direct-mcp/       (5010)
│   ├── langgraph/        (5011)
│   ├── crewai/           (5012)
│   ├── pydantic_ai/      (5013)
│   ├── autogen/          (5014)
│   └── llamaindex/       (5015)
├── harness/                    # FastAPI backend
│   ├── main.py / api.py / runner.py / efficacy.py / validator.py / audit_tail.py
│   ├── adapters_registry.py / providers.py / models.py / templates.py / trials.py
│   └── defaults.yaml           # matrix seed + turn templates
├── mcp/                        # 5 MCP services (weather, news, library, fetch, mutable)
├── mock_llm/                   # in-compose deterministic LLM
├── frontend/                   # plain HTML + AG-Grid + CodeMirror (no bundler)
│                               # pages: index.html, trial.html, pairs.html
├── data/                       # gitignored: trials/, matrix.json, settings.json
├── tests/                      # 34 test modules / ~307 pytest cases
└── docs/
    ├── design.md                  # original design (Plan A scope)
    ├── findings-plan-b.md         # per-adapter quirks + verdict matrix (Plan B output)
    ├── code-review-plan-a-and-b.md
    ├── enhancements.md            # open brainstorm topics (OTel, marker alternatives, combo, …)
    ├── brainstorming.md           # running decision log
    ├── memory-log.md              # parked items / deferred tasks
    └── plans/                     # Plan A + Plan B implementation plans
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
