# Customer Conversation View Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a new `Conversation` tab to `frontend/trial.html` that renders trials as a customer-value conversation tree (CID → turn → llm/tool) with anomaly callouts, replacing the current pipeline-internals first-impression of `trial.html` with a story view.

**Architecture:** Single-tab addition. No backend, no harness, no Python changes. All work is in `frontend/trial.html`, `frontend/trial.js`, `frontend/style.css`, plus an HTML-scrape extension to `scripts/smoke_rid.sh` as the v1 integration test. Render pipeline is pure JS: `trial.json → buildConversationTree(trial) → renderConversationTree(tree) → innerHTML`. Existing tabs untouched.

**Tech Stack:** Vanilla ES module JS (no bundler), vanilla CSS, vanilla HTML, semantic `<details>` for click-to-expand. Volume-mounted frontend (edits are live; hard browser refresh required).

**Spec:** `docs/superpowers/specs/2026-05-31-customer-conversation-view-design.md` (committed `738e213`).

---

## Testing model for v1

The spec (§9) chose **integration-only testing via extended `scripts/smoke_rid.sh`**. No JS test framework is introduced in v1. That means each task's verification step is one of:

1. **Static grep** against the file changed (e.g., "did we add the tab button?").
2. **Live browser load** against the running aiplay stack (`http://localhost:8000/trial.html?id=<trial_id>`) with **a hard refresh** (Ctrl-Shift-R) — the frontend is volume-mounted, but browsers cache aggressively.
3. **Smoke run** (`make smoke-rid` or `bash scripts/smoke_rid.sh`) once the smoke is extended (Task 9).

For the JS-side pure functions (`buildConversationTree`, `extractAgentText`, `generateElevatorPitch`), per-task verification is via temporary `console.log` invocations inside `renderConversationTab` while developing, removed before the task's commit. This is intentional: v2 introduces vitest if any function grows past ~150 LoC; until then, the smoke catches integration regressions.

---

## File structure

| File | Status | Responsibility |
|---|---|---|
| `frontend/trial.html` | Modify (lines 54-71) | One new tab button (`data-tab="conversation"`), one new tab-content `<div id="tab-conversation">`, default `active` class moves from `turns` → `conversation`. |
| `frontend/trial.js` | Modify (additions at end, plus tabContents map + renderTrial wire) | All new code grouped at the END of the file under a banner `// ── Customer Conversation View ──`. Public entry: `renderConversationTab(trial)`. Helpers: `buildConversationTree`, `extractAgentText`, `detectTurnAnomalies`, `generateElevatorPitch`, `renderConversationTree`, `renderCidRoot`, `renderTurnNode`, `renderLlmRunNode`, `renderToolNode`, `renderOrphanToolNode`, `_wireConvToggle`. New top-level state `let convShowGovInternals = false;` mirroring `showRunLineage` (line 48). |
| `frontend/style.css` | Modify (additions at end) | New `.conv-*` rules: tree indentation guides, badges (reuse `#28a745` / `#ffc107` / `#dc3545` palette), `<details>` reveal, `.conv-gov-internals { display: none }` toggled by `#conv-gov-internals-cb:checked`, `:target` ring animation. |
| `scripts/smoke_rid.sh` | Modify (extend at end) | Add HTML-scrape pass against `trial.html` (static structure) + manual checklist of JS-rendered assertions printed at end. |

**No new files.** Trial.js is already 2300+ lines but established as the single ES module for `trial.html`; splitting introduces import wiring that doesn't fit existing patterns. New code goes at the end under a clear banner.

---

## Working directory

All work is on **aiplay main** at `/home/nixusr/ws/aiplay/`. No worktree (matches Design A + B execution pattern). The `frontend/` directory is volume-mounted into the harness container, so JS/CSS/HTML edits are visible immediately on hard browser refresh — no rebuild step.

---

## Task overview

| # | Task | Files | Approx LoC |
|---|---|---|---|
| 1 | Add Conversation tab to trial.html + flip default-active | trial.html | +2 / ~2 changed |
| 2 | Wire trial.js: tabContents map entry + stub render + call from renderTrial | trial.js | +6 |
| 3 | extractAgentText(turn) helper | trial.js | +40 |
| 4 | buildConversationTree(trial) + helpers | trial.js | +180 |
| 5 | detectTurnAnomalies + finding propagation | trial.js | +90 |
| 6 | generateElevatorPitch(trial) | trial.js | +60 |
| 7 | renderConversationTree HTML emitter + sub-renderers | trial.js | +250 |
| 8 | CSS .conv-* rules + :target ring + gov-internals visibility | style.css | +130 |
| 9 | ⚙ Show governance internals toggle wiring | trial.js | +20 |
| 10 | Extend smoke_rid.sh with HTML-scrape assertions | scripts/smoke_rid.sh | +60 |
| 11 | End-to-end smoke + manual visual verification + cleanup | (verification only) | 0 |

---

## Task 1: Add Conversation tab to trial.html + flip default-active

**Files:**
- Modify: `frontend/trial.html` lines 55, 64

**Goal:** Add the new tab button as the FIRST tab and the matching tab-content `<div>` as the FIRST content. Move the `active` class from `turns` to `conversation` on both the button and the content div.

- [ ] **Step 1: Read the current tab block (verify lines 55-71)**

Run: `sed -n '54,72p' frontend/trial.html`

Expected: shows `<div class="trial-tabs">` containing 8 buttons starting with `data-tab="turns"` (with `active` class), followed by 8 `<div class="tab-content">` divs starting with `id="tab-turns"` (with `active` class).

- [ ] **Step 2: Add the Conversation button as first; remove `active` from Turns button**

Replace this block in `frontend/trial.html`:

```html
    <div class="trial-tabs">
      <button class="trial-tab-btn active" data-tab="turns">Turns</button>
      <button class="trial-tab-btn" data-tab="plan">Turn Plan</button>
```

with:

```html
    <div class="trial-tabs">
      <button class="trial-tab-btn active" data-tab="conversation">Conversation</button>
      <button class="trial-tab-btn" data-tab="turns">Turns</button>
      <button class="trial-tab-btn" data-tab="plan">Turn Plan</button>
```

- [ ] **Step 3: Add the Conversation tab-content as first; remove `active` from Turns tab-content**

Replace this block in `frontend/trial.html`:

```html
    <div id="tab-turns" class="tab-content active"></div>
    <div id="tab-plan" class="tab-content"></div>
```

with:

```html
    <div id="tab-conversation" class="tab-content active"></div>
    <div id="tab-turns" class="tab-content"></div>
    <div id="tab-plan" class="tab-content"></div>
```

- [ ] **Step 4: Verify the HTML is well-formed and the active class moved correctly**

Run: `grep -nE 'data-tab=|tab-content' frontend/trial.html | head -20`

Expected output shows 9 `data-tab=` lines (including `conversation` as the first with `active`) and 9 `tab-content` lines (including `tab-conversation` as the first with `active`). No two `active` classes on buttons; no two `active` classes on tab-content.

Run: `grep -c 'trial-tab-btn active' frontend/trial.html` — Expected: `1`
Run: `grep -c 'tab-content active' frontend/trial.html` — Expected: `1`

- [ ] **Step 5: Commit**

