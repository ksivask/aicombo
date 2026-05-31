---
name: customer-conversation-view-design
description: Design B follow-up — a Conversation tab on trial.html that renders trials as a customer-value-oriented conversation tree (CID → turn → llm/tool) instead of the current pipeline-internals view. Tier 2 scope: topology + user/agent text preview + per-turn latency + per-CID & per-turn pass/fail badges + auto-generated elevator-pitch header + anomalies-rise + ⚙ governance-internals overlay toggle.
status: proposed
created: 2026-05-31
metadata:
  parent_work: Design B (run identity / RID) — CHG-26A..G shipped; this view is a follow-up that turns the RID infrastructure into customer-facing value
  scope: aiplay/frontend only
---

# Customer Conversation View — Design

## 1. Motivation

Today's `trial.html` flow visualisations (`CID flow`, `CID flow (interactive)`) are pipeline-internals views: CIDs, SS snapshots, and audit phases are first-class nodes. That is the right shape for AGW *developers* debugging the governance pipeline — but it's the wrong shape for the people the project is supposed to win over: enterprise/security buyers, SEs in demos, compliance/audit teams, and operators who want a quick "did anything go wrong" scan.

The customer story is conversational, not architectural:

> "Here is one conversation your agent had. Here is each turn. Here is what the user said, what the agent said, what tools the agent called and which LLM run requested each one. Here is whether governance held up. Here is what broke if it did."

The Run ID (RID) infrastructure landed in Design B (CHG-26A..G) makes this rendering possible *provably*: `parent_run_rid` lets us draw the tool_call → originating-LLM-run edge as fact rather than time-window inference, and `parent_rid` chains llm_requests across turns into a verifiable lineage. Without RID, the only way to associate a tool_call to "the LLM run that made it" was guessing by timestamp — which broke under cross-trial contamination (the 48cb74a5 case). The Conversation view is the first frontend surface that puts that infrastructure to work for the customer story.

## 2. Goals (v1, Tier 2)

1. New `Conversation` tab on `trial.html`, **default-active**, leftmost tab position. Existing tabs (`Turns`, `Turn Plan`, `Verdicts`, `Note`, `CID flow`, `CID flow (interactive)`, `Services`, `Raw JSON`) are untouched and stay in current order after `Conversation`.
2. Render the trial as a conversation tree:
   - **Root(s)**: CIDs (one root per distinct CID in the trial).
   - **Children of CID root**: turns, in `turn_idx` order, each with one-line user message + one-line agent response preview.
   - **Children of turn**: `llm_request` → `llm_response` pairs by `rid`, in audit-position order.
   - **Children of `llm_response`**: tool_calls (with their tool_responses) whose `parent_run_rid` resolves to that response's `rid`.
   - **Children of turn (not an llm_response)**: orphan tool_calls — those with missing or unresolvable `parent_run_rid`.
3. Per-turn and per-CID pass/fail badges using the **anomaly-scoped** rule (see §6).
4. **Findings panel** at top — flat list of every anomaly with anchor links to the affected node; auto-open when count > 0.
5. **Auto-generated one-line elevator-pitch header** with global pass/fail badge.
6. **⚙ Show governance internals** checkbox that reveals technical fields (full RIDs, `parent_rid_sources`, snapshot hashes, full `mcp-session-id`, etc.) under each existing node — *without* introducing new node types.
7. **Multi-CID rendering** — multiple roots, prominent banner.

## 3. Non-goals (deferred)

- **Per-turn token counts** (Tier 3) — requires adapter-specific response parsing.
- **Share-this-trial anchor links** (Tier 3) — DOM is anchor-friendly already, so adding later is small.
- **Per-turn verdict re-run** — badges are anomaly-scoped, not verdict-rerun. The Verdicts tab remains authoritative for global verdicts.
- **Replacing or merging existing tabs** — explicitly out of scope. All existing tabs stay.
- **New JS test framework** — v1 testing is integration-only via extended smoke (§9).
- **Backend changes** — Conversation view is read-only of existing trial JSON; no harness or AGW changes.

## 4. Architecture

