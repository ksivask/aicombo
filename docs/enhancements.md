# aiplay — enhancements list

Open brainstorm topics for post-Plan-B. Each item is a discussion seed, not a committed work item.

---

## E1 — OpenTelemetry support per framework: minimum to propagate `conversation_id`

**Question.** What is the bare-minimum hook each framework gives us to attach (and propagate) a `conversation_id` (== cidgar `cid`) onto outbound LLM/MCP HTTP spans? cidgar today reads CID from a tool-arg / system-prompt marker — adding it as an OTel attribute on the span would give us a second, transport-independent channel that's invisible to the model.

**Per-framework angle to investigate.**

| Framework | OTel surface | First thing to try |
| - | - | - |
| langchain | `langchain.callbacks.tracers.LangChainTracer` + native OTLP exporter (langsmith integration); `BaseCallbackHandler.on_llm_start` exposes serialized run id | Subclass tracer, set span attribute `aiplay.cid` from a contextvar set per-turn. |
| langgraph | Inherits langchain callbacks; also has its own `RunTree` checkpoint events | Same as langchain, plus tag node-execution spans. |
| crewai | crewai 1.x emits its own telemetry (opt-out via `CREWAI_TELEMETRY_OPT_OUT`). Limited public callback API | Likely need httpx-instrumentation only — crewai SDK clients use the httpx we inject. Set span attribute via httpx event-hook. |
| pydantic-ai | First-class Logfire integration; `Agent.instrument_all()` wires OTel | `agent.instrument(openai_client=True)` then attach attribute via context. |
| autogen | autogen-core ships `opentelemetry` package with `tracing_config`. Spans for `llm_call`, `tool_call`, `agent_run` | Pass `tracer_provider` + custom `TextMapPropagator`, inject `cid` as baggage. |
| llamaindex | llama-index-instrumentation-opentelemetry package; events: `LLMChatStartEvent`, `LLMChatEndEvent` | Subscribe to dispatcher, mutate active span attrs. |
| direct-mcp | No agent — direct httpx call. Trivially set OTel attr in the same hook block where we set `_ib_cid`. | Reference baseline. |

**Cross-cutting.** `httpx`'s OpenTelemetry instrumentation (`opentelemetry-instrumentation-httpx`) might give us the propagation for free across all six frameworks since they all share the harness's `httpx.AsyncClient` — could be the lowest-LOC path to a uniform "every outbound span carries cid" guarantee.

**What success looks like.**
- A new `with_otel: bool` row toggle.
- Adapter starts an in-process Jaeger / OTLP collector (or sends to stdout exporter) per trial.
- `verdict_g_otel_propagation`: every llm_request audit entry's `cid` appears as a span attribute on the corresponding outbound POST span.

**Open Qs.**
- Should the harness ship a one-shot OTLP collector container in compose, or just dump JSON-stdout spans into the trial JSON?
- Does AGW already emit OTel spans from cidgar hooks? If yes, end-to-end correlation is the prize.

---

## E2 — Channel-2 marker (LLM non-tool responses): alternatives to HTML comment block

**Today.** cidgar f3 PATH B prepends `<!-- _ib_cid=ib_xxxx... -->` to assistant text content. Survives most renderers (chat UIs treat it as opaque whitespace), invisible in markdown view.

**Why look further.** The HTML comment is fragile in three ways: (1) some Anthropic clients normalize/strip leading whitespace AND comments before display, (2) tool-call-only responses have no text part to carry it, (3) it pollutes the model's own context if echoed back next turn (the model then learns to emit it itself, which corrupts the channel).

**Alternative carriers to evaluate.**

| Option | Where it lives | Pros | Cons |
| - | - | - | - |
| **Zero-width Unicode tag sequence** | Encoded into normal text using U+E0061..U+E007A "tag" chars (or U+200B/U+200C ZWNJ) — invisible everywhere | Survives copy-paste, indistinguishable from prose, robust to markdown stripping | Some terminals fail to render or warn; mobile clients show � for unsupported codepoints |
| **Response metadata field** (OpenAI `metadata`, Anthropic `_meta`, Responses `extra_body`) | Out-of-band field on the response envelope | Clean separation from prose; never reaches the model's next turn | Provider-specific shape; Anthropic strips unknown top-level fields on count_tokens; pass-through depends on AGW propagation |
| **HTTP response header** (`X-Ib-Cid: ib_xxx`) | Response headers cidgar adds in f3 | Truly out-of-band, never enters the conversation | Frameworks rarely surface response headers to the agent; needs adapter cooperation per framework |
| **Structured tool call with synthetic tool** | Synthetic `_ib_attest` tool call appended to assistant message | Strongly typed; survives streaming chunking | Agent loop will try to dispatch it; need swallow logic on tool-router side |
| **Hash-prefix tag in first token** (`#ibcid:ib_xxx\n`) | Plain text leading line | Trivial to parse, visible-but-ignorable | Model echoes it back; user-visible "noise" |
| **JSON sidecar via SSE event-name** (`event: ib_meta\ndata: {cid: ...}\n`) | Streaming-only carrier | Zero text contamination; standard SSE feature | No equivalent in non-streaming responses; client must subscribe to the side-channel |
| **Watermark in token statistics** (e.g. set `seed` echo or `system_fingerprint` to encode CID) | Provider-allowed metadata fields | Invisible | Provider-specific hijacking of fields with their own meaning; ugly |

**Recommended next step.** Build `verdict_b2_marker_robustness` test that round-trips each candidate through (a) markdown renderer, (b) Anthropic count_tokens, (c) a re-feed into the next user turn — then pick the highest-survival option as the new default. Keep HTML comment as a fallback for backward compat.

---

## E3 — Channel-3 marker (MCP `tools/call` response): alternatives to a synthetic resource block

**Today.** cidgar f5 appends a `resource` content block to the `tools/call` response listing the CID:

```json
{"content": [
  {"type": "text", "text": "<actual tool result>"},
  {"type": "resource", "resource": {"uri": "ib://cid/ib_xxx", "mimeType": "application/json", "text": "{...}"}}
]}
```

MCP spec defines `resource` blocks for "linked content" — we're abusing the slot for an attestation marker.

**Why look further.** (1) Some MCP clients (notably the LangChain MCP adapter pre-0.0.6 + a few others) drop unknown content block types or fail to round-trip them. (2) Models occasionally try to "use" the resource (fetch the URI), which 404s. (3) The marker is visible in the model's context — same context-pollution risk as E2.

**Alternative carriers to evaluate.**