```bash
git add frontend/trial.html
git commit -m "feat(frontend): add Conversation tab to trial.html, default-active

The new Conversation tab is the leftmost tab and default-active on
page load. Existing 8 tabs (Turns, Turn Plan, Verdicts, Note, CID
flow, CID flow (interactive), Services, Raw JSON) untouched and
shifted one slot right. JS wiring lands in Task 2.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Task 2: Wire tabContents map + add stub renderConversationTab + call from renderTrial

**Files:**
- Modify: `frontend/trial.js` line ~200 (tabContents map), line ~598 (renderTrial), and append a new function at end-of-file.

**Goal:** Make the new tab functional with a placeholder, so the page loads without JS errors and the Conversation tab shows visible content. This is the smallest possible "tab works" landing before any real rendering logic.

- [ ] **Step 1: Add the `conversation` entry to the tabContents map**

In `frontend/trial.js`, find this block:

```js
const tabContents = {
  turns: document.getElementById("tab-turns"),
  plan: document.getElementById("tab-plan"),
```

Change to:

```js
const tabContents = {
  conversation: document.getElementById("tab-conversation"),
  turns: document.getElementById("tab-turns"),
  plan: document.getElementById("tab-plan"),
```

- [ ] **Step 2: Add the stub `renderConversationTab` function at the end of `trial.js`**

Append to `frontend/trial.js`:

```js

// ── Customer Conversation View ──
// Customer-value-oriented rendering of a trial: CID → turn → llm/tool tree
// with anomaly callouts. See docs/superpowers/specs/2026-05-31-customer-
// conversation-view-design.md for the full design.

function renderConversationTab(trial) {
  // Stub — replaced in Task 7 by the full tree emitter.
  return `<p style="padding:16px;color:#666;">Conversation view — under construction. Use other tabs for now.</p>`;
}
```

- [ ] **Step 3: Call `renderConversationTab` in `renderTrial`**

In `frontend/trial.js`, find this line inside `renderTrial`:

```js
  tabContents.turns.innerHTML = (trial.turns || []).map((t, i) => renderTurnCard(trial, t, i)).join("")
```

Add ONE line directly BEFORE it:

```js
  tabContents.conversation.innerHTML = renderConversationTab(trial);
  tabContents.turns.innerHTML = (trial.turns || []).map((t, i) => renderTurnCard(trial, t, i)).join("")
```

- [ ] **Step 4: Verify in the browser**

Hard-refresh (Ctrl-Shift-R) any trial page, e.g., `http://localhost:8000/trial.html?id=<any_existing_trial_id>`.

Expected:
- Conversation tab is leftmost and active by default; shows "Conversation view — under construction. Use other tabs for now."
- Clicking Turns / Verdicts / etc. still works; clicking back to Conversation returns to the stub.
- No console errors.

If there are no existing trial IDs handy, run `bash scripts/smoke_rid.sh` to mint one, then browse to it.

- [ ] **Step 5: Commit**

```bash
git add frontend/trial.js
git commit -m "feat(frontend): wire Conversation tab map + stub render

Adds tabContents.conversation map entry, a stub renderConversationTab
that emits a placeholder string, and wires the call into renderTrial.
The tab is now functional end-to-end with placeholder content. Real
rendering lands in Tasks 3-9.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Task 3: extractAgentText(turn) helper

**Files:**
- Modify: `frontend/trial.js` (append helper inside the `// ── Customer Conversation View ──` section).

**Goal:** Extract human-readable agent response text from `trial.turns[i].response.body` across the adapter shapes aiplay supports (OpenAI chat completions, Anthropic messages, Responses API, SSE stream concat). The spec's open-item §11 assumed there was a renderTurnCard helper to factor out — there isn't. `renderTurnCard` (line 511) dumps `resp.body` raw via `renderBody`. So we create the helper fresh.

The function is best-effort and lossless: if all shape probes fail, return `null`. The caller renders `🤖 Agent: (response body — see Turns tab)` as the fallback so missing extraction never blocks the view.

- [ ] **Step 1: Append `extractAgentText` to `frontend/trial.js`**

Inside the `// ── Customer Conversation View ──` section (after `renderConversationTab`), append:

```js

/**
 * Extract a human-readable agent response from one trial.turns[i].response.body.
 *
 * Adapter shapes probed, in order:
 *   1. OpenAI chat completions:  {choices: [{message: {content: "..."}}]}
 *   2. OpenAI streaming concat:  SSE lines "data: {...}" where each delta has
 *                                choices[0].delta.content; concatenate.
 *   3. Anthropic messages:       {content: [{type:"text", text: "..."}, ...]}
 *   4. Responses API:            {output: [{type:"message", content: [{type:"output_text",
 *                                text: "..."}]}, ...]}
 *   5. Raw string fallback:      if body is already a string and looks like prose
 *                                (no leading "{" or "data:"), return it trimmed.
 *
 * Returns null if no shape matches. Caller is expected to render a graceful
 * fallback (e.g., "(response body — see Turns tab)").
 *
 * The function never throws — every JSON.parse is try/catch-wrapped because
 * adapter response bodies in the wild are inconsistent (string vs object,
 * single SSE event vs concatenated stream).
 */
function extractAgentText(turn) {
  const resp = turn && turn.response;
  if (!resp) return null;
  let body = resp.body;
  if (body == null) return null;

  // If body is a string, try to parse as JSON first; if that fails, try SSE.
  let parsed = null;
  if (typeof body === "string") {
    const trimmed = body.trim();
    if (trimmed.startsWith("{") || trimmed.startsWith("[")) {
      try { parsed = JSON.parse(trimmed); } catch (_) { parsed = null; }
    }
    if (parsed == null && trimmed.startsWith("data:")) {
      // SSE stream concat — walk lines, parse each JSON after "data: ".
      const chunks = [];
      for (const line of trimmed.split(/\r?\n/)) {
        const m = line.match(/^data:\s*(.*)$/);
        if (!m || m[1] === "[DONE]") continue;
        let ev = null;
        try { ev = JSON.parse(m[1]); } catch (_) { continue; }
        const d = ev && ev.choices && ev.choices[0] && ev.choices[0].delta;
        if (d && typeof d.content === "string") chunks.push(d.content);
      }
      if (chunks.length) return chunks.join("").trim() || null;
    }
    if (parsed == null) {
      // Plain prose fallback.
      return trimmed.length ? trimmed : null;
    }
  } else if (typeof body === "object") {
    parsed = body;
  }
  if (parsed == null) return null;

  // OpenAI chat completions.
  const oai = parsed.choices && parsed.choices[0] && parsed.choices[0].message
    && parsed.choices[0].message.content;
  if (typeof oai === "string" && oai.length) return oai;

  // Anthropic messages.
  if (Array.isArray(parsed.content)) {
    const texts = parsed.content
      .filter(b => b && b.type === "text" && typeof b.text === "string")
      .map(b => b.text);
    if (texts.length) return texts.join("\n").trim() || null;
  }

  // Responses API.
  if (Array.isArray(parsed.output)) {
    const texts = [];
    for (const item of parsed.output) {
      if (!item || item.type !== "message" || !Array.isArray(item.content)) continue;
      for (const c of item.content) {
        if (c && c.type === "output_text" && typeof c.text === "string") texts.push(c.text);
      }
    }
    if (texts.length) return texts.join("\n").trim() || null;
  }

  return null;
}
```

- [ ] **Step 2: Verify via temporary console.log in renderConversationTab**

Temporarily change `renderConversationTab` to:

```js
function renderConversationTab(trial) {
  // TEMP: probe extractAgentText for each turn.
  const sample = (trial.turns || []).map((t, i) =>
    `<li>Turn ${i}: ${(extractAgentText(t) || "(no text)").slice(0, 120)}…</li>`
  ).join("");
  return `<p style="padding:16px;color:#666;">Probe:<ul>${sample}</ul></p>`;
}
```

Hard-refresh a trial page (use one from a recent smoke run — `bash scripts/smoke_rid.sh` if needed). Expected: the Conversation tab lists each turn with a snippet of the agent's actual response text (not the raw HTTP body shape). If a turn shows `(no text)`, check the response body manually via the Turns tab and add the missing shape probe.

- [ ] **Step 3: Revert renderConversationTab to the Task 2 stub**

Restore `renderConversationTab` to:

```js
function renderConversationTab(trial) {
  // Stub — replaced in Task 7 by the full tree emitter.
  return `<p style="padding:16px;color:#666;">Conversation view — under construction. Use other tabs for now.</p>`;
}
```

- [ ] **Step 4: Commit**

```bash
git add frontend/trial.js
git commit -m "feat(frontend): extractAgentText helper — adapter-aware response parsing

Best-effort extraction of agent response text from trial.turns[i].response.body
across OpenAI chat completions, OpenAI streaming SSE, Anthropic messages, and
Responses API shapes. Returns null on all-shapes-miss; caller renders a
graceful fallback. Probed live against the smoke trial; reverted the temporary
debug render before commit.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Task 4: buildConversationTree(trial) + helpers

**Files:**
- Modify: `frontend/trial.js` (append the core data-shape builder + 3 small helpers in the Conversation section).

**Goal:** Pure function that takes a trial JSON and returns the `ConversationTree` shape defined in spec §5. This is the largest single function; tests for it are deferred to the smoke (Task 10) per v1 testing model. Verification is via temporary `console.log` against a real trial.

This task implements §5.1 steps 1-5 (partition, pair, attach). Anomaly detection (§5.1 step 6 → §6.1 inventory) and propagation are in Task 5; pitch generation (§5 → §8) is in Task 6; rendering (§7) is in Task 7.

- [ ] **Step 1: Append three small helpers above `buildConversationTree`**

Inside the Conversation section in `frontend/trial.js`, append:

```js

/**
 * Build an rid → {requestAuditIdx, responseAuditIdx, request, response} index
 * over the trial's llm_request and llm_response audit entries. Used by
 * buildConversationTree to resolve parent_run_rid lookups.
 */
function _indexLlmRunsByRid(audits) {
  const idx = new Map();
  for (let i = 0; i < audits.length; i++) {
    const e = audits[i];
    const b = e.body || {};
    const rid = b.rid;
    if (!rid) continue;
    const entry = idx.get(rid) || {};
    if (e.phase === "llm_request") {
      entry.requestAuditIdx = i;
      entry.request = e;
    } else if (e.phase === "llm_response") {
      entry.responseAuditIdx = i;
      entry.response = e;
    }
    idx.set(rid, entry);
  }
  return idx;
}

/**
 * Return the audit indices whose body.timestamp falls in [startedAt, finishedAt].
 * Inclusive on both ends. Timestamps are ISO strings or epoch numbers; coerce
 * via Date for comparison. Audits with no timestamp fall through (returned as
 * out-of-window).
 */
function _partitionAuditsByWindow(audits, startedAt, finishedAt) {
  const inWindow = [];
  const outOfWindow = [];
  const t0 = new Date(startedAt).getTime();
  const t1 = new Date(finishedAt).getTime();
  for (let i = 0; i < audits.length; i++) {
    const ts = (audits[i].body || {}).timestamp;
    if (!ts) { outOfWindow.push(i); continue; }
    const t = new Date(ts).getTime();
    if (isFinite(t) && t >= t0 && t <= t1) inWindow.push(i);
    else outOfWindow.push(i);
  }
  return {inWindow, outOfWindow};
}

/**
 * Last-6-hex of an mcp-session-id, or "?????" if absent. Matches the
 * abbreviation used in the CID flow tab's audit node second line.
 */
function _shortMcpSession(sid) {
  if (!sid || typeof sid !== "string") return "?????";
  return sid.length >= 6 ? sid.slice(-5) : sid;
}
```

- [ ] **Step 2: Append `buildConversationTree` immediately after the helpers**

```js

/**
 * Build the ConversationTree (spec §5) from a trial JSON.
 *
 * One pass over trial.turns[] (authoritative turn grouping), one pass over
 * audits to partition by turn window and CID, then within each (turn, cid)
 * slice: pair llm_request↔llm_response by rid, attach tool_calls under the
 * llm_response whose rid matches parent_run_rid, push orphan tool_calls under
 * the turn.
 *
 * Anomaly detection + finding propagation runs after this in
 * detectTurnAnomalies (Task 5). This function leaves the anomalies arrays
 * empty for downstream filling.
 *
 * Returns null if trial is unrecognisable (missing turns + audit_entries).
 */
function buildConversationTree(trial) {
  if (!trial || (!trial.turns && !trial.audit_entries)) return null;

  const audits = trial.audit_entries || [];
  const turns = trial.turns || [];
  const plan = (trial.turn_plan && trial.turn_plan.turns) || [];

  // Build trial-level rid→llmRun index (§5.1 step 4 — needed even before
  // we walk turns, so orphan detection can ask "does this parent_run_rid
  // resolve to ANY llm_request.rid in the trial").
  const rid2Run = _indexLlmRunsByRid(audits);

  // Top-level state — we may raise trial-wide anomalies during the walk
  // (e.g., turn_plan_misaligned, out-of-window audits).
  const trialAnomalies = [];

  // Turn-plan misalignment check (§6.1 row).
  if (plan.length > 0 && plan.length !== turns.length) {
    trialAnomalies.push({
      source: "trial",
      severity: "warn",
      reason: `turn_plan_misaligned: plan has ${plan.length} turns, execution has ${turns.length}`,
    });
  }

  // Group turns by CID. The common case is one CID; multi-CID falls out of
  // this naturally (each unique CID becomes its own root in `cidsMap`).
  // We resolve each turn's CID from the FIRST audit in its window that has
  // body.cid set. Turns whose audits use multiple CIDs raise mixed_cid_in_turn.
  const cidsMap = new Map();   // cid → {classification, turns: []}
  const usedAuditIdxs = new Set();

  for (let ti = 0; ti < turns.length; ti++) {
    const t = turns[ti];
    const {inWindow, outOfWindow: oo} = _partitionAuditsByWindow(
      audits, t.started_at, t.finished_at
    );
    for (const i of oo) {
      // Only report once per audit, the first time it slips a window.
      if (!usedAuditIdxs.has(i) && !audits[i].body) continue;
    }

    // Determine CID(s) used in this turn.
    const cidCounts = new Map();
    for (const i of inWindow) {
      const c = (audits[i].body || {}).cid;
      if (c) cidCounts.set(c, (cidCounts.get(c) || 0) + 1);
    }
    const cidsInTurn = [...cidCounts.keys()];
    const mixedCid = cidsInTurn.length > 1;

    // Turn-level fields.
    const userText = (plan[ti] && typeof plan[ti].content === "string" && plan[ti].content)
      ? plan[ti].content
      : (() => {
          // Fallback: first llm_request.uctx in window.
          for (const i of inWindow) {
            const a = audits[i];
            if (a.phase === "llm_request" && (a.body || {}).uctx) return a.body.uctx;
          }
          return "";
        })();
    const agentText = extractAgentText(t) || "";
    const latencyMs = (t.started_at && t.finished_at)
      ? Math.max(0, new Date(t.finished_at).getTime() - new Date(t.started_at).getTime())
      : null;

    const turnNode = {
      turnIdx: (typeof t.turn_idx === "number") ? t.turn_idx : ti,
      userText,
      agentText,
      startedAt: t.started_at,
      finishedAt: t.finished_at,
      latencyMs,
      badge: "pass",                 // filled by detectTurnAnomalies in Task 5
      anomalies: [],                 // filled by detectTurnAnomalies
      llmRuns: [],
      orphanToolCalls: [],
      _mixedCid: mixedCid,           // surfaced as anomaly in Task 5
      _inWindowAuditIdxs: inWindow,  // for the per-turn slice walk below
    };

    // Per-cid slice within the turn: walk inWindow audits in order, pair
    // llm_request↔llm_response by rid, attach tool_calls to their parent
    // llmRun via parent_run_rid lookup; collect orphan tool_calls.
    //
    // Key: an llm_request and its llm_response share rid. So we keep a
    // map from rid → llmRun-node we've started building this turn, and on
    // llm_response we close it out.
    const ridToNode = new Map();
    // Pending tool_call: keyed by mcp-session-id, so when tool_response
    // arrives we attach it to the same node + compute latency.
    const pendingTools = new Map(); // mcp-sid → {toolNode, ownerRunNode|null}

    for (const i of inWindow) {
      const e = audits[i];
      const b = e.body || {};
      if (e.phase === "llm_request") {
        const rid = b.rid;
        if (!rid) continue;
        let node = ridToNode.get(rid);
        if (!node) {
          node = {
            rid,
            parentRid: b.parent_rid || null,
            parentRidAnomaly: !!b.parent_rid_anomaly,
            isTurnBoundary: !!b.is_turn_boundary,
            requestAuditIdx: i,
            responseAuditIdx: null,
            providerResponseId: null,
            parentRidSources: Array.isArray(b.parent_rid_sources) ? b.parent_rid_sources : [],
            toolCalls: [],
          };
          ridToNode.set(rid, node);
          turnNode.llmRuns.push(node);
        }
      } else if (e.phase === "llm_response") {
        const rid = b.rid;
        if (!rid) continue;
        let node = ridToNode.get(rid);
        if (!node) {
          // Out-of-order or request-missing case — create a stub.
          node = {
            rid, parentRid: null, parentRidAnomaly: false, isTurnBoundary: false,
            requestAuditIdx: null, responseAuditIdx: i, providerResponseId: b.provider_response_id || null,
            parentRidSources: [], toolCalls: [],
          };
          ridToNode.set(rid, node);
          turnNode.llmRuns.push(node);
        } else {
          node.responseAuditIdx = i;
          node.providerResponseId = b.provider_response_id || null;
        }
      } else if (e.phase === "tool_call") {
        const sid = b["mcp-session-id"] || b.mcp_session_id || "";
        const prr = b.parent_run_rid;
        const ownerRun = prr ? ridToNode.get(prr) : null;
        // Strict orphan rule (§6.1): orphan if parent_run_rid is missing OR
        // doesn't resolve to a trial-local llm_request.rid. The trial-local
        // index (rid2Run) covers the "stamped but from a different conv"
        // case explicitly.
        const resolvable = !!prr && rid2Run.has(prr);
        const toolNode = {
          name: b.tool_name || b.name || "(unnamed)",
          mcpSession: _shortMcpSession(sid),
          mcpSessionFull: sid,
          parentRunRid: prr || null,
          ssConsumed: false,                 // filled if SS audit seen below
          ssAuditIdx: null,
          ssHash: null,
          callAuditIdx: i,
          responseAuditIdx: null,
          latencyMs: null,
          status: "ok",
          errorPreview: null,
          anomalies: [],
        };
        if (resolvable && ownerRun) {
          ownerRun.toolCalls.push(toolNode);
        } else {
          turnNode.orphanToolCalls.push(toolNode);
        }
        pendingTools.set(sid, {toolNode, ownerRunNode: ownerRun || null});
      } else if (e.phase === "tool_response") {
        const sid = b["mcp-session-id"] || b.mcp_session_id || "";
        const pending = pendingTools.get(sid);
        if (!pending) continue;
        pending.toolNode.responseAuditIdx = i;
        const tCallTs = audits[pending.toolNode.callAuditIdx]
          && (audits[pending.toolNode.callAuditIdx].body || {}).timestamp;
        const tRespTs = b.timestamp;
        if (tCallTs && tRespTs) {
          const dt = new Date(tRespTs).getTime() - new Date(tCallTs).getTime();
          if (isFinite(dt)) pending.toolNode.latencyMs = Math.max(0, dt);
        }
        if (b.status && b.status !== "ok") {
          pending.toolNode.status = String(b.status);
          pending.toolNode.errorPreview = (b.error_preview || b.error || "").slice(0, 200);
        }
        pendingTools.delete(sid);
      } else if (e.phase === "ib_ss" || e.phase === "snapshot") {
        // Snapshot audit — attach to whatever tool_call's snapshot_hash
        // matches, if any. Otherwise the snapshot is orphan (handled in
        // detectTurnAnomalies via per-turn unconsumed-snapshot count).
        const h = b.snapshot_hash || b.hash;
        if (!h) continue;
        // Walk all toolNodes in this turn (under runs + orphans) and match.
        let matched = false;
        const visitTools = nodes => {
          for (const tn of nodes) {
            if ((audits[tn.callAuditIdx] && audits[tn.callAuditIdx].body
                 && audits[tn.callAuditIdx].body.snapshot_hash) === h) {
              tn.ssConsumed = true;
              tn.ssAuditIdx = i;
              tn.ssHash = h;
              matched = true;
            }
          }
        };
        for (const r of turnNode.llmRuns) visitTools(r.toolCalls);
        visitTools(turnNode.orphanToolCalls);
        if (!matched) {
          turnNode.anomalies.push({
            source: `audit#${i}`,
            severity: "warn",
            reason: `snapshot_orphan: SS ${h.slice(0, 8)}… not consumed by any tool_call in this turn`,
          });
        }
      }
    } // end inWindow walk

    // Push the turn into the correct CID root. If multiple CIDs in turn,
    // duplicate the turn under each CID? No — that misrepresents history.
    // Instead, file the turn under the FIRST CID encountered and raise
    // mixed_cid_in_turn anomaly (Task 5).
    const primaryCid = cidsInTurn[0] || "(unknown)";
    if (!cidsMap.has(primaryCid)) {
      cidsMap.set(primaryCid, {cid: primaryCid, classification: "preserved", turns: []});
    }
    cidsMap.get(primaryCid).turns.push(turnNode);
  }

  // Classify each CID (preserved / single / audit-only). Matches the
  // logic implicit in renderCidFlowTab's existing CID classification
  // (turns-attributed = ≥1, multi-turn = ≥2). audit-only = CID was seen in
  // audits but no turn used it. For our cids map, "preserved" needs ≥2
  // turns referencing it; "single" = exactly 1; "audit-only" never happens
  // in this map because we only added CIDs that had at least one turn —
  // but the trial may have CIDs in audits that no turn touched, which
  // we surface as multiCidAnomaly + a "phantom" root in Task 5.
  for (const root of cidsMap.values()) {
    if (root.turns.length >= 2) root.classification = "preserved";
    else if (root.turns.length === 1) root.classification = "single";
    else root.classification = "audit-only";
  }

  const cids = [...cidsMap.values()];

  return {
    header: {
      rowId: (trial.config && trial.config.row_id) || null,
      rowDescription: (trial.config && trial.config.description) || null,
      status: trial.status || null,
      globalBadge: "pass",          // filled by detectTurnAnomalies (Task 5)
      elevatorPitch: "",            // filled by generateElevatorPitch (Task 6)
    },
    multiCidAnomaly: cids.length > 1,
    findings: [],                   // filled by detectTurnAnomalies (Task 5)
    trialAnomalies,
    cids,
  };
}
```

- [ ] **Step 3: Verify via temporary console.log**

Temporarily change `renderConversationTab` to:

```js
function renderConversationTab(trial) {
  const tree = buildConversationTree(trial);
  console.log("ConversationTree:", tree);
  return `<pre style="padding:16px;font-size:11px;">${escapeHtml(JSON.stringify(tree, null, 2))}</pre>`;
}
```

Hard-refresh a recent trial page. Expected: the Conversation tab dumps the full tree JSON. Walk it manually:
- `cids` length = expected distinct CID count.
- Each `cids[k].turns` has the right `turnIdx` values + `userText` + `agentText`.
- Each turn's `llmRuns` has rid pairs (request + response).
- `tool_call`s with resolvable `parent_run_rid` are under `llmRuns[k].toolCalls`.
- `tool_call`s with missing/unresolvable `parent_run_rid` are under `orphanToolCalls`.

If a smoke trial has all-orphan tools, check that the trial was actually started with RID toggles ON in `agw/config.yaml` (the strict orphan rule, §6.1, is correct in saying "RID off → all orphan").

- [ ] **Step 4: Revert renderConversationTab to the Task 2 stub**

```js
function renderConversationTab(trial) {
  // Stub — replaced in Task 7 by the full tree emitter.
  return `<p style="padding:16px;color:#666;">Conversation view — under construction. Use other tabs for now.</p>`;
}
```

- [ ] **Step 5: Commit**

```bash
git add frontend/trial.js
git commit -m "feat(frontend): buildConversationTree + index/window helpers

Pure data-shape builder (spec §5.1): one pass over trial.turns[] for
authoritative grouping, one pass over audits to partition by window and
CID, paired llm_request↔llm_response by rid, attaches tool_calls under
their parent_run_rid'ed llm_response, pushes orphans (strict rule) under
the turn. Anomaly detection and rendering land in Tasks 5+7.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Task 5: detectTurnAnomalies + finding propagation

**Files:**
- Modify: `frontend/trial.js` (append in Conversation section).

**Goal:** Walk the ConversationTree built in Task 4, attach anomaly objects per §6.1, propagate badges up (turn → CID → trial), produce the flat `findings` list with anchor IDs for the Findings panel.

- [ ] **Step 1: Append `detectTurnAnomalies` to `frontend/trial.js`**

Inside the Conversation section:

```js

/**
 * Walk the ConversationTree (Task 4 output) and:
 *   1. Attach anomaly objects per §6.1.
 *   2. Promote badges: turn = warn if any anomaly; cid = warn if any turn
 *      warn OR classification != "preserved"; cid promoted to fail if
 *      classification = "audit-only".
 *   3. Trial header globalBadge = "fail" if trial.status === "fail" OR
 *      multiCidAnomaly; "warn" if any cid badge !== "pass"; "pass" else.
 *      Multi-CID is severity "fail" per §6.1.
 *   4. Build the flat findings list with stable anchor IDs matching the
 *      DOM IDs that Task 7's renderer emits (#conv-t{turnIdx}-llm{k},
 *      #conv-t{turnIdx}-tool{k}, #conv-t{turnIdx}-orphan{k}, #conv-cid-{cidShort}).
 *
 * Mutates `tree` in place. Idempotent (safe to call twice).
 */
function detectTurnAnomalies(tree) {
  if (!tree) return;
  const findings = [];

  // Promote trial-wide anomalies (set by buildConversationTree) into findings.
  for (const a of (tree.trialAnomalies || [])) {
    findings.push({
      anchor: "#conv-header",
      title: "Trial",
      reason: a.reason,
      severity: a.severity,
    });
  }

  for (const cid of tree.cids) {
    let cidWarn = cid.classification !== "preserved";
    const cidAnchor = `#conv-cid-${(cid.cid || "unknown").slice(-6)}`;

    if (cid.classification === "audit-only") {
      findings.push({
        anchor: cidAnchor,
        title: `CID ${cid.cid.slice(0, 8)}…`,
        reason: "audit-only CID: appears in audits but no turn referenced it",
        severity: "fail",
      });
      cid.badge = "fail";
    } else if (cid.classification === "single") {
      findings.push({
        anchor: cidAnchor,
        title: `CID ${cid.cid.slice(0, 8)}…`,
        reason: "single-use CID: only one turn used this CID",
        severity: "warn",
      });
    }

    for (const turn of cid.turns) {
      // Mixed CID in this turn (raised as a flag in Task 4).
      if (turn._mixedCid) {
        turn.anomalies.push({
          source: "turn",
          severity: "warn",
          reason: "mixed_cid_in_turn: audits in this turn use multiple CIDs",
        });
      }

      // Turn-boundary mismatch (§6.1): first llm_request of the turn must
      // have is_turn_boundary === true. We treat absence as "feature off,
      // skip check" rather than failure. The check fires only when at least
      // one llmRun anywhere in the trial has the field set (proxy for
      // "RID feature on for this trial").
      const firstLlm = turn.llmRuns[0];
      const anyHasBoundary = tree.cids.some(
        c => c.turns.some(t => t.llmRuns.some(r => r.isTurnBoundary))
      );
      if (firstLlm && anyHasBoundary && firstLlm.isTurnBoundary !== true) {
        turn.anomalies.push({
          source: "llm_request",
          severity: "warn",
          reason: "turn_boundary_mismatch: first llm_request lacks is_turn_boundary=true",
        });
      }

      // Per-llm_request parent_rid_anomaly (§6.1).
      turn.llmRuns.forEach((run, ki) => {
        if (run.parentRidAnomaly) {
          turn.anomalies.push({
            source: `llm_request#${ki}`,
            severity: "warn",
            reason: `parent_rid_anomaly: same-position carrier conflict (rid ${run.rid.slice(0, 8)}…)`,
          });
          findings.push({
            anchor: `#conv-t${turn.turnIdx}-llm${ki}`,
            title: `Turn ${turn.turnIdx} · llm_request`,
            reason: `parent_rid_anomaly (CHG-26G same-position conflict)`,
            severity: "warn",
          });
        }
      });

      // Orphan tool_calls (§6.1) — already separated in Task 4.
      turn.orphanToolCalls.forEach((tn, oi) => {
        const reason = tn.parentRunRid
          ? `orphan tool_call ${tn.name}: parent_run_rid ${tn.parentRunRid.slice(0, 8)}… doesn't resolve to a trial-local LLM run`
          : `orphan tool_call ${tn.name}: missing parent_run_rid (RID injection may be off)`;
        tn.anomalies.push({source: "tool_call", severity: "warn", reason});
        turn.anomalies.push({source: `orphan#${oi}`, severity: "warn", reason});
        findings.push({
          anchor: `#conv-t${turn.turnIdx}-orphan${oi}`,
          title: `Turn ${turn.turnIdx} · orphan tool_call ${tn.name}`,
          reason,
          severity: "warn",
        });
      });

      // Per-turn badge.
      turn.badge = turn.anomalies.length > 0 ? "warn" : "pass";
      if (turn.badge !== "pass") cidWarn = true;
    }

    if (cid.badge !== "fail") cid.badge = cidWarn ? "warn" : "pass";
  }

  // Multi-CID fail (§6.1).
  if (tree.multiCidAnomaly) {
    findings.push({
      anchor: "#conv-multicid-banner",
      title: "Trial",
      reason: `multi-CID: trial spans ${tree.cids.length} distinct conversations (cross-trial drift suspected)`,
      severity: "fail",
    });
  }

  // Trial header global badge: spec §6.2 says "reflect, not recompute" but
  // multi-CID is treated as fail-level. So: prefer trial.status if fail/error;
  // else multi-CID → fail; else any cid badge !== pass → warn; else pass.
  tree.header.globalBadge = (() => {
    if (tree.multiCidAnomaly) return "fail";
    if (tree.cids.some(c => c.badge === "fail")) return "fail";
    if (tree.cids.some(c => c.badge !== "pass")) return "warn";
    return "pass";
  })();

  tree.findings = findings;
}
```

- [ ] **Step 2: Verify via temporary call from renderConversationTab**

Temporarily change `renderConversationTab` to:

```js
function renderConversationTab(trial) {
  const tree = buildConversationTree(trial);
  detectTurnAnomalies(tree);
  console.log("ConversationTree post-anomalies:", tree);
  return `<pre style="padding:16px;font-size:11px;">${escapeHtml(JSON.stringify(tree, null, 2))}</pre>`;
}
```

Hard-refresh a recent trial page. Expected:
- A clean pass trial: `tree.findings.length === 0`, every turn `badge === "pass"`, every cid `badge === "pass"`, `header.globalBadge === "pass"`.
- A trial with an orphan tool: at least one finding with `severity: "warn"`, turn badge warn, cid badge warn.
- A multi-CID trial (contaminated): one finding with `severity: "fail"`, `multiCidAnomaly: true`, `header.globalBadge === "fail"`.

- [ ] **Step 3: Revert renderConversationTab to the Task 2 stub**

(Same restoration as Task 4 Step 4.)

- [ ] **Step 4: Commit**

```bash
git add frontend/trial.js
git commit -m "feat(frontend): detectTurnAnomalies + finding propagation

