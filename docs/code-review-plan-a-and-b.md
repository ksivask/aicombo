# aiplay Plan A+B — End-to-end code review

> Independent review at end of Plan B (HEAD: `93c494d`, 117/117 pytest).
> Reviewer: superpowers:code-reviewer agent. Date: 2026-04-23.

## Executive summary

- **Overall posture: fix-and-ship.** The codebase is materially complete against both plans, 117/117 pytest green in 6.9s, 7 adapters + 4 MCPs + AGW wiring runs under docker-compose per findings. Three honest blockers and a handful of important holes stand between "it runs" and "I'd stake the release on it."
- **Top 3 blockers:** (B1) `lifespan` still starts the `AuditTail` subprocess during pytest — the prior review's C3 never landed; (B2) `inject_ambient_cid` is documented as a supported turn-kind in the README but is a bare `error` branch in the runner with no adapter surface — documentation lies; (B3) langchain `Trial.messages` is never reset across turns — the three seeded turns produce ever-growing prompts and, when the first turn's MCP tool_call is rebuilt on turn 3, can double-inject tool-call metadata into history.
- **Top 3 non-blocking fixes:** (F1) `AUDIT_BUFFER_PER_TRIAL` + `ABORT_EVENTS` are not cleaned up for trials that **error** in the except branch (only the happy path unsubscribes); (F2) `SSE_QUEUES` is declared, never used, and `trial_stream`/`audit_stream` still emit only `status`+`trial_done` / `keepalive` stubs — Plan A review I5/I6 still open; (F3) `Makefile:47` still uses `python` not `python3` — Plan A review I8 still open.
- **Test coverage health: thorough on efficacy, thin on adapters.** 117 tests is accurate. Efficacy coverage is the strongest surface (all six verdicts cover pass/fail/na, plus header-demux vs time-window for c). Adapter coverage is uniformly "URL picker + smoke init + optional state chain" — zero adapters have a `compact()` unit test, and the claimed "integration test" of the force_state_ref path is mocked at the `openai.AsyncOpenAI` boundary only. No FastAPI tests for `/trials/{row_id}/run`, `/trials/{id}`, `/trials/{id}/stream`, `/audit/stream`, `/templates/preview`, `/info`, `/providers` beyond a 200 check.
- **Notable strengths:** (S1) The `_http_exchanges` + `_mark_exchange_start` + `_capture_*_events` trio is genuinely idiomatic and actually applied consistently across all six framework adapters — captures are real wire bytes, not reconstructions; (S2) efficacy verdicts a–f each have the `na` short-circuit correctly before structural scan, and time-window fallback is exercised for c + d + e; (S3) `force_state_ref` plumbing runs cleanly through `runner → adapter-registry → TurnReq.target_response_id → Trial._forced_prev_id` in autogen/llamaindex and is explicitly 400-rejected in the five non-supporting adapters — this is hard to do right and was done right.

---

## Spec compliance — Plan A (T1–T16)

All 16 Plan A tasks now ship with Plan B enhancements layered on top. Grouped by status:

- **✓ Delivered and intact:** T1 (scaffold, `.gitignore`, `.env.example`), T2 (MCP verbatim import), T3 (agw/config.yaml cidgar policy), T5 (docker-compose), T6 (trials.py + tests), T7 (validator + templates + defaults.yaml — `matrix_seed_rows` now actually wired per `api.py:56`), T8 (providers + efficacy), T9 (audit_tail dual-format), T10 (runner), T11 (FastAPI app), T12 (langchain adapter with httpx hooks), T13 (frontend HTML/CSS — `drawer-maximize` now exists), T14 (AG-Grid), T15 (drawer.js — fully rewritten for turn-plan editor, T12 Plan B), T16 (E2E verified per findings-plan-b.md).
- **⚠ Partial / deviated:** T4 Makefile — prior review I8 still open (`python` not `python3` on line 47).

Prior review C1/C2 (drawer element missing, audit empty) are moot because drawer.js was rewritten for Plan B T12 with new semantics. Prior C3 (lifespan audit-tail starts in TestClient) is **still a live blocker** — see B1 below. I1 (matrix_seed_rows) is fixed. I2/I3 (Dockerfile curl|sh) — not re-read; commit history suggests still present.

---

## Spec compliance — Plan B (T1–T15)

