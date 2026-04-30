# CID flow — `mcp-session-id` labels Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Surface the `mcp-session-id` on `tools_list` and `tool_call` audit nodes in both CID flow tabs (Mermaid + cytoscape) by joining each audit to the same-turn framework_event of the matching MCP type.

**Architecture:** Frontend-only change to `frontend/trial.js`. Two new module-scope pure helpers (`_decodeMcpSessionAlias`, `_correlateTurnAuditSessions`); extension of the existing topology object returned by `_buildCidFlowTopology` with per-audit `sid` / `sidFull` fields; renderer updates in `renderCidFlowTab` (Mermaid) and `_buildAndMountCytoscape` (cytoscape); legend update in both tabs. No backend, no schema, no new vendor libs.

**Tech Stack:** Vanilla ES2020 JS, Mermaid (vendored), cytoscape.js (vendored). No JS test runner — verification is by console snippet for pure logic and manual browser inspection for rendering.

**Spec:** `docs/superpowers/specs/2026-04-30-cid-flow-mcp-session-labels-design.md`

**File map:**
- Modify: `frontend/trial.js` (add 2 helpers, extend 1 helper, update 2 renderers, update 2 legends, register 1 global no-op)

---

## Task 1: Add `_decodeMcpSessionAlias` helper

**Files:**
- Modify: `frontend/trial.js` (insert near other CID-flow helpers, before `_scanBodyForCids` at line ~1140)

**What it does.** Pure function that takes a raw `mcp-session-id` header value and returns `{alias, full}` where `alias` is the last 6 hex chars of the inner mutable hash (decoded from base64url → JSON → `s[0].s`), with a fallback to the last 6 chars of the raw header when decode fails. `full` is always the raw input. Returns `null` if input is falsy.

- [ ] **Step 1: Add the helper**

Insert the following function in `frontend/trial.js` directly above the comment `// ── CID flow tab ──` (around line 1124):

```javascript
// Decode an mcp-session-id header value into a short alias + full id pair.
// Header format observed in framework_events: base64url-encoded JSON of shape
//   {"t":"mcp","s":[{"t":"mutable","s":"<hex>"}]}
// Returns {alias: <last-6-hex>, full: <raw>} on success; on decode failure
// falls back to {alias: <last-6-of-raw>, full: <raw>}. Returns null when
// the input is empty/null.
function _decodeMcpSessionAlias(raw) {
  if (!raw || typeof raw !== "string") return null;
  // base64url → base64 → atob; pad to multiple of 4.
  let b64 = raw.replace(/-/g, "+").replace(/_/g, "/");
  while (b64.length % 4) b64 += "=";
  try {
    const decoded = atob(b64);
    const parsed = JSON.parse(decoded);
    const inner = parsed && parsed.s && parsed.s[0] && parsed.s[0].s;
    if (typeof inner === "string" && inner.length >= 6) {
      return {alias: inner.slice(-6), full: raw};
    }
  } catch (_e) {
    // fall through to fallback
  }
  return {alias: raw.slice(-6), full: raw};
}
```

- [ ] **Step 2: Verify in browser console**