Walks ConversationTree; attaches anomalies per spec §6.1 (parent_rid_anomaly,
orphan tool_call, mixed_cid_in_turn, turn_boundary_mismatch, audit-only CID,
single-use CID, multi-CID). Promotes badges turn→cid→trial header per §6.2.
Builds the flat findings list with stable anchor IDs matching the DOM IDs
Task 7 will emit.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Task 6: generateElevatorPitch(trial)

**Files:**
- Modify: `frontend/trial.js` (append in Conversation section).

**Goal:** Per §8, build the single-line auto-generated pitch from trial fields. Pure function. Returns `{icon, badge, line}`.

- [ ] **Step 1: Append `generateElevatorPitch` to `frontend/trial.js`**

```js

/**
 * Build the one-line elevator pitch for the trial header (spec §8).
 *
 *   Pass: ✓ PASS · {N}-turn {row} · {cid summary} · {tool summary}
 *   Warn: ⚠ {findings} of {N} turns flagged · {N}-turn {row} · {cid summary} · {anomaly summary}
 *   Fail: ✗ FAIL · {N}-turn {row} · {cid summary} · {failure highlights}
 *
 * Inputs: tree (already passed through detectTurnAnomalies), trial.
 * Returns {icon, badge: "pass"|"warn"|"fail", line: string}.
 *
 * Length cap: if line > 160 chars, drop the tool summary first, then the
 * cid summary's qualifier. Never truncate icon or row label.
 */
function generateElevatorPitch(trial, tree) {
  if (!tree) return {icon: "?", badge: "warn", line: "(unknown trial shape)"};

  const badge = tree.header.globalBadge;
  const icon = badge === "pass" ? "✓ PASS" : badge === "fail" ? "✗ FAIL" : `⚠ ${tree.findings.length} finding${tree.findings.length === 1 ? "" : "s"}`;

  const nTurns = tree.cids.reduce((s, c) => s + c.turns.length, 0);
  const rowLabel = tree.header.rowDescription || tree.header.rowId || "(unnamed row)";

  // CID summary.
  let cidSummary;
  if (tree.multiCidAnomaly) {
    cidSummary = `${tree.cids.length} distinct CIDs (drift)`;
  } else if (tree.cids.length === 1) {
    const c = tree.cids[0];
    if (c.classification === "preserved") cidSummary = "1 CID stable across all turns";
    else if (c.classification === "single") cidSummary = "1 CID — single-use";
    else cidSummary = "1 CID — audit-only";
  } else {
    cidSummary = "no CID";
  }

  // Tool summary.
  let totalTools = 0, orphanTools = 0;
  for (const c of tree.cids) for (const t of c.turns) {
    for (const r of t.llmRuns) totalTools += r.toolCalls.length;
    totalTools += t.orphanToolCalls.length;
    orphanTools += t.orphanToolCalls.length;
  }
  let toolSummary;
  if (totalTools === 0) toolSummary = null;
  else if (orphanTools === 0) toolSummary = `${totalTools} tool call${totalTools === 1 ? "" : "s"}, all traced to LLM run`;
  else toolSummary = `${totalTools} tool call${totalTools === 1 ? "" : "s"}, ${orphanTools} orphan`;

  // Findings count (for warn).
  const warnPart = badge === "warn"
    ? `${tree.findings.length} of ${nTurns} turn${nTurns === 1 ? "" : "s"} flagged`
    : null;

  const parts = [
    icon,
    warnPart,
    `${nTurns}-turn ${rowLabel}`,
    cidSummary,
    toolSummary,
  ].filter(Boolean);

  let line = parts.join(" · ");
  if (line.length > 160 && toolSummary) {
    line = parts.filter(p => p !== toolSummary).join(" · ");
  }
  if (line.length > 160 && cidSummary && cidSummary.includes(" — ")) {
    const shortened = cidSummary.split(" — ")[0];
    line = line.replace(cidSummary, shortened);
  }
  return {icon, badge, line};
}
```

