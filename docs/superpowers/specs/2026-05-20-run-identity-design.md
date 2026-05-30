---
status: proposed
drafted: 2026-05-15
resumed: 2026-05-20
shipped_at: null
phase_dep: cross-phase
companion_docs:
  - features/2026-04-19-governance-cidgar/spec.md
  - features/2026-05-15-cidgar-config-cleanup/design.md
---

# Run identity (RID) infrastructure for cidgar

**Date:** 2026-05-15 (drafted) / 2026-05-20 (resumed)
**Status:** **PROPOSED** — Design A (`features/2026-05-15-cidgar-config-cleanup/`) shipped 2026-05-18 on `ibfork/feat/cidgar` tip `258a9430`; preconditions verified 2026-05-20; ready for writing-plans.
**Scope:** AGW (`crates/agentgateway/src/governance/`) + aiplay (`agw/config.yaml` + new verdicts).
**Goal:** Introduce a second governance correlator — `_ib_rid` (LLM-run identifier) — that sits below CID in the conversation hierarchy, enabling per-run audit lineage and turn-boundary detection for the audit / forensic replay use case.
**Prerequisite (satisfied):** Design A (CHG-25A–G) landed all referenced names: `ibc_<12 hex>` CID value shape, `channels.{schema_cid,text_marker_cid,resource_block_cid,snapshot_correlation}` toggles, `governance.{id_algorithm,hash_algorithm}` top-level fields, `gar.mode: required|optional|none` ternary (default `none`), `governance/validate.rs` config-load warnings framework. Empirical evidence from Design A smoke confirms C2 marker propagation works end-to-end (see Carrier strategy section).

## Canonical doc location

This file lives in aiplay as the brainstorm artifact. Now that Design A has landed (2026-05-18), this design's canonical home becomes a feature folder on the **`ibfork/docs`** branch:

```
features/2026-05-20-run-identity/
├── README.md           # status frontmatter, scope summary, file index
├── design.md           # this content, transposed
├── plan.md             # implementation worklist (from writing-plans skill)
├── brainstorming.md    # decisions log from the 2026-05-13 → 2026-05-15 session
├── conversation-log.md # exchange-by-exchange record
└── memory-log.md       # deferred follow-ups
```

Companion AGW spec edits (post-resumption) land in `features/2026-04-19-governance-cidgar/spec.md`: new §3.3 (`_ib_rid`), §4.6 (RID lifecycle), §5.x extensions for f1/f3/f4/f5 RID handling, §9.x new edge cases (conv-mode parent_rid gap, multi-source disagreement, anomaly behavior), §10 audit-phase additions, §14.x new uniform-opt-in entry for RID toggles, §15 future-considerations entry for `X-IB-RID` header passthrough.

## Why this was parked (historical)

This design sat on top of Design A's clean foundation. Implementing it before Design A landed would have required either:

1. Mixing the breaking-change cleanups into RID's PR (muddies scope, harder review), OR
2. Writing extractor shims in this design that would have immediately gone away once Design A landed (wasted work).

Cleaner sequencing chosen: Design A landed first (2026-05-18) → Design B resumed against the new uniform config surface → Design B written/shipped (in progress).

## Use case driver

**Audit / forensic replay** (selected from a 4-way option set during brainstorming).

Per-turn and per-run granularity in the cidgar audit log so operators can reconstruct "which run within which turn within which conversation" for incident review. Drives audit schema additions; carrier injection is required so the chain reconstructs from wire-observable state at any point in the conversation.