Open `frontend/trial.html` (served via the project's existing dev server) and load any trial. In the browser console:

```javascript
// Sample real header from data/trials/001e019f-...json
_decodeMcpSessionAlias("eyJ0IjoibWNwIiwicyI6W3sidCI6Im11dGFibGUiLCJzIjoiZGE1YzA3OGM0YmRjNGE3MTg4N2M1ZGZjM2JhYzk2N2MifV19")
// Expected: {alias: "ac967c", full: "eyJ0IjoibWNwIiwic..."}

_decodeMcpSessionAlias(null)            // → null
_decodeMcpSessionAlias("")              // → null
_decodeMcpSessionAlias("not-base64!!")  // → {alias: "ase64!!", full: "not-base64!!"}  (fallback path; the raw last-6 chars include any non-hex)
```

Verify the alias is `"ac967c"` for the sample. (Decoded JSON: `{"t":"mcp","s":[{"t":"mutable","s":"da5c078c4bdc4a71887c5dfc3bac967c"}]}` → last 6 of the inner hash.)

Note: the helper is module-scope so it isn't on `window` by default. For console testing, temporarily add `window._decodeMcpSessionAlias = _decodeMcpSessionAlias;` after the function (remove before commit).

- [ ] **Step 3: Commit**

```bash
git add frontend/trial.js
git commit -m "feat(aiplay): _decodeMcpSessionAlias helper for CID flow session labels"
```

---

## Task 2: Add `_correlateTurnAuditSessions` helper

**Files:**
- Modify: `frontend/trial.js` (insert directly below `_decodeMcpSessionAlias`)

**What it does.** Pure function that takes one trial turn and its corresponding list of audit entries (already pre-filtered to that turn) and returns a `Map<auditOriginalIdx, {alias, full}>` mapping each audit's index (within the trial-wide audit list) to its session id pair, or omits the audit when no correlation is possible. Correlation rule: ordered same-turn matching — the kth `tools_list` audit binds to the kth `mcp_tools_list` framework_event in that turn, same for `tool_call` ↔ `mcp_tools_call`.

- [ ] **Step 1: Add the helper**

Insert directly below `_decodeMcpSessionAlias`:

```javascript
// Build a map from trial-global audit index → {alias, full} for the given
// turn's audits. Correlation: for each audit phase we care about
// (tools_list, tool_call) walk that turn's framework_events of the
// corresponding `t` value in order; the kth audit binds to the kth event.
// Audits with no matching event get no entry in the result. Read the
// session id from event.request.headers["mcp-session-id"] first, then
// event.response.headers["mcp-session-id"].
//
// Args:
//   turn: trial.turns[i] — must have .framework_events array
//   auditIndexedPairs: array of [globalAuditIdx, auditEntry] for audits
//     belonging to this turn (caller filters by header-demux or time-window;
//     same picker the rest of the CID flow uses)
// Returns: Map<number, {alias: string, full: string}>
function _correlateTurnAuditSessions(turn, auditIndexedPairs) {
  const out = new Map();
  const fes = (turn && turn.framework_events) || [];

  // Build ordered framework_event lists per MCP type we map.
  const eventsByType = {
    mcp_tools_list: [],
    mcp_tools_call: [],
  };
  for (const fe of fes) {
    if (eventsByType[fe.t] !== undefined) eventsByType[fe.t].push(fe);
  }

  // Audit-phase → framework_event-type binding.
  const phaseToType = {
    tools_list: "mcp_tools_list",
    tool_call:  "mcp_tools_call",
  };

  // Per-phase ordinal counters as we walk the audits.
  const counters = {tools_list: 0, tool_call: 0};

  for (const [globalIdx, audit] of auditIndexedPairs) {
    const phase = audit && audit.phase;
    const feType = phaseToType[phase];
    if (!feType) continue;
    const k = counters[phase]++;
    const fe = eventsByType[feType][k];
    if (!fe) continue;
    const headers =
      (fe.request  && fe.request.headers)  ||
      (fe.response && fe.response.headers) || {};
    // Try request headers first, then response headers, for the session id.
    const reqH = (fe.request  && fe.request.headers)  || {};
    const resH = (fe.response && fe.response.headers) || {};
    const raw = reqH["mcp-session-id"] || resH["mcp-session-id"] || null;
    const decoded = _decodeMcpSessionAlias(raw);
    if (decoded) out.set(globalIdx, decoded);
  }
  return out;
}
```

- [ ] **Step 2: Verify in browser console**

Load a trial that has multiple `tool_call` audits (e.g. `001e019f-a483-4ee8-a744-417a7ecac024`). Temporarily expose the helper: add `window._correlateTurnAuditSessions = _correlateTurnAuditSessions;` after the function. Reload, then in console:

```javascript
const trial = window.__lastTrialForCy;
const turn = trial.turns[1];                         // turn 1 has mcp_tools_call
const audits = trial.audit_entries
  .map((a, i) => [i, a])
  .filter(([_, a]) => {
    const ts = a.captured_at || "";
    return ts >= (turn.started_at || "") && ts <= (turn.finished_at || "9999");
  });
_correlateTurnAuditSessions(turn, audits);
// Expected: Map with at least one entry; values like {alias: "...", full: "eyJ0..."}
```

Confirm the returned aliases match the framework_event headers when you eyeball them with:

```javascript
turn.framework_events.filter(fe => fe.t === "mcp_tools_call")
  .map(fe => fe.request?.headers?.["mcp-session-id"]);
```

Remove the temporary `window.` exposure before commit.

- [ ] **Step 3: Commit**

```bash
git add frontend/trial.js
git commit -m "feat(aiplay): _correlateTurnAuditSessions helper for CID flow session labels"
```

---

## Task 3: Extend `_buildCidFlowTopology` with per-audit session fields

**Files:**
- Modify: `frontend/trial.js` lines ~1165-1252 (the `_buildCidFlowTopology` function)

**What it does.** Run `_correlateTurnAuditSessions` once per turn using the same audit-picking rule the function already uses (header-demux or time-window — code already inside the function), and attach `sid` / `sidFull` fields to each entry in the returned `audits` array. Audits without a session match get `sid: null, sidFull: null`. No other fields change.

- [ ] **Step 1: Build the per-audit session map alongside the existing turn↔audit correlation**

Locate the block (currently lines ~1198–1214) that computes `turnToAudit`. Right after that loop, add the session-correlation pass — it reuses the same `match` predicate so the audits we feed `_correlateTurnAuditSessions` are exactly those the graph already considers part of the turn.

Replace the existing block:

```javascript
  // Header-demux vs time-window correlation for turn↔audit edges.
  const headerDemux = audits.some(a => a.turn_id);
  const turnToAudit = [];
  turns.forEach((t, i) => {
    audits.forEach((a, j) => {
      let match = false;
      if (headerDemux) {
        match = (a.turn_id && a.turn_id === t.turn_id);
      } else {
        const ts = a.captured_at || "";
        const start = t.started_at || "";
        const end = t.finished_at || "9999";
        match = (ts >= start && ts <= end);
      }
      if (match) turnToAudit.push({turnIdx: i, auditIdx: j});
    });
  });
```

With:

```javascript
  // Header-demux vs time-window correlation for turn↔audit edges.
  // Per-turn we also build the audit-index → session-id map (used to label
  // tools_list / tool_call audit nodes with their mcp-session-id).
  const headerDemux = audits.some(a => a.turn_id);
  const turnToAudit = [];
  const auditSessions = new Map();  // global auditIdx → {alias, full}
  turns.forEach((t, i) => {
    const turnAuditPairs = [];
    audits.forEach((a, j) => {
      let match = false;
      if (headerDemux) {
        match = (a.turn_id && a.turn_id === t.turn_id);
      } else {
        const ts = a.captured_at || "";
        const start = t.started_at || "";
        const end = t.finished_at || "9999";
        match = (ts >= start && ts <= end);
      }
      if (match) {
        turnToAudit.push({turnIdx: i, auditIdx: j});
        turnAuditPairs.push([j, a]);
      }
    });
    const perTurnSessions = _correlateTurnAuditSessions(t, turnAuditPairs);
    for (const [k, v] of perTurnSessions) auditSessions.set(k, v);
  });
```

- [ ] **Step 2: Attach `sid` / `sidFull` to each audit entry in the returned topology**

Locate the return statement (currently around lines ~1227-1251). Replace the `audits:` line:

```javascript
    audits: audits.map((a, i) => ({
      idx: i, phase: a.phase || "audit", cid: a.cid || null,
      ss: _auditSnapshotHash(a),
    })),
```

With:

```javascript
    audits: audits.map((a, i) => {
      const s = auditSessions.get(i) || null;
      return {
        idx: i, phase: a.phase || "audit", cid: a.cid || null,
        ss: _auditSnapshotHash(a),
        sid:     s ? s.alias : null,
        sidFull: s ? s.full  : null,
      };
    }),
```

- [ ] **Step 3: Verify in browser console**

Load a trial with MCP traffic (e.g. `001e019f-a483-4ee8-a744-417a7ecac024`). In console:

```javascript
const trial = window.__lastTrialForCy;
// _buildCidFlowTopology is module-scope; expose temporarily:
//   window._buildCidFlowTopology = _buildCidFlowTopology;  (then reload)
const topo = _buildCidFlowTopology(trial);
topo.audits.filter(a => a.phase === "tool_call" || a.phase === "tools_list")
  .map(a => ({phase: a.phase, sid: a.sid, sidFull: a.sidFull?.slice(0, 24)}));
// Expected: each entry has a non-null sid (6 chars) and a non-null sidFull
// matching the corresponding framework_event header value.
```

Also load a trial with no MCP traffic (any `mcp_admin`-only trial) and verify all audits show `sid: null, sidFull: null`.

Remove temporary `window.` exposure before commit.

- [ ] **Step 4: Commit**

```bash
git add frontend/trial.js
git commit -m "feat(aiplay): topology helper attaches mcp-session-id alias to audits"
```

---

## Task 4: Render session label + tooltip in Mermaid CID flow tab

**Files:**
- Modify: `frontend/trial.js` lines ~1299-1304 (audit-node emission inside `renderCidFlowTab`)
- Modify: `frontend/trial.js` (one-time global no-op callback registration; place near the top of the file by other module-scope state, around line 32)

**What it does.** Mermaid audit nodes get a two-line label (`tool_call\n<sid>`) and a `click` directive that registers a tooltip with the full id. Audits without a session id render unchanged.

- [ ] **Step 1: Register the global no-op callback once at module scope**

Add the following near the existing module-scope state (around line 32, after the cytoscape state vars):

```javascript
// Mermaid `click <node> callback "<tooltip>"` requires a callback function.
// We don't need any actual click behavior here — the tooltip text is what
// we're after. One global no-op satisfies Mermaid's syntax check.
if (typeof window !== "undefined" && !window.__cidFlowNoop) {
  window.__cidFlowNoop = function () {};
}
```

- [ ] **Step 2: Update audit-node emission in `renderCidFlowTab`**

Locate the existing audit loop (currently lines ~1299-1304):

```javascript
  // Audit entry nodes — phase is the most useful label; fall back to "audit".
  for (const a of tAudits) {
    const phase = a.phase.replace(/[\[\]"]/g, "");
    mer += `  A${a.idx}["${phase}"]\n`;
    mer += `  class A${a.idx} auditNode\n`;
  }
```

Replace with:

```javascript
  // Audit entry nodes — phase is the most useful label; fall back to "audit".
  // When the audit corresponds to an MCP call we joined to a framework_event,
  // append the mcp-session-id alias on a second line and emit a Mermaid
  // `click` directive so hover shows the full id.
  for (const a of tAudits) {
    const phase = a.phase.replace(/[\[\]"]/g, "");
    const label = a.sid ? `${phase}\n${a.sid}` : phase;
    mer += `  A${a.idx}["${label}"]\n`;
    mer += `  class A${a.idx} auditNode\n`;
    if (a.sidFull) {
      // Escape any double-quotes in the tooltip; they'd break Mermaid's
      // string parser. Real session-id values are base64url so this is
      // mostly defensive.
      const tip = a.sidFull.replace(/"/g, '\\"');
      mer += `  click A${a.idx} __cidFlowNoop "mcp-session-id: ${tip}"\n`;
    }
  }
```

- [ ] **Step 3: Verify in browser**

Load a trial with MCP traffic (e.g. `001e019f-...`). Open the **CID flow** tab.

Expected:
- `tool_call` and `tools_list` audit nodes show two-line labels with a 6-char alias on line 2.
- Hovering each labeled node shows a tooltip starting `mcp-session-id: eyJ0...`.
- Other audit nodes (`llm_request`, `tool_planned`, `tool_response`, `terminal`) render unchanged.

Also load an `mcp_admin`-only trial: audit nodes (if any) render unchanged from baseline.

- [ ] **Step 4: Commit**

```bash
git add frontend/trial.js
git commit -m "feat(aiplay): CID flow Mermaid — session-id label + hover tooltip on MCP audits"
```

---

## Task 5: Render session label + tooltip in cytoscape CID flow tab

**Files:**
- Modify: `frontend/trial.js` lines ~1538-1543 (audit-node `data` in `_buildAndMountCytoscape`)
- Modify: `frontend/trial.js` lines ~1656-1657 (after the `style:` array closes, before toolbar wiring)

**What it does.** Cytoscape audit nodes get the same two-line label and a `sidFull` field on `data`. A `mouseover` / `mouseout` handler on `node[sidFull]` writes the cytoscape container's `title` attribute to surface the full id as a native browser tooltip.

- [ ] **Step 1: Update audit-node element data**

Locate the existing audit-node loop (currently lines ~1538-1543):

```javascript
  // Audit nodes
  for (const a of topo.audits) {
    elements.push({
      data: {id: `A${a.idx}`, label: a.phase},
      classes: "node-audit",
    });
  }
```

Replace with:

```javascript
  // Audit nodes — when the audit corresponds to an MCP call we joined to
  // a framework_event, append the mcp-session-id alias on a second line
  // and stash the full id on data.sidFull for the hover-tooltip handler.
  for (const a of topo.audits) {
    const label = a.sid ? `${a.phase}\n${a.sid}` : a.phase;
    elements.push({
      data: {
        id: `A${a.idx}`,
        label,
        sidFull: a.sidFull || null,
      },
      classes: "node-audit",
    });
  }
```

- [ ] **Step 2: Wire the hover tooltip handler**

Locate the line right after `cy.fit(undefined, 30)` and the resetBtn handler block, just before `return cy;` (around line 1685). Insert:

```javascript
  // Native-browser tooltip for audits that carry a session id. Set the
  // container's `title` attr on hover; clear on mouseout. Browser shows
  // its default tooltip after the usual hover delay. No vendor lib needed.
  cy.on("mouseover", "node[sidFull]", evt => {
    const sf = evt.target.data("sidFull");
    if (sf) container.setAttribute("title", `mcp-session-id: ${sf}`);
  });
  cy.on("mouseout", "node[sidFull]", () => {
    container.removeAttribute("title");
  });
```

- [ ] **Step 3: Verify in browser**

Load the same trial used in Task 4. Open the **CID flow (interactive)** tab.

Expected:
- `tool_call` and `tools_list` audit nodes show two-line labels with the 6-char alias on line 2.
- Hovering an MCP audit node sets the cytoscape container's `title` attribute (visible in DevTools → Elements as `title="mcp-session-id: eyJ0..."`); the browser shows the native tooltip after a short delay.
- Hovering a non-MCP audit node (or anywhere else) does not set the title.
- Drag, zoom, layout switch all still work.

- [ ] **Step 4: Commit**

```bash
git add frontend/trial.js
git commit -m "feat(aiplay): CID flow cytoscape — session-id label + hover tooltip on MCP audits"
```

---

## Task 6: Update legends in both CID flow tabs

**Files:**
- Modify: `frontend/trial.js` Mermaid tab legend block (around lines ~1378-1411)
- Modify: `frontend/trial.js` cytoscape tab legend block (around lines ~1474-1488)

**What it does.** One short line added to each legend explaining the new audit-node second-line suffix. Wording is identical between tabs.

- [ ] **Step 1: Add the legend line in the Mermaid tab**

Locate the existing legend `<div class="cid-flow-legend">` block in `renderCidFlowTab` (around lines ~1378-1380). After the existing `Dotted` `<div>` line and before `<details class="cid-flow-help">`, insert:

```javascript
        <div><span class="legend-glyph">↪</span> <strong>Audit node second line</strong> (e.g. <code>c967c</code> under <code>tool_call</code>) — last 6 chars of the <code>mcp-session-id</code> for that MCP call. Hover for the full id.</div>
```

- [ ] **Step 2: Add the legend line in the cytoscape tab**

Locate the cytoscape tab's legend block (around lines ~1474-1488). Inside the `<div class="cid-flow-help-body">`, after the closing `</ul>` and before the `<p>Edges:` line, insert:

```javascript
            <p><strong>Audit node second line</strong> (e.g. <code>c967c</code> under <code>tool_call</code>) — last 6 chars of the <code>mcp-session-id</code> for that MCP call. Hover for the full id.</p>
```

- [ ] **Step 3: Verify in browser**

Load any trial with MCP traffic. Open both **CID flow** and **CID flow (interactive)** tabs. Confirm the new legend line is visible and reads correctly in each.

- [ ] **Step 4: Commit**

```bash
git add frontend/trial.js
git commit -m "feat(aiplay): CID flow legends — explain session-id suffix on audit nodes"
```

---

## Task 7: End-to-end verification across multiple trials

**Files:** none (verification only).

**What it does.** Sanity-check the change against a representative spread of real trial data so we catch any case the synthetic walks missed.

- [ ] **Step 1: Verify on a multi-turn MCP trial**

Open trial `001e019f-a483-4ee8-a744-417a7ecac024` (4 turns, ~21 audits). On both CID flow tabs:
- Each `tool_call` and `tools_list` audit node shows a 6-char alias on line 2.
- Aliases group naturally per turn (turn 0's MCP audits share one alias, turn 1's share a different alias, etc.).
- Each alias's tooltip starts `mcp-session-id: eyJ0...`.

- [ ] **Step 2: Verify on a no-MCP trial**

Open any trial whose `framework_events` are empty or non-MCP-only (e.g. an `097ddd3d-...`-style trial with `audits=0`). Both tabs render exactly as they did before the change — no extra labels, no tooltips.

- [ ] **Step 3: Verify cache behavior**

Switch tabs back and forth (Turns → CID flow → Turns → CID flow) on the multi-turn trial. The CID flow tab should not re-render unnecessarily (existing `__cidFlowLastSourceHash` cache still applies because the topology is deterministic from the trial JSON). Inspect the console for any unexpected re-mount warnings.

- [ ] **Step 4: Verify Mermaid `click` directive does not trigger a click side-effect**

On the Mermaid CID flow tab, click an audit node that carries a session label. Expected: nothing happens (the registered callback is `__cidFlowNoop`). No console error.

- [ ] **Step 5: Final commit (if any cleanup needed)**

If steps 1-4 surfaced any minor fixes, commit them:

```bash
git add frontend/trial.js
git commit -m "fix(aiplay): CID flow session-id labels — <specific fix>"
```

If no fixes are needed, this task is a no-op.

---

## Self-review notes

**Spec coverage:**
- Decode rule (base64url → JSON → `s[0].s` → last 6) — Task 1.
- Correlation rule (kth audit ↔ kth framework_event per phase) — Task 2.
- Header read order (request first, response fallback) — Task 2 step 1.
- `sid` / `sidFull` on each audit entry — Task 3.
- Two-line label, no tinting — Tasks 4 & 5.
- Mermaid tooltip via `click ... callback` — Task 4.
- Cytoscape tooltip via container `title` on mouseover — Task 5.
- Legend update — Task 6.
- Caching unchanged — Task 7 step 3 verifies.
- Empty / degenerate cases — Task 7 step 2 verifies (no-MCP trial).

**Type consistency:**
- Helper return shape `{alias, full}` consistent across Tasks 1, 2, 3.
- Topology audit fields `sid` (string|null) and `sidFull` (string|null) consistent across Tasks 3, 4, 5.
- Global no-op named `__cidFlowNoop` consistent between Task 4 step 1 and Task 4 step 2.

**Placeholder scan:** none. All steps include literal code or explicit verification commands.