- **✓ Delivered:** T1 (3 new AGW routes + `ai.routes` for claude, correctly applying the `9d7b882e` gotcha per comment at `agw/config.yaml:112-127`), T2 (langgraph), T3 (crewai — with `drop_tool_calls` honestly falling back to `drop_half`), T4 (pydantic-ai — clean `toolsets=` + `http_client=` injection), T5 (autogen — covers all 4 APIs including the openai SDK bypass for Responses; `_forced_prev_id` + `force_state_ref` threaded), T6 (llamaindex — `OpenAILike` for non-catalog, `reuse_client=True`), T7 (registry/validator/frontend enumerate all 7 adapters with correct capability matrices matching `ADAPTER_CAPABILITIES`), T9 (verdict c implemented with header-demux fallback at `efficacy.py:366`), T10 (verdict d + compact wiring through runner + all 7 adapters), T11 (verdict e + force_state_ref in autogen/llamaindex), T12 (turn-plan editor — CodeMirror **5** not 6 as plan said, but functional + `/templates/validate` endpoint + DELETE override endpoint), T13 (clone-for-baseline: `matrix_clone_baseline` endpoint + frontend 🔀 button + `baseline_of` badge render), T14 (cooperative abort: `ABORT_EVENTS` registry, `abort_event.is_set()` check at top of turn loop, correctly registered BEFORE `create_task` spawn per comment at `api.py:368`), T15 (117 tests green, findings doc exists).
- **⚠ Partial:** T8 (README refresh) — README line 139 claims `inject_ambient_cid: Pre-seeds a CID into framework state`. The runner has no implementation; all adapters implicitly 400 it via `turn_kind` whitelist. This is **documentation that misrepresents reality**; either implement or delete the line.

Nothing is outright missing. T12's CodeMirror-5-vs-6 deviation is functionally neutral and is an unflagged but benign scope choice.

---

## Test coverage gaps

### Per-verdict

| Verdict | pass | fail | na | error | time-window path | header-demux path |
|---|---|---|---|---|---|---|
| (a) presence | ✓ (header) | ✓ | ✗ | ✗ | ✗ (no test hits `_has_header_demux=False`) | ✓ |
| (b) channel structure | ✓ C2 + ✓ C1 | ✓ C2 | ✗ | ✗ | ✗ all 3 b-tests use `turn_id=t0` → header path | ✓ |
| (c) continuity | ✓ (both paths) | ✓ | ✓ | ✓ (via header-demux variant) | ✓ (test_verdict_c_pass_when_cid_preserved_across_3_turns) | ✓ (test_verdict_c_pass_with_header_demux) |
| (d) resilience | ✓ | ✓ | ✓ (no-compact + no-post-turn) | ✗ (no test for `pre_cids` empty → error branch) | ✓ | ✗ (no header-demux test) |
| (e) state-mode gap | ✓ | ✓ | ✓ (3 na flavors) | ✗ (no test for missing audit both sides) | ✓ | ✗ |
| (f) GAR richness | ✓ | ✓ | ✓ (2 na flavors: omitted + no-tool) | ✗ (no malformed-JSON-throws test) | n/a (body-only check) | n/a |

**Gaps worth closing:** verdict (a) has no `na` test (trial with zero user_msg turns) and no error test (trial aborted, `trial.audit_entries==[]` but turns exist). Verdict (a) time-window branch at `efficacy.py:54-66` is **entirely unexercised** — every test helper uses `turn_id="t0"` which triggers header-demux. Verdict (b) has the same blind spot.

### Per-adapter

All 7 adapters have direct tests. Coverage density:

- **langchain: best covered.** 5 tests exercising the real `turn()` method with mocked LLM + MCP. Gold standard.
- **autogen / llamaindex: decent but openai-SDK-only.** Init smoke + `_default_model_name` + responses_direct chain via mocked `openai.AsyncOpenAI.responses.create`. The `agent` mode (autogen AssistantAgent, llamaindex chat path) is **never driven through a real `.turn()`**.
- **crewai / pydantic-ai / langgraph: init smoke + URL pickers only.** No `turn()` driven. No `compact()` driven.
- **direct-mcp: route() + URL picker tests only.**

**Recommended additions:** one `compact` test per adapter; `turn()` test for crewai and pydantic-ai with LLM mocked at SDK boundary; a `_capture_events_since_mark` classification test.