- [ ] **Step 2: Verify via temporary call**

Temporarily change `renderConversationTab` to:

```js
function renderConversationTab(trial) {
  const tree = buildConversationTree(trial);
  detectTurnAnomalies(tree);
  const pitch = generateElevatorPitch(trial, tree);
  return `<pre style="padding:16px;">${escapeHtml(JSON.stringify(pitch, null, 2))}\n\n${escapeHtml(pitch.line)}</pre>`;
}
```

Hard-refresh a clean pass trial. Expected line shape:
`✓ PASS · 3-turn weather query via langchain/ollama · 1 CID stable across all turns · 2 tool calls, all traced to LLM run`

For a contaminated trial:
`✗ FAIL · 3-turn weather query via langchain/ollama · 2 distinct CIDs (drift) · 4 tool calls, 2 orphan`

- [ ] **Step 3: Revert renderConversationTab to the Task 2 stub**

- [ ] **Step 4: Commit**

```bash
git add frontend/trial.js
git commit -m "feat(frontend): generateElevatorPitch — auto-generated trial header line

Single-line pitch from trial fields per spec §8: outcome icon + N-turn +
row label + CID summary + tool summary. Three templates by outcome (pass/
warn/fail). Length-cap drops tool summary first, then CID qualifier;
never truncates icon or row label.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Task 7: renderConversationTree HTML emitter + sub-renderers

**Files:**
- Modify: `frontend/trial.js` (append the renderer chain; replace the Task 2 stub `renderConversationTab` with the real implementation).

**Goal:** Emit the full DOM per spec §7. This is the largest single task by LoC. Implemented as one entrypoint + four sub-renderers for clarity.

- [ ] **Step 1: Append the sub-renderers**

```js