| Option | Where it lives | Pros | Cons |
| - | - | - | - |
| **MCP `_meta` field on the result envelope** | `{"content": [...], "_meta": {"_ib_cid": "ib_xxx"}}` — already reserved by MCP spec for client/server metadata | Spec-compliant, out-of-band, hidden from model | Adapter has to read it back; some MCP SDKs drop unknown `_meta` keys |
| **JSON-RPC response header / extension** | A custom field at the JSON-RPC top level (`{"jsonrpc": "2.0", "id": ..., "result": {...}, "_ib_meta": {...}}`) | Truly outside the result body | Strict JSON-RPC validators reject unknown top-level fields |
| **Streamable-HTTP response header** | `MCP-IB-Cid: ib_xxx` on the SSE / chunked HTTP transport | Transport-level, never enters MCP protocol layer | Stdio transport has no equivalent; harness needs httpx-event-hook capture (already in place) |
| **Annotate existing text block with attribution** | `{"type": "text", "text": "...", "annotations": {"_ib_cid": "ib_xxx"}}` — `annotations` is in the MCP spec for `audience`/`priority` hints | Spec-defined slot exists | Same drop-on-unknown-key risk; not all SDK versions surface annotations |
| **Sentinel tool-result `isError=false` + structured payload** | When server already returns structured content, embed `_ib_cid` in a known top-level key | Zero new content blocks | Only works for servers that emit JSON results, not free-form text |
| **Side-channel via MCP `notifications/progress`** | Send a progress notification with the CID before/after the tool result | Standard notification path, asynchronous | Progress notifications may not arrive in deterministic order with the result; client must correlate |
| **Resource-link block** (MCP 2025-06 spec addition) | New `{"type": "resource_link", "uri": "ib://cid/..."}` — lighter than full resource embed | Spec-blessed, smaller payload | Not all clients implement the new type yet |

**Recommended next step.** Add `verdict_f2_channel3_robustness` — round-trip each candidate through all 7 framework adapters' MCP clients (langchain-mcp-adapters, fastmcp.Client, MCPServerStreamableHTTP) and the direct-mcp control. Score by: marker preserved + not surfaced into the LLM's context + parseable on the harness side. Likely winner: `_meta` field on the result envelope, with the resource block kept as a one-version-back compatibility shim.

---

## E4 — Diff view: governed vs baseline trial comparison

**Today.** T13 landed clone-for-baseline (`POST /matrix/row/{id}/clone-baseline`, commit `0dbcc49`): a governed row (routing=via_agw) can be cloned as a direct-routed sibling with identical `framework`/`api`/`llm`/`mcp`/`turn_plan_override` but bypassing AGW. The clone relationship is tracked in matrix-row metadata (`baseline_of`). Workflow: run governed, run baseline, open both drawers, eyeball the two JSON trials for differences.

**Why look further.** The value of the baseline pair is in the **delta**, not in either trial alone. The difference is what proves cidgar is doing something; baseline alone is useless (nothing to prove), governed alone could be a false positive (any agent framework emits tool-call traffic). Without a diff surface the `baseline_of` metadata is decorative — the drawer doesn't consume it, and no endpoint pairs the two trials on read.

**What a diff should surface (ranked by pedagogical value).**

| Diff target | Pedagogical value |
| - | - |
| **Audit log entry count** | Governed > 0, baseline = 0. Fails loudly if baseline shows audit entries → bypass leak. |
| **LLM request body: system prompt** | Governed adds cidgar C1 marker (system-prompt CID); baseline doesn't. Visualizes Channel-1 injection. |
| **LLM request body: tool schemas** | Governed has `_ib_cid` + `_ib_gar` params on every tool (f1); baseline has bare schema. Visualizes Channel-2 injection. |
| **MCP `tools/call` args** | Governed passes through stripped (f4); baseline passes original. |
| **MCP `tools/call` response content blocks** | Governed has the extra `resource` block (f5); baseline doesn't. Visualizes Channel-3. |
| **LLM response content** | Governed has HTML-comment CID marker prepended (f3 PATH B); baseline doesn't. |
| **Latency per turn** | Governance overhead in ms. Feeds verdict (h) proposal. |
| **Token count per turn** | Governance-injected tokens (markers + schema additions) inflate cost. |
| **Verdicts** | Governed should pass `a/b/c/d/e/f` where applicable; baseline should be all `na` (direct routing short-circuits every verdict). |

**UI placement options.**

| Option | Pros | Cons |
| - | - | - |
| **(a) Side-by-side drawer split** — open both at once, two columns | Familiar diff-tool shape; no new route | Drawer already dense; horizontal scroll hell on laptops |
| **(b) Dedicated `/pairs/{row_id}` route** — full-page diff with turn-by-turn tabs, governed-left / baseline-right, inline highlights | First-class workflow; purpose-built layout; room for structural (not just textual) diffs | New page to maintain; extra routing / state plumbing |
| **(c) Drawer with "Show baseline inline" toggle** — toggle shows baseline's values as strikethrough / faint overlay next to each field | No navigation; diff is local to the thing you're already looking at | Only shows 1:1 same-position fields; misses structural diffs (extra blocks, missing entries) |

(b) is the likely candidate for v1 — structural diffs (extra resource block, missing tool param) are the main payload and need layout room. (c) is a natural follow-up once the classifier from (b) exists.

**Backend support needed.**

- `GET /pairs/{row_id}` — returns both trials (governed + its last baseline), paired by `baseline_of` metadata. Shape: `{governed: Trial, baseline: Trial, diff_summary: {...}}`.
- `GET /pairs/{row_id}/diff?path=turns.0.response` — returns scoped diff for a specific field-path. Avoids shipping 2× trial JSON on every diff-UI interaction.
- Optional: `POST /pairs/{row_id}/run` — triggers governed + baseline back-to-back atomically, with shared turn plan. Guarantees "same prompts, same clock neighborhood" so the diff is governance-only, not time-of-day drift.

**Challenges / open questions.**

- **Non-determinism of LLM output.** Even same-row re-runs differ. How much noise vs signal? Mitigations: pin `temperature=0`, pin `seed` when the API supports it, or focus the diff on request bodies (deterministic) rather than response bodies (non-deterministic).
- **Clock skew in audit-window correlation.** If governed runs 10s before baseline and AGW is warm for one but cold for the other, latency diff conflates governance-overhead with cache-warmth-overhead. The atomic pair-run endpoint helps; it doesn't fully cure.
- **"Significant" diff vs noise.** cidgar expects certain diffs (presence of CID markers, extra tokens). Making the UI highlight ONLY the unexpected deltas (missing CID on governed, present CID on baseline) requires a diff-classifier — not a raw textual diff. The classifier becomes its own artifact and needs its own tests.
- **Pair lifecycle.** What if user re-runs governed but not baseline? Pair becomes stale. Store `paired_at` timestamps and warn on stale pairs.
- **N > 2 rows.** Three-way diffs (e.g., via_agw cidgar-enabled vs via_agw cidgar-disabled vs direct) are a natural v2; punt for now.

**Verdict this unlocks.** `verdict_h_overhead`: delta of p50 latency between paired governed and baseline turns. Pass if median overhead < 200ms. Fail if > 2000ms or > 100% of baseline. `na` if no pair exists.

**Effort estimate.** **L (full day+).** Backend is moderate (2 new endpoints + diff-classifier), frontend is the bulk (new page or a structural drawer refactor). Not minimum-viable-without-it — aiplay works today without diffs. Likely revisit after (h) and (i) verdict additions land.

