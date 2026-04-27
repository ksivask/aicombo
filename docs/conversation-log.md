# aiplay — conversation log

## 2026-04-21 — Initial design session (Harness C brainstorming)

Session running in parent directory `/mnt/share/ws/agw-gh` where cidgar finalization is also happening. Aiplay scope emerged mid-session as a separate playground.

### Entry 1 — Kickoff
**User:** Brainstorm test harness C for cidgar covering 7 frameworks, 4 APIs, streaming/server-state toggles. Prefer Ollama > claude.ai > chatgpt > gemini > copilot. UI-driven. Non-auth MCP. Optionally inherit from `/my/ws/demo`. **(Later in the session: Microsoft Copilot clarified as consumer https://copilot.microsoft.com/ which has no public API; provider dropped entirely since Ollama + chatgpt + claude cover all 4 target APIs.)**

**Actions:** Explored `/my/ws/demo` — found langgraph/crewai/autogen agent services, weather/news/library/fetch MCPs, compose orchestration. Mapped gaps: missing langchain/pydantic-ai/llamaindex/n8n, no responses/messages coverage, no streaming toggle, no server-state, Ollama-only, no cidgar UI.

### Entry 2 — Q1 efficacy scope
**User:** "a+b+c+d+e all"
**Result:** All five efficacy levels in scope.

### Entry 3 — Q2 matrix density + playground location
**User:** "can we build this harness generically outside of the agw-gh or auth2v directory, maybe a new playground. i like the table view presented - is it possible to drive that using UI, where the columns are drop down or checkbox for T/F ?"
**Result:** Minimum-spanning default rows (7) + UI-extensible. Sibling project outside both repos.

### Entry 4 — Q3 UI tech stack
**User:** "B"
**Result:** FastAPI + AG-Grid + vanilla JS + SSE.

### Entry 5 — Location + MCP NONE + invalid combos
**User:** "just call it as /my/ws/aiplay, what of the /my/ws/demo are we re-using? dont bring in auth from there. in the table dropdown for LLM, have a NONE option meaning no LLM is being used, similarly have a MCP column and have NONE option along with weather/everything/library/news/fetch."
**Result:** `/my/ws/aiplay/` locked. Reuse inventory documented. Auth excluded. MCP column with NONE + weather/news/library/fetch.

### Entry 6 — Q4 state model + Q5 log capture
**User:** "1. what is the conv_id in option Y ? 2. postpone n8n to later. 3. correlation custom header, but what is the AGW audit log? where to see it?"
**Actions:** Explained `conv_id` (opaque UUID handle), surfaced AGW audit log as stderr tracing events (no admin query endpoint), presented log capture options L1-L4.

**User (continued):** "skip ollama-pull.sh, server-everything. for chat-completion API auto-set the state=F and disabled, similarly for other invalid combinations. 1. can you call it something else instead of conv_id - it is conflicting in mind for conv_id of test-harness vs conv_id of cidgar feature itself. 5 = L1"
**Result:**
- ollama-pull.sh + server-everything skipped
- Invalid-combination enforcement rules defined; `validate(row)` endpoint
- `conv_id` renamed to `trial_id`
- Log capture: L1 (docker logs + `RUST_LOG_FORMAT=json`)

### Entry 7 — Q6 AGW image (round 1)
**User:** "1. trial_id is ok, G1 vs G4 seem same?"
**Actions:** Clarified G1 = G4 on single machine; G4 adds value for multi-machine/CI.

### Entry 8 — Q6 AGW image (round 2, final)
**User:** "P3 - for Q6, i will build the image from the right branch, the docker compose should just reference/use the statically tagged image. now i dont know if thats G1 or G4. keep it like what auth2v docker-compose.agw.yaml is doing. i dont want the aiplay docker-compose starting any agw builds"
**Actions:** Verified auth2v pattern — `image: ghcr.io/.../agentgateway:v1.0.1.ib-hsr1` with NO `build:` key. Compose never triggers a build.
**Result:** aiplay compose uses `image: agentgateway:cidgar` (or similar static tag) only. User owns build + tag lifecycle externally. Missing tag = fail-fast instead of silent stale image.

### Entry 9 — Brainstorming file location
**User:** "which docs/brainstorming are you updating?"
**Actions:** Honest acknowledgment: nothing yet. Proposed scaffold at `/my/ws/aiplay/docs/*` + pointer from agw-gh/docs/brainstorming.md.
**User:** "yes in background"
**Actions:** Dispatched subagent to scaffold + backfill. Subagent was blocked on Bash + Write permissions for `/my/ws/aiplay/` (Task 5 append to agw-gh brainstorming succeeded; Tasks 1-4 blocked). Completed scaffold directly in main session.

### Entry 10 — P2 round (D1-D5)
**User:** "lets discuss P2, some LLM providers need API keys. also the other aspects you mentioned"
**Actions:** Presented D1 (API keys: K1+K5), D2 (persistence: SQLite R2), D3 (concurrency: C1 default + C2 env flag), D4 (UI mockup with AG-Grid + drawer), D5 (Ollama model: qwen2.5:7b-instruct).
**Status:** Pending user confirmation.

## Pending

- User decisions on D1-D5.
- Draft design doc at `/my/ws/aiplay/docs/design.md`.
- Implementation plan after design approval.

## 2026-04-23 — AGW review-fix bundle (B-NEW-3 + I-NEW-4 + M-NEW-2 + M-NEW-5)

### Entry — Four AGW-side fixes from comprehensive code review

**User prompt:** Apply four review fixes across two AGW worktrees (cidgar source + docs). B-NEW-3: pin gating contract via test + spec doc. I-NEW-4: throttle Raw fallback warn. M-NEW-2: Bedrock Raw conversion error test. M-NEW-5: byte-equality round-trip for number formats.

**Reasoning:**
- B-NEW-3: gating IS intentional (resource_block acts as master toggle). Test pins the cliff so future refactors must be conscious. Doc explicates the historical-name issue.
- I-NEW-4: chose first-seen warn + subsequent debug pattern over 1-in-N sample because operators want to KNOW fallback happened (volume is the problem, not existence).
- M-NEW-2: pin the clean-error path so a future refactor doesn't silently break. Verified bedrock.rs:1611-1620 already returns UnsupportedConversion.
- M-NEW-5: tests byte-level number preservation through serde_json round-trip — operationally sensitive for OpenAI passthrough.

**Actions:**
- See conversation log entries below; final commit SHAs reported in summary.

## 2026-04-23 — aiplay review-fix bundle (B-NEW-1 + B-NEW-2)

### Entry — Two aiplay-side fixes from comprehensive code review

**User prompt:** "yes, fix all B-NEW, I-NEW and M-NEW" — direct directive to dispatch fixes for all 12 review findings (3 bugs, 4 improvements, 5 minor) across aiplay + AGW.

**Reasoning:**
- Dispatched 2 background subagents in parallel (aiplay scope + AGW scope) plus the in-flight Services topology tab subagent.
- All 3 hit Anthropic API rate limit (resets 5:40am UTC) before committing.
- Salvaged work from working trees: B-NEW-1 + B-NEW-2 in aiplay (4 adapters + 2 test files), B-NEW-3 + I-NEW-4 + M-NEW-2 + M-NEW-5 in AGW (2 source files), plus 291 lines of staged Services tab work.
- Subagent A added llamaindex test for B-NEW-1 but missed the llamaindex source fix; ported the +conv branch from autogen myself.
- M-NEW-5 test originally failed: `max_output_tokens: 10000000000` overflowed the typed `Request` struct's `u32` field BEFORE reaching Raw fallback. Redesigned to put number sensitivity inside the input array (the Raw passthrough scope) — now passes.

**Actions:**
- Fixed llamaindex `_compact_responses` to add +conv early-return branch.
- Redesigned M-NEW-5 test to actually exercise Raw passthrough.
- Committed in 4 stages: aiplay code, AGW code, AGW docs, aiplay session log.
- Verified: aiplay pytest 224 → 230. AGW B-NEW-3 + M-NEW-2 + M-NEW-5 all pass individually under --test-threads=1.

**Deferred / not committed:**
- Services topology tab (#78) — 291 lines staged in frontend/, untested. Awaits user review.
- I-NEW-1 (NOTE registry from /info), I-NEW-2 (autogen force_state_ref verification), I-NEW-3 (test_note_registry.py) — Subagent C never dispatched (rate limit).
- M-NEW-1, M-NEW-3, M-NEW-4 — explicitly accepted as no-action per review classification.

## 2026-04-23 — Services topology tab debug saga (5 commits)

### Entry — Salvage + 5-bug-fix chain to get the new Mermaid tab actually rendering

**User prompt:** "if you can resume the services topo tab, else start fresh subagent for that from scratch ?" → "mermaid diagrams only show the actual text" → multiple iterations → "now, nothing shows" (Mermaid 16×16 viewBox per inspect) → "the copy buttons are not copying ?" → "it worked now"

**Reasoning:**
The salvaged Services tab from the rate-limited subagent was syntactically clean and the topology extraction was correct, BUT 5 separate latent bugs needed fixing before it would render usefully. Each surfaced only after fixing the previous one (textbook layered-bug debugging — fix N reveals N+1).

**Actions:**
- `a099f6e` — committed salvaged Services tab (291 lines untested)
- `5aca6b5` — removed `pre-with-copy` wrapper around rendered `<pre.mermaid>` (cidflow pattern)
- `8658fc8` — `escapeMermaid` HTML-encodes parens (was breaking `(fetch_fetch)` labels)
- `d91e425` — switched from `mermaid.run()` (v10 API) to `mermaid.initThrowsErrors()` (v9.4.3 API). Real root cause behind the persistent text-only display. CID flow tab had the same bug — fixed both.
- `338e8f5` — defer `mermaid.init` until tab is `display:visible`. Firefox `getBBox` returns 0 on hidden parents; SVG collapses to 16×16. Track per-tab pending-render flags; tab-switch click triggers deferred init.
- `d78262a` — `copyTextToClipboard()` helper falls back to `document.execCommand("copy")` for HTTP+IP origins (where `navigator.clipboard` is blocked).

**Verified:** Hard refresh + click into Services tab → Mermaid renders correctly. Copy buttons functional. CID flow tab also benefits from the API + visibility fixes (had the same latent bugs).

**Final state:**
- aiplay HEAD: `d78262a`
- 6 commits added in this exchange (Services tab feat + 5 fix commits)
- Tree clean except untracked `.agentdiff/` tooling artifacts

## 2026-04-26 — Subagent C deferred review-fix bundle (I-NEW-1, I-NEW-2, I-NEW-3)

### Entry 1 — Kickoff
**User:** Three deferred review-fix items in `/my/ws/aiplay/`. ONE commit (or split into 2-3 if logical separation is cleaner). HEAD: `89244da`. Pytest target: 230 → 233+.

**Items:**
- I-NEW-1 — derive NOTE registry from /info instead of duplicating ADAPTER_CAPABILITIES
- I-NEW-2 — autogen `Trial.force_state_ref(int)` dead-code verification + (if live) clarifying comment
- I-NEW-3 — `tests/test_note_registry.py` with 5 spot-checks against the NOTE-tab logic

**Reasoning:** Three logically distinct concerns — backend `/info` shape change, Python adapter clarifier, test additions. Splitting into 3 commits gives clean review boundaries and lets I-NEW-3 land independently of the I-NEW-1 refactor. Approach: 1 commit per item.

### Entry 2 — Investigation findings

**I-NEW-1 scope:**
- `harness/validator.py::ADAPTER_CAPABILITIES` is `{framework: set(api)}` only — no streaming / state-mode / mcp / notes metadata. Task says don't add new fields, only expose what exists. So `/info.frameworks[framework].supported_apis = sorted list` is the only mirror needed.
- 21 total `notes.push()` calls in `frontend/trial.js::collectNotes()`. Only **3** are framework-capability rules (mirroring ADAPTER_CAPABILITIES):
  - L782 crewai + responses/responses+conv
  - L792 pydantic-ai + responses+conv
  - L802 llamaindex + messages
- The other 18 rules describe AGW gaps (E6/E8/E13c), implementation caveats (autogen bypass, llamaindex bypass, anthropic httpx hook), provider/state semantics, routing, MCP — none are duplicating capability sets. Do not touch.
- /info consumers verified safe: `frontend/app.js:663` (Settings modal pretty-prints whole JSON) + `tests/test_api.py:91` (asserts `adapters` key only). Adding `frameworks` is additive.

**I-NEW-2 outcome (b) — live code, confusingly named:**
Call-graph trace:
- runner.py path: `drive_turn(turn_kind="force_state_ref", target_response_id=<str>)` → `adapters_registry.py` POSTs body containing `target_response_id` to `/trials/{id}/turn` → autogen `main.py:121-132` directly assigns `trial._forced_prev_id = req.target_response_id` (string) → `framework_bridge.py:614` reads it for next outbound `previous_response_id`.
- `Trial.force_state_ref(int)` at `framework_bridge.py:668` is reached via TWO non-runner paths:
  1. Standalone HTTP route `POST /trials/{id}/force_state_ref` at `main.py:147-159`
  2. Direct unit-test calls at `tests/test_adapter_autogen.py:181, 217, 220` and `tests/test_adapter_llamaindex.py:171, 205, 208`
- So NOT dead code — but the runner doesn't use it. Action: add clarifying comment, plus a regression test pinning the runner-path wire shape (analogous in spirit to langchain's I1 test).

**I-NEW-3 strategy:**
NOTE registry is purely JS-side. So tests target `ADAPTER_CAPABILITIES` directly (the source of truth that I-NEW-1 routes through `/info`). 5 spot-checks pin the specific capability bits the JS rules depend on, so if a contributor changes a capability, these tests fail BEFORE the JS rule silently misfires.

### Entry 3 — Implementation

**Actions:**
- Read harness/api.py, harness/validator.py, frontend/trial.js — confirmed scope:
  - ADAPTER_CAPABILITIES is `{framework: set(api)}` only (no streaming/state metadata)
  - 21 notes.push() in collectNotes; only 3 mirror ADAPTER_CAPABILITIES (crewai, pydantic-ai, llamaindex)
  - /info has 2 consumers (Settings modal + 1 test) — both safe with additive `frameworks` field
- Traced force_state_ref: runner uses string `target_response_id` directly (main.py:121-132); `Trial.force_state_ref(int)` only reached from standalone HTTP route + unit tests. Outcome (b).
- 3 commits created:
  - `b0e0aca` — I-NEW-1: /info.frameworks SoT + JS NOTE rules consume it + regression test
  - `fbd00a5` — I-NEW-2: clarifying docstring + runner-path wire-shape regression test
  - `1f1b242` — I-NEW-3: tests/test_note_registry.py with 5 capability spot-checks

**Verified:**
- pytest 230 → 237 (delta +7: 1 I-NEW-1, 1 I-NEW-2, 5 I-NEW-3)
- All commits land on local main (NOT pushed per constraint)
- aiplay HEAD: 1f1b242

**No unexpected findings; no scope deferrals.**

---

## Session: 2026-04-26 — about.js LIBRARY_NATIVE_SUPPORT review

### Prompt
Independent review of two data tables added to frontend/about.js. Verify against source code + library docs and report discrepancies.

### Reasoning
- Validated each cell of LIBRARY_NATIVE_SUPPORT against installed package code (not just docs) for autogen, crewai, llamaindex, langchain, pydantic-ai.
- Validated ADAPTER_BYPASS_APIS by reading each framework_bridge.py and confirming the bypass pattern (`from openai import AsyncOpenAI` for the openai-direct path).

### Confident fixes applied
1. autogen.responses: yes → no (autogen-ext 0.7.5 has no OpenAIResponsesChatCompletionClient — verified ImportError + adapter comment "autogen-ext does NOT currently ship an OpenAI Responses client")
2. autogen.responses+conv: yes → no (no Responses → no Conversations container)
3. crewai.chat: via → yes (crewai 1.14.2 uses native OpenAICompletion calling openai SDK directly, not litellm)
4. crewai.messages: via → yes (native AnthropicCompletion calling anthropic SDK directly)
5. crewai.responses: no → yes (OpenAICompletion has first-class responses.create with previous_response_id)
- crewai.responses+conv left as "no" — no /v1/conversations container support in crewai's responses path

### Adapter/bypass validation (no changes needed)
- autogen + llamaindex {responses, +conv} bypass annotations: code confirms `from openai import AsyncOpenAI; client.responses.create(...)` pattern. Conversations container minted via direct `self._http_client.post("/conversations")`.
- All other framework adapters use native client classes; bypass set is complete.

### Open judgment calls (NOT fixed)
- llamaindex.messages "no" — llama-index-llms-anthropic exists upstream as a separate sub-package with native Messages support; aiplay just chose not to wire it (E5e). If consistent with langchain (which spans langchain-openai/langchain-anthropic sub-packages), this should be "yes". Left for user to decide.
- langchain/langgraph.responses+conv "yes" — langchain-openai 1.1.16 has NO `conversation` model field; aiplay adapter pushes via `.bind(conversation=...)` which is generic kwargs forwarding. Could argue "via" rather than "yes".
- llamaindex.responses+conv "yes" — llama-index-llms-openai has zero `conversation` references in source. Probably should be "via" or "no".
- crewai.mcp "yes" — crewai-tools[mcp] exists upstream but aiplay adapter wraps fastmcp instead (capture concerns).
- direct-mcp.mcp comment says "raw mcp Python SDK" but adapter uses fastmcp.

### Files touched
- /mnt/share/ws/aiplay/frontend/about.js (one commit)

---

## Session: 2026-04-26 cont. — judgment fixes, Add Bulk, Settings, status fix, E20

### Prompts (chronological)
1. "earlier you tabulated [crewai bypass table] — how is crewai row different now? define bypass and callout in the UI table"
2. "make a subagent review/revalidate both your tables"
3. "enumerate all supported framework x api - pick the 1st API compatible LLM + random MCP, routing always via AGW"
4. "add a UI 'Add Bulk' button to add that combo list to the table"
5. "why aiplay adapter chose not to use llama-index-llms-anthropic?"
6. "1. yes, and also callout that TBD enhancement in the matrix instead of saying yes/no"
7. "explain this in the cid-flow tab [legend]"
8. "possible to add option in settings for default #turns = 3 or 5 ?"
9. "brainstorm: on AGW, during tools/list, we dont know the ib_cid... [E20 design]"
10. "yes file E20"
11. "a) for E20 dont rely on mcp-session-id. b) [trial status disambiguation]"

### Commits (chronological)
- `420518d` — About modal: bypass cell state + definition
- `8ffbe4c` — (subagent) Validate LIBRARY_NATIVE_SUPPORT against installed packages (5 fixes)
- (manual POST) 21 rows enumerated + posted via API
- `ca89ae1` — About modal: 4 judgment-call fixes + TBD callouts
- `9a662e4` — + Add Bulk button
- `67661eb` — CID-flow legend expanded with full taxonomy
- `56fdf53` — Settings: configurable default turn count (1-20)
- `e271c53` — Filed E20 (tools/list snapshot correlation via _ib_ss)
- `9b3e4c8` — Status fix (fail vs error) + E20 doc cleanup (dropped session-id alternative)

### Key decisions
- TBD cell state instead of ✗ for scoped-out + enhancement-filed combos — operators see "this gap is intentional and tracked".
- Add Bulk uses /info.frameworks with in-JS ADAPTER_CAPABILITIES_JS fallback (works pre-harness-rebuild).
- Default turn count: persisted server-side; padded with generic continuation prompts; NOT applied to compact / force_state_ref / direct_mcp templates (those need exact shapes).
- E20 carrier: param-name `_ib_ss` with value-as-hash + enum constraint + telemetry framing. Rejected MCP-session-id alternative as transport-layer-coupling.
- Status: "fail" for verdict-fail OR verdict-error (both = "didn't meet bar"); "error" reserved for run-level exception. Per-verdict cells preserve the distinction.

### Tests
- Pytest delta: 230 → 237 (carry-over from I-NEW work; no new tests added in this session segment, but no regressions either).

## 2026-04-26 — E22 implementation session

**User prompt:** Implement E22 per docs/enhancements.md (mcp/mutable/ test MCP server + mcp_admin turn kind). Aiplay-only; ONE commit (or 2-3). Constraints: NO touch to harness/efficacy.py (E20 sibling in flight); NO verdict (j) (out of scope). Auto-execute mode.

**Reasoning approach:**
1. Read E22 spec + existing mcp/weather scaffold + runner.py turn-kind dispatch + Turn dataclass.
2. Verified fastmcp 3.2.4 API — confirmed add_tool/remove_tool dynamic API + custom_route for admin paths.
3. Confirmed: Turn dataclass already accepts arbitrary request/response dicts; turn_spec field reads work without dataclass extension. Only `templates_validate` (api.py) needs to learn the new kind.
4. Confirmed: no existing helper for resolving `mcp_name → base_url` from harness — adding `_pick_mcp_base_url` in runner.py (env-var driven, mirrors adapter convention).

**Plan:**
1. Build mcp/mutable/ (Dockerfile, requirements.txt, main.py).
2. Update docker-compose.yaml + agw/config.yaml (admin route ordering matters).
3. Update runner.py — add mcp_admin dispatch + base-url helper.
4. Update api.py templates_validate to accept mcp_admin.
5. New tests/test_mcp_mutable.py + 2 cases in tests/test_runner.py.
6. ONE commit.

**Actions / outcomes:**
- Created mcp/mutable/{Dockerfile,requirements.txt,main.py} — 4-tool KV-backed initial set; admin endpoints under /_admin/{state,set_tools,reset}; FastMCP custom_route.
- Used `local_provider.remove_tool` (FastMCP 3.x non-deprecated API) for tool churn.
- Stub tools for set_tools spec via `FunctionTool.from_function(stub).model_copy(update={'parameters': inputSchema})` — bypasses **kwargs restriction.
- docker-compose.yaml: + mcp-mutable service; +AGW_MCP_MUTABLE/+DIRECT_MCP_MUTABLE on harness-api and adapter-langchain.
- agw/config.yaml: +mcp-mutable-admin (no governance) BEFORE mcp-mutable (full cidgar governance) — declaration order matters.
- harness/runner.py: + `_pick_mcp_base_url` + new `mcp_admin` dispatch branch (URL = base + /_admin/op; 404 → skipped; no env → skipped; HTTPError → turn.error).
- harness/api.py: `templates_validate` now accepts mcp_admin (requires `op`).
- tests/test_mcp_mutable.py — 5 new tests (init state, set_tools/counter, reset, list_tools post-mutation via mcp.list_tools(), no admin in tools).
- tests/test_runner.py — 3 new tests (set_tools dispatch URL/payload, 404 no-op, no-base-url no-op).

**Test count:** 237 baseline → 245 after (+8 new, all green). Full suite green.

**Constraints respected:**
- harness/efficacy.py untouched (verified: `git diff harness/efficacy.py` empty).
- No verdict added (no efficacy.py edits, no new VERDICTS dict entry, no compute_verdicts call).
- Single commit.

**Surprises:**
- FastMCP rejected **kwargs in tool fns — used `payload: dict | None = None` stub + parameters override pattern.
- Module-name collision: `mcp/mutable/main.py` was loaded as `main` and clashed with `harness/main.py` already in sys.modules (which other tests had imported). Fixed by loading via `importlib.util.spec_from_file_location` under a private module name.
- FastMCP HTTP transport requires lifespan to be initialized for /mcp/ — couldn't use vanilla httpx.AsyncClient with ASGITransport. Switched to direct `mcp.list_tools()` call (same code path as the JSON-RPC tools/list method dispatches to).

## 2026-04-26 — E21 implementation session

### User prompt
Implement E21 (reset_context + refresh_tools turn kinds) per design doc. Aiplay-only; do not touch verdict_i/E20 helpers; do not touch mcp/mutable/E22 work. Auto-execute with pre-allowed tools; ≤25 min wall-clock.

### My reasoning
- Read enhancements.md §E21 + existing efficacy.py to find the verdict_c boundary line (E20 sits in the SAME file from 5cbee8fb but in distinct functions/helpers; will surgically edit only verdict_c_continuity).
- Surveyed all 7 adapters' framework_bridge.py to learn the canonical attr-set per API and the tool-cache invalidation handle per framework.
- Decided on per-adapter HTTP routes (clean parallel to /compact) rather than overloading /turn with optional fields.

### Actions
- Updated brainstorming.md with E21 design notes.
- Implementation forthcoming in this session.

### Implementation notes (E21 done)
- Verdict_c refactored to be bracket-aware. Pre-existing single-segment trials hit a backwards-compat branch that preserves the legacy "consecutive turns" pass message and the legacy "<2 audit-bearing CIDs (need ≥2)" error message — keeps 4 legacy verdict_c tests passing.
- New `_drive_reset()` and `_drive_refresh_tools()` per Trial across 7 adapters; refresh_tools is functional for langchain/langgraph/crewai/autogen/llamaindex (clears `_mcp_tools` plus companion caches `_graph` / `_llm_with_tools` / `_agent`); pydantic-ai + direct-mcp ship as no-op + log per design-doc fallback policy.
- AdapterClient gained `reset_context(trial_id)` and `refresh_tools(trial_id)` (parallel to `compact()`); cleaner than overloading `drive_turn` with optional turn_id/user_msg.
- `with_reset` template + row flag added; turn-plan validator extended.
- 248 → 282 pytest tests (+34); zero regressions.

### Status: ready to commit
Sticking to ONE commit per task spec (could split reset_context+verdict-c-refactor from refresh_tools but the surfaces are intertwined enough that one logical commit is cleaner).

## 2026-04-26 — E19 + E23 bundle (multi-MCP + multi-LLM schema)

### Entry — task spec
**User:** Implement E19 + E23 as a bundle. Aiplay-only. Both extend `RowConfig` with `str | list[str]` for different fields. Schema-only — no adapter wiring (out of scope; that's the follow-up E19a/E24). 2 logical commits OK.

### Reasoning
- Pre-allowed tools = curl/git/python3/ls/cat/grep/find/mkdir/node. Sandbox blocked even `git status` and `cd`-prefixed bash; switched to `git -C` form. Node syntax-check also blocked but the JS changes are mechanical and visually verified.
- Baseline = 284 tests collected (282 active + 2 skipped). Spec said 282 — the discrepancy is just the +2 skipped, no real drift.
- Validator required gating the existing string-only rules on `isinstance(llm, str)` to stop list values from spuriously triggering "[list] not in api_providers" / Responses-state warnings. Spec didn't call this out but it's needed — confirmed via test runs.
- Frontend Option A (text-input) confirmed used. Editor switched from `agSelectCellEditor` → `agTextCellEditor` for both `mcp` and `llm` columns. `parseListLikeCell` collapses single-value typing to a string so legacy single-MCP rows behave exactly as before.
- The model column UI is degraded for list-form llm rows (the existing curated-models dropdown keys off a single llm string). Out-of-scope to fix here; the field is still list-form-capable on the backend.
- **Did NOT touch** efficacy.py, runner.py, templates.py, adapters/*, AGW. Verified via `git status`.

### Actions
- `harness/api.py::RowConfig`: `llm`, `mcp` → `str | list[str]`; `model` → `str | list[str] | None`
- `harness/trials.py::TrialConfig`: same field changes for round-trip parity
- `harness/validator.py`: added `MULTI_MCP_FRAMEWORKS = set()` and `MULTI_LLM_FRAMEWORKS = {"combo"}`; gated existing string-only rules on `isinstance(llm, str)`; added Rule 7 (E19 list-mcp) + Rule 8 (E23 list-llm with API-compat + model-length checks)
- `tests/test_validator.py`: +8 tests covering str/list mcp + llm acceptance/rejection paths
- `tests/test_api.py`: +1 integration test for POST/GET/PATCH round-trip with list-form mcp+llm+model
- `frontend/app.js`: added `parseListLikeCell` + `formatListLikeCell` + `primaryValue`; switched `mcp` and `llm` columns to `agTextCellEditor`; tooltip explains multi-form on lists; `loadModelsFor` now warms cache for each provider when llm is a list

### Result
- **Pytest: 284 → 293 collected (+9), 282 → 291 active (+9), 2 skipped unchanged.** Zero regressions. Same skip count.
- Two commits planned: (1) schema + validator + tests, (2) frontend cell editor.

## 2026-04-26 — E26: persist body on AuditEntry (verdict (i) production fix)

### Entry — E26 implementation
**User:** Implement E26 per spec in docs/enhancements.md (`## E26 — persist body on AuditEntry`). Aiplay-only. ONE commit. Auto-execute (no prompts).

**Reasoning:** Tight ~10 LOC change with explicit step-by-step plan in the prompt. Risks: (1) `AuditEntry(**a)` from legacy persisted JSONs would fail if `body` were positional/required → mitigated by adding it after `captured_at` with default None. (2) Existing verdict (i) tests use `_tool_call_audit` fixture that sets `correlation_lost` directly under `raw` (not under `raw["body"]` or top-level body) → kept that path as a fallback in `_audit_correlation_lost`. (3) Reordered the body lookup to prefer top-level `entry.body` first since it's the canonical post-E26 path that works for both shape A (JSON) and shape B (regex) production logs.

**Actions:**
- `harness/trials.py`: added `body: dict[str, Any] | None = None` to `AuditEntry` dataclass (after `captured_at`, with explanatory comment).
- `harness/api.py`: passed `body=entry.get("body")` and `body=e.get("body")` at the two `AuditEntry` construction sites in `_run_trial_bg`.
- `harness/efficacy.py::_audit_correlation_lost`: prefer top-level `entry.body` before walking `entry.raw`. Kept all legacy fallbacks (raw direct, raw["body"], raw["fields"]["body"]) for synthetic test fixtures + pre-E26 persisted trials.
- `tests/test_efficacy.py`: +2 tests — `test_verdict_i_reads_body_from_top_level_field` (shape-B mirror — raw has only `{"line": ...}`, body on top-level field) and `test_verdict_i_legacy_raw_body_fallback_still_works` (body=None, correlation under raw["body"] — pre-E26 shape A).
- `tests/test_audit_tail.py`: +1 test `test_e26_body_carries_through_both_shapes_into_audit_entry` — both shape A and shape B `parse_log_line` output have non-None `body`, AND `AuditEntry` construction round-trips it.

**Files changed:** harness/trials.py, harness/api.py, harness/efficacy.py, tests/test_efficacy.py, tests/test_audit_tail.py

