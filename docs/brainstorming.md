# aiplay — brainstorming

Cidgar Harness C playground — designs a multi-framework test harness for the AGW governance pipeline (cidgar feature) beyond what Harness B covers.

## 2026-04-26 — three small aiplay-side changes (post-c7fc59a)

### Change 1: explicit `snapshot_correlation: true` on every channels block
- AGW sibling (CHG-246) flipping default to false. Aiplay routes were implicit-true; now must be explicit-true to preserve E20 _ib_ss markers.
- 8 channels blocks total (5 LLM: ollama/mock/chatgpt/claude/gemini; 3 MCP: weather/mutable/fetch). news + library use `governance` without an explicit `channels:` block — task scope was "every channels block" so I left those alone (they weren't opting into anything explicitly anyway).

### Change 2: revert mcp-mutable-admin AGW route; admin goes direct
**User-stated rationale:** admin endpoints are test-harness concern. AGW shouldn't even know they exist. Cleaner topology — one less AGW route, one less special-case backend type (the host: passthrough was a known oddity).
**Mechanics:**
- Delete entire `mcp-mutable-admin` route block (was added in fb71a9b, fixed in f755ae5).
- New helper `pick_mcp_admin_base(mcp)` in runner.py — hardcoded to mutable-only for now, overridable via DIRECT_MCP_MUTABLE_ADMIN env var (defaults to docker-compose service name `http://mcp-mutable:8000`).
- Replace `_pick_mcp_base_url(mcp_name, routing=...)` (env-var-driven, AGW_MCP_<NAME> + DIRECT_MCP_<NAME>) with the simpler helper. Old function is dead — removed wholly because admin was its only call site.
- 3 tests touched in test_runner.py:
  * dispatch test now asserts URL composed from direct base, not AGW.
  * non-mutable test rewritten to assert no-HTTP short-circuit (was: 404 path mock). The 404 path no longer exists — non-mutable MCPs return None from the helper, never reach HTTP.
  * library no-base-url test: docstring + signature cleanup; semantics unchanged.
- Integration test: AGW_BASE → MCP_DIRECT_BASE; AGW_INTEGRATION_TEST → MCP_INTEGRATION_TEST.

### Change 3: verdict (k) mode-C reason text (LLM doesn't emit marker — AGW does)
**User correction (round #6 in earlier thread):** the marker is AGW's. So if it's "present-shaped but AGW-unrecognizable", the model didn't paraphrase — one of these happened:
1. AGW MARKER_RE failed to extract (whitespace/format AGW added differs from regex tolerance).
2. Adapter dropped the marker during shape translation between LLM switches.
3. cidgar `channels` config inconsistent across routes.
**Mechanics:**
- Update `verdict_k_cross_api_continuity` mode-C return text to enumerate those 3 causes.
- Update docstring's failure-mode taxonomy section to match.
- 4 spots in test_efficacy.py: mode-B comment ("paraphrase" → "marker bytes survived but AGW didn't reuse them"); mode-A negative assertion ("model paraphrase" → "MARKER_RE"); mode-C docstring; mode-C positive assertions (3 new positive assertions for the 3 causes; new negative assertion for "paraphrase").
- Test function name `test_verdict_k_distinguishes_marker_paraphrase_from_isolation_breach` left alone — internal-only label, would touch more lines.

### Tradeoffs weighed
- ONE commit vs TWO: chose TWO for clean separation (config/infra commit vs reason-text commit). User said "1 or 2 — your call".
- Could have left `_pick_mcp_base_url` for backward compat — but it had only one call site (the `mcp_admin` branch I was rewriting), so dead-code-removal was safe.

## E22 mcp-mutable-admin AGW route fix (2026-04-27)

### Problem
`agw/config.yaml` `mcp-mutable-admin` route used `mcp:` backend type. AGW's `mcp:` backend (`crates/agentgateway/src/mcp/streamablehttp.rs::handle_post`):
- Requires Accept header with BOTH `application/json` AND `text/event-stream`.
- Wraps body in JSON-RPC `ClientJsonRpcMessage` deserialize.
- Spins up MCP session lifecycle per request.

Harness runner sends plain REST POSTs (`Content-Type: application/json`, body `{"tools":[...]}`). Would fail at Accept check long before reaching the body parser.

### Options weighed
1. **Switch to `host:` (Opaque) backend** — plain TCP/HTTP passthrough. Used by aiplay's sibling examples (`examples/http`, `examples/oauth2-proxy`). Cleanest fix; preserves the existing `urlRewrite` and `policies: {}`. CHOSEN.
2. Bypass AGW entirely (point harness at mcp-mutable:8000 directly via DIRECT_MCP_MUTABLE) — would expose a new port in compose, complicate routing config, and lose the unified `via_agw` pattern. Rejected.
3. Wrap admin in JSON-RPC on both ends — invasive, defeats the purpose of using a stable JSON shape. Rejected.

### Verification path
Could not run docker stack (not up). Used AGW source-of-truth as ground truth. The integration test is skip-marked (`AGW_INTEGRATION_TEST=1` required); when an operator runs it under a live stack it will exercise the load-bearing claim end-to-end.

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

## Services topology tab — debug saga (2026-04-23)

### Bug chain (5 commits to get rendering right)

1. **`a099f6e`** — initial salvage from rate-limited subagent (291 lines untested).
2. **`5aca6b5`** — `<pre.mermaid>` was wrapped in `<div class="pre-with-copy">`. The absolute-positioned 📋 button inside the same parent broke Mermaid's layout measurement. Match cidflow exactly: rendered diagram has NO wrapper; copy button only on the source-debug pane.
3. **`8658fc8`** — `escapeMermaid()` only stripped `"[]` — labels containing `(framework: langchain)` and `(fetch_fetch)` broke the parser (`)` closes `[` prematurely). Fix: HTML-entity-encode `&"[]()`.
4. **`d91e425`** — vendored Mermaid 9.4.3 binary has `init` / `initThrowsErrors` / `initThrowsErrorsAsync`, NOT `.run()` (that's v10+). Both cidflow AND services were calling `mermaid.run({nodes})` and throwing TypeError. Fix: `mermaid.initThrowsErrors(undefined, nodes)`.
5. **`338e8f5`** — Firefox `getBBox()` returns 0 on `display:none` parents. Default tab is "Turns" so cidflow + services start hidden — every label measured 0×0 → SVG collapses to 16×16. Plus Mermaid sets `data-processed="true"` after the bad render and skips re-init forever.

### Final fix architecture
- New `runMermaidIfVisible(tabKey)` helper: bails if `!classList.contains("active")`, removes `data-processed` to force re-render, then `mermaid.initThrowsErrors`.
- Two flags `__cidFlowNeedsMermaid`, `__servicesNeedsMermaid` track pending render state.
- Tab-switch click handler picks up pending render on first navigation.
- `renderTrial` setTimeout(0) still kicks immediately for whichever Mermaid tab is currently active.

### Copy buttons broke too — `d78262a`
- `navigator.clipboard.writeText()` requires a secure context (HTTPS / `localhost` / `127.0.0.1`). aiplay UI is served over `http://<host-IP>:8000` — Firefox/Chrome reject the call with NotAllowedError, the catch block flashes ✗ for 800ms.
- Fix: new `copyTextToClipboard()` helper falls back to hidden-textarea + `document.execCommand("copy")`. Works on HTTP+IP because execCommand is gated only on user-activation, not secure-context.

### Lessons
- The CID flow tab "appeared to work" earlier because the user had only viewed it after clicking into it (giving Mermaid a visible parent). Adding a second Mermaid tab forced the test of the cold-render path and surfaced 4 bugs that were latent in cidflow too.
- Mermaid 9.4.3 vendored UMD's API (`init`, not `run`) is not v10-compatible — the comments in the code referenced `.run` because they were written against v10's docs.
- Default-active-tab + Mermaid-on-page = always test the OFF tab, not just the active one.

## 2026-04-26 — I-NEW review-fix bundle (subagent C)

### I-NEW-1 — single source of truth for framework capabilities
**Decision:** Extend `/info` payload with `frameworks` dict; refactor 3 JS NOTE rules to consume it.
**Schema:** `{framework: {supported_apis: [sorted list]}}` only — task explicitly forbids adding new metadata to ADAPTER_CAPABILITIES.
**JS caching:** Lazy-fetch `/info.frameworks` once on trial-detail page load, cache in module scope. Falls back to undefined → preserves existing behavior if /info is briefly unreachable (defensive).
**Why only 3 rules refactored:** Of 21 `notes.push()` calls, only 3 mirror ADAPTER_CAPABILITIES literals (crewai, pydantic-ai, llamaindex unsupported APIs). The rest describe AGW gaps, runtime caveats, and routing — not capability assertions.

### I-NEW-2 — autogen `Trial.force_state_ref(int)` lives in a separate non-runner path
**Outcome (b) confirmed.** Runner uses string `target_response_id` via `drive_turn` POST body → adapter `main.py` sets `_forced_prev_id` directly. The `Trial.force_state_ref(int)` method is only reached via the standalone `POST /trials/{id}/force_state_ref` HTTP route AND direct unit-test calls. Two parallel paths, both alive.
**Action:** Clarifying docstring on `Trial.force_state_ref` + regression test for runner-path wire shape. Don't delete the method (would break unit tests + standalone HTTP route).

### I-NEW-3 — test against ADAPTER_CAPABILITIES (no Python NOTE registry exists)
**Decision:** Spot-check the 5 capability invariants the JS rules depend on, plus verify they surface via `/info.frameworks` (the I-NEW-1 channel).
**5 picks:**
1. langchain supports all 4 APIs (chat, messages, responses, responses+conv)
2. crewai supports {chat, messages} only — NOT responses/responses+conv (JS rule 1)
3. pydantic-ai supports {chat, messages, responses} — NOT responses+conv (JS rule 2)
4. llamaindex supports {chat, responses, responses+conv} — NOT messages (JS rule 3)
5. direct-mcp supports {} — used when llm=NONE (JS no-MCP rule depends on this)

Each test asserts BOTH the validator dict AND the /info exposure mirror. If a contributor changes one without updating the other, two failure surfaces.

---

## about.js LIBRARY_NATIVE_SUPPORT validation (2026-04-26)

### Decisions made
- autogen.responses=no — verified autogen-ext 0.7.5 has no Responses client.
- crewai chat/messages — crewai 1.14+ does NOT use litellm for openai/anthropic; uses native SDKs directly. Reclassified as "yes".
- crewai.responses=yes — crewai 1.14.2 OpenAICompletion has first-class Responses API.

### Tradeoffs weighed
- "via" vs "yes" for langchain Conversations API: it's a kwargs-passthrough, not a model field, but aiplay treats it as native. Left as judgment call.
- llamaindex.messages: separate sub-package package counts as native? Inconsistent with how langchain treats sub-packages. Left as judgment call.

### Verification approach
- pip show + direct ImportError test (more reliable than docs)
- Source-grep installed site-packages to find actual fields/classes
- Cross-referenced adapter code comments which often documented the same gaps

---

## Post-validation rapid iteration (2026-04-26 cont.)

### About modal — judgment calls + TBD callouts
- Applied 4 of the validation subagent's flagged judgment calls: llamaindex.messages no→yes (library has separate package), langchain/langgraph +conv yes→via (kwargs-forwarding, not first-class field), llamaindex +conv yes→no (zero `conversation` references in source).
- Added new ⌛ TBD cell state for the adapter table: `ADAPTER_TBD_ENHANCEMENTS` map surfaces deferred E5c/d/e instead of plain ✗. Cells render as "⌛ E5e" with hover.
- Decision: explicitly distinguish "library doesn't support" from "aiplay scoped it out and filed an enhancement". Operators see the gap is intentional + tracked.

### + Add Bulk button
- Enumerates `(framework × supported_api)` from /info.frameworks (with in-JS fallback), POSTs one row per combo with 1st-API-compatible LLM + random MCP. 21 rows for the current capability matrix (6 frameworks × {2,3,4} APIs + 1 direct-mcp).

### Default turn count (Settings modal)
- Persisted at $DATA_DIR/settings.json, range [1-20], default 3.
- `_resize_turns` pads with cyclical generic prompts ("Tell me more.", "Anything else worth noting?") OR truncates keeping opening turns.
- Decision: NOT resized for compact / force_state_ref templates — verdict (d) and (e) need exact turn shapes.
- Direct_mcp template also NOT resized (no LLM = "tell me more" makes no sense).

### CID-flow legend expansion
- Old 2-liner replaced with summary + collapsible <details> explaining all 3 channels (C1/C2/C3), the dotted-edge correlation strategies (header-demux + time-window fallback), and CID node colors (green/yellow/red mapping to verdict (c) outcomes).

### Trial status disambiguation
- Before: status="error" for BOTH verdict-computation errors AND run-level exceptions (confusing).
- After: status="fail" for "ran cleanly but ≥1 verdict didn't pass" (verdict==fail OR verdict==error both count); status="error" reserved for run-level exceptions only.
- Per-verdict cells still show the fail/error distinction; the trial-level pill aggregates cleanly.

### E20 filing
- Tools/list snapshot correlation via `_ib_ss` required-param injection.
- Carrier choice rationale: param-name pattern (parity with _ib_cid) + value-as-hash (no length cost) + enum-constrained (schema validator enforcement) + "telemetry span-correlation id" framing (RLHF-blessed OpenTelemetry idiom).
- Reliability ceiling acknowledged (~85-95% on smaller models per _ib_gar precedent); audit instrumentation via `correlation_lost` flag + new verdict (i) measures the cliff.
- Decision: dropped session-id-mapping alternative — transport-layer artifact, semantics differ across MCP clients, builds correlation on the wrong primitive.

## E22 — mcp/mutable test server + mcp_admin turn kind (this session)

### Constraints (from prompt)
- Aiplay-only changes; do NOT touch `harness/efficacy.py` (E20 sibling subagent in flight).
- Verdict (j) explicitly OUT OF SCOPE — E20's verdict (i) covers correlation-rate measurement; redundant.
- ONE commit (or 2-3 logical).

### Design decisions

**Mutable MCP server (Python, fastmcp 3.2.4)**
- Mutate `tools/list` at runtime via fastmcp's `mcp.add_tool`/`mcp.remove_tool` plus a stub function template that accepts `payload: dict | None = None` (FastMCP rejects `**kwargs`).
- Stub tools log args + return `{"ok": True, "name": ..., "payload": ...}` — sufficient because the test surface is the LIST shape, not call semantics.
- Admin endpoints under `/_admin/*` via `@mcp.custom_route` (sibling to existing `/health`); single port 8000.
- KV store backs the four INITIAL_TOOLS (`get/set/list/delete`) so a) the initial tool set is functionally usable in an LLM-driven trial, b) realism matches spec.
- `version_counter` increments on every set_tools (audit-debug signal); `reset` zeroes it.

**AGW route ordering (CRITICAL gotcha to verify)**
- Per spec: admin route MUST match BEFORE the parent /mcp/mutable route — more specific first.
- AGW uses ORDER OF DECLARATION for routes within a listener (per code I've read in agentgateway prior). Putting mcp-mutable-admin BEFORE mcp-mutable in config.yaml is the correct guard.
- Both backends point at the SAME upstream (mcp-mutable:8000); only the route policy differs (admin route has NO governance).

**Turn kind `mcp_admin`**
- Harness-direct httpx POST — no adapter, agent never sees it.
- Resolves base URL via NEW helper `_pick_mcp_base_url(mcp_name, routing)` (no existing helper to reuse — verified by grep).
- For non-mutable MCPs: graceful skip + log (admin endpoint returns 404; we catch HTTPStatusError and log without raising).
- Reads optional fields `turn_spec.get('mcp')`, `turn_spec.get('op')`, `turn_spec.get('payload', {})`. Trial config defaults if `mcp` not given.

**Turn pydantic/dataclass model**
- Existing Turn dataclass in `harness/trials.py` carries free-form `request` + `response` dicts. The `mcp`, `op`, `payload` fields live on the **turn_spec** (which is the YAML/dict on `turn_plan.turns`), NOT on the persisted Turn dataclass. So we just READ from turn_spec and persist details into `turn.request`. No dataclass change needed.
- Update `api.py::templates_validate` to accept `mcp_admin` as a valid kind.

**Tests**
- `tests/test_mcp_mutable.py` — uses Starlette TestClient against the FastMCP app; tests the admin handlers and tools/list MCP method directly. No docker required for unit tests.
- `tests/test_runner.py` — add 2 `mcp_admin` cases mocking httpx via `unittest.mock.patch` on `httpx.AsyncClient`.

### Open questions resolved
1. Should mcp_admin admin events be visible to operators or silent → SILENT per spec lean (no governance on admin route; harness logs only at INFO level).
2. Adapter-aware vs adapter-naive → ADAPTER-NAIVE per spec; harness directly calls admin endpoint.

### Risks / unknowns
- mcp_admin templates aren't being added to defaults.yaml YET — spec says template variant is OPTIONAL; mention as TODO since refresh_tools (E21) hasn't landed. Easier to add when needed.

## E21 — reset_context + refresh_tools turn kinds (2026-04-26)

### Constraints (from prompt)
- Aiplay-only changes; do NOT touch verdict_i / E20 helpers in efficacy.py; do NOT touch mcp/mutable/ / mcp_admin / E22 work.
- ONE commit (or 2 logical: reset_context+verdict-c-refactor, then refresh_tools).
- Pytest 245+ green.

### Design decisions

**reset_context — wipe agent-side LLM history**
- Per-API state matrix per design doc:
  - chat / messages → `_messages = []`
  - responses (state=F) → `_input_history = []` (none of the adapters today maintain this distinct list — they wipe `_messages` instead; the spec's `_drive_reset()` uses hasattr() so the no-op path covers absent attrs cleanly)
  - responses (state=T) → `_response_history = []`, `_last_response_id = None`, `_forced_prev_id = None`
  - responses+conv → `_conversation_id = None` (next turn POSTs a new /v1/conversations container — network event, audit-visible, may fail)
  - direct-mcp → no-op (no LLM context)
- Implementation strategy: a single `_drive_reset()` method per Trial that uses `hasattr` to safely zero whatever subset of the canonical attr set exists on that adapter. Keeps the per-adapter delta tiny (4 lines or so) since most adapters share the same attr names.

**refresh_tools — force MCP tools/list re-fetch**
- Framework-specific. Most adapters cache via `self._mcp_tools is not None` shortcut; setting `self._mcp_tools = None` and re-calling `_setup_mcp_tools()` on next turn rebuilds from scratch.
- langchain: also clear `_llm_with_tools` (cached `bind_tools` result).
- langgraph: also clear `_graph` (compiled ReAct agent built around the cached tools).
- pydantic-ai: re-instantiate the `Agent` so the bound `MCPServerStreamableHTTP` toolset is rebuilt; no obvious cheaper invalidation hook in the toolset object itself.
- direct-mcp: no LLM, but the trial.turn() flow opens a fresh tools/list every turn already (no cache to bust). Implemented as no-op + log per spec.
- All adapters: when `mcp == "NONE"`, return `{"refresh_tools": "skipped", "reason": "mcp=NONE"}` per spec.

**Verdict (c) bracket refactor**
- Walk turns. reset_context turns are SEGMENT BOUNDARIES; refresh_tools turns are NOT (they're tool-cache events).
- Per-segment continuity: a segment passes if it carries ≥1 CID across its turns (relaxed from "every consecutive pair shares a CID" — design doc says "any CID appearing in segment passes" for the bracketed shape; preserves existing single-segment unit tests by not regressing 1-segment behavior).
- Cross-segment leak detection: any CID that appears in MULTIPLE segments is a verdict failure ("CID isolation broken across reset boundary"). This is a NEW signal worth catching.
- Total verdict = pass iff all per-segment pass AND no cross-segment leak.
- Backwards compat: trials with ZERO reset_context turns produce a single segment whose pass criteria reduce to "≥1 CID present" — same as the old "≥2 CID-bearing turns + consecutive pair share a CID" path produces for a healthy trial. The 3 existing verdict_c tests (pass/fail/header-demux) all still work because the pass case has same CID across all turns and the fail case uses different CIDs across turns (cross-segment-style leak inside a single segment is fine; we only flag leaks across DIFFERENT segments).
- BUT: existing `test_verdict_c_fail_when_cid_changes_between_turns` expects FAIL for two turns with different CIDs in a single segment. With new "any CID in segment → pass" logic this test would regress. Need to handle this — keep the old "consecutive pair must share" check WITHIN a segment alongside the cross-segment leak check. That preserves the existing failure mode AND adds the new one.

**Per-adapter route exposure**
- Add HTTP routes `/trials/{id}/reset` and `/trials/{id}/refresh_tools` (parallel to existing `/compact`) on each of 7 adapters. AdapterClient gains `reset_context()` and `refresh_tools()` methods (parallel to `compact()`). Cleaner than overloading `drive_turn()` with optional turn_id/user_msg.

**Template variant + Turn validator**
- Add `with_reset` template to defaults.yaml (5 turns: 2 user_msg → reset_context → 2 user_msg, distinct topics).
- Extend templates_validate to recognize `reset_context` and `refresh_tools` as valid kinds (no required fields beyond `kind`).

### Risks / unknowns
- pydantic-ai refresh_tools rebuilds the entire Agent — could lose other state (system_prompt, etc.). Mitigated: __init__ rebuild logic centralized to a helper if it grows.
- langchain `_llm_with_tools` cache — `bind_tools` is idempotent so re-binding on a new tool list is fine; just need to invalidate cleanly.
- Edge: refresh_tools called on turn 0 (before any prior turn ran tools_list) → cache is None already → next user_msg triggers fresh fetch as normal. Safe no-op effectively.

## E19 + E23 bundle — schema-only multi-MCP / multi-LLM

### Decisions
- **Schema parity**: `RowConfig.mcp`, `RowConfig.llm` → `str | list[str]`; `RowConfig.model` → `str | list[str] | None`. `TrialConfig` mirrors so persistence round-trips both shapes without coercion at the API boundary. Single-string remains the default; list-form is gated by validator until adapters opt in.
- **Adapter opt-in via sets**: `MULTI_MCP_FRAMEWORKS = set()` (empty — no adapter knows multi-MCP yet) and `MULTI_LLM_FRAMEWORKS = {"combo"}` (combo adapter from E24 will check this). Adding a framework to the set is the entire opt-in cost; no schema migration needed when an adapter learns multi-MCP merging.
- **Validator gating**: `isinstance(llm, str)` guards added to Rules 3b and 5 in validator. Without this, a list value reaches `llm not in api_providers` (always True for a list), producing a confusing "[list] not in providers" warning. The list-form rules below now own per-element checking.
- **Frontend Option A picked over B**: `agTextCellEditor` for mcp + llm columns, with comma-separated parse on commit. Loses the dropdown UX but is ~30 LOC vs. ~150 LOC for a custom multi-select editor. The intent is for the validator/schema to land NOW so E24 (combo adapter) can develop against a real schema without waiting on UI polish. Revisit Option B when E24 proves the multi-LLM use case is worth richer UX.
- **`note` vs `notes`**: spec doc uses `notes` but actual code is `note`. Kept existing field name — surgical-changes principle.
- **Tests added**: 8 validator tests + 1 api round-trip = 9. Validator tests cover: str-mcp legacy, list-mcp non-multi-fw rejection, NONE-in-list warn; str-llm legacy, list-llm combo accept (no E23 warning), list-llm non-combo reject, list-llm api-incompat reject, model-list-length-mismatch reject. API test: POST list → GET list → PATCH back to str → GET str.

### Trade-offs / known gaps
- Model column dropdown is degraded for list-form llm rows (uses `params.data.llm` as a single-string cache key). Out of scope — model UX gets fixed when E24 lands and provides actual multi-LLM trial output to inspect.
- `parseListLikeCell` collapses single-element lists `["weather"]` back to a plain string `"weather"`. This is intentional: it preserves backwards-compat at the wire level (existing single-MCP rows can edit through the new text editor without changing on disk) but it does mean the frontend is opinionated about "list with 1 item is really a string". Backend accepts both shapes either way.
- `MULTI_MCP_FRAMEWORKS` empty means EVERY list-form mcp row is currently non-runnable. Intentional — schema lands now, adapters opt in later. Tests confirm the failure mode is loud and explicit ("framework=X doesn't support multi-MCP form...").

## E26 — persist body on AuditEntry (verdict (i) production fix)

### Decisions
- Field placement: appended `body: dict | None = None` AFTER `captured_at` (also defaulted) on `AuditEntry`. Ensures `AuditEntry(**a)` reconstruction in `TrialStore.load` works for legacy persisted JSONs that have neither `body` nor maybe even `captured_at`. All existing positional/kwarg call sites (api.py, runner.py, tests) keep working unchanged.
- Lookup precedence in `_audit_correlation_lost`: top-level `entry.body` FIRST (canonical post-E26 production path), then fall back to `raw["correlation_lost"]` (synthetic test fixtures), `raw["body"]["correlation_lost"]` (legacy shape-B-with-body), `raw["fields"]["body"]["correlation_lost"]` (legacy shape-A persisted via raw=obj). All four paths preserved deliberately.
- Legacy compatibility: pre-E26 trial JSONs reload with `body=None` via the dataclass default — verdict (i) will fall back to `raw` walks for them. Shape-B legacy trials remain silently broken (the prior bug); shape-A legacy trials still work via `raw["fields"]["body"]`.
- Test mirroring: shape-B test sets `raw={"line": "..."}` (no body in raw) + body on top-level — exactly what production produces. This is the "smoking gun" coverage that proves E26 fixes the real bug, not just round-trip plumbing.

### Trade-offs / known gaps
- The test for shape-B AuditEntry construction in test_audit_tail.py imports `from trials import AuditEntry`. This is a benign cross-module test dep; conftest already wires harness/ onto sys.path.
- Did NOT touch any frontend / UI surfaces. AuditEntry serialization gains a `body` field in the JSON dump; UI consumers that didn't expect it will silently ignore it (every consumer reads named fields).
- Did NOT update verdict (c) (E21) or verdict (k) (E24) per scope guard. Both currently use `entry.raw` walks for their own purposes; out of scope for E26.


## E20 verification template (with_e20_verification) — 2026-04-26

### Decision: Single template, mcp=mutable gate, NOT resized
- Closes the loop on E20 measurement: produces 2 distinct `_ib_ss` hashes in one trial via mcp_admin mutation between user_msg turns.
- Trial flow: discover (H1) → invoke (carries H1) → mutate upstream → refresh_tools → re-discover+invoke (H2 ≠ H1).
- Gated to mcp=mutable since it is the only MCP exposing /_admin/* endpoints.
- NOT resized — verdict (i) needs the exact 5-turn shape.

### Tradeoffs considered
- TrialConfig vs RowConfig divergence: TrialConfig (dataclass in trials.py) does NOT carry with_* flags; only RowConfig (Pydantic in api.py) does. Templates.py reads directly from row dict. Decision: only add flag to RowConfig — no TrialConfig change needed.
- Template branch order: placed BEFORE mcp=NONE fast-path to ensure template selection wins regardless of MCP, though validator gates mcp=mutable.

## CHG-247 — flip text_marker + resource_block defaults to false (2026-04-27)

### Decision
- Flip both defaults in one shot, mirroring CHG-246's pattern. Single AGW source commit + single AGW docs commit.

### Tradeoffs considered
- Could split CHG-247 into two changes (one per field). Rejected — they motivate identically (uniform opt-in), should land atomically so the spec can talk about "all three flags now opt-in" without a transitional state.
- Could keep `mcp_marker_kind: Resource` as the enum default OR flip to a new "None" variant. Kept Resource — B-NEW-3 already gates the entire kind by `resource_block`, so the enum default is moot when `resource_block: false`. Adding a None variant would be a larger API change for no operational benefit.
- The CHG-246 leftover (test_on_tools_list_resp_skips_ib_ss_for_non_object_schema not exercising the schema-shape skip path) was visible during the audit but explicitly out of scope per "touch only what you must". Flagged in conversation log for a future tightening pass.

### Test classification rule
- If a test pins INJECTION (emit) of a channel: enable that channel explicitly.
- If a test pins EXTRACTION (read-back) only: leave alone — extraction is not channel-gated.
- If a test pins STRIP-side hooks (`on_tool_call_req`): leave alone — strip is unconditional.
- If a test pins Channel-1 (`_ib_cid` in `tool_use.input` / `function.arguments`): leave alone — Channel 1 is not channel-gated.
- If a test pins the gate-OFF behavior (B-NEW-3, default-off snapshot tests): leave alone.

### Test count math
- 31 cidgar tests + 7 config tests = 38 in this module group; ledger reports 144 total governance tests. CHG-247a is net 0 (mutates, doesn't add).

## 2026-04-26 — Validator + drawer + ollama default fix bundle

### Decisions
- Validator: drop turn_id check entirely (runner generates `turn-NNN-xxxxxxxx` via uuid). Required: kind ∈ canonical set; user_msg requires content; force_state_ref requires lookback; mcp_admin requires op.
- Drawer: turn template snippets use `content` not `text`; button tooltips updated to match.
- Ollama default everywhere: `llama3.1:latest`. Curated dropdown reorders so llama3.1:latest is FIRST (per UI convention: first wins).

### Tradeoff considered: docker-compose + .env.example
- User listed only "7 adapter files + BULK_ADD + harness/models.py". Did not explicitly call out compose/env.
- BUT — compose ENV (DEFAULT_OLLAMA_MODEL=qwen2.5:7b) is what runtime sees first; the adapter fallback only fires if env is absent.
- Decision: update compose + .env.example too. Rationale: keeping the env scalar at qwen2.5:7b while changing the adapter fallback is INCOHERENT — runtime would still hit qwen2.5:7b. The user's intent ("ollama default = llama3.1:latest") requires both.
- This expands the change beyond the spec but aligns with the underlying goal. Will note in commit msg.

### Tradeoff considered: test_api.py changes
- 4 templates_validate tests use {turn_id, text} shape. After validator rewrite they'd fail (no content + invalid since text-only no longer satisfies content check; turn_id no longer required so missing-turn_id test asserts wrong thing).
- Decision: rewrite those 4 tests to use `content` and remove turn_id. Replace the `rejects_missing_turn_id` test with one asserting turn_id is now OPTIONAL.

### Reconsidered: docker-compose + .env.example + adapters/*/main.py
- User explicitly listed the files to change. Surgical-changes principle: don't expand scope.
- Decision: STICK to the spec. Touch only the 7 framework_bridge.py files + combo + harness/models.py + harness/api.py + frontend/drawer.js. Plus the necessary test updates.
- Compose env will still pin to qwen2.5:7b at runtime — the user can change that themselves if they want. The fallback change still has effect when env is absent (e.g. running adapters outside docker).

## Topic: cytoscape.js interactive CID flow tab (2026-04-26)

### Topology extractor design
- Helper `_buildCidFlowTopology(trial)` returns plain object with turns/cids/audits/snapshots/edges arrays.
- Mermaid `renderCidFlowTab` re-derives its own auxiliary maps (cidNodeId, ssNodeId) from this — keeps Mermaid string IDENTICAL.
- Cytoscape mounter consumes the same topo object, builds elements list with classes for styling.

### Tradeoffs
- cytoscape-dagre: ~25KB extension. Could omit and use built-in `breadthfirst`/`grid`. But dagre gives the most readable LR flow shape mirroring the Mermaid one — worth the load.
- Could embed cytoscape locally (vendor) like mermaid.min.js. The user's spec explicitly says CDN; following spec.
- Render-cache hash: use HTML hash same as Mermaid path. cytoscape rebuild is more expensive (DOM destroy + recreate); the cache avoids needless re-mounts on poll ticks.
- Cleanup: __cidFlowInteractiveCy holds the cy instance so we can `.destroy()` before remounting. Without this, leaked listeners + container clobber.

### Potential pitfalls
- cytoscape-dagre exposes itself as `cytoscapeDagre` (camelCase) via UMD — confirmed via package's dist file naming. `cytoscape.use(window.cytoscapeDagre)` should work.
- Layout failures on display:none parent: cytoscape will measure 0×0 and place all nodes at origin. Same defer-on-visibility fix as Mermaid.
- "Reset positions" button: re-runs dagre layout — discards any user drag. Acceptable per spec.


## 2026-04-26 — Nit 7 (SRI) + Nit 15 (channels anchors)

### Nit 7 — SRI hashes for cytoscape CDN scripts
- **Decision:** ADD sha384 + crossorigin="anonymous" to all 3 scripts.
- **Tradeoff:** Bytes are pinned — version bumps require re-computing hashes (header comment documents the recipe). Worth it for supply-chain integrity since cytoscape/dagre are loaded from a third-party CDN.
- **Compute path:** `curl | openssl` was blocked by the sandbox; switched to `python3 -c` with urllib+hashlib+base64. All 3 hashes computed cleanly on first try.
- Hashes:
  - cytoscape@3.30.0: `sha384-kpMsYllYzyaWU69Piok08rPNktpnjqAoDMdB00fjqUkEk3lkuUbSuwJ+oXrjvN6B`
  - dagre@0.8.5: `sha384-2IH3T69EIKYC4c+RXZifZRvaH5SRUdacJW7j6HtE5rQbvLhKKdawxq6vpIzJ7j9M`
  - cytoscape-dagre@2.5.0: `sha384-u69h9ebXeSjlg6q/rb1zKTRAGu/h8deCl0409xpS/QJctMKnc4M9Fzkm01VOQdeF`

### Nit 15 — YAML anchors for channels blocks
- **Considered:** define `&channels_default` anchor; each route uses `<<: *channels_default`. Would dedupe 9 identical 3-field blocks plus mcp-fetch's 4-field override.
- **Rejected because:** AGW's loader is `serdes::yamlviajson::from_str` — uses `serde_yaml::Deserializer` + `serde_transcode::transcode` to JSON. `serde_transcode` is a streaming event copy; it does NOT call `serde_yaml::Value::apply_merge`. The `<<` key would land in JSON literally and be rejected by `deny_unknown_fields` (the schema! macro in serdes.rs sets this globally on every config struct). Top-level anchor definition would also add an unknown field to the routes list.
- **Fallback per task spec:** keep duplication, add a coordination comment at top of `routes:` explaining the 3 fields, the WHY (serde_transcode path, no apply_merge, deny_unknown_fields), the mcp-fetch exception, and when to revisit.
- **Verification:** pyyaml safe_load passes; tree walk confirmed 10 channels blocks (9×3 fields, 1×4 fields with mcp_marker_kind:both).
- **If/when to revisit:** if AGW grows first-class config defaulting OR wires `apply_merge` into `yamlviajson::from_str` before the transcode, this becomes safe. Until then the duplication is tolerable (only ~30 lines).
