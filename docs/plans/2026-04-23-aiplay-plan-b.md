# aiplay Plan B — Implementation Plan

> **For agentic workers:** Dispatch one subagent per task. Tasks 2-7 (adapters) are largely independent but all depend on Task 1 (agw routes). Verdicts (Tasks 9-11) depend on the adapters they test against. UI polish (12-14) can happen any time after the base system is green.

**Goal:** Extend aiplay from 1 framework adapter (langchain) to all 6 frameworks, cover all 4 APIs (chat, responses, responses+conv, messages), compute verdicts c/d/e, and polish UI (turn-plan editor, clone-for-baseline, abort).

**Architecture:** No architectural changes — Plan A's harness infrastructure (adapter contract, audit-tail correlation, per-turn HTTP capture, efficacy verdict module, trial detail page) is the foundation. Plan B fills in the remaining adapters and verdicts using Plan A's patterns as reference.

**Tech Stack:** Python 3.12, FastAPI per-adapter, langchain-mcp-adapters, crewai, pydantic-ai, autogen (microsoft/autogen-agentchat), llamaindex, fastmcp, httpx with event hooks (shared capture pattern from Plan A).

**Reference:** `adapters/langchain/framework_bridge.py` is the canonical implementation. Every new adapter follows its shape:
- `Trial.__init__`: httpx client with event hooks → capture every wire byte
- `Trial.turn()`: agent loop with max N hops, multi-step event capture
- `Trial.aclose()`: release resources
- `_http_exchanges` + `_mark_exchange_start` + `_capture_mcp_op_events` for MCP demux

---

## File structure (additions to Plan A)

```
/my/ws/aiplay/
├── agw/config.yaml                                  [T1 — expand]
├── adapters/
│   ├── langchain/                                   [Plan A — reference]
│   ├── direct-mcp/                                  [Plan A]
│   ├── langgraph/                                   [T2 — new]
│   ├── crewai/                                      [T3 — new]
│   ├── pydantic_ai/                                 [T4 — new]
│   ├── autogen/                                     [T5 — new]
│   └── llamaindex/                                  [T6 — new]
├── harness/
│   ├── adapters_registry.py                         [T7 — expand]
│   ├── validator.py                                 [T7 — expand ADAPTER_CAPABILITIES]
│   ├── providers.py                                 [T7 — OK as-is]
│   ├── efficacy.py                                  [T9/T10/T11 — implement c/d/e]
│   ├── runner.py                                    [T10 — handle compact turn kind]
│   ├── templates.py                                 [T9 — add multi-turn templates]
│   └── defaults.yaml                                [T9 — expand seeded rows]
├── frontend/
│   ├── app.js                                       [T7/T12/T13 — expand dropdowns, add clone/abort]
│   ├── trial.js                                     [T12 — turn-plan editor]
│   └── style.css                                    [T12 — editor styling]
├── docker-compose.yaml                              [T2-T6 — register 5 new services]
├── tests/
│   ├── test_adapter_langgraph.py                    [T2]
│   ├── test_adapter_crewai.py                       [T3]
│   ├── test_adapter_pydantic_ai.py                  [T4]
│   ├── test_adapter_autogen.py                      [T5]
│   ├── test_adapter_llamaindex.py                   [T6]
│   ├── test_verdict_c_continuity.py                 [T9]
│   ├── test_verdict_d_resilience.py                 [T10]
│   └── test_verdict_e_state_mode_gap.py             [T11]
└── docs/plans/
    └── 2026-04-23-aiplay-plan-b.md                  [this file]
```

Target test count after Plan B: 58 → ~90 (32 new tests).

---

## Scope

### IN Plan B

- 5 new framework adapters (langgraph, crewai, pydantic-ai, autogen, llamaindex)
- 3 new AGW routes (claude Messages API, chatgpt Responses API, gemini chat-compat) + `ai.routes` map for claude
- Verdicts (c), (d), (e) — computed per spec
- Turn kinds: `compact`, `force_state_ref`, `inject_ambient_cid`
- Stream toggle actually wired (pass stream=true through adapter LLM clients)
- State toggle actually runnable (responses + state=T via chatgpt through autogen/llamaindex)
- Turn-plan JSON editor in drawer
- Clone-for-baseline row action
- Abort running trial action (backend cancellation + UI)
- Plan B findings doc at end (analogous to Plan A review)

### OUT of Plan B (further deferred)