**Cross-references.**

- Builds on T13 clone-for-baseline — `baseline_of` metadata already exists; no schema change needed for the minimum pair-lookup endpoint.
- Feeds verdict (h) proposal in the Plan C brainstorm.
- Complements E2/E3 — a diff surface is where a marker-choice's effect becomes legible. Swapping HTML-comment → zero-width tag for Channel-2 is invisible without this view.

---

## E5 — close remaining framework × API gaps

E5a (langchain) + E5b (langgraph) shipped messages + responses + responses+conv via the libraries' native wrappers (`ChatAnthropic`, `ChatOpenAI(use_responses_api=True)`). Three per-adapter gaps remain between library capability and our current adapter implementation — each is an independent small enhancement.

| | Framework | Missing API(s) | Library path | Why skipped in Plan B |
| - | - | - | - | - |
| **E5c** | crewai | responses, responses+conv | Bypass to `openai.AsyncOpenAI.responses.create` (mirror autogen/llamaindex pattern) | crewai 1.14 has no first-class Responses client; Plan B T3 scoped crewai to the APIs the SDK supports natively |
| **E5d** | pydantic-ai | responses+conv | Bypass to openai SDK for state-chain threading (pydantic-ai's `OpenAIResponsesModel` doesn't thread `previous_response_id` as of 1.86) | library gap, not T4 scope gap |
| **E5e** | llamaindex | messages | Add `llama-index-llms-anthropic` dep + branch the adapter on `api=messages` → `Anthropic` class | separate package; Plan B T6 chose to skip the extra dep |

**Shape of each fix.** All three mirror the canonical multi-API adapter shape already in use (autogen / llamaindex today, langchain / langgraph post-E5a/b): branch inside `_build_llm(api, config)`, extend `ADAPTER_CAPABILITIES`, update `/info`, add constructor tests + a compact test for the new mode.

**Effort.** Each one is **S** (~1-2 hours including tests). All three would bring every adapter to 4/4 API coverage except direct-mcp (intentional N/A).

**Why close them.**

- **Coverage parity** — today only autogen has all 4 APIs; langchain/langgraph will after E5a/b; adding E5c/d/e takes the remaining 3 adapters to full coverage.
- **Verdict (e) strengthens further** — state-mode gap is currently exercised by autogen + llamaindex only; adding crewai/langchain/langgraph/pydantic-ai (via E5c + E5a/b + E5d) would 5× the cross-check signal.
- **Cross-framework marker-robustness testing (E2/E3)** — more adapters × more APIs × more protocols = more places the marker-choice must survive. The diff view (E4) makes this legible.

**Dependencies.**

- E5c depends on nothing (pure crewai change)
- E5d depends on nothing (pure pydantic-ai change)
- E5e requires adding `llama-index-llms-anthropic` to llamaindex adapter's requirements.txt + `ANTHROPIC_API_KEY` plumbing (already done in sibling adapters)

**Ordering recommendation.** Do E5e first (broadens messages coverage — easiest signal); then E5c (exercises the autogen-style bypass pattern one more time); then E5d last (pydantic-ai responses+conv is the subtlest — state-chain semantics around typed `ModelMessage`).

---

## E6 — Responses API shape handler in cidgar (POSTPONED — mega item)

**Status: postponed.** This is the largest outstanding gap but requires substantial AGW Rust work. Filed here so the question doesn't get lost.

**What we found.** AGW's cidgar pipeline has NO `responses_shape.rs`. The `LlmRequest`/`LlmResponse` enums in `crates/agentgateway/src/governance/mod.rs:23-32` have only `Messages(&mut messages::Request)` and `Completions(&mut completions::Request)` variants. The dispatch in `llm/mod.rs:770-778`:

```rust
match original_format {
    InputFormat::Messages    => gov.on_llm_request(LlmRequest::Messages(m), ...),
    InputFormat::Completions => gov.on_llm_request(LlmRequest::Completions(c), ...),
    _ => None,  // Responses, Embeddings, CountTokens, Realtime, Detect all fall through
}
```

And response-path `llm/mod.rs:960-989`:

```rust
matches!(req.input_format, InputFormat::Messages | InputFormat::Completions)
// Other response shapes (responses, embeddings, count_tokens, ...): no-op per §14.6.
```

**Net consequence.** On `api=responses` or `api=responses+conv` rows, AGW applies ZERO cidgar governance to LLM traffic:
- Channel 1 (tool args CID) — never injected
- Channel 2 (text marker) — never appended
- `llm_request` / `llm_response` / `tool_planned` / `terminal` audit-log phases — never emitted

The ONLY cidgar channel that fires for Responses trials is **Channel 3** (MCP tool-result resource block), because the MCP session handler's f4/f5 hooks are format-agnostic (they run on JSON-RPC `CallToolRequest` dispatch regardless of what LLM API the agent is using upstream).

Concretely: a Responses-API trial without MCP is completely cidgar-invisible. A Responses-API trial with MCP only gets CID on the MCP path, not the LLM path.

**Previously flagged in cidgar spec §14.6** as an intentional "future item" during the initial spec/design phase — not a missed implementation, but a deferred one. Adding it is mega work because it requires:

1. Add `Responses(&mut responses::Request)` variant to `LlmRequest` (and symmetric `LlmResponse`).
2. Write `crates/agentgateway/src/governance/responses_shape.rs` (~750 LoC, mirroring `messages_shape.rs` / `completions_shape.rs`):
   - `extract_uctx` / `extract_sctx` walkers
   - `clean_and_scan_request` — scan `input[]` / `messages[]` for `_ib_cid` markers on the request side
   - `inject_cid_into_tool_calls_response` — walk `output[]` where `type=function_call`, mutate `arguments` string (str→json→mutate→json→str)
   - `append_text_marker_response` — append `output[i]` text block OR modify last existing `output_text` (which? — design Q)
   - `peek_gar_from_tool_call` — extract `_ib_gar` from the arguments JSON
3. Wire it in `llm/mod.rs:770` dispatch + `llm/mod.rs:960` response dispatch.
4. Add ~40-50 unit tests covering request-scan + response-inject + round-trip for Channel 1 + Channel 2.
5. Decide: on `responses+conv` (previous_response_id state mode) WITHOUT MCP, even Channel 1 + Channel 2 may not be enough — turn N's request body contains only `input` (not prior `output[]`), so Channel 1 marker from turn N-1's response doesn't echo back. A **server-stateful Channel 4** (AGW stores `response_id → cid` map, reads incoming `previous_response_id`, re-injects / verifies) may be required. This violates cidgar's stateless-pipeline design principle — meta-decision needed.

**Effort.** **XL (1-2 weeks of AGW work).** Prerequisite for verdict (e) to be meaningful on ANY Responses-mode adapter (autogen/llamaindex/langchain-post-E5a/langgraph-post-E5b all fall through to the no-op today).