/**
 * Render a single tool_call node (used both under llm_runs and as orphan).
 * `isOrphan` controls anchor id prefix and the "orphan" badge.
 */
function _renderConvToolNode(turn, tool, k, isOrphan) {
  const id = isOrphan ? `conv-t${turn.turnIdx}-orphan${k}` : `conv-t${turn.turnIdx}-tool${k}`;
  const badgeClass = isOrphan || tool.anomalies.length > 0 ? "warn" : "pass";
  const badgeIcon = badgeClass === "warn" ? "⚠" : "✓";
  const ssBit = tool.ssConsumed
    ? `<span class="conv-ss-flag pass">✓ ss consumed</span>`
    : "";
  const orphanFlag = isOrphan ? `<span class="conv-orphan-flag">orphan</span>` : "";
  const respLine = tool.responseAuditIdx != null
    ? `<li class="conv-tool-resp">tool_response · mcp-ss <code>${escapeHtml(tool.mcpSession)}</code>${tool.latencyMs != null ? ` · ${tool.latencyMs}ms` : ""} · ${escapeHtml(tool.status)}${tool.errorPreview ? ` · <span class="conv-err">${escapeHtml(tool.errorPreview)}</span>` : ""}</li>`
    : `<li class="conv-tool-resp">(no tool_response)</li>`;
  const govInternals = `<ul class="conv-gov-internals">
      <li>parent_run_rid: <code>${escapeHtml(tool.parentRunRid || "—")}</code> · mcp-session-id: <code>${escapeHtml(tool.mcpSessionFull || "—")}</code>${tool.ssHash ? ` · snapshot_hash: <code>${escapeHtml(tool.ssHash)}</code>` : ""}</li>
    </ul>`;
  return `<li class="conv-tool ${isOrphan ? "conv-anomaly" : ""}" id="${id}">
    <span class="conv-badge ${badgeClass}">${badgeIcon}</span>
    ${isOrphan ? "tool_call (orphan)" : "tool_call"} <code>${escapeHtml(tool.name)}</code>
    · mcp-ss <code>${escapeHtml(tool.mcpSession)}</code>
    ${ssBit}${orphanFlag}
    <ul class="conv-tool-resp-list">${respLine}</ul>
    ${govInternals}
  </li>`;
}

/**
 * Render one llmRun (llm_request + llm_response pair) and its tool_calls.
 *
 * Anchor ID: only the REQUEST <li> gets a stable id (`conv-t{idx}-llm{k}`).
 * The response has no id because no finding in §6.1 attaches to it
 * (parent_rid_anomaly is on the request; provider_response_id is operator
 * telemetry, not a finding). Keeping IDs sparse means the Task 5 anchor
 * formula `#conv-t{idx}-llm{k}` lines up with the llmRun index directly.
 */
function _renderConvLlmRun(turn, run, k) {
  const reqId = `conv-t${turn.turnIdx}-llm${k}`;
  const reqAnomaly = run.parentRidAnomaly ? " conv-anomaly" : "";
  const parentBit = run.parentRid
    ? `parent: <code>${escapeHtml(run.parentRid.slice(0, 12))}…</code>${run.parentRidAnomaly ? ' <span class="conv-anomaly-flag">⚠ same-position conflict</span>' : ""}`
    : "parent: —";
  const reqInternals = `<ul class="conv-gov-internals">
      <li>rid: <code>${escapeHtml(run.rid)}</code> · is_turn_boundary: ${run.isTurnBoundary}${run.parentRidSources.length ? ` · parent_rid_sources: [${run.parentRidSources.map(s => `<code>${escapeHtml(s)}</code>`).join(", ")}]` : ""}</li>
    </ul>`;
  const respInternals = `<ul class="conv-gov-internals">
      <li>rid: <code>${escapeHtml(run.rid)}</code>${run.providerResponseId ? ` · provider_response_id: <code>${escapeHtml(run.providerResponseId)}</code>` : ""}</li>
    </ul>`;
  const tools = run.toolCalls.map((t, ti) => _renderConvToolNode(turn, t, ti, false)).join("");
  return `<li class="conv-llm${reqAnomaly}" id="${reqId}">
      <span class="conv-phase">▸ llm_request</span>
      <span class="conv-rid">rid=<code>${escapeHtml(run.rid.slice(0, 12))}…</code></span>
      <span class="conv-parent">${parentBit}</span>
      ${reqInternals}
    </li>
    <li class="conv-llm">
      <span class="conv-phase">▸ llm_response</span>
      <span class="conv-rid">rid=<code>${escapeHtml(run.rid.slice(0, 12))}…</code></span>
      ${respInternals}
      ${tools.length ? `<ul class="conv-tools">${tools}</ul>` : ""}
    </li>`;
}

