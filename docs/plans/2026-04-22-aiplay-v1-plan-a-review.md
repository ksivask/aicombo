# aiplay v1 — Plan A MVP — Code Review Findings

**Reviewer:** Claude (Senior Code Reviewer role)
**Date:** 2026-04-22
**Scope reviewed:** `/my/ws/aiplay/{harness,adapters,frontend,tests,agw,Makefile,docker-compose.yaml,.gitignore,.env.example,pytest.ini}` — excluding `docs/` and `mcp/` (imported verbatim from demo per plan Task 2).
**Reviewed commits:** `c3ddea2` through `c56001d` (22 commits: Plan A scaffold + 4 follow-up fixes).
**Design:** `/my/ws/aiplay/docs/design.md`
**Plan:** `/my/ws/aiplay/docs/plans/2026-04-22-aiplay-v1-plan-a-mvp.md`

Verdict: **APPROVED_WITH_FIXES** — plan scope is hit, 41 tests green, end-to-end verified working. A handful of issues block Plan B cleanly building on top, and one is a user-visible front-end bug.

---

## 1. Strengths

1. **Disciplined layering.** `harness/` has clean module boundaries (api, trials, runner, audit_tail, efficacy, providers, templates, validator, adapters_registry). Nothing leaks across. Each file owns one concern.

2. **TrialStore write is atomic** (`harness/trials.py:99–103`): writes to `.tmp` then renames. Safe under crash mid-save — won't produce truncated JSON. Good defensive programming.

3. **Real wire capture via httpx event hooks** (`adapters/langchain/framework_bridge.py:74–100`): the adapter captures exact request bytes (headers + body + byte count + elapsed ms) as sent by langchain — not a reconstruction. This is essential for pedagogy ("what did langchain actually send"). Implemented correctly: body is `aread()`'d inside the hook before returning.

4. **Dual-format audit parser** (`harness/audit_tail.py:57–116`): handles both JSON-per-line and structured-text cidgar log shapes. Regex-based shape-B parser is robust, and the module-level docstring explains why both are needed. Good engineering for observable reality vs. documented expectation.

5. **Time-window fallback for audit correlation** (`harness/audit_tail.py:178–181`, `harness/efficacy.py:43–66`): cidgar's governance log doesn't emit request headers, so header-demux would fail. The code gracefully falls back to time-window correlation and the verdict code detects which mode to use via `_has_header_demux()`. Preserves correctness once cidgar is fixed to emit headers (future-forward).

6. **Container-name resolution handles compose prefixes** (`harness/audit_tail.py:191–213`): tries the configured name first, then scans `docker ps` for any container containing "agentgateway". This is the right fallback given compose's `aiplay-agentgateway-1` naming.

7. **Test coverage is respectable for the implemented surface area.** 41 tests covering 9 modules with ~40% unit + ~60% integration-ish (FastAPI TestClient, mocked adapter in runner). Tests do NOT require a running AGW/Docker.

8. **Verdict applicability is correctly short-circuited.** `efficacy.py:160–166`: `routing=direct` → all `na`; `status=aborted` → all `na`. Matches design §7.7 applicability matrix.

9. **Matrix row completion reconciliation** (`harness/api.py:206–220`): if the SSE client drops, the background task still writes the final verdicts into the matrix row. Frontend 5-second polling (`frontend/app.js:301–318`) means a user who closed the drawer still sees the result. Thoughtful recovery.

10. **Good use of async subprocess for audit tail** (`harness/audit_tail.py:225–243`): uses `asyncio.create_subprocess_exec` with PIPE-stdout, handles reader EOF, wraps in a reconnect loop. Simpler and more robust than the docker-py SDK alternative (which has a history of hangs on log streams).

---

## 2. Critical issues (must fix before Plan B starts)

### C1. `drawer.js` queries DOM elements that don't exist in `index.html` — page likely throws on load

**File:** `/my/ws/aiplay/frontend/drawer.js:6–7`, `/my/ws/aiplay/frontend/index.html:23–40`

```js
// drawer.js:6-7
const maximizeBtn = document.getElementById("drawer-maximize");
const resizeHandle = drawerEl.querySelector(".drawer-resize-handle");
```

`index.html` has no element with `id="drawer-maximize"` nor class `drawer-resize-handle`. `maximizeBtn` is `null`; `resizeHandle` is `null`.

Then `drawer.js:25` does `maximizeBtn.addEventListener(...)` → **TypeError on page load**, stops module execution. Same for `resizeHandle.addEventListener` at line 44. Also line 69: `document.querySelector(".drawer-header").addEventListener("dblclick", …)` can then reach `maximizeBtn.click()` but that's already null.

Expected behaviour per commit `20177b8` message ("resizable+maximizable drawer") was to add these to the HTML too, but only the `style.css` + `drawer.js` side landed. Verify by opening the browser console — the TypeError is the first cue.

**Fix:** add to `frontend/index.html` inside the `<div id="drawer" class="drawer hidden">`:

```html
<div class="drawer-resize-handle"></div>
<!-- then in drawer-header: -->
<button id="drawer-maximize" title="maximize">⇱</button>
```

Also guard the listeners with `if (maximizeBtn)` to fail gracefully if any future drift recurs.

### C2. Drawer's "Governance audit" section shows empty on every turn even when audit entries exist

**File:** `/my/ws/aiplay/frontend/drawer.js:133`

```js
const audits = (trial.audit_entries || []).filter(a => a.turn_id === t.turn_id);
```

In practice cidgar governance log does **not** emit `X-Harness-Trial-ID`/`X-Harness-Turn-ID` headers (design decision documented at `audit_tail.py:15–25`). So every `audit_entry.turn_id` is `null`, but every `turn.turn_id` is a non-null string. `null === "turn-000-abc"` → always false. Per-turn audit section always renders `(no audit entries for this turn — AGW governance may not have fired)` — which is pedagogically misleading when 6 audit entries exist in the same trial JSON.

Verified on trial `33fd8e9d-92f8-4dcd-9e91-580a144eb18a`: 3 turns, 6 audit entries, all with `turn_id: null`, `cid: ib_c22aa98abe1d`. Drawer per-turn audit is empty; verdicts computed correctly server-side. User's mental model of "click turn, see its audit entries" fails silently.

**Fix:** mirror the backend's dual-mode logic in the drawer:

```js
// Detect if any audit entry has a turn_id. If none, fall back to time-window:
// split audit entries across turns by captured_at proximity to turn's started_at/finished_at.
const hasHeaderDemux = (trial.audit_entries || []).some(a => a.turn_id);
const audits = hasHeaderDemux
  ? (trial.audit_entries || []).filter(a => a.turn_id === t.turn_id)
  : auditEntriesInWindow(trial.audit_entries || [], t.started_at, t.finished_at);
```

This needs a small helper. Alternative: render ALL audit entries once at the bottom of the drawer with a banner explaining "cidgar log doesn't emit correlation headers yet; showing raw stream." That's more honest pedagogically.

### C3. Running `lifespan` inside pytest `TestClient` starts a real audit-tail subprocess and polls `docker ps`

**File:** `/my/ws/aiplay/harness/main.py:16–27`, `/my/ws/aiplay/tests/test_api.py:8`

`test_api.py` uses `with TestClient(app) as client:` — the context manager runs the FastAPI lifespan, which calls `AuditTail.start()` → starts a background task that spawns `docker ps` and `docker logs -f` subprocesses. On hosts without Docker (or CI), that subprocess fails silently in the background loop, retries every 5s forever during the test run, and the task is never cancelled. After the TestClient context exits, the task keeps running until the pytest process dies.

Not a test-failure blocker today (the subprocess errors are swallowed in `audit_tail.py:244–246`), but:
- Adds flaky CI risk when CI environments evolve
- Leaks subprocesses across tests (possible zombie buildup in long runs)
- Makes `test_api.py` implicitly require Docker, which the comment at `conftest.py:14` says is explicitly NOT wanted

**Fix:** gate the audit tail startup:

```python
# main.py
async def lifespan(app: FastAPI):
    if os.environ.get("AIPLAY_DISABLE_AUDIT_TAIL") != "1":
        tail = AuditTail(container_name=os.environ.get("AGW_CONTAINER_NAME", "agentgateway"))
        tail.start()
        api.AUDIT_TAIL = tail
    yield
```

Then in `conftest.py`:

```python
os.environ["AIPLAY_DISABLE_AUDIT_TAIL"] = "1"
```

