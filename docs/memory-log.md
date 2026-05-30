# aiplay — memory log

Deferred / parked items from aiplay brainstorming sessions. Each entry is self-contained.

## 2026-04-26 — Admin endpoint design lesson
**Context:** E22 originally added an `mcp-mutable-admin` AGW route so admin calls "felt unified" with the rest of the gateway. User reverted in this session (commit `8bfb649`) on the principle: AGW should NOT know about test-harness concerns. Admin endpoints exist purely to mutate test fixtures; they're a backstage door for the harness.
**Implication:** When adding new test-harness-only paths in the future (e.g. for E27+ verification harnesses), default to direct-container dispatch via docker-compose service name. Reach for an AGW route ONLY when the path is part of the user-facing surface or governance-relevant.

## 2026-04-26 — Verdict (k) mode-C: who emits the marker
**Context:** Verdict (k)'s mode-C originally read "model paraphrase suspected" — implying the LLM mangled the marker. Wrong. The LLM doesn't emit the marker; AGW does. Mode-C now lists 3 causes: AGW MARKER_RE didn't extract, adapter dropped marker during shape translation, channels config inconsistent across routes.
**Implication:** When an AGW-emitted artifact appears garbled in audit, FIRST check AGW's emission code path (regex tolerances, shape-translation hops in the adapter, per-route config consistency) — NOT the LLM. The LLM is downstream of AGW for these markers.

## 2026-04-21 — Deferred for v1.1

### n8n framework integration
**Context:** User wants n8n in the harness but postponed to v1.1.
**Details:** n8n is a workflow platform, not a Python library — can't `import` like other frameworks. Integration options:
1. Run n8n as docker service, author one workflow per (API × stream × state) combo, trigger via webhook, read response body. Workflow's LLM-node custom headers propagate `X-Harness-Trial-ID` + `X-Harness-Turn-ID`.
2. State lives in n8n memory node or external store.
3. ~2-4h authoring + one webhook adapter endpoint.
**Blockers:** None — purely scope. Add once v1 adapter contract proven.

### Microsoft Copilot — removed from scope (2026-04-22)
**Context:** User initially listed "copilot" as a fallback provider; clarified twice (rounds 1 and 2) that it meant consumer https://copilot.microsoft.com/. Then decided to remove it entirely.
**Why removed:** copilot.microsoft.com has no documented public API. Integration would require browser automation (Playwright headless Chromium), which is fragile and doesn't map cleanly to cidgar's MCP tool-calling hooks. Not worth the investment when Ollama + chatgpt + claude already cover all 4 target APIs (chat, responses, responses+conv, messages).
**Prior drafts of design.md contained Azure OpenAI env vars and v1.2+ Playwright deferral notes** — those were removed when the provider was dropped entirely. If Microsoft ever exposes a Copilot API, revisit.

### Registry publishing path
**Context:** v1 uses locally-built image referenced by static tag (mirrors auth2v). No registry push.
**Details:** If team/CI needs arise, add `docker push` as a separate user-owned step. Not aiplay's concern to automate.

## 2026-04-21 — Worth investigating later

### Spec §14.5 header-based CID passthrough
**Context:** Efficacy level (e) — Responses API + server-state may expose that body-level CID propagation is impossible without history.
**Details:** If aiplay's responses+state tests show CID breaks, that's evidence for promoting spec §14.5 (`X-IB-CID` request header) from "future item" to v1.1. Aiplay becomes the forcing function.

### Native Ollama API shape coverage
**Context:** auth2v uses `/api/chat` not `/v1/chat/completions`. Known cidgar v1 shape gap.
**Details:** Future extension (Harness C v1.2 or separate Harness D) adds native-Ollama shape walker tests. Outside aiplay v1 unless priority shifts.

### Compaction simulation strategies
**Context:** Efficacy level (d) — compaction resilience.
**Details:** Each adapter's `POST /conv/{id}/compact` takes `strategy: "drop_half" | "summarize" | "drop_tool_calls"`. Implementation varies per framework:
- langchain: `ConversationBufferMemory` partial clear
- crewai: `crew.history` manipulation
- autogen: `agent.chat_messages` mutation
- llamaindex: `ChatStore` truncation
- pydantic-ai: message history list slicing
- langgraph: state-graph checkpoint pruning

Per-framework research needed during adapter build.

## 2026-04-21 — Locked decisions (reference)

### Nomenclature
`cid` = cidgar, `trial_id` = harness row invocation, `turn_id` = per-turn UUID, `session_id` = MCP-layer. `conv_id` is forbidden — ambiguous.

### Auth out of scope
User directive. No Keycloak, OAuth, auth-mcp. Auth integration testing is auth2v's responsibility.

### AGW image ownership
User owns build + tag lifecycle externally. aiplay compose references static tag via `image:` only — no `build:` key. Mirrors auth2v pattern. Missing tag = fail-fast.

### Subagent blockage on /my/ws/aiplay/ writes
Background subagent hit permission denial on Bash + Write to `/my/ws/aiplay/`. Main session can write there. If future subagents need to write to aiplay paths, pre-create the files or dispatch from main session.

## 2026-04-26 — E22 implementation complete