### Per-API endpoint

`test_api.py` covers `/health`, `/providers`, `/validate`, `/info`, `/matrix` CRUD, clone-baseline (3 cases), turn_plan_override roundtrip + 404, abort (3 cases). **Missing direct tests:** `/trials/{row_id}/run`, `/trials/{trial_id}` GET, `/trials/{trial_id}/stream` SSE, `/audit/stream` SSE, `/templates/preview`, `/matrix/row/{row_id}` GET 404, `/matrix` DELETE.

`/trials/{row_id}/run` is the highest-value missing test — it's the entry point and would need mocking `AdapterClient` and asserting the task is created + `ABORT_EVENTS[trial_id]` is registered.

### Per-turn-kind

`test_runner.py` covers user_msg happy path, force_state_ref with lookback=2, abort pre-loop, abort mid-loop, adapter-raises. Missing: force_state_ref without prior user_msg, compact turn driven through runner, inject_ambient_cid (unimplemented), verdicts on aborted trial.

### Frontend

Confirmed: zero frontend tests. **Don't add them** — surface is small enough that manual click-through is proportionate. One worthy test: `TestClient.get("/")` asserting CodeMirror CDN tags are present so a bad html edit doesn't silently break the drawer.

---

## Code quality findings

### Blockers

- **(B1) Audit-tail starts in TestClient — prior C3 never fixed.** `harness/main.py:16-27` unconditionally calls `tail.start()` in lifespan, shutdown block has literal `pass` with wrong comment. `test_api.py` uses `with TestClient(app)` which runs lifespan, so every API test spawns `docker ps` + `docker logs -f` subprocesses that fail silently in the no-Docker case, retry every 5s, never cancelled. Fix: env gate + cancel `tail._task` in shutdown.

- **(B2) README documents an unimplemented turn kind.** `README.md:139` claims `inject_ambient_cid: Pre-seeds a CID into framework state`. No adapter accepts it; runner records `turn.error = {"reason": "turn kind 'inject_ambient_cid' not implemented"}` (`runner.py:185`). Plan B success criteria #4 says "compact, force_state_ref, inject_ambient_cid turn kinds execute correctly" — unchecked. Implement or delete.

- **(B3) langchain `Trial.messages` grows unbounded across turns.** `adapters/langchain/framework_bridge.py:395` appends without reset between turns. On a 3-turn MCP trial, turn 3's messages contain turn 1's tool_calls AIMessage + ToolMessage + turn 2's AIMessage + ToolMessage. For qwen2.5:7b it's lenient; stricter models may produce invalid tool_call_id references. Fix: clear between turns when `state=False`, or document persistence + add a test (none exists).

### Important

- **(I1) `ABORT_EVENTS` + `AUDIT_TAIL.unsubscribe` not in `finally`.** If `run_trial` itself raises during error-recovery, both leak. Move to `finally`.
- **(I2) `AUDIT_BUFFER_PER_TRIAL` never drained.** Grows unbounded. One-line fix in `_run_trial_bg` finally.
- **(I3) `SSE_QUEUES` declared but never populated.** `trial_stream` still emits only `status` + `trial_done`. Stub debt — drawer polls anyway.
- **(I4) `MAX_CONCURRENT_TRIALS` + `TURN_CAP` env-declared but unread.** Wire or delete.
- **(I5) Runner `turn_plan.turns` weakly typed.** `kind: "user_message"` typo silently defaults to user_msg.
- **(I6) `turn_kind` validation lives in adapter main.py, not runner.** Surprising but functional.
- **(I7) `efficacy.py:285` could crash on non-str gar_val refactor.** Guarded today; consider helper.
- **(I8) Matrix PATCH accepts arbitrary dict.** No RowConfigPatch model.
- **(I9) Deep copies on every HTTP request in event hooks.** Per-trial OK, scales poorly with concurrency.

### Minor

- (M1) `MARKER_RE` hardcodes `ib_[a-f0-9]{12}`.
- (M2) `_ensure_adapter_on_path()` in every adapter test — fragile under pytest-xdist.
- (M3) Two `captured_at` fields with different types (float in audit_tail buffer, ISO string in persisted AuditEntry). Works because each system uses its own; documentation gap.
- (M4) `_run_trial_bg` reconciliation `try/except: pass` swallows.
- (M5) Docker container-name resolution picks first match silently.
- (M6) `trials.py:117` deserialized verdicts are dicts, type hint says `dict[str, Verdict]`.
- (M7) `UnraisableExceptionWarning` — openai AsyncClient `__del__` after loop closed. Cosmetic.