Also ensure the background task is cancelled on lifespan exit (current `shutdown` is a no-op, comment says "subprocess exits with process" which isn't true for pytest).

---

## 3. Important issues (should fix before merge)

### I1. `matrix_seed_rows` in `harness/defaults.yaml` is dead code — first-boot grid is empty

**File:** `/my/ws/aiplay/harness/defaults.yaml:3–6`, `/my/ws/aiplay/harness/api.py:39–43`

Design §8.4 says defaults.yaml seeds the matrix on first boot. `_load_matrix()` returns `[]` when `matrix.json` doesn't exist — never reads `matrix_seed_rows`. Users land on an empty grid and must click `+ Add Row` to do anything. Pedagogically this is a speed-bump on first-run ("where do I start?").

**Fix:** in `_load_matrix`, when the file is missing, read `matrix_seed_rows` from `defaults.yaml`, generate `row_id`s, write them to `matrix.json`, return them. Roughly 10 lines. The same `templates.py` already loads defaults.yaml so `yaml` is in the dep tree.

### I2. Dockerfile pipes `curl | sh` from `get.docker.com` — supply-chain risk and bloated image

**File:** `/my/ws/aiplay/harness/Dockerfile:6–9`

```dockerfile
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates curl && \
    curl -fsSL https://get.docker.com | sh && \
    rm -rf /var/lib/apt/lists/*
```

Pipes a shell script from the internet at image-build time, and installs the **full Docker engine including dockerd + containerd** when all the harness needs is the `docker` CLI for `docker logs -f` and `docker ps`. The resulting image is ~700 MB larger than needed. Also, a build that happens when get.docker.com is temporarily compromised or down would either fail or pull a bad payload.

**Fix:** install only the Docker CLI via apt:

```dockerfile
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates docker.io && \
    rm -rf /var/lib/apt/lists/*
```

The `docker.io` Debian package provides only the CLI + daemon but the daemon never runs inside this container. For a smaller footprint still, use the static `docker-cli` binary download from `docker.com/static-binaries` with a pinned SHA256.

### I3. Docker socket is mounted read-only but the harness container installs full Docker engine — unnecessary attack surface

**File:** `/my/ws/aiplay/docker-compose.yaml:23`, `/my/ws/aiplay/harness/Dockerfile`

`docker-compose.yaml:23` correctly mounts `/var/run/docker.sock:/var/run/docker.sock:ro`. But the full Docker engine binary (including `dockerd`) is installed in the image (I2 above). Since docker.sock is RO, `dockerd` can't run — fine. But `docker logs -f` on a RO socket still works (log streams are read-only). Security posture is OK but messy: minimize what's installed.

Separately, per design §6.5: "docker.sock on harness-api — for Docker SDK log tailing". The implementation uses the docker **CLI** (subprocess), not the SDK (docker-py). Either works, but the CLI approach makes the dep-tree simpler (`docker-py` dropped from requirements.txt, which is correct).

### I4. Langchain adapter MCP path is stubbed but the turn-plan template exercises it

**File:** `/my/ws/aiplay/adapters/langchain/framework_bridge.py:29–37` (defined, tested, unused), `/my/ws/aiplay/harness/defaults.yaml:19–23` (weather template)

`pick_mcp_base_url()` is defined and has 4 unit tests, but the `Trial` class never calls it — `ChatOpenAI` has no `bind_tools()` or MCP adapter attached. Meanwhile `defaults.yaml` defines `with_mcp_weather` template whose content is `"What's the weather in Paris?"`. Running this on a `mcp=weather` row invokes the LLM, which **responds with text** ("I don't have access to weather tools, but…") — MCP governance never fires because no `tools/list` or `tools/call` is issued.

Plan Task 12.3 explicitly flags this ("Plan A's MCP seeded row is primarily for the direct_mcp case…"), so it's documented scope. But:
- The per-MCP turn templates (`with_mcp_weather`, `with_mcp_news`, etc.) presuppose tool calling that's not wired
- Running the `{mcp=weather, llm=ollama}` seeded row yields a false pass (C2 text-marker channel works; C1 tool-calls channel has no chance to fire)

**Fix:** either
- **(a) Document** this limit at the top of `defaults.yaml` and in a new line on the drawer banner when `config.mcp != "NONE"` and `framework="langchain"`. "MCP=weather with langchain Plan A uses LLM-side text only; tool calling deferred to Plan B."
- **(b) Remove** `with_mcp_{news,library,fetch}` templates from defaults.yaml for Plan A — they're misleading.
- **(c) Wire up** MCP via `langchain-mcp-adapters` and a `bind_tools()` pass. This is the biggest scope bump — correctly flagged as Plan B.

### I5. `trial_stream` SSE endpoint doesn't emit the event types design §5.4 specifies

**File:** `/my/ws/aiplay/harness/api.py:233–247`

Design §5.4 lists 7 event types: `trial_started`, `turn_started`, `turn_complete`, `audit_entry`, `trial_done`, `error`, `keepalive`. Implementation emits only `status` (a one-off state-poll) and `trial_done`.

Impact: the drawer can only refresh once at end — no live turn-by-turn updates, no audit-entry streaming. Frontend masks this with 5-second polling (`app.js:301`). Works, but loses the pedagogy of "watch the governance log populate in real time as the model calls each tool."

**Fix before Plan B:** build event emission into `runner.py`'s turn loop + `audit_tail.py`'s callback so the drawer gets real-time updates. Use an `asyncio.Queue` per trial_id. Shouldn't be a huge lift — `SSE_QUEUES: dict[str, deque]` is already declared at `api.py:34` but unused. Wire it up.

### I6. `audit_stream` endpoint is a pure stub

**File:** `/my/ws/aiplay/harness/api.py:250–257`

Emits only `keepalive`. Design says raw AGW audit entries demuxed by trial-id. Currently no consumer — frontend doesn't subscribe. Either implement for Plan B, or remove to avoid confusion.

### I7. No cancellation/unsubscribe for SSE clients

**File:** `/my/ws/aiplay/harness/api.py:233–247`

`trial_stream`'s `while True: await asyncio.sleep(1.0)` runs forever if trial never terminates AND the client disconnects — the generator is never notified of client-disconnect. FastAPI/Starlette handles this for you by raising `asyncio.CancelledError`, which the current loop doesn't handle. A leaked task might survive forever (until status settles or file disappears). Low impact in Plan A (trial always terminates within seconds), but worth a `try/finally` catch.

### I8. `test` Makefile target uses `python`, not `python3`

**File:** `/my/ws/aiplay/Makefile:47`

```make
test:
	cd harness && python -m pytest ../tests/ -xvs
```

On Debian/Ubuntu without `python-is-python3`, this fails with `/bin/sh: 1: python: not found`. Verified on this host.

**Fix:** use `python3`.

### I9. Matrix row accepts arbitrary keys via PATCH — no schema check

**File:** `/my/ws/aiplay/harness/api.py:112–120`

```python
@router.patch("/matrix/row/{row_id}")
def matrix_update(row_id: str, updates: dict = Body(...)):
    rows = _load_matrix()
    for r in rows:
        if r["row_id"] == row_id:
            r.update(updates)  # unchecked merge
```

A client can add `{"secret": "…"}` and it'll be persisted. Low severity (no multi-tenant surface), but a clean schema (Pydantic) here would also catch typos like `"stream": "true"` (string) silently becoming the stored value.

**Fix:** validate with a `RowConfigPatch` Pydantic model that has the same fields as `RowConfig` but all Optional.

---

## 4. Minor issues (nice to fix)

### M1. `SSE_RETRY_MS` and `MAX_ROWS` in `frontend/config.js` are defined but unused

**File:** `/my/ws/aiplay/frontend/config.js:2,5`

Dead exports. Either use them or remove.

### M2. `paired_trial_id` field on `Trial` is a dead field

**File:** `/my/ws/aiplay/harness/trials.py:65`

Defined for Plan B's clone-for-baseline. Fine to leave as forward-thinking — but document it or it'll look like orphaned code to the next reviewer.

### M3. Stored trial JSONs in `data/trials/` are stale relative to current efficacy code

**Files:** `data/trials/*.json` (multiple)

E.g. `0242388d-…json`'s verdict_b reads "turn 0 has no audit entry — verdict_a should have caught this" (error). Re-running `compute_verdicts` on the same trial today yields `fail` with a different message. The stored reason is from an earlier efficacy.py version. Nothing breaks — verdicts are recomputable — but the stored JSON becomes untrustworthy as the reason field. Consider versioning the verdict schema, or adding a re-compute pass on load.

### M4. `_run_trial_bg`'s matrix-row reconciliation swallows all exceptions

**File:** `/my/ws/aiplay/harness/api.py:208–220`

```python
try:
    final = STORE.load(trial_id)
    …
except Exception:
    pass
```

If this fails, the UI never updates. Log the exception at least (`log.exception(…)`).

### M5. Adapter HTTP client timeout = 120s; no liveness check

**File:** `/my/ws/aiplay/harness/adapters_registry.py:18`

If `adapter-langchain` hangs, `drive_turn` blocks for 120 seconds. For Ollama this is plausible (cold model load). But harness holds the slot the entire time. A sidecar `GET /health` pre-check before `POST /trials/{id}/run` would cost nothing and catch a dead adapter immediately.

### M6. `TurnPlan.turns` is `list[dict]`, not `list[Turn]` — loses type safety

**File:** `/my/ws/aiplay/harness/trials.py:26`

Using dicts instead of typed `TurnSpec` dataclass. Valid in Python, but any typo in `{kind: "user_msg", ...}` vs `{kind: "user_message", ...}` silently becomes "unknown kind". Minor — pydantic `TurnSpec` would clean this up.

### M7. `efficacy.MARKER_RE` hardcodes 12-hex CID but AGW config says `generator: uuid4_12`

**File:** `/my/ws/aiplay/harness/efficacy.py:10`

```python
MARKER_RE = re.compile(r"<!--\s*ib:cid=(ib_[a-f0-9]{12})\s*-->")
```

Tightly coupled to AGW config's CID generator. If someone changes `generator: uuid4_16` or a future generator uses uppercase, the regex silently misses. At minimum, comment the coupling. Ideally, make the regex configurable.

### M8. `api.AUDIT_BUFFER_PER_TRIAL` is never drained

**File:** `/my/ws/aiplay/harness/api.py:33`

`defaultdict(list)` grows unbounded with every trial. Long-running harness instance leaks memory. Should be cleared on `audit_tail.unsubscribe(trial_id)` in `_run_trial_bg`.

### M9. `framework_bridge.Trial` stores `_last_request/_last_response` on instance state — race-prone under concurrent turns

**File:** `/my/ws/aiplay/adapters/langchain/framework_bridge.py:68–69`

If Plan B bumps `MAX_CONCURRENT_TRIALS > 1` and two turns overlap on the same trial (legal but uncommon), the event hooks will race and overwrite each other's capture. Prob. not Plan A critical; will bite Plan B.

**Fix:** use `contextvars.ContextVar` or pass a per-turn capture dict through event hook `request.extensions`.

### M10. Status pill CSS has no style for `aborted` or `paused`

**File:** `/my/ws/aiplay/frontend/style.css:18–23`

Status values supported in trials.py (aborted, paused) don't have a pill class. Will render as plain text. Minor.

### M11. `docker-compose.yaml` `extra_hosts` value `192.168.64.1` is host-specific

**File:** `/my/ws/aiplay/docker-compose.yaml:43,68`

Hardcoded Linux-bridge-specific IP. Per commit `9bc1c2c`, this was intentional. But `host-gateway` alias is the canonical cross-platform choice (works on macOS/Linux). If the user can run on more than one host, pull this into `.env` as `AIPLAY_HOST_INTERNAL_IP=192.168.64.1` default.

---

## 5. Design alignment

| Design item | Implementation | Note |
|---|---|---|
| §1 principle 6 (via_agw + direct modes) | partial | `TrialConfig.routing` exists, verdicts short-circuit for `direct` (efficacy.py:160). But actually switching adapter base URL happens only in `pick_llm_base_url` — untested against a running AGW-less path in this review |
| §2.1 harness `/adapters_registry.py /info /providers` endpoints | implemented | `/info` hardcodes `adapter-langchain` (fine for Plan A) |
| §2.1 "docker (Python SDK)" | deviated — uses subprocess | acceptable — CLI is more robust than docker-py for log streaming |
| §3.2 trial start: audit subscribe + SSE open | partial | audit subscribe exists (for future header demux), SSE only emits `status`/`trial_done` — see I5 |
| §3.3 turn loop supports 4 kinds | partial | only `user_msg` wired; others Plan B (runner.py:55–57 explicitly marks them) |
| §3.5 verdict computation at trial end | implemented | runner.py:71, called post-turn-loop with 0.3s grace |
| §4 adapter contract | aligned | `POST /trials`, `POST /trials/{id}/turn`, `DELETE` implemented; `force_state_ref`/`inject_ambient_cid`/`compact` not — Plan B |
| §5.1 grid columns: 14 columns | 10 columns (no # / Actions has 2 buttons) | Actions reduced to run + delete; clone + next-turn + abort deferred |
| §5.3 Drawer 4 tabs (Turn Plan, Turns, Verdicts, Raw JSON) | 3 tabs (no Turn Plan tab) | planning assumed editing the plan before run; deferred to Plan B. Acceptable |
| §5.4 SSE event types | 2 of 7 | see I5 |
| §5.6 Settings panel | partial | modal exists but mostly read-only info display; no sliders for `MAX_CONCURRENT_TRIALS` / `turn_cap` / etc |
| §7.2 verdict (a) presence | implemented + time-window fallback | |
| §7.3 verdict (b) channel struct | implemented — OpenAI shape only | Anthropic shape (Plan B) parser not present — correct per scope |
| §8.4 `matrix_seed_rows` seeds matrix on first boot | NOT wired | see I1 |
| §9.9 missing-API-key UI | implemented | validator returns `disabled_dropdown_options.llm` |

Overall design alignment: **~85%**. The major design items that aren't implemented are either explicitly Plan B (5 adapters, verdicts c/d/e, turn-plan editor) or listed in this review. No subtle silent deviations.

---

## 6. Plan alignment

Comparing against `/my/ws/aiplay/docs/plans/2026-04-22-aiplay-v1-plan-a-mvp.md`:

| Task | Aligned? | Note |
|---|---|---|
| T1 scaffold + gitignore + env | yes | |
| T2 MCP import from /my/ws/demo | yes | imported verbatim, mcp/auth excluded as specified |
| T3 agw/config.yaml | yes | Plan A routes only (ollama + 4 MCPs) |
| T4 Makefile | mostly | `make test` uses `python` not `python3` (I8); `rotate-keys` has a shell guard that requires compose to be up — plan didn't |
| T5 docker-compose | yes | host.docker.internal IP swap is a post-plan fix |
| T6 trials.py + tests | yes | |
| T7 validator + templates + defaults.yaml | mostly | `matrix_seed_rows` defined but not consumed (I1) |
| T8 providers + efficacy | yes | efficacy has time-window correlation that was added post-plan (c56001d); correct call |
| T9 audit_tail | yes + dual-format post-plan | post-plan changes are correct — plan assumed JSON-format logs, reality was mixed |
| T10 runner | yes | |
| T11 FastAPI app | yes | `/info` hardcodes single adapter; fine |
| T12 langchain adapter | yes + httpx wire capture post-plan | the httpx hook capture is a clean improvement over the plan's stub |
| T13 frontend HTML + CSS | yes | see C1 — drawer-maximize/resize-handle in CSS+JS but not HTML |
| T14 frontend JS + AG-Grid | yes | |
| T15 drawer.js | yes + resize/maximize post-plan (incomplete — see C1) | |
| T16 integration testing | verified end-to-end pass in data/trials (`33fd8e9d-…`) | |

**Post-plan fix commits assessed:**

- `9bc1c2c` (Ollama IP fix + UI polish): all 4 changes are correct and address real user issues.
- `a714292` (pedagogical HTTP capture): the httpx event-hook pattern is a genuine win over plan's reconstruction stub. Good call.
- `20177b8` (resizable drawer + settings modal + polling fallback): settings modal + polling are fine; resizable drawer landed half-done (**C1**).
- `c56001d` (dual-format audit parser + container resolution + time-window verdicts): all three are correct responses to observed reality. Should be folded back into the plan doc for Plan B's reference.

**Plan deviations that were beneficial:** httpx event hook capture (a714292), dual-format log parser (c56001d), time-window verdict correlation (c56001d).

**Plan deviations that introduced defects:** drawer resize (20177b8 — C1), no matrix seed (silent — I1).

---

## 7. Test coverage

41 tests across 9 modules. All pass in 2.5–3.5 seconds. Python 3.12, pytest-asyncio auto mode.

### What's covered well
- `test_trials.py` (5): round-trip save/load, append-turn, append-audit, list, missing-file error
- `test_validator.py` (7): all 4 API state rules + NONE/NONE + missing-key
- `test_efficacy.py` (7): verdict-a pass/fail, verdict-b C1/C2 pass/fail, direct mode, Plan B na
- `test_audit_tail.py` (4): JSON parse, non-governance skip, malformed, trial matcher
- `test_adapter_langchain.py` (4): base URL picks for both routing modes
- `test_templates.py` (3): chat-no-mcp / weather / direct-mcp
- `test_providers.py` (4): ollama / chatgpt with+without key / all-4-returned
- `test_runner.py` (2): single user_msg + adapter error path
- `test_api.py` (5): health, providers, validate, matrix CRUD, info

### What's NOT covered
1. **Structured-text log parsing (`audit_tail.parse_log_line` shape B).** Only JSON shape is tested. The dual-format parser was added post-plan but only the JSON branch has a test. A sample structured-text line from a real `docker logs` output would make a perfect fixture.
2. **Time-window audit correlation path.** `entries_since(ts)` has no test. Given this is the actual production path (header-demux is dead code for v1 cidgar), it deserves a test.
3. **`verdict_a` time-window branch.** The new `_has_header_demux`/fallback logic (efficacy.py:43–66) is not exercised directly — existing tests create `AuditEntry(turn_id="t0")` which triggers header-demux. Add a test with `turn_id=None` entries that exercises the time-window counting logic.
4. **`verdict_b` time-window branch.** Same as above — time-window expected-cids branch (efficacy.py:124–125) is untested.
5. **Tool-calls C1 channel with missing _ib_cid in args.** Only pass case is tested. Need a fail case where the model emits tool_calls WITHOUT `_ib_cid` in args — that should fail verdict_b.
6. **End-to-end integration test.** No test instantiates the full pipeline (runner + real-ish adapter + audit_tail). `test_runner.py` mocks the adapter and provides a pre-canned audit list. An integration test that uses a fake subprocess for `docker logs -f` output and a fastapi TestClient adapter would catch many of the issues flagged here (especially C2/I4/I5).
7. **Framework_bridge.Trial class.** Only the `pick_*_base_url` helpers are tested. The `Trial.turn()` method — which is the actual adapter entry point — has no test. Mock `ChatOpenAI.ainvoke` and verify headers propagation, message accumulation, tool_calls extraction.
8. **`TurnPlan` with multiple turn kinds.** Only `user_msg` is tested. Add a test that a Plan A turn plan containing `compact` produces a `turn.error` with correct message (per runner.py:55–57).
9. **API endpoint `/trials/{row_id}/run` when row_id is invalid.** `test_api.py` doesn't check the 404 path.
10. **Matrix PATCH with invalid keys / malformed types.** Since PATCH accepts `dict` today (I9), a test showing the current "stores anything" behavior would pin it down until I9 is fixed.

### Test hygiene concerns
- `conftest.py:15–18` sets a module-level `DATA_DIR` env var at collection time. Test collection order is deterministic but if pytest-xdist is added, this will race. Since `pytest.ini` doesn't set `-n`, it's fine today.
- `test_adapter_langchain.py:13–14` inserts `adapters/langchain/` into `sys.path`, which shadows any future adapter `main.py`. The comment says it's safe for current suite; if Plan B adds more adapter tests, this pattern breaks.
- No test mocks `docker` subprocess, so `audit_tail.run()`'s main loop is 100% untested.

---

## 8. Security / secret handling

1. **`.env` is gitignored**, `.env.example` is committed with empty values. Good.
2. **`.gitignore` also lists `.claude/` and `.agentdiff/`** (line 30, 25–29) — prevents session state leakage. Good.
3. **`.env` is at the repo root with mode 664** (`-rw-rw-r--` per `ls -la`). A group-readable `.env` is a minor local hygiene concern but standard practice. On a shared dev box, recommend `chmod 600 .env`.
4. **docker.sock mounted RO** (`docker-compose.yaml:23`) — good. A RO docker.sock can still stream logs and list containers, but can't exec/run new containers. Minimal surface.
5. **Harness-api has no API keys** — correctly follows design §6.4: keys live on adapter services only, harness-api gets them from `os.environ` only for `get_providers()` key-detection which reads presence not values. Good.
6. **`providers.py` reads env at request time**, never logs the keys. Good.
7. **Settings modal displays availability, not values** (`app.js:258–262`). Good.
8. **`harness/Dockerfile` pipes `curl | sh`** from get.docker.com — see I2. Supply chain concern.
9. **No secrets in trial JSONs** — verified by spot-checking `/data/trials/33fd8e9d…json`: no API keys or headers that leak `Authorization:` etc. The langchain adapter passes `api_key="ollama"` placeholder (`framework_bridge.py:104`), so no real secrets enter the trial record.
10. **Trial JSONs under `data/trials/` are world-readable** in the host filesystem (inherit umask). Since they contain no secrets but DO contain full prompts and LLM outputs, consider `chmod 700 data/trials/` via Makefile or post-up step.
11. **No XSS protection on the drawer** — `drawer.js:111` does `String(v).replace(/</g, "&lt;")` for header values only. Request body JSON values are rendered as `pre > JSON.stringify(...)` which is safe. But `audit_entry.raw` (`drawer.js:125`) renders `JSON.stringify(a.raw, null, 2)` — safe. Turn ID and kind render without escaping at line 136 — `turn.turn_id` is adapter-generated UUID-ish so safe in practice but trust-bound to adapter. Nothing critical.
12. **`adapters_registry.py:15`** reads `ADAPTER_LANGCHAIN_URL` from env but default falls back to `http://adapter-langchain:5001` — hardcoded to compose DNS. If the user flips to a different topology, they need to set the env var. Fine for Plan A.

**Overall security: acceptable for a dev tool.** Not a production system, not network-exposed (harness on 8000, AGW on 8080 — both localhost). The `.env` discipline is sound. Fix I2 before wider use.

---

## 9. Pedagogy

The drawer is the primary pedagogical surface ("what did cidgar do to this trial?"). Assessment:

### What works pedagogically
- **Request/Response side-by-side** — `drawer.js:138–156` renders request body (pre-cidgar mutation) and response body (post-cidgar mutation) as two `<details>` sections. User can visually see the C2 marker `<!-- ib:cid=ib_... -->` in the response text that's absent from the request. Exactly the "what does cidgar do" story.
- **Headers rendered as k/v table** (`drawer.js:108–113`) — makes `X-Harness-Trial-ID`/`X-Harness-Turn-ID` visible per turn, proving the adapter actually propagated them.
- **Audit phase badges are color-coded** (`style.css:117–122`) — `llm_request` blue, `terminal` green, `tool_call` pink. Easy scan.
- **Verdict cards show pass/fail + one-line reason** (`drawer.js:167–178`) — the reason strings from efficacy.py are descriptive ("CID present across 6 audit entries for 3 turns (time-window correlation); unique CIDs: ['ib_c22aa98abe1d']"). Clear evidence trail.
- **Raw JSON tab** (`drawer.js:181`) — gives the escape hatch to see everything.

### What breaks pedagogically
- **C2 (drawer.js:133 — audit filter):** all audit entries render as "(no audit entries for this turn — AGW governance may not have fired)" when in reality 6 entries exist per trial. User thinks "governance didn't fire" when actually it did 6 times. This is the biggest pedagogical failure. **See C2 in this review.**
- **No "cidgar added these fields" diff highlight.** User has to visually diff the request body vs. response body themselves to see where the CID was injected. A dedicated `diff` section in each turn card showing "adds: `<!-- ib:cid=... -->` at choices[0].message.content tail" would make the story explicit.
- **Drawer doesn't distinguish via_agw from direct visually.** Design §5.3 spec said "For baseline (routing=direct) trials: banner at top of tab 'Baseline run — cidgar not in path; verdicts N/A'". Implementation has no banner. Given clone-for-baseline is deferred, this matters less — but running one `direct` trial and one `via_agw` trial side-by-side and NOT getting a clear "this is the baseline" visual hint makes the compare harder than it needs to be.
- **Verdict a passing reason mentions "time-window correlation"** — a user who hasn't read the code won't know what that means. Add a tooltip or a hyperlink to a one-line explanation.

### Suggested pedagogy improvements (Plan B or before)
1. **Fix C2 above** — either implement time-window turn binning on the frontend, or render audit entries in a global section below turns with a "cidgar log doesn't emit correlation headers in this build" banner.
2. **Add a "diff" sub-section in each turn card** — pseudo-JSON diff of request body vs. response body, highlighting added/modified paths. A tiny JS diff lib (diff-patch or manual) would do it.
3. **Add a "routing: direct" banner** in the drawer when `trial.config.routing === "direct"` so the user knows "this is the control, not the experiment."
4. **Add phase-sequence visualization** in the audit section — a horizontal flow `llm_request → terminal → llm_request → terminal` showing how governance is firing. Ties audit phases to the turn's narrative.

---

## 10. Plan B readiness

Before Plan B builds on top, these items should be addressed:

### Blockers for Plan B (fix first)
- **C1 / C3 / I5 / I7** — frontend drawer broken, audit-tail leaks in tests, SSE events missing, SSE cancellation absent. Each of these becomes worse as more adapters and trial types are added.
- **I1 (matrix_seed_rows)** — Plan B adds 5 more adapters; users need a seeded grid to be able to run them without clicking `+ Add Row` 6 times. Wire this.
- **M9 (Trial._last_request race)** — Plan B might bump MAX_CONCURRENT_TRIALS. The shared-instance capture is unsafe. Move to ContextVar or per-turn dict.
- **I2 (curl|sh in Dockerfile)** — fix once, not five more times when Plan B Dockerfiles are copied.

### Shape changes needed for Plan B
- **Harness `/info` endpoint** (`api.py:71–78`) hardcodes a single adapter. Plan B needs it to enumerate all 6 via HTTP discovery (call each adapter's `/info` endpoint as design §2.1 specifies). This is a ~30-line change; do it before Plan B starts.
- **`adapters_registry.py`** (hardcoded single URL) needs to become a loop over 6 URLs with health probing. Same moment.
- **Verdict schema versioning** (per M3) — once Plan B ships verdicts c/d/e, older trial JSONs will lack them. Add a `verdict_schema_version` field and either upgrade on load or mark absent verdicts as `na` with reason "not computed by this harness version."
- **Turn-plan editor** (`defaults.yaml` templates) — Plan B promises `compact`, `force_state_ref`, `inject_ambient_cid`. The turn-spec dict type needs to accommodate these; currently `runner.py:55–57` just records an error. Plumb through a proper dispatch.
- **Efficacy c/d/e** — stub functions returning `na` should exist in `efficacy.py` now (they don't) so Plan B fills them in rather than creating them. Minor.

### Architecture that will scale cleanly to Plan B
- **TrialStore persistence** — JSON files scale to hundreds of trials fine. Plan A's R3 decision holds.
- **Adapter HTTP boundary** — the contract is cleanly separable. Each Plan B adapter can be built independently.
- **Verdict computation** — `compute_verdicts(trial)` returns a dict; adding c/d/e is additive with no cross-impact.
- **httpx event-hook capture** pattern in langchain adapter is the template for the other 5 adapters. Reuse.
- **Audit-tail's dual-format parser** absorbs whatever cidgar log shape variations land.

### Items to NOT carry forward into Plan B
- `TurnPlan.turns: list[dict]` (M6) — replace with typed `TurnSpec` dataclass.
- Unused `SSE_RETRY_MS` / `MAX_ROWS` (M1) — delete.
- `AUDIT_BUFFER_PER_TRIAL` leak (M8) — clean up before concurrent trials arrive.

---

## Summary

Plan A reaches its stated goal: a working cidgar test harness with one LLM framework, cidgar-governed routes, and (a)+(b) efficacy verdicts computed from real audit logs on real Ollama traffic. The implementation is cleanly layered, well-tested at unit level, and the post-plan fixes address genuine real-world discoveries (not scope creep).

Key issues to resolve: one user-visible frontend bug (C1 drawer elements missing), one pedagogy bug (C2 per-turn audit filter empty in practice), and one test hygiene issue (C3 audit-tail subprocess starts in tests). Plus `matrix_seed_rows` deadwood (I1) and `curl | sh` supply chain (I2).

Plan B foundations are sound. Address C1/C2/C3/I1/I5 before scaling to 6 adapters.

---

## Test run at review time

```
$ python3 -m pytest /my/ws/aiplay/tests/ -xvs 2>&1 | tail -5
tests/test_validator.py::test_none_llm_disables_api_stream_state PASSED
tests/test_validator.py::test_invalid_combo_api_responses_stream_off_state_on_is_valid PASSED
tests/test_validator.py::test_missing_provider_key_disables_option PASSED

============================== 41 passed in 2.48s ==============================
```

All 41 tests green. Collection: 41 items. Python 3.12.3, pytest 9.0.2, pytest-asyncio auto mode.

## Stack status at review time

```
$ docker compose -f /my/ws/aiplay/docker-compose.yaml ps
NAME                         SERVICE             STATUS         PORTS
aiplay-adapter-langchain-1   adapter-langchain   Up 2 minutes   5001/tcp
aiplay-agentgateway-1        agentgateway        Up 2 minutes   0.0.0.0:8080->8080, 0.0.0.0:15000->15000
aiplay-harness-api-1         harness-api         Up 2 minutes   0.0.0.0:8000->8000
aiplay-mcp-fetch-1           mcp-fetch           Up 2 minutes   8000/tcp
aiplay-mcp-library-1         mcp-library         Up 2 minutes   8000/tcp
aiplay-mcp-news-1            mcp-news            Up 2 minutes   8000/tcp
aiplay-mcp-weather-1         mcp-weather         Up 2 minutes   8000/tcp
```

7 services up, all healthy (agentgateway, adapter-langchain, harness-api, and 4 MCP servers). `agentgateway:cidgar` image loaded from the external build per plan.