- mcp/mutable test MCP server + admin endpoints + mcp_admin turn kind landed on main.
- Tests: 237 → 245 pytest (+8). All green. No touch to harness/efficacy.py (E20 sibling work).
- Open follow-up: when E21 lands (refresh_tools turn kind), add a `with_mutation` template variant to defaults.yaml exercising mcp_admin + refresh_tools composition. Spec calls this out as the canonical E20 verification trial.
- Open follow-up: integration test that drives the full pipeline (docker-compose up, real http POSTs). Currently the mcp-mutable container is built but no test boots it. Spec calls this out under §Tests > Integration; deferred since requires running services.


## 2026-04-26 — AGW yaml loader does NOT apply merge keys
- AGW loads `agw/config.yaml` via `crates/agentgateway/src/serdes.rs::yamlviajson::from_str`, which is `serde_yaml::Deserializer::from_str` + `serde_transcode::transcode` → JSON.
- `serde_transcode` is a streaming event-by-event copy. It does NOT invoke `serde_yaml::Value::apply_merge`.
- Combined with the global `deny_unknown_fields` set by the `schema!` macro alias in serdes.rs:52–56, this means YAML merge keys (`<<: *anchor`) WILL FAIL at runtime — the literal `<<` key reaches the schema and is rejected.
- DO NOT introduce YAML anchors anywhere in `agw/config.yaml` or any other AGW-consumed YAML file until this loader changes.
- Future fix: wire `serde_yaml::Value::apply_merge` into `yamlviajson::from_str` before the transcode (or migrate to `serde_yml`/manual `Value` round-trip). Then merge keys become safe.

---

## 2026-05-20 — Aiplay topology + smoke gotchas (memory)

- **Ollama lives on the HOST (192.168.64.1), not in this VM (192.168.64.2).** Pre-flight from VM: `curl http://192.168.64.1:11434/api/tags`. NEVER `curl localhost:11434` from VM shell. Container-side via `host.docker.internal:11434` (mapped by compose `extra_hosts` from `.env`'s `HOST_DOCKER_INTERNAL_IP`).
- **AGW image is distroless** — no `sh`, no `curl`. `docker compose exec agentgateway sh …` will fail. Test functionally via a trial.
- **Matrix re-seed**: `DELETE /matrix` leaves `data/matrix.json` as `{"rows": []}` and `/matrix` will NOT re-seed. To re-seed from `harness/defaults.yaml`: `rm data/matrix.json && docker compose restart harness-api`.
- **Verdict (b) is strict**: real LLMs don't echo CID markers in NL replies. (b) fail with (a/c/f/i) pass is acceptable; (b) is an LLM-behavior probe, not a governance check.
- **tool_call audit body** has `gar` + `snapshot_hash` at TOP LEVEL (not nested under `args._ib_gar`). Probes that look inside `args` will undercount.

---

## 2026-05-27 — RID (Design B/C) findings + deploy/test gotchas (memory)

- **CHG-26A follow-up (logged, deferred — Task #84):** AGW f2 resolves `parent_rid` as the LAST occurrence of the highest-priority carrier class (`prev_resp_id>c1>c3>c2`), NOT the globally-last rid. So it can SKIP the immediate predecessor when that run used a lower-priority carrier or no marker (smoke `de0d6b63`: run4→run2, because run3 was a terminal text reply with no C1 tool_use marker). Also `parent_rid_anomaly` (= any candidate ≠ winner) fires on NORMAL multi-turn replay (every older rid in replayed history "disagrees") → noisy/near-useless. FIX: resolve by GLOBAL recency (max body position, any carrier) + flag anomaly only on SAME-position carrier conflict. Tradeoff: recency-over-trust vs current trust-ordering (C1 hard to forge, C2 spoofable).
- **`parent_run_rid` (MCP call ↔ requesting LLM run) is CORRECT and robust — different mechanism, NOT affected by the parent_rid quirk.** It's a direct f3-stamp (inject current run's rid into that tool_use.input) / f4-pop. Can only be correct or absent (if framework strips the key), never mis-attributed. This is the primary RID use case and it works.
- **aiplay correlation should be POSITIONAL:** the `llm_request` audit entries in capture order ARE the runs in order; immediate predecessor = run k−1 positionally. verdict_l (C1) does this and treats AGW's `parent_rid` as the value-under-test — resilient to the AGW resolution quirk. Don't depend on AGW's parent_rid as ground-truth for "which run preceded."
- **pytest is NOT on the host VM.** efficacy/api tests run in `aiplay-harness-api-1` (has pytest-asyncio). Adapter tests (`test_adapter_combo.py`, imports `framework_bridge`) run in `aiplay-adapter-combo-1`, which needs `pytest-asyncio==0.23.8` pinned (newer 1.x breaks event-loop on 3 init tests). Pattern: `docker cp harness/efficacy.py tests pytest.ini <container>:/app/ && docker exec <container> sh -c 'cd /app && python -m pytest …'`.
- **Harness Python is NOT volume-mounted** (`/app` is baked into the `aiplay-harness:local` image; only `data`, `frontend`, `defaults.yaml` are mounts). So harness code changes (e.g. C1 verdicts l/m) need an IMAGE REBUILD to go live — and the running uvicorn caches the import at startup, so even an in-place `docker cp` needs a process restart (and a recreate reverts it). **`frontend/` IS mounted** → C2 viz changes are live immediately.
- **AGW image for the RID arc: `ghcr.io/agentgateway/agentgateway:v1.0.1-ib.cidgar`** (CHG-26 + CHG-26F). docker-compose line ~40.