Out of scope (other candidate use cases that were considered):
- OTel span hierarchy for AGW (separate feature; would build on top of this once landed)
- Aiplay verdicts beyond basic lineage-integrity (separate enhancement; verdict_l "run lineage integrity" might be a Design C)
- Per-turn billing / rate-limiting (production AGW feature; would consume this design's audit fields)

## Hierarchy + lineage model (agreed during brainstorm)

```
Conversation       — identified by CID  (ibc_<12hex>, AGW-minted, stable per conversation)
 ├─ Turn 1
 │   ├─ LLM Run    — identified by RID  (ibr_<12hex>, AGW-minted, one per LLM call)
 │   │   ├─ tool_call audit  ← parent_run_rid = the issuing run's RID
 │   │   └─ tool_response audit
 │   ├─ LLM Run    — parent_rid points to prior run
 │   └─ LLM Run    — terminal (last run of turn)
 ├─ Turn 2
 │   ├─ LLM Run    — parent_rid = Turn 1's terminal run RID
 │   └─ ...
```

**Key observations agreed during brainstorm:**

- The `parent_rid` chain is **unbroken** across the entire conversation. Only Run 0 of Turn 1 (the conversation's first LLM call) has `parent_rid = None`. Every other run — including new-turn-boundary runs — has a parent pointing backward.
- "Turn boundary" is NOT `parent_rid == None`. It is a separate per-call derived boolean: does the current request body contain a NEW user-role message after the parent's terminal output?
- Therefore the audit needs **two distinct per-call fields**: `parent_rid` (chain pointer) and `is_turn_boundary` (boolean derived from body shape).
- A turn id (TID) is derivable from `is_turn_boundary` ancestor walk; not materialized as a third stored field in v1.

## Locked decisions (from brainstorm)

| Decision | Value |
|---|---|
| RID value shape | `ibr_<12 hex>` |
| Field name | `_ib_rid` (parallel to `_ib_cid`) |
| Algorithm | shared `governance.id_algorithm` (set by Design A) — same UUIDv4 → 12 hex truncation as CID |
| Generation site | f2 (before AGW forwards LLM request) |
| Carrier strategy | combined-carrier across CID's existing 3 channels (NOT parallel channels) |
| Marker grammar | `<!-- ib:cid=ibc_xxx,rid=ibr_yyy -->` (key=value pairs, order-independent) |
| Channel toggles | 3 new payload toggles + `mcp_marker_kind` shared with CID |
| Provider native ids | recorded as `provider_response_id` sibling audit field on `LlmResponse` phase; NOT injected into any carrier |
| Header passthrough | NOT added for RID in v1 (no current consumer); deferred to future considerations |

## Carrier strategy (combined payload)

Three carriers, each can independently carry CID and/or RID. Six payload toggles + one shared shape selector (all defined post-Design A):

```
C1 (inputSchema + tool_use.input override)
   payload fields:  _ib_cid  (gated by schema_cid)
                    _ib_rid  (gated by schema_rid — NEW)
                    _ib_gar  (gated by gar.mode)
                    _ib_ss   (gated by snapshot_correlation)

C2 (text marker on terminal LLM responses)
   <!-- ib:[kvpairs] -->
   payload fields:  cid  (gated by text_marker_cid)
                    rid  (gated by text_marker_rid — NEW)

C3 (emission on MCP tools/call response)
   shape: mcp_marker_kind ∈ {resource | text | both}
   payload (regardless of shape):
                    cid  (gated by resource_block_cid)
                    rid  (gated by resource_block_rid — NEW)
```

Each emission site builds the payload from currently-enabled fields. Empty payload → no emission.

**Empirical evidence from Design A smoke (2026-05-20):** Channel 2 propagation verified end-to-end on a 3-turn `mcp=NONE` chat trial (`c3a6d3be-d9af-4579-b309-86a77255b442`). With no tool calls, only C2 carried CID: AGW's f3 PATH B injected `<!-- ib:cid=ibc_249539c0a2f0 -->` into terminal response content; agent (langchain) stored the marker verbatim in conversation history; AGW's f2 on subsequent turns scanned `messages[*].content`, found the marker, resolved the same CID. Verdicts (a/b/c) all passed. The same C2 carrier mechanism applies to RID with identical reliability.

## Per-API behavior — what AGW does at each f-hook

### f1 (`on_tools_list_resp`)

No change from Design A for CID. RID addition:
- If `schema_rid: true`: inject `_ib_rid` into each tool's `inputSchema.properties` as an optional string field (no `enum` constraint — values aren't predictable at f1 time). Add to `required` array? **No** — the LLM doesn't need to populate it (AGW overwrites at f3 PATH A). Optional in schema; field exists so LLM is informed, but no enforcement.

### f2 (`on_llm_request`) — RID resolution + minting

Resolve `parent_rid` via priority chain (extended from current CID-resolution chain):

```
1. previous_response_id field          (Responses + state=T chain mode)
2. _ib_rid in latest tool_use.input    (C1 carrier replayed in body)
3. _ib_rid in latest C3 resource block / text content block in tool_result
                                       (C3 carrier replayed)
4. _ib_rid in latest C2 text marker    (replayed terminal assistant content)
5. null                                (no signal recoverable)
```

Multi-source: scan body for ALL occurrences; if multiple values disagree, record all in audit + set anomaly flag.

Mint `current_rid` via `governance.id_algorithm` (UUIDv4 → 12 hex; prefix `ibr_`). Always at f2 — even if a provider's native `response.id` exists, AGW uses its own minted value for uniform shape across the audit.

Derive `is_turn_boundary` from body shape:
- **Stateless APIs** (chat completions, Anthropic messages, stateless Responses): walk `messages` / `input` array. If latest `user`-role item's index is AFTER latest `assistant`-role item's index → `is_turn_boundary: true`. Otherwise `false` (continuation of an existing turn).
- **Stateful Responses + state=T**: inspect `input` items. Contains a `user` role item → `is_turn_boundary: true`. Contains only `tool_outputs` → `is_turn_boundary: false`.
- **Responses + Conversations API**: same input-shape rule as state=T. The `conversation` parameter doesn't affect boundary detection. Note: `parent_rid` for new-turn-boundary runs in conv mode is unreachable (see Acknowledged Gaps).

### f3 (`on_llm_response`) — RID injection into C1 / C2

**PATH A (response contains tool_use blocks):**
- If `schema_rid: true`: for each tool_use, set `tool_use.input._ib_rid = current_rid` (mirrors the existing `_ib_cid` overwrite logic).

**PATH B (terminal text response):**
- If any C2 payload toggle is on (`text_marker_cid` or `text_marker_rid`): build payload from enabled fields, append text content block `<!-- ib:[payload] -->`.
- Payload format: `key=value` pairs joined by `,`. Single-field emission still uses key=value form: `<!-- ib:cid=ibc_xxx -->` or `<!-- ib:rid=ibr_yyy -->`.
- Empty payload (all C2 toggles off) → no marker emission.

Audit's `LlmResponse` phase gets: `rid`, `provider_response_id` (from the response's native `id` field if present), and `is_turn_boundary` flag for THIS run's request (carried forward from f2 context).