**Consequence for existing verdicts.** Re-examine all verdict-(e) passes recorded on responses trials — they are likely false positives. The CID the harness observed in the audit window probably came from MCP (Channel 3) + adapter-side `_response_history` coincidence, not from AGW's channel machinery. `docs/findings-plan-b.md` should carry a caveat.

**Why postponed.** aiplay's current Plan B + E1-E5 scope does not include AGW changes beyond policy config. E6 needs the AGW owner to schedule a dedicated cidgar v1.1 cycle.

---

## E8 — streaming-aware Ch1 + Ch2 injection (REVISED)

**Status: parked. Bigger than originally scoped.**

**What we found.** `llm/mod.rs:871-876` early-exits streaming responses to a dedicated `process_streaming` path that bypasses cidgar's `on_llm_response`. Comment references "non-streaming only per V5 / Plan Addendum Delta D" — documented spec gap.

**Consequence.** Every trial with `stream=True` has Ch1+Ch2 skipped regardless of API style. Verdict (a) presence + (b) channel structure silently degrade. Only Ch3 (MCP resource block) still fires.

**Original E8 sketch (too narrow).** "Terminal-only Ch2 via final SSE event." Only helps Channel 2 — the text marker gets into the audit stream via a trailer. **Channel 1 is untouched** by this approach, and Channel 1 is the PRIMARY carrier for MCP-routing — it's how the `_ib_cid` gets into the tool-call arguments that the MCP server sees. Skipping Ch1 on streaming means streaming + MCP trials cannot have CID-in-tool-args AT ALL.

**Channel 1 on streaming is the harder problem.** When an LLM streams a tool_call, the `choices[0].delta.tool_calls[0].function.arguments` field is assembled incrementally across many SSE chunks:

```
data: {"choices":[{"delta":{"tool_calls":[{"index":0,"function":{"arguments":"{\"loc"}}]}}]}

data: {"choices":[{"delta":{"tool_calls":[{"index":0,"function":{"arguments":"ation"}}]}}]}

data: {"choices":[{"delta":{"tool_calls":[{"index":0,"function":{"arguments":"\":\"SF\"}"}}]}}]}
```

By the time the stream's `finish_reason=tool_calls` fires, the client has already assembled and dispatched. Injecting `_ib_cid` in a terminal trailer is too late — the client has routed.

**Three approaches (pick one or combine):**

| Approach | What | TTFT impact | Complexity | Channel coverage |
| - | - | - | - | - |
| **(a) Buffer-and-reinject** | AGW accumulates the full streamed response, invokes `on_llm_response` on the assembled body, re-emits either as a single block or as a new stream from the buffer. | **Breaks streaming UX** (loses "first token ASAP" guarantee). For tool-call responses specifically, TTFT matters less — most clients buffer tool_calls locally before dispatch anyway, so user-visible impact may be small. | Low — reuse existing non-streaming path after buffering. | Full Ch1 + Ch2. |
| **(b) Stream-aware partial-JSON injection** | AGW detects `tool_calls[i].function.arguments` assembly in-flight, inserts `"_ib_cid":"ib_xxx",` at the JSON opening brace before forwarding the first argument chunk. Partial-JSON state tracking per chunk. | Preserves TTFT. | **High** — needs partial-JSON parser + chunk-boundary-aware mutation + recovery on malformed fragments. | Ch1 ✓, Ch2 (terminal) ✓. |
| **(c) Terminal-chunk append** | For Ch2: append a final SSE event carrying the marker after `[DONE]`. For Ch1: too late (client already dispatched). This is the original E8 — doesn't solve Ch1. | None. | Trivial. | **Ch2 only** (Ch1 stays broken). |

**Recommendation.** **(a) buffer-and-reinject** gated by config option `channels.streaming_mode: buffer | passthrough`. Default `buffer` for correctness; operators who need raw TTFT can opt into `passthrough` and accept CID loss on streaming trials. Add `verdict_l_streaming_integrity` to check that Ch1 + Ch2 round-trip through streaming as they do through non-streaming.