/**
 * Render one turn block.
 */
function _renderConvTurn(turn) {
  const badgeClass = turn.badge;
  const badgeIcon = badgeClass === "pass" ? "✓" : "⚠";
  const latency = turn.latencyMs != null ? `${(turn.latencyMs / 1000).toFixed(1)}s` : "";
  const userPreview = turn.userText ? _truncate(turn.userText, 120) : "(no user message)";
  const agentPreview = turn.agentText ? _truncate(turn.agentText, 120) : "(no agent text — see Turns tab for raw response)";
  const userBlock = turn.userText
    ? `<div class="conv-msg user">👤 User: <span class="conv-text">${escapeHtml(userPreview.shown)}</span>${userPreview.truncated ? `<details><summary>more</summary><div class="conv-text-full">${escapeHtml(turn.userText)}</div></details>` : ""}</div>`
    : `<div class="conv-msg user">👤 User: <em>(no user message)</em></div>`;
  const agentBlock = turn.agentText
    ? `<div class="conv-msg agent">🤖 Agent: <span class="conv-text">${escapeHtml(agentPreview.shown)}</span>${agentPreview.truncated ? `<details><summary>more</summary><div class="conv-text-full">${escapeHtml(turn.agentText)}</div></details>` : ""}</div>`
    : `<div class="conv-msg agent">🤖 Agent: <em>${escapeHtml(agentPreview)}</em></div>`;
  const runs = turn.llmRuns.map((r, k) => _renderConvLlmRun(turn, r, k)).join("");
  const orphans = turn.orphanToolCalls.length
    ? `<ul class="conv-orphans">
        <li class="conv-orphan-label">Orphan tool calls (no resolvable parent_run_rid):</li>
        ${turn.orphanToolCalls.map((t, k) => _renderConvToolNode(turn, t, k, true)).join("")}
      </ul>`
    : "";
  return `<section class="conv-turn ${badgeClass === "warn" ? "conv-anomaly" : ""}" id="conv-t${turn.turnIdx}">
      <header class="conv-turn-header">
        <span class="conv-badge ${badgeClass}">${badgeIcon}</span>
        <span class="conv-turn-title">Turn ${turn.turnIdx}</span>
        ${latency ? `<small>${latency}</small>` : ""}
      </header>
      ${userBlock}
      ${agentBlock}
      <ul class="conv-llm-list">${runs}</ul>
      ${orphans}
    </section>`;
}

/**
 * Render one CID root.
 */
function _renderConvCidRoot(cid) {
  const badgeClass = cid.badge;
  const badgeIcon = badgeClass === "pass" ? "✓" : badgeClass === "fail" ? "✗" : "⚠";
  const stab = cid.classification === "preserved"
    ? `${cid.turns.length} turns · CID stable`
    : cid.classification === "single"
    ? `1 turn · CID single-use`
    : `audit-only`;
  return `<article class="conv-cid" id="conv-cid-${(cid.cid || "unknown").slice(-6)}" data-cid="${escapeHtml(cid.cid || "")}">
      <h2><span class="conv-badge ${badgeClass}">${badgeIcon}</span> conversation <code>${escapeHtml(cid.cid || "(unknown)")}</code>
          <small>${stab}</small></h2>
      ${cid.turns.map(_renderConvTurn).join("")}
    </article>`;
}

/**
 * Word-aware truncation. Returns {shown, truncated} so the caller can decide
 * whether to render an expand <details>.
 */
function _truncate(text, maxLen) {
  if (text.length <= maxLen) return {shown: text, truncated: false};
  let cut = text.lastIndexOf(" ", maxLen);
  if (cut < maxLen / 2) cut = maxLen;
  return {shown: text.slice(0, cut) + "…", truncated: true};
}
```

- [ ] **Step 2: Replace the stub `renderConversationTab` with the full implementation**

Replace the stub `renderConversationTab` (in `frontend/trial.js`) with:

```js
function renderConversationTab(trial) {
  const tree = buildConversationTree(trial);
  if (!tree) {
    return `<p style="padding:16px;">No conversation data for this trial.</p>`;
  }
  detectTurnAnomalies(tree);
  const pitch = generateElevatorPitch(trial, tree);

  const header = `<header class="conv-header">
      <span class="conv-badge ${pitch.badge}">${escapeHtml(pitch.icon)}</span>
      <span class="conv-pitch">${escapeHtml(pitch.line)}</span>
      <label class="conv-toggle">
        <input type="checkbox" id="conv-gov-internals-cb" ${convShowGovInternals ? "checked" : ""}>
        ⚙ Show governance internals
      </label>
      <a class="conv-link" href="#" data-tab-target="cidflow-interactive">or open Operator: CID flow / Interactive →</a>
    </header>`;

  const multiBanner = tree.multiCidAnomaly
    ? `<section class="conv-multicid-banner" id="conv-multicid-banner">⚠ This trial spans ${tree.cids.length} conversations — see verdict (a) / (i). Cross-trial drift suspected.</section>`
    : "";

  const findings = tree.findings.length
    ? `<section class="conv-findings" id="conv-findings">
        <details open><summary>Findings (${tree.findings.length})</summary>
          <ul>${tree.findings.map(f => `<li class="conv-finding-${f.severity}"><a href="${escapeHtml(f.anchor)}">${escapeHtml(f.title)}</a> — ${escapeHtml(f.reason)}</li>`).join("")}</ul>
        </details>
      </section>`
    : "";

  const cids = tree.cids.map(_renderConvCidRoot).join("");

  return header + multiBanner + findings + cids;
}
```

- [ ] **Step 3: Verify visually in the browser**

Hard-refresh a recent trial page. Expected:
- The header line shows the auto-generated pitch + outcome badge.
- Each CID root renders with the right turns under it.
- Each turn shows User/Agent preview, with "…" + click-to-expand details when the message is long.
- LLM runs are paired (one request + one response per rid).
- Tool calls appear under the matching `llm_response` rid.
- Orphan tool calls (if any) appear at turn level under the "Orphan tool calls" heading with `⚠`.
- Findings panel is open when count > 0; otherwise the section is absent.
- Multi-CID banner appears iff > 1 CID.

CSS isn't styled yet (Task 8), so the visual will be unstyled HTML — verify *structure* is correct, not *appearance*.

- [ ] **Step 4: Commit**

```bash
git add frontend/trial.js
git commit -m "feat(frontend): renderConversationTree HTML emitter

Full DOM emitter per spec §7. Replaces the Task 2 stub with the real
renderer: header pitch + ⚙ toggle + multi-CID banner + Findings panel
(auto-open when count>0, omitted when 0) + per-CID roots + per-turn
sections with user/agent preview (word-aware ellipsis + click-to-expand
via <details>) + per-llmRun rid pairing + tool_calls under llm_response
+ orphan tool_calls under the turn. Stable anchor IDs (#conv-tN-llmK /
toolK / orphanK / cid-XXXXXX) match the Findings panel links.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Task 8: CSS .conv-* rules + :target ring + gov-internals visibility

**Files:**
- Modify: `frontend/style.css` (append at end).

**Goal:** Style the tree per spec §7's intent: indentation guides via left borders, badge colors matching existing palette (`#28a745` green, `#ffc107` yellow, `#dc3545` red), anomaly highlights, click-to-expand chevrons, `:target` ring animation, governance-internals hidden by default.

- [ ] **Step 1: Append the full `.conv-*` block to `frontend/style.css`**