### f4 (`on_tool_call_req`) — extract RID for audit join

- `pop_ib_rid_from_value(args)` — new helper in `value_ops.rs`, parallel to existing `pop_cid_from_value` / `pop_gar_from_value` / `pop_ib_ss_from_value`.
- The popped `_ib_rid` value becomes the `parent_run_rid` field on the `ToolCall` audit phase — identifies which LLM run issued this tool call.
- Strip `_ib_rid` from args before forwarding to MCP (same pattern as `_ib_cid` / `_ib_gar` / `_ib_ss`).

### f5 (`on_tool_call_resp`) — RID injection into C3

- Originating LLM run's RID is available from the GovContext (set at f4 via the `_ib_rid` extraction).
- If any C3 payload toggle is on (`resource_block_cid` or `resource_block_rid`): build combined payload, emit per `mcp_marker_kind` (resource block, text block, or both).
- Resource block text payload: JSON of `{cid?, rid?}` based on which toggles are on.
- Empty payload (all C3 toggles off) → no emission.

## Audit shape extensions

```rust
// LlmRequest phase — additions
{
  phase: "llm_request",
  // existing: cid, backend, trace_id, uctx, sctx, etc.
  rid: "ibr_a1b2c3d4e5f6",                   // NEW. Current call's RID.
  parent_rid: "ibr_xyz1234abcde" | null,     // NEW. Resolved at f2 from chain.
  is_turn_boundary: bool,                    // NEW. Derived from body shape.
  parent_rid_sources: ["c1", "c2"] | null,   // NEW. Which carriers contributed.
                                             // Null if parent_rid is null.
  parent_rid_anomaly: bool,                  // NEW. True iff multiple carriers
                                             // disagreed on parent_rid value.
}

// LlmResponse phase — additions
{
  phase: "llm_response",
  // existing: cid, backend, etc.
  rid: "ibr_a1b2c3d4e5f6",                   // NEW. Same value as the LlmRequest's rid.
  provider_response_id: "chatcmpl-xxx" | null, // NEW. Provider's native id, if present.
}

// ToolCall phase — additions
{
  phase: "tool_call",
  // existing: cid, tool, args, gar, etc.
  parent_run_rid: "ibr_a1b2c3d4e5f6",        // NEW. RID of the LLM call that issued this.
                                             // From f4 extraction of _ib_rid in args.
}

// ToolResponse phase — additions
{
  phase: "tool_response",
  // existing: cid, tool, is_error
  parent_run_rid: "ibr_a1b2c3d4e5f6",        // NEW. Same as ToolCall's value (same transaction).
}
```