**Hybrid option (worth noting).** Start with **(c)** for Ch2 (cheap, lands in weeks); defer **(a)**/**(b)** for Ch1 to a follow-up once the Ch2 piece is validated. Accept interim Ch1 gap on streaming trials — document in verdict (a)/(b) test expectations so false fails don't surface.

**Effort.** **M** for (c) + Ch2-only partial win; **L** for (a) buffer-and-reinject full win; **XL** for (b) stream-aware partial-JSON. E8 blocks an entire axis (stream=True) of the matrix — higher strategic value than the T/F marginal counts suggest.

---

## E9 — curated model list per provider + UI dropdown

**Status: in flight (subagent dispatched).**

**What.** Today the matrix's `model` column is a text field (defaults via `DEFAULT_<PROVIDER>_MODEL` env, manual override). E9 surfaces a real dropdown of curated models per provider, with semantic metadata (display name, tier, capability flags).

**Why now.** `claude-3-5-haiku-20241022` died on the active OpenAI/Anthropic accounts mid-session — the env-default trick worked but exposed that operators don't know which models are accessible. A dropdown shows the curated set instead of demanding the user know provider catalog conventions.

**Why not pure env.** The list NEEDS metadata env can't carry: per-model capability flags (does it support tool_calls? streaming? Responses API?), display names, tier annotations (`cheap` / `mid` / `reasoning`). Env stays for `DEFAULT_<PROVIDER>_MODEL` (scalar — already shipped). Code dict carries the structured list. Env can OVERRIDE the list per provider via `CHATGPT_MODELS=a,b,c` for ops emergencies (with metadata defaulting).

**Design.**

```python
# harness/models.py
@dataclass(frozen=True)
class ModelInfo:
    id: str
    display: str
    tier: str  # "cheap" | "mid" | "reasoning" | "custom"
    supports_tools: bool = True
    supports_responses_api: bool = False

CURATED = {
    "ollama":  [ModelInfo("qwen2.5:7b", "Qwen 2.5 7B", "cheap"), ...],
    "chatgpt": [ModelInfo("gpt-4o-mini", ...), ModelInfo("gpt-4o", ...), ModelInfo("o1-mini", ..., supports_tools=False)],
    "claude":  [ModelInfo("claude-haiku-4-5", ...), ModelInfo("claude-sonnet-4-6", ...), ModelInfo("claude-opus-4-7", ...)],
    "gemini":  [ModelInfo("gemini-2.0-flash", ...), ...],
}

def get_models(provider: str) -> list[ModelInfo]:
    if raw := os.environ.get(f"{provider.upper()}_MODELS"):
        return [ModelInfo(id=m.strip(), display=m.strip(), tier="custom") for m in raw.split(",")]
    return CURATED.get(provider, [])
```

Plus `GET /providers/{id}/models` endpoint, frontend dropdown that populates on llm-change, "Custom..." sentinel for free text.

**Picked models per provider (initial curated set).**

| Provider | Cheap | Mid | Special |
|---|---|---|---|
| ollama | qwen2.5:7b | llama3.1:8b | mistral:7b |
| chatgpt | gpt-4o-mini | gpt-4o | o1-mini (reasoning, no tools) |
| claude | claude-haiku-4-5 | claude-sonnet-4-6 | claude-opus-4-7 |
| gemini | gemini-2.0-flash | gemini-2.0-pro | gemini-2.0-flash-thinking-exp |

**Effort.** **S** (~2-3h). Backend module + endpoint, frontend dropdown wiring, ~5 tests. Validator integration for `supports_tools` deferred to v2.

**Future-proofing.** If `(C) hybrid` (live discovery via provider `/v1/models`) ever lands, the curated list becomes the cache + fallback when the API is down.

---

## E13 — three distinct Responses-API state modes (aiplay adapter)

**Status: a/b in flight or planned, c is config-only.**

OpenAI's Responses API has three first-class state mechanisms on the same `/v1/responses` endpoint. Today aiplay collapses them into two by accident: the validator allows `responses + state=T` but the adapter ignores `state` and treats it as `state=F`; meanwhile `responses+conv` is implemented via `previous_response_id` chaining despite the "+conv" naming (originally meant: Conversations API).

### Three target modes

| Mode | Wire shape | Setup |
| - | - | - |
| **F (state=F)** | full history in `input` each turn; no state field | none |
| **T (state=T)** | `previous_response_id: "resp_xxx"` (chain) | none — anchor flows from each prior response's id |
| **C (responses+conv)** | `conversation: {id: "conv_xxx"}` (container reference) | `POST /v1/conversations` to mint the id once per trial |

Mode F is what library default-emits today. The adapter code path needs zero change. (Mode F surfaced AGW's `InputItem` strictness — see E14, separate fix.)

### E13a — state=T support (chain mode)

**Scope: 5 adapter files**, ~10 LOC each + 1 test each.

In every `framework_bridge.py::turn()` that handles api=responses, change the previous_response_id-threading branch from:

```python
if api == "responses+conv":
    invoke_kwargs["previous_response_id"] = ...
```

to:

```python
if api == "responses+conv" or (api == "responses" and config.get("state")):
    invoke_kwargs["previous_response_id"] = ...
```

(After E13b lands, the `"responses+conv"` half flips to using `conversation` instead — see below — so this branch becomes only the `responses + state=T` case.)

Validator already permits `state=T` on `(api=responses, llm=chatgpt)`. UI already shows the checkbox as editable. Just the runtime is wrong.

### E13b — responses+conv via Conversations API (container mode)

**Scope: 4 adapter files + AGW config + tests.** The bigger semantic shift.

Per-trial setup once on first turn:

```python
async def _ensure_conversation_id(self):
    if self._conversation_id is not None:
        return self._conversation_id
    # Hits /v1/conversations through our hooked httpx client →
    # all wire bytes captured + (with passthrough route) audit-tail logged.
    r = await self._http_client.post(
        f"{self._llm_base_url}/conversations",
        json={},
        headers={"Authorization": f"Bearer {self._api_key}"},
    )
    r.raise_for_status()
    self._conversation_id = r.json()["id"]
    return self._conversation_id
```

Per-turn `extra_body`:

```python
if api == "responses+conv":
    conv_id = await self._ensure_conversation_id()
    invoke_kwargs["extra_body"] = {"conversation": {"id": conv_id}}
    # NOTE: no previous_response_id when using conversation container
```

Verify `langchain_openai.ChatOpenAI(use_responses_api=True)` accepts `extra_body` (it does in 1.x via the openai SDK passthrough). For autogen/llamaindex which bypass langchain to openai SDK directly, pass `conversation={"id": conv_id}` as a kwarg.

Affected adapters: langchain, langgraph, autogen, llamaindex (4 — pydantic-ai doesn't currently support responses+conv).

### E13c — AGW config: /v1/conversations passthrough route

**Scope: 3-line YAML.** Add to `agw/config.yaml::llm-chatgpt::ai.routes`:

```yaml
ai:
  routes:
    "/v1/chat/completions": completions
    "/v1/responses": responses
    "/v1/conversations": passthrough     # E13c
```

Without this, the setup call `POST /v1/conversations` would 503 (default Completions parser). With `passthrough`, AGW just byte-forwards. No governance instrumentation on setup (cidgar's f2/f3 hooks don't apply to non-LLM endpoints anyway), but the bytes flow + audit-tail trace logs the request/response for diagnostic visibility.

Requires the AGW image to support `passthrough` as a valid `RouteType` value in YAML — verify by grepping AGW source: `RouteType::Passthrough` exists in the enum, but its serde naming may need confirmation (`passthrough` lowercase).

### Effort & dependencies

- E13a (S, ~1h): independent of E14. Can ship now.
- E13b (M, ~half-day): requires E13c (AGW config). Doesn't require E14.
- E13c (XS, ~5min): just a YAML add + AGW restart. Does require AGW image to recognize the value.

### Why we want all three

Each mode stresses cidgar's eventual E6 instrumentation differently:

- **F**: cidgar would walk full history every turn → most audit volume, repeated CID-marker observations
- **T**: cidgar sees one new user turn + open tool_outputs → minimal audit volume; CID had to be embedded in prior response (Channel 1 or 2) AND survive server-side persistence
- **C**: same minimal volume as T, BUT identity is via container ID not response chain — different failure mode if conversation ID is lost vs response chain breaks

Verdict (e) — state-mode gap — would have THREE distinct failure surfaces to validate. Today it's one (poorly-implemented chain).

---

## E14 — relax AGW Responses input strictness (Option B custom Deserializer)

**Status: in flight (subagent dispatched).**

**The bug.** AGW's `crates/agentgateway/src/llm/types/responses.rs::Request.input: Input` field uses async-openai 0.34's strict `InputParam` untagged enum. Legitimate library-emitted bodies (langchain re-sending prior assistant content with `status` field on stateless multi-turn) fail all three `InputItem` variants and produce the unhelpful "data did not match any variant of untagged enum InputParam at line 1 column N" error → AGW returns 503.

**Why it adds zero value today.** The strict typing is only consumed by Bedrock/Vertex conversion paths (`conversion/bedrock.rs:1638`, `conversion/vertex.rs:17`) and by E6 governance (which doesn't exist). For OpenAI backend (our case), AGW deserializes strictly only to immediately re-serialize and forward — pure overhead.

**The fix (Option B — custom Deserializer over Option A — untagged enum):**

```rust
#[derive(Debug, Clone, Serialize)]
pub enum InputCompat {
    Typed(Input),
    Raw(serde_json::Value),
}

impl<'de> Deserialize<'de> for InputCompat {
    fn deserialize<D: Deserializer<'de>>(d: D) -> Result<Self, D::Error> {
        let v = serde_json::Value::deserialize(d)?;
        match Input::deserialize(v.clone()) {
            Ok(typed) => Ok(InputCompat::Typed(typed)),
            Err(e) => {
                tracing::warn!(error = %e, "responses input fell back to raw passthrough");
                metrics::counter!("agw.responses.input.fallback").increment(1);
                Ok(InputCompat::Raw(v))
            }
        }
    }
}
```

**Why B beats A:**

- **Diagnostic logging**: when typed parse fails, the actual serde error is logged. Eliminates "didn't match any variant" debugging black holes.
- **Metrics**: `agw.responses.input.fallback` counter tells operators when a new client library shape needs explicit support.
- **Single-buffer**: deserialize Value once, attempt typed conversion from it. Untagged enum (A) buffers twice in worst case.
- **Future-proof for E6**: E6 can call `Input::deserialize(raw_value)` itself for the Raw variant on a best-effort basis.
- **Explicit fallback policy**: can decide "fall back only on shape mismatch, bubble on truly malformed JSON" later if needed.

Cost: ~80 LOC vs A's ~30. Justified by the operational visibility — this bug already cost hours of debugging that B's logging eliminates.

**AGW callers update**: `conversion/bedrock.rs` + `conversion/vertex.rs` need to handle both variants. For Bedrock/Vertex backends with Raw input, return a typed-conversion-required error (clean error path; not silent broken). For OpenAI backend (our chatgpt route), neither file is invoked — Raw flows straight through.

**Tests:** typed-input round-trip preserved, raw-input fallback works, prior-assistant-message-with-status (the exact body that broke trial `e2413edf`) deserializes as Raw, metrics counter fires on fallback.

**Effort: M (~half-day, AGW Rust + tests + AGW build).**

**Dependencies:** none. Independent from E13.

---

## E15 — wire OpenAI `/v1/responses/{id}/compact` for state=T compact

**Status: future.**

Today `compact()` on a trial running `api=responses + state=T` (chain mode) is effectively a no-op — the implicit consequence of "client-side trim doesn't affect server-held context." But OpenAI's Responses API has a real server-side compaction endpoint:

```
POST /v1/responses/{response_id}/compact
→ returns a CompactionSummaryItem (type: "compaction") that becomes a new chain anchor
```

The async-openai 0.34 type system already has `Item::Compaction(CompactionSummaryItemParam)` for this. Calling it server-side is the only way to TRULY compact a chain-mode trial.

**Adapter impl** (4 adapters: langchain, langgraph, autogen, llamaindex):

```python
async def compact(self, strategy: str) -> dict:
    api = self.config.get("api")
    state = self.config.get("state", False)
    if api == "responses" and state and self._last_response_id:
        # E15 — server-side compact
        base_url = pick_llm_base_url(self.config["routing"], self.config["llm"])
        api_key = _pick_api_key(self.config["llm"])
        r = await self._http_client.post(
            f"{base_url}/responses/{self._last_response_id}/compact",
            json={},
            headers={"Authorization": f"Bearer {api_key}"},
        )
        r.raise_for_status()
        new_anchor = r.json()
        compaction_id = new_anchor.get("id")
        if compaction_id:
            self._last_response_id = compaction_id
            self._response_history.append(compaction_id)
        return {"strategy": strategy, "method": "openai_server_compact",
                "anchor_before": self._response_history[-2] if len(self._response_history) > 1 else None,
                "anchor_after": compaction_id}
    # Fall through to existing branches (state=F client-side trim, +conv no-op)
```

**Truth table after E15:**

| Mode | compact behavior |
|---|---|
| `responses + state=F` | client-side trim (today) |
| `responses + state=T` | server-side `POST /v1/responses/{id}/compact` (NEW via E15) |
| `responses+conv` | no-op (no API for it; documented in E13b) |

**Effort.** S — ~30 LOC per adapter + 1 test per adapter (4 total). Wire bytes for the compact call flow through the same hooked httpx client; cidgar would see them when E6 lands.

**Why now.** E13b explicitly documented `+conv` compact as a no-op. State=T was left silent — adapter just falls through to no-op too, but undocumented. E15 either implements the real capability (preferred) OR adds a docstring note that state=T compact is intentionally no-op pending E15. Either is acceptable; implementing is cleaner.

**Dependencies:** none.

---

## E16 — migrate langgraph adapter from `langgraph.prebuilt` to `langchain.agents`

**Status: future. Defer until LangGraph V2.0 is announced.**

`langgraph.prebuilt.create_react_agent` emits `LangGraphDeprecatedSinceV10` warnings since LangGraph V1.0. Will be removed in V2.0. The replacement lives in the `langchain` package:

```python
# Today (deprecated, still works):
from langgraph.prebuilt import create_react_agent

# Required after LangGraph V2.0:
from langchain.agents import create_agent  # NB: renamed from create_react_agent
```

Migration touches `adapters/langgraph/framework_bridge.py` only (one import + one rename). But:

1. **Signature parity** — confirm kwargs the adapter passes (`tools`, possibly `state_modifier` for system prompts) keep their names. `create_agent` may have renamed some.
2. **Behavioral parity** — recursion_limit, message-history defaults, binding semantics for `previous_response_id` (which conflicts with our E13a + E13b plumbing if the new wrapper auto-threads it differently).
3. **Package ownership** — `langchain.agents.create_agent` lives in the `langchain` (or `langchain-classic`) package. May require version bumps in `requirements.txt`.

**Effort.** S (~1hr if signature/behavior is identical; up to half-day if anything changed).

**Risk.** Agent abstractions are notoriously version-fragile. Defer until LangGraph V2.0 RC is out — too early and you fight the migration twice (once now, once when V2 finalizes).

**Why filed now.** The deprecation warning is visible in pytest output today. Capturing the migration plan while the context is fresh.

**Dependencies:** None for the migration itself; tightly intertwined with langchain ecosystem version bumps.

---

## E17 — optional strict mode knob for E14 InputCompat

**Status: future. File-on-demand — implement when an operator actually asks.**

E14's `InputCompat` is unconditional: every `/v1/responses` body that fails strict typed parse silently falls through to `Raw` with a warning log. Today this is the right behavior — AGW shouldn't be stricter than upstream OpenAI, and the strict typing was vestigial for openAI-backend routes.

But a future security-conscious operator might want defense-in-depth: "reject any body shape I can't audit, instead of byte-forwarding it." E17 adds the opt-in.

**Per-route YAML:**

```yaml
governance:
  kind: cid_gar
  channels:
    text_marker: true
    resource_block: true
    mcp_marker_kind: resource
  responses:
    input_strict: false   # default — E14 permissive (Raw fallback on parse failure)
                          # true → reject Raw fallback with HTTP 400
```

**Implementation.** ~5 LOC in `crates/agentgateway/src/llm/types/responses.rs` + 1 test:

```rust
impl<'de> Deserialize<'de> for InputCompat {
    fn deserialize<D: Deserializer<'de>>(d: D) -> Result<Self, D::Error> {
        let v = serde_json::Value::deserialize(d)?;
        match Input::deserialize(v.clone()) {
            Ok(typed) => Ok(InputCompat::Typed(typed)),
            Err(e) => {
                // E17: future hook — consult a thread-local strict-mode flag
                // here; if set, propagate the error → caller maps to HTTP 400.
                tracing::warn!(error = %e, "responses input fell back to raw passthrough");
                Ok(InputCompat::Raw(v))
            }
        }
    }
}
```

The strict-mode flag would need to flow from policy config down to the deserializer call site (via thread-local OR via a `DeserializeSeed` wrapper that captures the flag from context).

**Use cases that would justify it:**
- Compliance frameworks requiring "all proxied content was inspectable" — Raw fallback breaks that invariant.
- Security review wanting to fail-closed on unknown shapes (potential injection / spec evolution surveillance).
- Dev/prod parity: dev permissive for fast iteration, prod strict for contract enforcement.

**Why deferred.** No real ask today. Adding the knob now means maintaining a config surface no one's using. Diagnostic log + metrics counter (E14's pending TODO) provide most of the visibility the knob would give for monitoring. Implement when the use case shows up.

**Dependencies:** E14 (already shipped). No other prerequisites.

---

## E18 — header-demux for concurrent trial isolation

**Status: future. Required prerequisite for first-class parallel trial runs.**

### The gap

Today aiplay's runner has no concurrency limit (M10's `MAX_CONCURRENT_TRIALS` env var was deleted in F2 because nothing read it). Mechanically you CAN issue two `POST /trials/{id}/run` simultaneously — each kicks off as its own `asyncio.create_task`. Adapter state, MCP sessions, and AGW request handling all isolate cleanly per-trial. **But verdicts will lie.**

`audit_tail.py` correlates governance log entries to a trial via the **time-window** `[started_at, finished_at]`:

```python
def entries_since(self, ts: float) -> list:
    return [e for e in self.buffer if e.get("captured_at", 0) >= ts]
```

Two trials whose windows overlap will pull the SAME governance entries into both verdict computations → cross-contamination. Verdicts (a) presence, (b) channel structure, (c) continuity, (d) resilience, (e) state-mode gap all rely on audit-log scanning, so all of them silently fail under concurrency.

audit_tail's own docstring (lines 14-23) is honest about this — header-demux is the documented future path:

> If concurrent trials are ever introduced, either cidgar needs to include request headers in the governance log, or we add a stateful correlation layer (trial currently-running flag on top of time-window).

### The fix (header-demux)

Adapters already inject `X-Harness-Trial-ID` + `X-Harness-Turn-ID` on every outbound request via the per-turn mutable headers dict. AGW just needs to **log them** as part of each governance entry. Then audit_tail filters by exact match instead of time window — concurrent trials don't bleed.

**AGW side (small Rust change):**

`crates/agentgateway/src/governance/log.rs::LogEntry` gains optional fields:
```rust
pub struct LogEntry {
    // ... existing fields ...
    pub harness_trial_id: Option<String>,   // E18 — from X-Harness-Trial-ID request header
    pub harness_turn_id: Option<String>,    // E18 — from X-Harness-Turn-ID request header
}
```

Hook into `governance/cidgar.rs::on_llm_request` + `on_tool_call_req` to extract these from the in-scope `request.headers()` and stash on `GovContext`, then `LogEntry::new` reads them off ctx. ~30 LOC + 2 tests in cidgar.

**aiplay side (small Python change):**

`audit_tail.py::AuditTail` gains a per-trial-id index:
```python
def entries_for_trial(self, trial_id: str) -> list[dict]:
    """Header-demux path. Returns audit entries explicitly tagged with this
    trial_id via the X-Harness-Trial-ID header (logged by cidgar post-E18).
    Falls back to time-window if no entries are tagged (older AGW builds)."""
    tagged = [e for e in self.buffer if e.get("harness_trial_id") == trial_id]
    return tagged  # caller falls back to entries_since(...) if empty
```

`api.py::_run_trial_bg`'s `audit_provider` already prefers header-demux when `_has_header_demux(trial)` returns True (verdict_b's existing `header_demux = ...` branch) — that path becomes live once entries actually carry the harness_trial_id field.

~5 LOC in audit_tail.py + 1 test.

### After E18 lands

Re-introduce `MAX_CONCURRENT_TRIALS` (env var + asyncio.Semaphore in `_run_trial_bg`):
```python
_TRIAL_SEMA = asyncio.Semaphore(int(os.environ.get("MAX_CONCURRENT_TRIALS", "1")))
async def _run_trial_bg(...):
    async with _TRIAL_SEMA:
        ...
```

Now N concurrent trials with correct verdicts. CI batch runs become viable.

### Dependencies

- **B5 prerequisite**: per-turn header propagation must actually work end-to-end across all adapters. The bonus B4 finding showed `httpx.AsyncClient` copies the `headers=` dict at construction — the per-turn mutation trick was silently broken in pydantic-ai (and possibly other adapters). E18 is useless without B5; B5 is the audit + fix.
- AGW deploy required (post-E18 commits in `ibfork/feat/cidgar`); aiplay-only change is insufficient.

### Effort

S in AGW (~30 LOC + 2 tests in `governance/log.rs`, `governance/cidgar.rs`), S in aiplay (~5 LOC + 1 test in `audit_tail.py`), trivial config addition (re-introduce `MAX_CONCURRENT_TRIALS`). M total including AGW build cycle.

### Validation that it worked

After E18 + B5 land:
1. Run two parallel via_agw trials with `framework=langchain` + `framework=autogen` (different containers, different LLMs, different MCPs).
2. Inspect both trials' `audit_entries` — each should contain ONLY entries tagged with its own trial_id; no cross-contamination.
3. Verdicts should be identical to the same trials run serially.

---

## E19 — multi-MCP per trial row

**Status: future. Schema-impacting; touches every adapter + validator + UI.**

### The gap

Today the matrix's `mcp` column is a single string per row: `weather` | `news` | `library` | `fetch` | `NONE`. Each Trial gets exactly one MCP server bound at construction. Real agents typically connect to multiple MCP servers in one session — a research agent might use `fetch` (web) + `library` (papers) + `news` (recent events) simultaneously.

Limiting trials to one MCP server narrows the matrix's coverage in three ways:

1. **Cross-MCP tool-name collision behavior** untested — what if `weather.weather_get` and a hypothetical `news.weather_get` co-exist? cidgar's f1 hook may prefix or namespace; agents may pick either; verdicts may miscount.
2. **Channel-3 audit attribution under multi-server load** untested — when an agent fires `weather` then `fetch` in the same turn, AGW emits two backend-tagged audit entries; verdict_a's CID-presence check today is shape-agnostic about backend, but multi-server may surface latent assumptions.
3. **Realistic agent topologies** can't be exercised in the harness — biggest pedagogical gap.

### Schema change

`harness/api.py::RowConfig`:
```python
class RowConfig(BaseModel):
    framework: str
    api: str
    # ... other fields ...
    mcp: str | list[str]  # was: str. Accept either form.
                          # str = single-MCP (legacy); list = multi-MCP.
                          # "NONE" still means no MCP.
```

`harness/trials.py::TrialConfig` mirror.

`harness/validator.py`: extend the validator's MCP cell rule to accept either a string OR a list. UI multi-select cell editor sends the list form; legacy single-select rows continue working as strings.

Backwards-compatible: existing matrix rows with `mcp: "weather"` continue to deserialize cleanly into `mcp: str` and adapter code that handles both forms via `mcps = [m] if isinstance(m, str) else m` (with `[]` for `"NONE"`).

### Adapter change (~7 adapters)

Each adapter's `framework_bridge.py::Trial.__init__` (or `_setup_mcp_tools`) currently does:
```python
self.mcp_url = pick_mcp_base_url(config["routing"], config["mcp"])
# ... later: connect to self.mcp_url, load tools, bind to LLM
```

Becomes:
```python
mcps = config["mcp"] if isinstance(config["mcp"], list) else (
    [] if config["mcp"] == "NONE" else [config["mcp"]]
)
self.mcp_urls = [pick_mcp_base_url(config["routing"], m) for m in mcps]
# ... _setup_mcp_tools loops self.mcp_urls, merges tool lists per-framework
```

Per-framework tool merge:
- **langchain / langgraph** (langchain-mcp-adapters): call `MultiServerMCPClient(connections=...)` instead of a single `StreamableHttpConnection`. Native multi-server support exists since langchain-mcp-adapters 0.1.x.
- **pydantic-ai**: `Agent(toolsets=[MCPServerStreamableHTTP(url=u, http_client=...) for u in mcp_urls])` — toolsets list already supports multiple.
- **crewai / autogen / llamaindex**: each builds a per-MCP `FunctionTool` / `BaseTool` wrapping `fastmcp.Client`. Loop over `mcp_urls`, build all tools, concat the lists.
- **direct-mcp**: routing logic already keyword-matches user_msg → MCP; extend the matcher to multiple MCPs (weather words → weather, fetch words → fetch, etc.).

### Tool name collision handling

cidgar's f1 hook injects `_ib_cid` + `_ib_gar` schema fields per-tool, so multi-server tools don't clash at the schema level. Name collisions ARE possible (two servers exposing a `read_resource` tool, for example) — frameworks differ:

- **langchain-mcp-adapters MultiServerMCPClient** prefixes tool names with the server alias by default (`weather__weather_get`).
- **pydantic-ai** raises on duplicate tool names; user must alias.
- **crewai/autogen/llamaindex** raise on duplicate; manual aliasing needed.

For the harness, simplest is to prefix tool names with `<mcp_name>__` for ALL frameworks at adapter level — gives consistent naming + zero collisions across the 4 fastmcp servers (weather/news/library/fetch all have unique names today, but defense-in-depth).

### Frontend

`frontend/app.js`: Replace the MCP column's `agSelectCellEditor` (single-pick) with a multi-select editor. Two options:
- **AG-Grid's native `agRichSelectCellEditor` with `multiSelect: true`** (community edition supports this) — UX shows checkboxes
- **Custom comma-separated text editor** — `weather,fetch` → list. Less polished but trivial

Pick AG-Grid's native multi-select for first cut. Display in cell: `[weather, fetch]` (joined with comma). NONE special-cases to a single-pick "NONE" entry.

### Verdict implications

- (a) Presence: works per-turn, audit-entry-keyed. Multi-MCP fans out audit entries; verdict_a counts CID-bearing per-turn → unchanged. Pass condition unaffected.
- (b) Channel structure: walks bodies for CIDs. Channel 3 (MCP resource block) appears once per `tools/call` response — multi-server means more bodies to scan but same logic. Unchanged.
- (c) Continuity: counts unique CIDs across turns. Unchanged.
- (d) Resilience: same.
- (e) State-mode gap: same.
- (f) GAR richness: counts tool_calls with valid GAR. Multi-MCP increases tool_call count → more chances to validate. Unchanged.

So no efficacy code changes needed — the verdict logic is shape-agnostic about which MCP backend served each tool_call. Just more data flowing through.

### Tests

- 1 unit test per adapter: construct Trial with `mcp=["weather", "fetch"]`, assert tool list has tools from both MCP servers, no collisions.
- 1 integration test: validator accepts list form + roundtrips through PATCH /matrix/row/{id}.
- 1 end-to-end test (skip-marked unless ollama available): run a trial with `mcp=["weather", "fetch"]`, assert turn 0 invokes a weather tool and turn 1 invokes a fetch tool (via prompt routing).

### Effort

- Backend (RowConfig + validator): **S** (~30 LOC + 3 tests)
- Adapter changes (7 adapters): **M** (~50 LOC each, framework-specific tool merging, total ~350 LOC + 7 tests)
- Frontend multi-select: **S-M** (depends on AG-Grid version's `multiSelect` support; community edition may need a custom editor)
- E2E smoke (post-build): **S** (~1hr)

Total: **M-L** (~half-day to day).

### Why now (or why later)

**Argue for now**: opens a major coverage axis the harness currently can't exercise. Real agents are almost always multi-MCP.

**Argue for defer**: schema change cascades into every adapter + UI; risk of regression on single-MCP path. No urgency — current single-MCP coverage is enough for cidgar Plan B/E5 verification work.

**My pick**: defer until after E18 (header-demux) ships and is exercised. Multi-MCP would multiply audit volume + complicate any concurrent-trial scenarios; cleaner header-demux first, then add the multi-MCP axis on top of working concurrency.

### Cross-references

- Builds on direct-mcp's existing keyword-routing logic (already multi-aware in spirit, just hardcoded to one server)
- E18 is independent but synergistic — multi-MCP trials run concurrently put more load on audit demux
- E6 (Responses-API governance) is unaffected — multi-MCP is an MCP-side concern; cidgar's MCP path (f1/f4/f5) already works regardless of LLM API style

---

## Cross-references

- E1 builds on the per-turn header-injection pattern already in every adapter (mutable dict in `httpx.AsyncClient`).
- E2 + E3 are complementary — together they form a "richer/cleaner channels" track. A `with_alt_markers` row toggle would let the harness diff old-vs-new on the same row pair.
- E4 is the UI surface that makes E2/E3 marker-swap experiments observable — and E1's OTel spans could themselves become a diff target (governed span tree should carry `aiplay.cid`, baseline's shouldn't).
- E5 closes per-adapter gaps between library capability and aiplay adapter implementation; amplifies the signal of every other E-item by expanding the testable surface.
- **E6 (Responses shape handler)** is a strict prerequisite for any meaningful verdict on `api=responses` / `api=responses+conv` trials. Today those trials are cidgar-invisible on the LLM path (Channel 3 MCP-only).
- **E7 (per-route mcp_marker_kind)** lets operators work around agents that flatten tool_result content — unblocks Channel-3 reaching the model for frameworks that don't preserve resource blocks.
- **E8 (streaming Ch1+Ch2)** unblocks the `stream=True` axis — currently silently no-op'd.
- All of E1/E2/E3/E4/E5/E6/E7/E8 would produce new verdicts (g, b2, f2, h, j-streaming-integrity, k-ch3-text-path) or strengthen existing ones.