| File | Change |
|---|---|
| `frontend/trial.html` | Add `<button class="trial-tab-btn active" data-tab="conversation">Conversation</button>` as the **first** tab; add `<div id="tab-conversation" class="tab-content active">` as the first tab-content. Move the existing `active` class from `turns`/`tab-turns` to `conversation`/`tab-conversation`. |
| `frontend/trial.js` | Add `renderConversationTab(trial)` and helpers (`buildConversationTree`, `renderConversationTree`, `renderCidRoot`, `renderTurnNode`, `renderLlmRunNode`, `renderToolNode`, `renderOrphanToolNode`, `generateElevatorPitch`, `detectTurnAnomalies`, `collectFindings`). Wire `tabContents.conversation` into the existing tab-rendering loop. All existing renderers untouched. |
| `frontend/trial.css` | New `.conv-*` rules (tree indentation guides, badges, expandable text, anomaly highlight). Reuse existing palette: pass-green, warn-orange, fail-red, anomaly-ring animation. |

The view is **read-only** of trial data. It does not mutate, does not refetch, and consumes the same `trial` object every other tab does. Local toggle state (`⚙ Show governance internals`, expanded `<details>`) lives on a small `window.convState` object (mirroring how `showRunLineage` works today for the run-lineage overlay).

## 5. Data model — `buildConversationTree(trial)`

Pure function: trial JSON in, tree object out. Renderers consume the tree; tests assert on the tree. The function makes one pass over `audit_entries` and one pass over `trial.turns[]`.

```js
ConversationTree = {
  header: {
    rowId, rowDescription,
    status,                          // trial.status: pass | fail | error | running
    globalBadge: "pass" | "warn" | "fail",
    elevatorPitch                    // string, see §8
  },
  multiCidAnomaly: boolean,
  findings: [Finding],               // flat list, see §6
  cids: [
    {
      cid: "ibc_xxx",
      classification: "preserved" | "single" | "audit-only",   // reused from renderCidFlowTab classification logic
      badge: "pass" | "warn" | "fail",
      turns: [
        {
          turnIdx,
          userText,                  // from trial.turn_plan.turns[i].content (fallback: first llm_request.uctx in window)
          agentText,                 // parsed from trial.turns[i].response (reuse existing Turns-tab parser)
          startedAt, finishedAt, latencyMs,
          badge: "pass" | "warn",
          anomalies: [Anomaly],
          llmRuns: [
            {
              rid, parentRid, parentRidAnomaly, isTurnBoundary,
              requestAuditIdx, responseAuditIdx,
              providerResponseId, parentRidSources,   // for ⚙ overlay
              toolCalls: [           // tool_call/tool_response pairs hanging off this run
                {
                  name, mcpSession, mcpSessionFull, parentRunRid,
                  ssConsumed, ssAuditIdx, ssHash,
                  callAuditIdx, responseAuditIdx,
                  latencyMs, status: "ok" | "error", errorPreview,
                  anomalies: [Anomaly]
                }
              ]
            }
          ],
          orphanToolCalls: [         // unresolved parent_run_rid
            { name, mcpSession, parentRunRid /* or null */, callAuditIdx, responseAuditIdx, anomalies: [Anomaly] }
          ]
        }
      ]
    }
  ]
}

Anomaly = { source, severity: "warn" | "fail", reason }
Finding = { anchor, title, reason, severity }
```

### 5.1 Build algorithm

1. Walk `trial.turns[]` — **authoritative** turn grouping (`turn_idx` + `started_at`/`finished_at`). For each turn:
   - `userText` = `trial.turn_plan.turns[i].content` (assumes `trial.turn_plan.turns` is index-aligned with `trial.turns` — the harness produces them in lockstep; if `trial.turn_plan.turns.length !== trial.turns.length`, raise anomaly `turn_plan_misaligned` on the trial and skip user-text extraction from the plan). If `content` is empty or absent, fall back to the first `llm_request.uctx` whose `body.timestamp` falls in this turn's `[started_at, finished_at]` window.
   - `agentText` = parsed from `trial.turns[i].response` using the existing adapter-aware parser already used by `renderTurnCard` (the Turns tab) — do NOT duplicate parser logic.
   - `latencyMs` = `finished_at - started_at` in milliseconds.