All new fields skipped on serialize when None for backwards-compat with audit consumers from before this design.

## Channel toggle additions (atop Design A's renamed surface)

```yaml
channels:
  # (Design A established: mcp_marker_kind, schema_cid, text_marker_cid, resource_block_cid,
  #  snapshot_correlation)

  schema_rid:            false      # NEW. C1: inputSchema injection + tool_use.input
                                    # override carry _ib_rid. Default false (CHG-247).
  text_marker_rid:       false      # NEW. C2 carries rid. Default false (CHG-247).
  resource_block_rid:    false      # NEW. C3 carries rid. Default false (CHG-247).
```

Six payload toggles total (3 carriers × 2 correlators), all independently controllable per route. `mcp_marker_kind` continues to control C3 wire format for whatever payload it carries.

## Marker grammar evolution (`MARKER_RE`)

```
Current grammar (after Design A's CID rename):
   <!-- ib:cid=ibc_[0-9a-f]{12} -->

New combined grammar:
   <!-- ib:<kvpairs> -->
   where <kvpairs> = <key>=<value>(,<key>=<value>)*
   keys: cid | rid (closed set, spec'd; future correlators extend)
   values: prefix-specific shape (ibc_<12hex> for cid; ibr_<12hex> for rid)
```

Parser: ~10 LOC in Rust replacing the current single-capture regex with a key=value pair walker. Order-independent. Unknown keys logged at debug and skipped (forward-compat for future correlator additions).

**Operational lesson from Design A:** when `MARKER_RE` changes, BOTH (a) the harness daemon process must restart (Python compiles the regex once at module import; no hot-reload) AND (b) any cached verdict blocks in `data/matrix.json` must be invalidated — otherwise stale verdict output appears against fresh trial inputs. The Design A smoke initially showed a false `verdict (b) fail` for this exact reason; restarting the harness + flushing matrix.json produced clean passes. Tracked as a small aiplay DX follow-up (auto-invalidate cached verdicts on regex change, or add a "re-evaluate" affordance). Not load-bearing on Design B's architecture.

## Validation warnings (atop Design A's validation)

| Pattern | Warning |
|---|---|
| `schema_rid: true` AND `text_marker_rid: false` AND `resource_block_rid: false` | "RID injected at C1 but cannot propagate to subsequent LLM requests. `parent_rid` chain will break at next LLM call. Enable `text_marker_rid` and/or `resource_block_rid`." |
| (`text_marker_rid: true` OR `resource_block_rid: true`) AND `schema_rid: false` | "RID propagated across LLM hops but not injected into MCP tool args. `tool_call` audits will lack `parent_run_rid` association. Enable `schema_rid` for full coverage." |
| (Any RID toggle on) AND (all of `schema_cid`, `text_marker_cid`, `resource_block_cid` are false) | "RID enabled without any CID propagation channel. RIDs lose their global uniqueness anchor (always interpreted within a `(cid, rid)` pair). Enable at least one CID channel." |

