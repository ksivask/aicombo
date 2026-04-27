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