- n8n framework integration
- Microsoft Copilot (via Playwright — dropped)
- Native Ollama API shape (`/api/chat`) — cidgar coverage gap, not aiplay
- Multi-provider trials (same trial using chatgpt for turn 1, claude for turn 2)
- Headless CI mode (`aiplay run-all --junit`)
- OTel trace export

---

## Task 1: Expand agw/config.yaml with 3 new LLM routes

**Files:**
- Modify: `agw/config.yaml`

Add routes for /llm/chatgpt/, /llm/claude/, /llm/gemini/ — each with full cidgar governance policy. claude route MUST include the `ai.routes` map for Messages-shape dispatch (per Harness B's `9d7b882e` fix) or governance walker won't fire.

- [ ] **Step 1.1:** Add `llm-chatgpt` route (pathPrefix `/llm/chatgpt/`, hostOverride `api.openai.com:443`, governance block identical to llm-ollama).
- [ ] **Step 1.2:** Add `llm-claude` route (pathPrefix `/llm/claude/`, hostOverride `api.anthropic.com:443`, governance block, AND **critical** `ai.routes` map):
  ```yaml
  ai:
    routes:
      "/v1/messages": messages
      "/v1/messages/count_tokens": anthropicTokenCount
  ```
  Without this, AGW defaults to Completions parser and governance walker never fires.
- [ ] **Step 1.3:** Add `llm-gemini` route (pathPrefix `/llm/gemini/`, hostOverride `generativelanguage.googleapis.com:443`, openAI provider since Gemini has OpenAI-compat endpoint at `/v1beta/openai/`).
- [ ] **Step 1.4:** Restart AGW container + curl-test each route reaches its upstream with correct base URL.
- [ ] **Step 1.5:** Commit with explicit mention of the `ai.routes` footgun documented in-file.

---

## Task 2: adapter-langgraph (chat + MCP agent loop)

**Files:**
- Create: `adapters/langgraph/{Dockerfile,main.py,framework_bridge.py,requirements.txt}`
- Create: `tests/test_adapter_langgraph.py`
- Modify: `docker-compose.yaml` (+ adapter-langgraph service)
- Modify: `harness/adapters_registry.py` (+ langgraph URL)
- Modify: `harness/validator.py` (ADAPTER_CAPABILITIES["langgraph"] = {"chat"})
- Modify: `frontend/app.js` (ADAPTER_CAPABILITIES_JS["langgraph"] = ["chat"])

Langgraph wraps langchain's ChatOpenAI in a StateGraph. The tool-use flow uses `create_react_agent` or manual graph nodes. For Plan B:

- [ ] **Step 2.1:** requirements.txt: fastapi, uvicorn, httpx, langchain, langgraph, langchain-mcp-adapters
- [ ] **Step 2.2:** `framework_bridge.py::Trial` — same shape as langchain adapter. Use `langgraph.prebuilt.create_react_agent(llm, tools)` to get a graph. Invoke per turn via `graph.ainvoke({"messages": [...]})`. Agent loop is handled by langgraph internally; capture hook fires on each internal LLM + MCP call.
- [ ] **Step 2.3:** Port `main.py` verbatim from langchain adapter (endpoints identical, just different port `5011`).
- [ ] **Step 2.4:** Tests: pick_llm_base_url, create_react_agent integration (mocked), multi-step event capture works like langchain adapter.
- [ ] **Step 2.5:** Compose service `adapter-langgraph` with port 5011, same env vars as langchain.
- [ ] **Step 2.6:** Validator `langgraph → {"chat"}`. Frontend dropdown.
- [ ] **Step 2.7:** E2E smoke: new matrix row (framework=langgraph, llm=ollama, mcp=fetch) → runs trial → verdicts a/b/f pass.

---

## Task 3: adapter-crewai (chat + messages)

**Files:**
- Create: `adapters/crewai/{Dockerfile,main.py,framework_bridge.py,requirements.txt}`
- Create: `tests/test_adapter_crewai.py`
- Modify: compose + registry + validator + frontend

Crewai has its own `LLM` class and `BaseTool` abstraction. MCP integration via custom `BaseTool` subclass wrapping fastmcp client. Supports chat via OpenAI-compat (`litellm` under the hood) and messages via `model="claude-*"`.

- [ ] **Step 3.1:** requirements.txt: crewai, langchain-mcp-adapters (for tool discovery), fastmcp, httpx, fastapi
- [ ] **Step 3.2:** `framework_bridge.py::Trial` — instantiate `crewai.LLM(model=..., base_url=..., additional_headers=headers)` with X-Harness-* headers. Convert MCP tools to `crewai.tools.BaseTool` subclasses (custom wrapper around fastmcp.Client.call_tool). Drive turns via `Crew(...).kickoff_async(inputs={"user_msg": ...})`.
- [ ] **Step 3.3:** Port main.py (port 5012). `/info` advertises `apis: ["chat", "messages"]`.
- [ ] **Step 3.4:** Config dispatch: `api=chat` → LLM with openai-compat URL; `api=messages` → LLM with `model=anthropic/claude-3-haiku-20240307` (litellm routes to Anthropic).
- [ ] **Step 3.5:** Tests: URL selection, tool-wrapping, chat turn, messages turn.
- [ ] **Step 3.6:** Compose + registry + validator. ADAPTER_CAPABILITIES["crewai"] = {"chat", "messages"}.
- [ ] **Step 3.7:** E2E smoke for both api=chat and api=messages.

---

## Task 4: adapter-pydantic-ai (chat + messages + responses)

**Files:**
- Create: `adapters/pydantic_ai/{Dockerfile,main.py,framework_bridge.py,requirements.txt}`
- Create: `tests/test_adapter_pydantic_ai.py`
- Modify: compose + registry + validator + frontend

Pydantic-ai is the modern typed alternative to langchain. It natively supports OpenAI Responses API via `OpenAIResponsesModel`.

- [ ] **Step 4.1:** requirements.txt: pydantic-ai, fastmcp, httpx, fastapi. Pydantic-ai has MCP integration via its `MCPServerStdio` / `MCPServerHTTP` — use HTTP mode.
- [ ] **Step 4.2:** `framework_bridge.py::Trial` — use `pydantic_ai.Agent(model=..., mcp_servers=[MCPServerHTTP(url, http_client=our_httpx)])`. Agent.run() or Agent.run_stream() per turn.
- [ ] **Step 4.3:** Config dispatch by api:
  - `chat` → `OpenAIModel(provider=OpenAIProvider(base_url=chatgpt-agw-url))`
  - `messages` → `AnthropicModel(provider=AnthropicProvider(base_url=claude-agw-url))`
  - `responses` → `OpenAIResponsesModel(base_url=...)`
- [ ] **Step 4.4:** Pydantic-ai returns `Agent.run()` as `AgentRunResult` with `all_messages()` — capture each iteration as framework_events.
- [ ] **Step 4.5:** main.py on port 5013.
- [ ] **Step 4.6:** Tests + compose + registry + validator. capabilities = {"chat", "messages", "responses"}.
- [ ] **Step 4.7:** E2E smoke for all 3 APIs.

---

## Task 5: adapter-autogen (chat + messages + responses + responses+conv)

**Files:**
- Create: `adapters/autogen/{Dockerfile,main.py,framework_bridge.py,requirements.txt}`
- Create: `tests/test_adapter_autogen.py`
- Modify: compose + registry + validator + frontend

Autogen (microsoft/autogen-agentchat) supports the full OpenAI Responses API including conversation state via `previous_response_id`. **This adapter unlocks verdict (e) testing.**

- [ ] **Step 5.1:** requirements.txt: autogen-agentchat, autogen-ext[openai,anthropic,mcp], fastmcp, httpx, fastapi
- [ ] **Step 5.2:** `framework_bridge.py::Trial` — use `autogen_agentchat.agents.AssistantAgent` with `autogen_ext.models.openai.OpenAIChatCompletionClient` OR `OpenAIResponsesClient` (for api=responses). Set `http_client=our_httpx` on the model client.
- [ ] **Step 5.3:** MCP integration via `autogen_ext.tools.mcp.mcp_server_tools(server_params)`. Pass our httpx factory.
- [ ] **Step 5.4:** State mode (`responses+conv`, state=T): adapter tracks `previous_response_id` from each turn's response, passes it as the next turn's `previous_response_id` parameter. Implement `force_state_ref` turn kind: override the tracked ID from a specific earlier turn.
- [ ] **Step 5.5:** main.py on port 5014. `/info` advertises all 4 APIs + `state_modes: ["stateless", "responses_previous_id"]`.
- [ ] **Step 5.6:** Tests including a responses+conv turn sequence with state preservation.
- [ ] **Step 5.7:** Compose + registry + validator. capabilities = {"chat", "messages", "responses", "responses+conv"}.
- [ ] **Step 5.8:** E2E smoke for each API. Special: a state=T trial with 3 turns verifying previous_response_id threaded correctly.

---

## Task 6: adapter-llamaindex (chat + responses + responses+conv)

**Files:**
- Create: `adapters/llamaindex/{Dockerfile,main.py,framework_bridge.py,requirements.txt}`
- Create: `tests/test_adapter_llamaindex.py`
- Modify: compose + registry + validator + frontend

Llamaindex has native Responses API support + conversation state via `ChatStore`. No native Anthropic Messages API support (goes through litellm or langchain).

- [ ] **Step 6.1:** requirements.txt: llama-index-core, llama-index-llms-openai, llama-index-tools-mcp, fastmcp, httpx, fastapi
- [ ] **Step 6.2:** `framework_bridge.py::Trial` — use `llama_index.llms.openai.OpenAIResponses` for responses API, `llama_index.llms.openai.OpenAI` for chat. Wrap MCP via `llama_index_tools_mcp.McpToolSpec(http_client=our_httpx)`.
- [ ] **Step 6.3:** State: use `llama_index.core.memory.ChatMemoryBuffer` with a `ChatStore` keyed by trial_id. For responses+conv, extract `previous_response_id` from response metadata.
- [ ] **Step 6.4:** main.py on port 5015.
- [ ] **Step 6.5:** Tests + compose + registry + validator. capabilities = {"chat", "responses", "responses+conv"}.
- [ ] **Step 6.6:** E2E smoke.

---

## Task 7: Harness registry + validator + frontend expansion

**Files:**
- Modify: `harness/adapters_registry.py`
- Modify: `harness/validator.py` (ADAPTER_CAPABILITIES)
- Modify: `frontend/app.js` (ADAPTER_CAPABILITIES_JS)

If T2-T6 each updated registry/validator inline, this task is mostly a cleanup. Otherwise, consolidate here.

- [ ] **Step 7.1:** `ADAPTER_URLS` dict has all 6 framework adapters + direct-mcp.
- [ ] **Step 7.2:** `ADAPTER_CAPABILITIES` = {langchain:{chat}, direct-mcp:∅, langgraph:{chat}, crewai:{chat,messages}, pydantic-ai:{chat,messages,responses}, autogen:{chat,messages,responses,responses+conv}, llamaindex:{chat,responses,responses+conv}}
- [ ] **Step 7.3:** Frontend mirror.
- [ ] **Step 7.4:** Validator unit tests covering ALL (framework, api) combos: runnable, unrunnable with correct reason.
- [ ] **Step 7.5:** Expand `harness/defaults.yaml` with seeded rows covering each framework×api combo (~9 new seeded rows).

---

## Task 8: Update Plan A support matrix in README → Plan B coverage

**Files:**
- Modify: `README.md`

The README's Plan A matrix table becomes a Plan B matrix. Before Plan B starts writing new code, the old "deferred to Plan B" callouts should be renamed or expanded.

- [ ] **Step 8.1:** Replace "Plan A — exhaustive support matrix" section with "Current support matrix (Plan B complete)". Update runnable-configs table to cover all 6 frameworks × their APIs.
- [ ] **Step 8.2:** Mark verdicts c/d/e as ✅ computed.
- [ ] **Step 8.3:** Mark turn kinds (compact, force_state_ref, inject_ambient_cid) as ✅.
- [ ] **Step 8.4:** Remove the "Plan B candidates" note; add a "Further work" section for the out-of-scope items.
- [ ] **Step 8.5:** Commit the README refresh at Plan B completion (Task 15 — final task).

This task runs LAST. Deferring to T15's sub-step.

---

## Task 9: Verdict (c) — multi-turn continuity

**Files:**
- Modify: `harness/efficacy.py` (implement `verdict_c_continuity`)
- Modify: `harness/defaults.yaml` (3+ turn templates)
- Create: `tests/test_verdict_c_continuity.py`

Per spec: turn N's CID == turn N+1's ingress-extracted CID across ≥3 turns. AGW's audit shows the extracted CID on `llm_request` phase (time-window correlated). If CID carried forward, turns share CID. If framework dropped all channels, AGW mints a new CID → mismatch.

- [ ] **Step 9.1:** Write `verdict_c_continuity(trial)` — requires ≥3 user_msg turns with audit entries. Check turn N's last audit cid == turn N+1's first audit cid. Fail if any boundary differs.
- [ ] **Step 9.2:** `defaults.yaml` turn templates must emit ≥3 turns by default for chat+MCP combos (they already do in Plan A).
- [ ] **Step 9.3:** Tests: pass case (3 turns same cid), fail case (turn 2 gets new cid), na case (<3 turns).
- [ ] **Step 9.4:** Live verify on a langchain+ollama+weather trial — should pass.

---

## Task 10: Verdict (d) — compaction resilience + compact turn kind

**Files:**
- Modify: `harness/efficacy.py` (verdict_d_resilience)
- Modify: `harness/runner.py` (handle compact turn kind)
- Modify: each `adapters/*/framework_bridge.py` (implement /trials/{id}/compact endpoint + internal history mutation)
- Modify: `adapters/*/main.py` (add compact endpoint)
- Create: `tests/test_verdict_d_resilience.py`

The compact turn kind instructs the adapter to mutate its internal conversation history. Strategies: `drop_half`, `summarize`, `drop_tool_calls`. After compaction, next turn should still have CID (via C3 resource block that survived, or any other channel).

- [ ] **Step 10.1:** Define `compact` turn kind handling in `runner.py`: instead of calling `/trial/{id}/turn`, call `/trial/{id}/compact` with `{strategy: ...}`.
- [ ] **Step 10.2:** Each adapter's main.py adds `POST /trial/{id}/compact`. Each framework_bridge.py implements `Trial.compact(strategy)`:
  - langchain/langgraph: `ConversationBufferMemory` message list slicing
  - crewai: `crew.memory.storage` manipulation
  - autogen: `agent.chat_messages[recipient]` slicing
  - pydantic-ai: `message_history` list slicing
  - llamaindex: `ChatMemoryBuffer.get_all()` → slice → `.set()`
- [ ] **Step 10.3:** verdict_d_resilience: find the compact turn index; compare pre-compact last cid vs post-compact first cid. If preserved via any channel → pass.
- [ ] **Step 10.4:** Tests covering each strategy.
- [ ] **Step 10.5:** Live verify: add a seed row with `with_mcp_with_compact_no_stateref` template.

---

## Task 11: Verdict (e) — server-state-mode gap + force_state_ref

**Files:**
- Modify: `harness/efficacy.py` (verdict_e_state_mode_gap)
- Modify: `harness/runner.py` (handle force_state_ref turn kind)
- Modify: `adapters/autogen/framework_bridge.py` + `adapters/llamaindex/framework_bridge.py` (implement force_state_ref: override previous_response_id for next turn)
- Create: `tests/test_verdict_e_state_mode_gap.py`

Only adapters supporting `responses+conv` (autogen, llamaindex) implement `force_state_ref`. Verdict checks: in state=T mode, does CID propagate when client only sends previous_response_id (no history)?

- [ ] **Step 11.1:** autogen's Trial: add `force_state_ref(turn_id_to_ref)` → overrides the next turn's `previous_response_id` to reference turn N's response id instead of the most recent.
- [ ] **Step 11.2:** Same for llamaindex.
- [ ] **Step 11.3:** runner.py: handle force_state_ref turn kind.
- [ ] **Step 11.4:** verdict_e_state_mode_gap: for responses+conv trials, compare turn N's audit cid vs turn N-1's audit cid. Expected: often DIFFERENT because body-level CID propagation may break (spec §14.5 future item). Verdict fails = known expected gap (documents the limitation).
- [ ] **Step 11.5:** Tests + live verify.

---

## Task 12: Turn-plan editor in drawer (CodeMirror JSON)

**Files:**
- Modify: `frontend/trial.html` (add CodeMirror CDN import)
- Modify: `frontend/trial.js` (Turn Plan tab becomes editable when status=idle or trial not yet started)
- Modify: `frontend/style.css`
- Modify: `harness/api.py` (add PATCH /matrix/row/{row_id}/turn_plan)
- Modify: `harness/templates.py` (support per-row turn_plan overrides)

Per design §5.3, the Turn Plan tab has a CodeMirror JSON editor + buttons: Reset to default, Add user_msg turn, Add compact turn, Add force_state_ref turn, Run full plan, Run next turn only.

- [ ] **Step 12.1:** CodeMirror 6 via CDN (lightweight, ~200KB).
- [ ] **Step 12.2:** Backend PATCH endpoint stores per-row turn_plan override; `default_turn_plan(row)` now checks override first.
- [ ] **Step 12.3:** Turn Plan tab: if row has no active trial, show editable CodeMirror with current turn plan. Save button PATCHes backend.
- [ ] **Step 12.4:** Schema-validate on save (client-side): valid JSON, valid turn kind, no more than TURN_CAP turns.
- [ ] **Step 12.5:** Tests: backend endpoint, template override logic.

---

## Task 13: Clone-for-baseline row action

**Files:**
- Modify: `frontend/app.js` (⎘ button in Actions column)
- Modify: `harness/api.py` (POST /matrix/row/{row_id}/clone-baseline)

Per design §3.7. Clone the row with `routing=direct`, link both via `paired_trial_id`.

- [ ] **Step 13.1:** Backend endpoint: duplicates row, sets routing=direct, returns new row_id.
- [ ] **Step 13.2:** Frontend ⎘ button in Actions column.
- [ ] **Step 13.3:** When clone's trial completes, set `paired_trial_id` on both trials.
- [ ] **Step 13.4:** Trial detail page: if paired, show a link to the paired trial + a "Compare" button that opens both side-by-side (simple 2-column layout; Plan B basic).

---

## Task 14: Abort running trial

**Files:**
- Modify: `frontend/app.js` (enable ⏸ button when status=running)
- Modify: `harness/api.py` (POST /trials/{trial_id}/abort)
- Modify: `harness/runner.py` (cancellable trial task)
- Modify: each `adapters/*/main.py` (honor DELETE during in-flight turn — already exists, but cancel pending HTTP)

- [ ] **Step 14.1:** `runner.py`: wrap `run_trial` in an `asyncio.Task`, keep a registry `RUNNING_TRIALS: dict[str, Task]`. Abort cancels the task.
- [ ] **Step 14.2:** `/trials/{id}/abort` endpoint cancels the task, sets status=aborted.
- [ ] **Step 14.3:** Frontend ⏸ button now enabled during running, calls abort.
- [ ] **Step 14.4:** Adapter DELETE already exists; ensure in-flight httpx requests get cancelled when the trial task is cancelled (`CancelledError` propagates).
- [ ] **Step 14.5:** Tests: abort mid-trial, status lands at `aborted`, verdicts all `na`.

---

## Task 15: End-to-end verification + Plan B findings + README refresh

**Files:**
- Create: `docs/plans/2026-04-24-aiplay-plan-b-review.md`
- Modify: `README.md`

- [ ] **Step 15.1:** Dispatch the superpowers:code-reviewer subagent on the full Plan B diff.
- [ ] **Step 15.2:** Run the complete matrix (all 6 frameworks × their allowed APIs, ~15-18 trials) + verify each row passes its expected verdicts.
- [ ] **Step 15.3:** Update README.md Plan A matrix → Plan B support matrix (per Task 8 checklist).
- [ ] **Step 15.4:** Findings doc summarizing: what worked, what cidgar gaps surfaced (expect verdict e to fail on all state-mode trials — documents spec §14.5 as needed Plan C scope), test count growth (58 → ~90).
- [ ] **Step 15.5:** Commit, tag, note that aiplay v1 is shippable for hyperstate dependency closure.

---

## Execution order (recommended)

1. **T1** (agw config — prereq for most adapters)
2. **T2** (langgraph — simplest, validates langchain pattern applies)
3. **T3** (crewai — messages API validation)
4. **T4** (pydantic-ai — responses API validation)
5. **T5** (autogen — responses+conv, unlocks verdict e)
6. **T6** (llamaindex — last framework)
7. **T7** (registry/validator consolidation — if not done inline in T2-T6)
8. **T9** (verdict c — most framework adapters in place)
9. **T10** (verdict d + compact turn kind — touches all adapters)
10. **T11** (verdict e + force_state_ref — autogen + llamaindex only)
11. **T12** (turn-plan editor)
12. **T13** (clone-for-baseline)
13. **T14** (abort)
14. **T15** (E2E verification + README refresh + findings)

**Dispatch cadence:** one subagent per task sequentially. Each subagent's job is self-contained. Reviewer subagent at T15 for the whole Plan B diff.

---

## Success criteria

Plan B is complete when:
- [ ] All 6 framework adapters build and run under docker-compose
- [ ] Every (framework, api) combo in ADAPTER_CAPABILITIES runs a trial end-to-end with its expected verdicts
- [ ] Verdicts c, d, e all return pass/fail (not na — they're actually computed)
- [ ] `compact`, `force_state_ref`, `inject_ambient_cid` turn kinds execute correctly in the frameworks that implement them
- [ ] Turn-plan editor lets user modify a row's plan before Run
- [ ] Clone-for-baseline creates a paired direct-routing row
- [ ] Abort cancels an in-flight trial and marks it aborted
- [ ] README Plan B matrix reflects new reality
- [ ] pytest suite at ~90 tests, all green
- [ ] Plan B findings doc committed