Same throttling pattern as Design A's `governance/validate.rs` (extends the module shipped under CHG-25G; commits `5cb06855` + `1cf4ed28` test-isolation fix).

## Acknowledged gaps (documented limitations)

### Responses + Conversations API — new-turn-boundary `parent_rid` unreachable

When an agent uses `conversation: {id: "conv_xxx"}` (NOT `previous_response_id`) and the call is the start of a new turn (`input: [new_user_msg]`):

- `previous_response_id` field absent
- No `_ib_rid` in the body (new user message; no replayed assistant content; no tool_outputs path)
- `parent_rid = null` for this call — but the TRUE parent (prior turn's terminal run RID) lives in OpenAI's conversation server state, unreachable from AGW's wire vantage

Mitigation: AGW records `conversation_id` as a sibling audit field. Downstream consumers reconstruct chain order by time-ordering audits within the same `conversation_id`. Probabilistic for serial conversations, breaks down under any concurrent activity in one conversation (but conv-mode concurrency is itself unusual).

Documented as known limitation in spec; not addressable by any channel scheme. Future work (out of this design): if conv-mode coverage matters, AGW could add a per-(conversation_id) "last response id seen" map at significant statelessness cost (anti-pattern per §8).

### Truncated agent history

Same failure mode as CID (§9.5). If an agent drops all replayed assistant content + tool results, no `_ib_rid` survives anywhere in the body. AGW emits `parent_rid = null` and a `parent_rid_sources: null` annotation. The chain reconstructs from that point forward but lineage to the conversation's true history is lost.

### Multi-source disagreement

When multiple carriers carry conflicting `_ib_rid` values (theoretically possible if an agent splices in a non-AGW-sourced marker), AGW records all observed values, picks the most-recent-by-position, and sets `parent_rid_anomaly: true` for downstream investigation. Free signal; no behavior change.

### Concurrent trials on one AGW instance

Same time-window correlation limitation as CID today. Header-demux is the future fix (spec §15.5 + aiplay E18). Not addressed by this design.

## Aiplay companion changes

After AGW Design B lands:

- `agw/config.yaml` — every governance route opts in:
  ```yaml
  channels:
    schema_rid:          true
    text_marker_rid:     true
    resource_block_rid:  true
  ```

- New verdict candidates (separate enhancement, possibly Design C):
  - **verdict_l "run lineage integrity"**: per turn, walk `parent_rid` chain; assert no breaks within a turn (every run except the turn-boundary run has a parent in the same conversation).
  - **verdict_m "turn boundary correctness"**: assert `is_turn_boundary: true` rate matches expected turn count from the turn plan.
  - **verdict_n "cross-API run continuity"**: for combo adapter trials, assert `parent_rid` chain survives provider switches (this is the test for the Channel 2/3 marker resilience design).

- Frontend: extend the existing CID flow tabs to optionally render run lineage as a sub-graph. Out of scope for this design's PR; track as separate frontend enhancement.

## Spec language additions

- **§3.3 (FILL existing placeholder)** — Design A reserved §3.3 with a "Reserved for `_ib_rid`" placeholder (post-CHG-25 spec update). Design B FILLS the reserved section with full semantics, value shape, lifecycle, field-name conventions (parallel to §3.1 CID, §3.2 GAR).
- **§4.6 (new)** — RID lifecycle (parallel to §4): Birth at f2, Persistence Channels with toggles, Extraction priority at f2.
- **§5.x (extensions)** — each flow spec section updated with RID-specific behavior at f1/f3/f4/f5.
- **§7.x (extensions)** — token budget table extended with RID overhead estimates.
- **§9.x (new edge cases)** — conv-mode parent_rid gap, multi-source disagreement, parent_rid_anomaly behavior.
- **§10 (audit schema)** — phase field additions documented.
- **§14.x (new)** — RID feature-flag entries mirroring CHG-247 pattern for the three new toggles.
- **§15 (future considerations)** — `X-IB-RID` header passthrough as the future analogue of §15.5.

## Out of scope (deferred beyond Design B)

- OTel span integration — Design B emits enough audit shape to feed into OTel; the integration glue is a separate piece.
- Aiplay frontend lineage visualization — separate enhancement after this design's audit fields land.
- New verdicts (l, m, n) — separate aiplay enhancement(s); design depends on this audit shape.
- `X-IB-RID` header passthrough — deferred until an instrumented-agent consumer surfaces.
- Per-conversation RID chain reconstruction in conv mode — would require statefulness; anti-pattern.

## Cross-references

- **Prerequisite design (SHIPPED 2026-05-18):** Canonical at `features/2026-05-15-cidgar-config-cleanup/design.md` on `ibfork/docs` tip `fce33690`. Aiplay-side brainstorm artifact at `docs/superpowers/specs/2026-05-15-cidgar-config-cleanup-design.md`. AGW source at `ibfork/feat/cidgar` tip `258a9430`.
- **AGW cidgar spec (current):** `features/2026-04-19-governance-cidgar/spec.md` on `ibfork/docs`. Pre-implementation tip was `8b06e2e2` (post-CHG-25); post-Design-B tip is `1f2f4905` (after the §3.3 FILL + §4.6 + §5.x extensions + §9.16-18 + §10.2 + §14.6 + §15.6 additions for run identity). Sections added at implementation time: §3.3 (FILL the reserved placeholder for `_ib_rid`), §4.6 (RID lifecycle), §5.x extensions per f-hook, §9.x edge cases (conv-mode gap, multi-source disagreement, truncated history), §10.2 audit-phase additions, §14.6 uniform-opt-in entry, §15.6 future-considerations entry for header passthrough.
- **Change-ledger entries (provisional, finalized at resumption):** CHG-26A (`_ib_rid` value shape + `ibr_<12hex>` minting), CHG-26B (3 new RID toggles: `schema_rid`, `text_marker_rid`, `resource_block_rid`), CHG-26C (combined-carrier marker grammar in `MARKER_RE`), CHG-26D (audit phase extensions: `rid`, `parent_rid`, `is_turn_boundary`, `parent_run_rid`, `provider_response_id`, `parent_rid_sources`, `parent_rid_anomaly`), CHG-26E (validation warnings for asymmetric RID toggle states).
- **Per-feature inventory:** `per-feature-reimplementation-inventory.md` at `ibfork/docs` root — extend the `governance-cidgar` section with RID infrastructure recipe at resumption time.
- **Brainstorm conversation:** session of 2026-05-13 through 2026-05-15 in aiplay's `docs/conversation-log.md`.
- **Naming decision history:** value shape evolved through `ib_rid_` → `r_` → `ibr_`; CID rename evolved through `ib_` → `ic_` → `ibc_` (driven by IB brand preservation argument).
- **Combined-carrier vs parallel-carrier decision:** combined carrier chosen over Option B (full mirror with 6 independent channels) for token efficiency and operator UX simplicity. Symmetric per-correlator toggles still preserved through the 6-toggle-on-3-carriers pattern.

## Branch / worktree mapping for resumption-time implementation

| Repo / worktree path | Branch | What lands here |
|---|---|---|
| `/home/nixusr/ws/agw-gh/.worktrees/cidgar` (or new sibling) | `ibfork/feat/cidgar` (or `ibfork/feat/cidgar-rid`) | Rust source: `governance/{config,cidgar,value_ops,log,validate}.rs` extensions for RID; combined-carrier `MARKER_RE` parser; audit phase additions |
| `/home/nixusr/ws/agw-gh/.worktrees/docs-v2` | `ibfork/docs` | New `features/2026-05-20-run-identity/{README,design,plan,brainstorming,conversation-log,memory-log}.md`; updates to `features/2026-04-19-governance-cidgar/spec.md` (FILL §3.3 reserved placeholder, add §4.6/§5.x/§9.x/§10/§14.x/§15 sections); new CHG-26A..E ledger rows; inventory sweep |
| `/home/nixusr/ws/aiplay` | `main` | New aiplay verdicts (l, m, n candidates); `agw/config.yaml` opt-ins for RID toggles; frontend extension to render run lineage in CID flow tabs (optional, separate enhancement); bump AGW image tag in `docker-compose.yaml` once the AGW build is published |

## Resumption verification (completed 2026-05-20)

Verified during Design B brainstorm-refresh after Design A landing:

1. ✅ Design A's field names landed: all 6 referenced names (`text_marker_cid`, `resource_block_cid`, `schema_cid`, `governance.id_algorithm`, `governance.hash_algorithm`, `gar.mode`) confirmed via the final code review of `ibfork/feat/cidgar` tip `258a9430`.
2. ✅ CID value shape `ibc_<12 hex>` live end-to-end: smoke trials `c3a6d3be` (chat, 1 distinct CID) and `fdc025fa` (tool, 1 distinct CID) all `ibc_<12hex>` shape; verdicts (a/b/c/f/i) pass.
3. ✅ `governance/validate.rs` framework in place (CHG-25G); zero warnings fire against aiplay's full opt-in config.
4. ✅ Per-API `is_turn_boundary` rules apply unchanged to current 8 aiplay adapters (langchain, langgraph, crewai, pydantic_ai, autogen, llamaindex, direct-mcp, combo) — all produce standard-shape OpenAI/Anthropic/Responses bodies that match the parked spec's wire-shape-based rules.
5. ✅ File rename + status flip + light edits: applied during this brainstorm-refresh (file moved from `2026-05-15-run-identity-design-PARKED.md` → `2026-05-20-run-identity-design.md`; frontmatter status `parked` → `proposed`).

---

## Post-execution follow-ups (added 2026-05-30)

After Phase-1 smoke against the deployed `feat/cidgar` build, two follow-up fixes landed beyond this spec's original CHG-26A..E scope:

- **CHG-26F — RID hook-boundary handoff** (`feat/cidgar` `c7338181`). The AGW proxy builds a fresh `GovContext` per hook leg (it does not share one across f2→f3 or f4→f5). CID survived only because of `LLMRequest.governance_cid`; RID had no parallel carrier, so `ctx.rid` (minted at f2) and `ctx.originating_run_rid` (popped at f4) were lost before f3/f5 used them — `llm_response` lacked `rid`, and f3 PATH A/B + f5 C3 RID injection silently no-oped. Fix: added `LLMRequest.governance_rid: Option<Rid>` (sibling of `governance_cid`); f4's `originating_run_rid` is captured into the f5 closure rather than relying on ctx survival.
- **CHG-26G — recency-based `parent_rid` resolution + same-position anomaly** (`feat/cidgar` `8ebf73f1`). The original §3.3 priority chain (`prev_resp_id > c1 > c3 > c2`, last-occurrence within class) could skip the immediate predecessor when it used a lower-trust carrier (smoke: run4→run2 skipping run3's only-C2 marker). And `parent_rid_anomaly = any candidate disagrees` fired on normal multi-turn replay (the body legitimately carries every prior run's `_ib_rid`). Fix: candidates gained a message-position element; resolution picks the globally most-recent-by-position; carrier-trust is consulted only as a same-position tiebreak; `parent_rid_anomaly` is set only when ≥2 distinct rids appear at the *winning* position.

This spec is left as the original Design B intent. The as-landed scope (A..G), canonical resolution algorithm, and audit-field semantics are documented in:
- AGW `change-ledger.md` (CHG-26A..G rows)
- AGW `features/2026-04-19-governance-cidgar/spec.md` (§3.3, §4.6.3 — recency + same-position anomaly)
- AGW `per-feature-reimplementation-inventory.md` (CHG-26F + CHG-26G recipes)

All three were synced on `ibfork/docs` in commit `3af1b118`.