---

## Findings doc accuracy

`docs/findings-plan-b.md` is **accurate** on all spot-checked claims:
- ✓ crewai `drop_tool_calls → drop_half` fallback (verified at `crewai/framework_bridge.py:691-696`)
- ✓ pydantic-ai compact → drop_half (verified at `pydantic_ai/framework_bridge.py:494-515`)
- ✓ llamaindex `MessageRole.TOOL` filter (`llamaindex/framework_bridge.py:741-744`)
- ✓ autogen `AsyncOpenAI` direct bypass (`autogen/framework_bridge.py:311-317`)
- ✓ autogen `max_tool_iterations=3` (line 446)
- ✓ llamaindex `reuse_client=True` (line 178)
- ✓ Verdict matrix: e correctly limited to autogen/llamaindex (others 400 on force_state_ref)
- ✓ 117/117 pytest reproduced (6.93s)

**Wording nits:** "(d) Resilience, pass example: langchain+with_compact (Plan A reference)" — Plan A doesn't implement (d); the langchain adapter is the Plan A reference, but verdict (d) is Plan B T10. Misleading phrasing.

**Unsurfaced concern:** autogen+responses 503 from AGW chatgpt route is called "orthogonal AGW issue." Autogen test suite mocks `openai.AsyncOpenAI` at SDK boundary — never hits AGW. Bug could re-emerge on real-traffic path with green tests. Worth a real-AGW smoke once mock LLM supports Responses.

---

## Recommendations

Prioritized. S = small (under 1hr), M = medium (~half-day), L = large (full day+).

1. **(S) Fix B1** — env gate + cancel task on shutdown
2. **(S) Fix B2** — implement no-op stub for inject_ambient_cid OR delete README row
3. **(S) Drain `AUDIT_BUFFER_PER_TRIAL` in `_run_trial_bg` finally**
4. **(S) Move `ABORT_EVENTS.pop` + `AUDIT_TAIL.unsubscribe` into `finally`**
5. **(S) Add direct verdict (a) + (b) time-window branch tests** (3 lines each)
6. **(S) Makefile `python` → `python3`**
7. **(M) `compact()` unit test per adapter** — 7 tests, catch regressions on framework upgrades
8. **(M) Direct tests for `/trials/{row_id}/run`, `/templates/preview`, `/trials/{trial_id}` GET**
9. **(M) B3 — decide langchain Trial.messages state semantics** + test
10. **(M) Wire `MAX_CONCURRENT_TRIALS` (asyncio.Semaphore) or delete env var**
11. **(L) SSE endpoints — implement or delete (drawer polling is good enough)**
12. **(L) Pydantic models for TurnPlan + AuditEntry persistence**

---

## Strengths worth preserving

- **Single shared `httpx.AsyncClient` + mutable `self._headers` dict across turns.** Every Plan B adapter uses the same trick — works through nested SDKs uniformly.
- **`_mark_exchange_start()` + `_capture_events_since_mark()` for multi-hop classification.** Cleanly decouples capture from attribution. Scales by reuse.
- **`_httpx_factory` pattern for MCP capture** — one-line plumb-through, no monkey-patching.
- **Verdict applicability short-circuits** at `efficacy.py:550-555`. New verdicts inherit edge-case handling free.
- **Cooperative abort at turn boundaries only** — comment correctly explains why mid-turn cancellation would corrupt framework/MCP state.
- **Audit-tail dual-format parser + time-window fallback** — adaptive to whichever cidgar ships.
- **Headers via `default_headers={}` closure-captured dict** — survives all six frameworks' different httpx plumbing. The "one weird trick" that makes the harness work.
- **`force_state_ref` lifecycle** — 400-reject early in unsupported adapters; set+consume+clear in autogen/llamaindex. Tight contract.
- **Abort registry registration order** — `ABORT_EVENTS[trial_id]` registered BEFORE `create_task` so racing POST /abort can't miss. Comment calls it out.

---

## Ship gate

Ship after **B1 + B2 + B3 + F1 + F2 + F3** (all small, ~3 hours total).
Rest is backlog.