2. Partition `audit_entries` to turns by timestamp window from step 1. Entries with no in-window match are flagged as a top-level anomaly (`out_of_window_audit`) but rendered under the closest turn — they should be rare.
3. Within each turn-partition, partition by `body.cid`. The common case is one CID per turn; if a turn has audits from multiple CIDs, mark a turn-level anomaly (`mixed_cid_in_turn`) and render each CID's audits under their respective CID root.
4. Build a trial-level `rid → llmRun` index from the union of all `llm_request` and `llm_response` entries.
5. Within each `(turn, cid)` slice, in audit-position order:
   - Pair `llm_request` ↔ `llm_response` by `rid`. Each pair becomes one `llmRun` with both audit indices.
   - For each `tool_call`/`tool_response`: look up `body.parent_run_rid` in the index from step 4.
     - **Hit** (resolves to a trial-local `llm_request.rid`): attach this tool_call (and its matching tool_response) under that llmRun's `toolCalls`.
     - **Miss** (null/absent OR resolves to no in-trial run): push to the turn's `orphanToolCalls` with anomaly `orphan_tool_call`.
   - Pair `tool_call` with its `tool_response` by `mcp-session-id` (existing logic).
   - For each `ib_ss` snapshot audit observed in this slice, look up which `tool_call.snapshot_hash` consumed it (existing E20 correlation). Attach the ssAuditIdx + ssHash + ssConsumed to that tool_call. If a snapshot has no consumer in this trial, raise anomaly `snapshot_orphan` on the would-be consumer turn (or the trial root if none).
6. Compute `anomalies` per node from the conditions in §6; propagate up. Build `findings` as a flat list with stable anchor ids.
7. `multiCidAnomaly = cids.length > 1`.

### 5.2 `is_turn_boundary` usage

Not used for grouping (`trial.turns[]` is the source of truth — the framework knows what a turn is). It is used as a **consistency check**: if the first `llm_request` in a turn has `is_turn_boundary !== true`, raise anomaly `turn_boundary_mismatch` on the turn (verdict-(m) flavour). When the RID feature is off, `is_turn_boundary` is absent — the check no-ops (do not raise).

## 6. Anomaly semantics

### 6.1 Inventory