```css

/* ── Customer Conversation View ── */

#tab-conversation { padding: 16px; max-width: 1100px; }

.conv-header {
  display: flex; align-items: center; gap: 12px; flex-wrap: wrap;
  padding: 12px; background: #f8f9fa; border-radius: 6px;
  margin-bottom: 16px;
}
.conv-pitch { flex: 1 1 60%; font-size: 14px; line-height: 1.4; }
.conv-toggle { font-size: 13px; cursor: pointer; user-select: none; }
.conv-link { font-size: 12px; color: #007bff; text-decoration: none; }
.conv-link:hover { text-decoration: underline; }

.conv-badge {
  display: inline-block; padding: 2px 6px; border-radius: 3px;
  font-weight: bold; font-size: 12px; min-width: 24px; text-align: center;
}
.conv-badge.pass { background: #d4edda; color: #155724; }
.conv-badge.warn { background: #fff3cd; color: #856404; }
.conv-badge.fail { background: #f8d7da; color: #721c24; }

.conv-multicid-banner {
  padding: 10px 14px; margin-bottom: 12px;
  background: #f8d7da; border-left: 4px solid #dc3545;
  border-radius: 4px; font-weight: 500;
}

.conv-findings {
  padding: 8px 12px; margin-bottom: 16px;
  background: #fff3cd; border-left: 4px solid #ffc107;
  border-radius: 4px;
}
.conv-findings summary { cursor: pointer; font-weight: bold; }
.conv-findings ul { margin: 6px 0 0 0; padding-left: 20px; }
.conv-findings li { margin: 4px 0; font-size: 13px; }
.conv-findings a { color: #007bff; text-decoration: none; }
.conv-findings a:hover { text-decoration: underline; }
.conv-finding-fail a { color: #721c24; font-weight: bold; }

.conv-cid {
  border: 1px solid #dee2e6; border-radius: 6px;
  padding: 10px 14px; margin-bottom: 12px;
}
.conv-cid h2 {
  margin: 0 0 8px 0; font-size: 16px; font-weight: 600;
  display: flex; align-items: center; gap: 8px;
}
.conv-cid h2 small { color: #6c757d; font-weight: normal; font-size: 12px; }

.conv-turn {
  border-left: 3px solid #dee2e6;
  padding: 6px 0 6px 12px; margin: 8px 0 8px 8px;
}
.conv-turn.conv-anomaly { border-left-color: #ffc107; background: #fffaeb; }
.conv-turn-header {
  display: flex; align-items: center; gap: 8px;
  margin-bottom: 4px;
}
.conv-turn-title { font-weight: 600; }
.conv-turn-header small { color: #6c757d; font-size: 12px; }

.conv-msg {
  padding: 3px 0; font-size: 13px;
}
.conv-msg.user { color: #495057; }
.conv-msg.agent { color: #212529; }
.conv-text { font-style: italic; }
.conv-msg details { display: inline; }
.conv-msg summary {
  cursor: pointer; color: #007bff; font-style: normal;
  font-size: 12px; margin-left: 4px;
}
.conv-text-full {
  white-space: pre-wrap; padding: 6px 10px;
  background: #f1f3f5; border-radius: 3px;
  font-style: normal; font-size: 12px; margin-top: 4px;
}

.conv-llm-list {
  list-style: none; padding-left: 16px; margin: 6px 0;
  border-left: 2px dashed #dee2e6;
}
.conv-llm {
  padding: 3px 0; font-size: 12px; font-family: monospace;
}
.conv-llm.conv-anomaly { background: #fffaeb; padding-left: 6px; border-left: 2px solid #ffc107; }
.conv-phase { color: #6c757d; }
.conv-rid { color: #495057; margin-left: 4px; }
.conv-parent { color: #6c757d; margin-left: 4px; }
.conv-anomaly-flag { color: #b8651e; font-weight: bold; }

.conv-tools {
  list-style: none; padding-left: 16px; margin: 4px 0;
  border-left: 2px dotted #adb5bd;
}
.conv-tool {
  padding: 2px 0; font-size: 12px; font-family: monospace;
}
.conv-tool.conv-anomaly { background: #fffaeb; padding-left: 6px; border-left: 2px solid #ffc107; }
.conv-tool code { background: #f1f3f5; padding: 1px 3px; border-radius: 2px; }
.conv-tool-resp-list {
  list-style: none; padding-left: 14px; margin: 2px 0;
  color: #495057;
}
.conv-ss-flag.pass { color: #155724; font-weight: bold; margin-left: 4px; }
.conv-orphan-flag { color: #b8651e; font-weight: bold; margin-left: 4px; }
.conv-err { color: #721c24; }

.conv-orphans {
  list-style: none; padding-left: 16px; margin: 6px 0;
  border-left: 2px solid #ffc107;
}
.conv-orphan-label {
  font-weight: 600; color: #856404; font-size: 12px;
  padding-bottom: 2px; font-family: inherit;
}

/* Governance internals overlay: hidden by default; toggled by the checkbox
 * via the JS handler that flips a body-level class (see _wireConvToggle in
 * trial.js Task 9). The class-based rule avoids :checked-based selectors,
 * which can't reach siblings of the checkbox's parent label. */
.conv-gov-internals { display: none; margin: 2px 0 2px 12px; padding-left: 6px; border-left: 1px dotted #adb5bd; font-size: 11px; color: #6c757d; }
body.conv-gov-on .conv-gov-internals { display: block; }

/* :target ring — flashes the linked node when navigating from Findings. */
@keyframes conv-target-ring {
  0%   { box-shadow: 0 0 0 4px rgba(255, 193, 7, 0.7); }
  100% { box-shadow: 0 0 0 4px rgba(255, 193, 7, 0); }
}
.conv-llm:target, .conv-tool:target, .conv-turn:target, .conv-cid:target {
  animation: conv-target-ring 1.5s ease-out;
}
```

- [ ] **Step 2: Verify visually in the browser**

Hard-refresh (Ctrl-Shift-R) any trial page. Expected:
- Header band has light gray background, badge + pitch + ⚙ checkbox + Operator link visible.
- Each CID root is a bordered card. Each turn has a thin left border (orange-tinted when warn).
- Pass badge = green, warn = yellow, fail = red — matches existing CID-flow palette.
- User/Agent lines show italic text + a "more" link when truncated.
- Tool calls indented under llm_response with monospace font.
- Findings panel (when present) is a yellow-tinted band with clickable links.
- Multi-CID banner (when present) is red-tinted at the very top.
- Clicking a finding link scrolls to + briefly flashes the target node.

- [ ] **Step 3: Commit**

```bash
git add frontend/style.css
git commit -m "style(frontend): .conv-* CSS for Conversation tab

Reuses existing palette (#28a745 green / #ffc107 yellow / #dc3545 red);
adds tree indentation guides via left-borders on nested <ul>s, badge
classes (pass/warn/fail), anomaly highlights (orange-tint background),
click-to-expand chevron styling, :target ring animation for Findings-
panel anchor jumps, and the .conv-gov-internals visibility rule
toggled by a body-level class (wired in Task 9).

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Task 9: ⚙ Show governance internals toggle wiring

**Files:**
- Modify: `frontend/trial.js` (add top-level state + `_wireConvToggle`, call it after rendering, and wire the Operator link).

**Goal:** Make the ⚙ checkbox functional + the "Operator: CID flow / Interactive" link clickable. The CSS already does the show/hide via `body.conv-gov-on` (Task 8); JS just adds/removes the class on toggle. The Operator link uses the existing tab-button click handler.

- [ ] **Step 1: Add the top-level state right next to `showRunLineage`**

In `frontend/trial.js`, find this line near line 48:

```js
let showRunLineage = false;
```

Change to:

```js
let showRunLineage = false;
let convShowGovInternals = false;
```

- [ ] **Step 2: Append `_wireConvToggle` in the Conversation section**

```js

/**
 * Wire the ⚙ Show governance internals checkbox + the "Operator: CID
 * flow / Interactive" anchor inside the conversation tab. Called after
 * each renderConversationTab innerHTML write (renderTrial flow).
 *
 * Toggle persistence model mirrors showRunLineage: top-level let,
 * re-applied to the body class on every wire pass.
 */
function _wireConvToggle() {
  const cb = document.getElementById("conv-gov-internals-cb");
  if (cb) {
    cb.checked = convShowGovInternals;
    cb.addEventListener("change", () => {
      convShowGovInternals = cb.checked;
      document.body.classList.toggle("conv-gov-on", convShowGovInternals);
    });
  }
  document.body.classList.toggle("conv-gov-on", convShowGovInternals);

  // Operator link: switch to the cidflow-interactive tab using the same
  // mechanism the tab buttons use. The link has data-tab-target attribute.
  document.querySelectorAll(".conv-link[data-tab-target]").forEach(a => {
    a.addEventListener("click", (e) => {
      e.preventDefault();
      const target = a.dataset.tabTarget;
      const btn = document.querySelector(`.trial-tab-btn[data-tab="${target}"]`);
      if (btn) btn.click();
    });
  });
}
```

- [ ] **Step 3: Call `_wireConvToggle` after writing innerHTML**

In `frontend/trial.js`, find the line we added in Task 2:

```js
  tabContents.conversation.innerHTML = renderConversationTab(trial);
```

Add ONE line directly after:

```js
  tabContents.conversation.innerHTML = renderConversationTab(trial);
  _wireConvToggle();
```

- [ ] **Step 4: Verify in the browser**

Hard-refresh a trial page with anomalies (e.g., a contaminated multi-CID trial from earlier in the session, or one with orphan tools). Expected:
- ⚙ checkbox is unchecked by default.
- The `.conv-gov-internals` blocks (under each llm_request/llm_response/tool_call) are hidden.
- Checking ⚙ reveals all those blocks: full `rid`, `parent_rid_sources`, `parent_run_rid`, full `mcp-session-id`, snapshot hash, `provider_response_id`.
- Unchecking hides them again.
- Clicking "or open Operator: CID flow / Interactive →" switches to the existing cidflow-interactive tab.
- Toggle state survives the SSE poll re-render (clicking other tabs and coming back should preserve the on/off state).

- [ ] **Step 5: Commit**

```bash
git add frontend/trial.js
git commit -m "feat(frontend): wire ⚙ Show governance internals toggle + Operator link

Adds top-level convShowGovInternals state mirroring the showRunLineage
pattern, _wireConvToggle helper that re-applies state after each
innerHTML write and binds the checkbox change handler. The CSS visibility
rule (Task 8) drives off body.conv-gov-on. Operator link uses the
existing trial-tab-btn click handler — no new tab-switch logic.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Task 10: Extend `scripts/smoke_rid.sh` with HTML-scrape assertions

**Files:**
- Modify: `scripts/smoke_rid.sh` (append a new Python block after the existing RID-shape Python block).

**Goal:** Per spec §9.1, after the existing RID-shape assertions pass, fetch `trial.html` and assert the Conversation tab static structure: button present + default-active, tab-content div present + default-active, and (via grep on the served JS) that the rendering functions are present. The smoke can't run the JS rendering, so we only verify *static* HTML structure + that the JS code is shipped; the actual rendered output is verified manually post-smoke.

- [ ] **Step 1: Append the new assertions to `scripts/smoke_rid.sh`**

Open `scripts/smoke_rid.sh` and append AFTER the closing `PY` of the existing Python heredoc (line 118), BEFORE `EOF` or end-of-file:

```bash

# ── Conversation View HTML-scrape pass ──
# After the RID-shape audits pass, verify the static structure of trial.html
# carries the Conversation tab as the first tab + default-active, and that
# the JS file ships the renderConversationTab + buildConversationTree
# entrypoints. The smoke can't execute JS to verify the rendered output, so
# we cover that via manual visual checks listed at the end.
echo ""
echo "─── Conversation View HTML-scrape ───"
html_url="$API/trial.html"
js_url="$API/trial.js"
css_url="$API/style.css"

# trial.html: Conversation button is first + default-active; the tab-content
# div is first + default-active.
html=$(curl -fsS "$html_url" 2>/dev/null) \
  || { echo "❌ failed to fetch trial.html for scrape" >&2; exit 4; }
echo "$html" | grep -qE 'class="trial-tab-btn active" data-tab="conversation"' \
  || { echo "❌ trial.html: Conversation tab button missing default-active class" >&2; exit 4; }
echo "$html" | grep -qE '<div id="tab-conversation" class="tab-content active"' \
  || { echo "❌ trial.html: tab-conversation content missing default-active class" >&2; exit 4; }
echo "$html" | awk '/<div class="trial-tabs">/,/<\/div>/' | head -2 | tail -1 | grep -q 'data-tab="conversation"' \
  || { echo "❌ trial.html: Conversation button is not the FIRST tab" >&2; exit 4; }

# trial.js: required functions are exported (defined at file level).
js=$(curl -fsS "$js_url" 2>/dev/null) \
  || { echo "❌ failed to fetch trial.js for scrape" >&2; exit 4; }
for fn in renderConversationTab buildConversationTree detectTurnAnomalies generateElevatorPitch extractAgentText _wireConvToggle; do
  echo "$js" | grep -qE "^function ${fn}\\b" \
    || { echo "❌ trial.js: function ${fn} not found" >&2; exit 4; }
done
echo "$js" | grep -q 'let convShowGovInternals' \
  || { echo "❌ trial.js: convShowGovInternals top-level state not found" >&2; exit 4; }

# style.css: required class rules ship.
css=$(curl -fsS "$css_url" 2>/dev/null) \
  || { echo "❌ failed to fetch style.css for scrape" >&2; exit 4; }
for cls in '\.conv-badge' '\.conv-cid' '\.conv-turn' '\.conv-llm' '\.conv-tool' '\.conv-gov-internals' '\.conv-multicid-banner' '\.conv-findings' '@keyframes conv-target-ring'; do
  echo "$css" | grep -qE "$cls" \
    || { echo "❌ style.css: rule ${cls} not found" >&2; exit 4; }
done

echo "✅ Conversation View HTML-scrape PASSED"
echo ""
echo "─── Manual visual checklist (open the trial in a browser) ───"
echo "Trial: $API/trial.html?id=$trial_id"
echo "  □ Conversation tab is leftmost and active on load."
echo "  □ Header shows auto-generated pitch (one line, with ✓/⚠/✗ badge)."
echo "  □ Each turn shows 👤 User / 🤖 Agent preview (or graceful 'no text' fallback)."
echo "  □ LLM runs paired (request + response per rid); tool_calls under llm_response."
echo "  □ ⚙ Show governance internals: OFF hides internals; ON reveals rid / parent_run_rid / snapshot_hash / etc."
echo "  □ If trial has > 1 CID: multi-CID banner present + red."
echo "  □ Findings panel auto-open when anomalies > 0; clicking a link scrolls + flashes the target."
echo "  □ 'or open Operator: CID flow / Interactive →' link switches to that tab."
```

- [ ] **Step 2: Make sure the script is still executable**

Run: `ls -la scripts/smoke_rid.sh`

Expected: shows `-rwxr-xr-x` (executable bit set). If not, `chmod +x scripts/smoke_rid.sh`.

- [ ] **Step 3: Run the extended smoke**

Run: `bash scripts/smoke_rid.sh`

Expected: all existing RID-shape assertions pass, THEN the new "Conversation View HTML-scrape" block prints `✅ Conversation View HTML-scrape PASSED`, followed by the manual visual checklist with the actual trial URL filled in.

If any HTML-scrape assertion fails, the script exits with code 4 and prints `❌` with the specific reason.

- [ ] **Step 4: Commit**

```bash
git add scripts/smoke_rid.sh
git commit -m "test(smoke): HTML-scrape pass for Conversation view static structure

Extends scripts/smoke_rid.sh with a static-structure pass over trial.html /
trial.js / style.css after the existing RID-shape assertions: verifies the
Conversation tab is first + default-active, the JS file ships the
renderConversationTab / buildConversationTree / detectTurnAnomalies /
generateElevatorPitch / extractAgentText / _wireConvToggle entrypoints
and the convShowGovInternals top-level state, and the CSS ships the
.conv-* class rules and :target ring keyframes. Prints a manual visual
checklist with the actual trial URL at the end.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Task 11: End-to-end smoke + manual visual verification + cleanup

**Files:** (verification only; one final cleanup commit if any drift surfaces)

**Goal:** Run the full smoke against a fresh trial, then walk through the manual checklist from the smoke output. Catch anything the static scrape couldn't.

- [ ] **Step 1: Run the smoke**

Run: `bash scripts/smoke_rid.sh`

Expected: both the RID-shape pass and the Conversation View HTML-scrape pass print `✅`. The manual checklist prints the trial URL.

- [ ] **Step 2: Walk the manual checklist**

Open the trial URL in a browser, hard-refresh (Ctrl-Shift-R). For each item in the checklist printed by the smoke:

- Conversation tab is leftmost and active on load. → Pass / Fail.
- Header shows auto-generated pitch (one line, with ✓/⚠/✗ badge). → Pass / Fail.
- Each turn shows 👤 User / 🤖 Agent preview (or graceful 'no text' fallback). → Pass / Fail.
- LLM runs paired (request + response per rid); tool_calls under llm_response. → Pass / Fail.
- ⚙ Show governance internals: OFF hides internals; ON reveals rid / parent_run_rid / snapshot_hash / etc. → Pass / Fail.
- If trial has > 1 CID: multi-CID banner present + red. → Pass / Fail / N/A.
- Findings panel auto-open when anomalies > 0; clicking a link scrolls + flashes the target. → Pass / Fail / N/A (no anomalies in this trial).
- "or open Operator: CID flow / Interactive →" link switches to that tab. → Pass / Fail.

If any item fails, diagnose, fix in the appropriate task's files, re-verify, and commit the fix with a `fix(frontend): …` message. Do NOT amend any prior commit.

- [ ] **Step 3: Final state check**

Run: `git status --short`

Expected: clean working tree (no uncommitted changes).

Run: `git log --oneline ^main HEAD 2>/dev/null || git log --oneline -12`

Expected: the last 10-11 commits include each task's commit in order, with no fixup/squash needed.

- [ ] **Step 4: No commit if nothing to commit**

If Step 2 found nothing to fix, there's no Task 11 commit. If a fix was needed, that fix's own `fix(frontend): …` commit completes Task 11.

---

## Spec coverage check

| Spec section | Implemented by |
|---|---|
| §2.1 New Conversation tab, default-active, leftmost | Task 1 (HTML), Task 2 (JS wiring) |
| §2.2 Topology tree (CID → turn → llm/tool) | Task 4 (data model), Task 7 (renderer) |
| §2.3 Per-turn + per-CID anomaly-scoped badges | Task 5 (detect), Task 7 (render) |
| §2.4 Findings panel with anchor links | Task 5 (build findings), Task 7 (render), Task 8 (CSS), Task 9 (anchor scroll via :target) |
| §2.5 Auto-generated elevator-pitch header | Task 6 |
| §2.6 ⚙ Show governance internals checkbox | Task 7 (render), Task 8 (CSS), Task 9 (wire) |
| §2.7 Multi-CID rendering (multiple roots + banner) | Task 4 (cids map), Task 5 (multiCidAnomaly badge), Task 7 (banner + multiple roots) |
| §3 Non-goals (no tokens, no share-link, no replaced tabs, no JS test framework, no backend) | enforced by all tasks; tested by Task 10 (only existing tabs verified) |
| §4 Files touched (trial.html, trial.js, style.css, smoke_rid.sh) | Tasks 1 / 2-7 + 9 / 8 / 10 |
| §5 Data model | Task 4 (with helpers), Task 5 (anomaly + finding fill-in) |
| §5.1 Build algorithm | Task 4 |
| §5.2 is_turn_boundary as consistency check | Task 5 (`turn_boundary_mismatch` raising) |
| §6.1 Anomaly inventory | Task 5 (each row implemented in detectTurnAnomalies) |
| §6.2 Propagation | Task 5 (turn → cid → trial badge promotion) |
| §6.3 Findings + anomalies-rise rendering | Task 5 (build findings), Task 7 (render), Task 8 (:target ring, anomaly-class tint) |
| §7 DOM structure | Task 7 (matches the example exactly: header, banner, findings section, .conv-cid > .conv-turn > .conv-llm-list > .conv-llm / .conv-tools / .conv-tool, anchor ids) |
| §8 Elevator pitch templates + building blocks | Task 6 |
| §9.1 Integration-only smoke testing | Task 10 (HTML scrape + manual checklist) |
| §9.3 Visual / manual checks | Task 11 (final walk) |
| §10 Older trials render with all-orphan tools (correct behavior) | Task 4 strict orphan rule; Task 5 raises anomaly; observable in the rendered output |
| §11.1 Tab move mechanics audit | Task 1 verification (grep for single `active` per type), Task 2 (renderTrial call order) |
| §11.2 Agent text parser hand-off | Task 3 (fresh helper, not factored — spec §11 hedged "if it isn't already"; renderTurnCard doesn't have such logic) |
| §11.3 Snapshot orphan placement | Task 4 (per-tool match attempts; unmatched → turn-level anomaly) |

No gaps.

---

## Cross-references

- **Spec:** `docs/superpowers/specs/2026-05-31-customer-conversation-view-design.md` (committed `738e213`)
- **Prerequisite Design B spec + plan (RID infrastructure this view leverages):** `docs/superpowers/specs/2026-05-20-run-identity-design.md`, `docs/superpowers/plans/2026-05-20-run-identity-plan.md`
- **AGW canonical cidgar spec (CHG-26F + CHG-26G synced):** `features/2026-04-19-governance-cidgar/spec.md` on `ibfork/docs` (sync commit `3af1b118`)
- **Brainstorm conversation:** aiplay `docs/conversation-log.md` entries from 2026-05-31
- **Execution skill:** `superpowers:subagent-driven-development` (matches the working pattern used to ship Design A + Design B)