| Source | Anomaly attached to | Severity | Condition |
|---|---|---|---|
| `body.parent_rid_anomaly === true` | the `llm_request` node | warn | CHG-26G same-position carrier conflict |
| First `llm_request` of turn has `is_turn_boundary !== true` (and feature is on) | the turn | warn | Verdict-(m) flavor; AGW disagreed with framework on boundaries |
| `parent_run_rid` missing OR not found in trial-local `rid` index | the orphan `tool_call` | warn | Strict orphan rule |
| `snapshot_orphan` (SS generated, no tool_call consumed it) | the consuming `tool_call`/`tool_response` if present, else the turn | warn | Existing E20 logic |
| CID classification ≠ `preserved` (single-use or audit-only) | the CID root | warn | Existing CID classification |
| `multiCidAnomaly === true` (> 1 distinct CID) | trial header banner | fail | CID drift suspected |
| Out-of-window audit (timestamp doesn't fall in any turn) | trial-level | warn | Rare; data quality signal |
| Mixed CID within a single turn | the turn + each affected CID root | warn | A turn's audits should belong to one CID |
| `turn_plan_misaligned` (`trial.turn_plan.turns.length !== trial.turns.length`) | trial-level | warn | Plan/execution misalignment; user-text falls back to `uctx` |
| Trial's global verdict is `fail` | trial header badge | fail | Existing global verdict logic (Verdicts tab) — reflected, not recomputed |

### 6.2 Propagation

- Turn badge = `warn` if `anomalies.length > 0`; else `pass`.
- CID badge = `warn` if any turn warn OR classification ≠ `preserved`; else `pass`. Promoted to `fail` if the CID is `audit-only`.
- Trial header global badge = trial's global verdict outcome (existing logic). Not recomputed.

### 6.3 Findings & anomalies-rise rendering

Each anomaly generates a `Finding`:

```js
{ anchor: "#conv-t1-tool0",
  title: "Turn 1 · tool_call weather_lookup",
  reason: "orphan: no resolvable parent_run_rid (CHG-26F may be off / cross-trial drift)",
  severity: "warn" }
```

Rendering of "anomalies-rise":

1. **Trial header** carries the global outcome badge above the fold.
2. **Findings panel**: a `<section class="conv-findings">` near the top. When `findings.length === 0` the *entire section* is omitted from the rendered HTML (no empty panel). When > 0, the section renders a `<details open>` (auto-expanded on first paint, user can collapse) with a bulleted list, each `<li><a href="#conv-tN-XK">title</a> — reason</li>`. Single click → browser anchor-scrolls to the affected node; the target node gets a transient CSS ring animation (`:target { animation: ring 1.5s }`).
3. Affected DOM nodes get `class="conv-anomaly"` → orange-tinted background + left-border so problems are visually obvious even without using Findings.
4. **Multi-CID banner** sits *above* Findings — it's the most severe trial-level state and dominates.

## 7. DOM structure

Plain semantic HTML — no graph library, no virtual DOM. `<details>`/`<summary>` does click-to-expand for free; indentation comes from CSS `border-left` guides on nested `<ul>`s.

```html
<div id="tab-conversation" class="tab-content active">

  <header class="conv-header">
    <span class="conv-badge fail">✗ FAIL</span>
    <span class="conv-pitch">3-turn weather query via langchain/ollama · 2 distinct CIDs (drift) · 4 tool calls, 2 orphan</span>
    <label class="conv-toggle">
      <input type="checkbox" id="conv-gov-internals-cb"> ⚙ Show governance internals
    </label>
    <a class="conv-link" href="#" data-tab-target="cidflow-interactive">or open Operator: CID flow / Interactive →</a>
  </header>

  <section class="conv-multicid-banner" hidden>⚠ This trial spans 2 conversations — see verdict (a) / (i).</section>

  <section class="conv-findings" id="conv-findings">
    <details open><summary>Findings (3)</summary>
      <ul>
        <li><a href="#conv-t1-llm0">Turn 1 · llm_request</a> — parent_rid_anomaly (same-position conflict at idx 8)</li>
        <li><a href="#conv-t2-tool0">Turn 2 · tool_call weather_lookup</a> — orphan: no resolvable parent_run_rid</li>
        <li>…</li>
      </ul>
    </details>
  </section>

  <article class="conv-cid" data-cid="ibc_eddd…">
    <h2><span class="conv-badge warn">⚠</span> conversation <code>ibc_eddd…</code>
        <small>2 turns · CID stable ✓</small></h2>

    <section class="conv-turn" id="conv-t0">
      <header>
        <span class="conv-badge pass">✓</span>
        <span class="conv-turn-title">Turn 0</span>
        <small>1.2s</small>
      </header>
      <div class="conv-msg user">👤 User: <span class="conv-text">what's the weather in NYC<details><summary>…</summary>full text</details></span></div>
      <div class="conv-msg agent">🤖 Agent: <span class="conv-text">It's 72°F …<details><summary>…</summary>full text</details></span></div>
      <ul class="conv-llm-list">
        <li class="conv-llm" id="conv-t0-llm0">
          <span class="conv-phase">▸ llm_request</span>
          <span class="conv-rid">rid=ibr_5f7c…7a49</span>
          <span class="conv-parent">parent: —</span>
          <ul class="conv-gov-internals" hidden>
            <li>parent_rid_sources: [c1] · is_turn_boundary: true · provider_response_id: —</li>
          </ul>
        </li>
        <li class="conv-llm" id="conv-t0-llm1">
          <span class="conv-phase">▸ llm_response</span> rid=ibr_5f7c…7a49
          <ul class="conv-tools">
            <li class="conv-tool" id="conv-t0-tool0">
              <span class="conv-badge pass">✓</span> tool_call <code>weather_lookup</code> · mcp-ss <code>c967c</code> · ss consumed
              <ul><li>tool_response · mcp-ss c967c · 180ms · ok</li></ul>
            </li>
          </ul>
        </li>
      </ul>
    </section>

    <section class="conv-turn" id="conv-t1">…</section>
  </article>

  <article class="conv-cid" data-cid="ibc_1766…">…</article>   <!-- multi-CID case -->
</div>
```

Notes:
- All anchor ids follow the stable pattern `#conv-t{turnIdx}-{llm|tool}{K}` so findings links never break across trials.
- `.conv-gov-internals` blocks are toggled by the ⚙ checkbox via a single CSS rule (`#conv-gov-internals-cb:checked ~ * .conv-gov-internals { display: block }`).
- The `or open Operator: CID flow / Interactive →` link is a small affordance — clicking it switches the active tab using the existing tab-button click handler (do not re-implement).

## 8. Elevator-pitch header — `generateElevatorPitch(trial)`

Pure function. Inputs: `trial.config` (row id, optional description), `trial.turns.length`, distinct CIDs across `audit_entries`, tool counts (total + orphan), trial's global verdict pass/fail, `findings.length`.

Output `{ icon, badge: "pass" | "warn" | "fail", line: string }`.

### 8.1 Templates

| Outcome | Line shape |
|---|---|
| PASS | `✓ PASS · {N}-turn {row label} · {cid summary} · {tool summary}` |
| WARN | `⚠ {findings count} of {N} turns flagged · {N}-turn {row label} · {cid summary} · {anomaly summary}` |
| FAIL | `✗ FAIL · {N}-turn {row label} · {cid summary} · {failure highlights}` |

### 8.2 Building blocks (each safe-defaults if data missing)

- **Row label**: `trial.config.description` if present, else `trial.config.row_id`, else `(unnamed row)`.
- **CID summary**: `"1 CID stable across all turns"` if 1 CID + classification = preserved; `"{M} distinct CIDs (drift)"` if M > 1; `"1 CID — single-use"` / `"1 CID — audit-only"` otherwise.
- **Tool summary**: `"{K} tool calls, all traced to LLM run"` if all tool_calls have resolvable `parent_run_rid`; `"{K} tool calls, {X} orphan"` otherwise. Omit if K = 0.
- **Anomaly / failure summary**: short list of the highest-severity finding kinds (`orphan tool_call`, `cross-trial drift`, `same-position rid conflict`), max 2 mentioned in the line; rest are visible in the Findings panel.
- **Length cap**: if the assembled line exceeds 160 chars, drop the tool summary first, then the CID summary's qualifier. Never truncate the outcome icon or the row label.

### 8.3 Examples

- `✓ PASS · 3-turn weather query via langchain/ollama · 1 CID stable across all turns · 2 tool calls, all traced to LLM run`
- `⚠ 1 of 3 turns flagged · 3-turn weather query via langchain/ollama · 1 CID · 1 orphan tool_call (see Findings)`
- `✗ FAIL · 3-turn weather query via langchain/ollama · 2 distinct CIDs (drift) · 4 tool calls, 2 orphan`

## 9. Testing strategy

### 9.1 v1: integration-only via extended smoke (Recommended)

Extend `scripts/smoke_rid.sh` with an HTML-scrape pass after the trial finishes:

1. `curl` the rendered `trial.html?id=<trial_id>` page (or the Conversation tab's HTML fragment via a small new aiplay-api route, if we want to bypass JS execution).
2. Assert:
   - `Conversation` tab button exists and has the `active` class.
   - The tab content has exactly `len(trial.turns)` `.conv-turn` elements.
   - The first turn has at least one `.conv-msg.user` and one `.conv-msg.agent`.
   - The trial header badge matches the expected global verdict.
   - The Findings panel is present (open) iff anomalies > 0.
   - If trial has > 1 distinct CID: multi-CID banner is visible; else hidden.
3. **Golden anchor assertion**: on the canonical pass trial, assert `#conv-t0-llm0` and `#conv-t0-tool0` exist. On the orphan-tool fixture (built from a contaminated-trial JSON), assert the orphan render anchor is present and the Findings panel lists it.

No new test framework. Integration with the existing smoke gate.

### 9.2 v2 path (when needed)

If `buildConversationTree` or `generateElevatorPitch` grows past ~150 LoC each, or we see regressions caught only by the smoke, introduce **vitest** for granular JS unit tests:

- `buildConversationTree(trialFixture)` returns tree matching expected shape.
- `generateElevatorPitch(trial)` returns expected line for each of the three templates.
- `detectTurnAnomalies(turnPartition)` returns expected anomaly set for each condition in §6.1.

Fixtures: small handcrafted trial JSON files under `frontend/tests/fixtures/`. Reuse the verdict-test fixture patterns from `tests/test_efficacy.py`.

### 9.3 Visual / manual

After implementation, the user manually verifies:
- A clean pass trial renders the value story above the fold without scrolling.
- An anomaly trial draws attention to the failure first (banner + findings + anomaly-class styling).
- The ⚙ toggle reveals the technical layer without re-shuffling the topology.
- The default tab on a fresh `trial.html` load is Conversation; navigation to other tabs still works.

## 10. Risks & operating notes

- **Older trials without RID feature on** render with every tool_call as orphan (no `parent_run_rid` was stamped). This is *correct* behavior — the rendering reflects what the data says — but it will look "broken" until users learn that all-orphan = RID feature was off. Add a one-line in-tab callout when 100% of tool_calls are orphan: "This trial appears to predate Run ID injection. To trace tool calls back to their LLM run, enable the `schema_rid` / `text_marker_rid` / `resource_block_rid` toggles in `agw/config.yaml`."
- **Adapter-specific agent text parse** is the highest-touch surface and the most likely source of "the Agent line is blank" complaints. Reuse the existing Turns-tab parser exactly; do not duplicate adapter-specific paths in the Conversation renderer.
- **Cross-trial contamination** still surfaces — both via multi-CID banner (when CIDs differ) and via orphan tool_calls (when parent_run_rid points outside the trial). The Conversation view's value is precisely that these now LOOK wrong instead of being silently averaged into a green tree.
- **Long conversations**: a 20-turn trial fits in a single column with `<details>` collapsing each turn body — the turn header stays visible while content collapses. No virtualization in v1; revisit if a trial > 50 turns ever lands.
- **Wider RID coverage**: this design assumes the trial config has the RID toggles ON. Trials with partial RID coverage (e.g., schema_rid on but text_marker_rid off) will show some tool_calls as orphan even though they had a real association — flag this in the documentation but accept it: the orphan is correct given the toggle state.

## 11. Open items (for the plan)

- **Tab move mechanics**: confirm that moving the `active` class from `turns`/`tab-turns` to `conversation`/`tab-conversation` doesn't break any existing JS that assumes `tab-turns` is initialized first. Quick audit of `trial.js` initialization order during the plan task that touches `trial.html`.
- **Agent text parser hand-off**: trace exactly which function in `trial.js` (`renderTurnCard`?) extracts displayable response text today, factor it into a small exported helper if it isn't already, and reuse from `buildConversationTree`.
- **Snapshot orphan placement**: when `snapshot_orphan` fires and no consuming `tool_call` exists, the anomaly attaches to the turn. Confirm this matches operator intuition; revisit if it reads as too distant from the actual SS generation event.

## 12. Cross-references

- **Design B spec (the RID infrastructure this view leverages)**: `docs/superpowers/specs/2026-05-20-run-identity-design.md`
- **Design B plan**: `docs/superpowers/plans/2026-05-20-run-identity-plan.md`
- **AGW canonical cidgar spec**: `features/2026-04-19-governance-cidgar/spec.md` on `ibfork/docs` (CHG-26F + CHG-26G synced via `3af1b118`)
- **Brainstorm conversation**: aiplay `docs/conversation-log.md` entries from 2026-05-31
- **Provisional implementation tasks**: to be written by `superpowers:writing-plans` after spec approval
