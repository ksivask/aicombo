# Design C2 — RID Lineage Overlay in CID Flow Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans. Steps use checkbox (`- [ ]`) syntax. NOTE: the frontend has no automated test harness — verification is `node --check` (syntax) + manual visual. There are no failing-test-first steps.

**Goal:** Add an opt-in "Show run lineage" overlay to both CID-flow tabs that draws the `rid → parent_rid` run chain on top of the existing CID flow.

**Architecture:** Frontend-only. A shared `buildRunLineage(trial)` helper derives the lineage model from `audit_entries[].body`; the Mermaid (`renderCidFlowTab`) and Cytoscape (`_buildAndMountCytoscape`) renderers consume it behind a module-level `showRunLineage` toggle. Off by default → base CID flow byte-identical.

**Tech Stack:** Vanilla JS (`frontend/trial.js`), HTML (`frontend/trial.html`), CSS (`frontend/style.css`), Mermaid 9.4.3, Cytoscape (UMD global). `node --check` for syntax.

**Spec:** `docs/superpowers/specs/2026-05-27-design-c2-rid-lineage-viz-design.md`

**Key facts (verified):**
- Served trial JSON: `trial.audit_entries[i].body.{rid, parent_rid, parent_rid_anomaly, is_turn_boundary}`; `parent_rid` may be `null` (genesis/truncation/pre-CHG-26F).
- `renderCidFlowTab(trial)` (trial.js ~1413) builds a Mermaid `graph LR` from `_buildCidFlowTopology(trial)` → `{turns, audits, cids, snapshots, edges, counts}`. Audit nodes are emitted as `A${a.idx}["${label}"]` + `class A${a.idx} auditNode`. classDefs at ~1517-1524.
- The interactive tab mounts via `_buildAndMountCytoscape` (same `cidNodeId`/`ssNodeId` helpers; shares node ids with the mermaid graph).
- Mermaid render is deferred/ cached: `runMermaidIfVisible("cidflow")`, `__cidFlowNeedsMermaid`, and a cidflow HTML render-cache (hash). Cytoscape: `__cidFlowInteractiveNeedsMount` + its own cache.

**Verification mechanism:** `node --check frontend/<file>.js`. Manual visual against a live trial in a browser at `trial.html?id=<id>` — ideally a **post-CHG-26F** trial (Task 6 / B-Task 19 produces it).

---

## File map
- **Modify** `frontend/trial.js` — `buildRunLineage` helper; `showRunLineage` state + checkbox wiring; Mermaid overlay in `renderCidFlowTab`; Cytoscape overlay in `_buildAndMountCytoscape`.
- **Modify** `frontend/trial.html` — (only if the checkbox must be a static element; preferred is JS-injected, so likely no change).
- **Modify** `frontend/style.css` — classes for rid label, dashed run-edge, anomaly node.

---

## Task 1: `buildRunLineage(trial)` helper

**Files:** Modify `frontend/trial.js` (add near `_buildCidFlowTopology`).

- [ ] **Step 1.1: Read the topology builder to get the audit-node idx scheme**

Run: `grep -n "_buildCidFlowTopology\|audits\b\|\.idx\b\|llm_request" frontend/trial.js | head -30`
Read `_buildCidFlowTopology` fully. Confirm: each audit in `topo.audits` has `.idx` (the `A${idx}` node id), `.phase`, and enough to recover the source `audit_entries` element (e.g. it's built in `audit_entries` order, or carries the original index). The helper must map a run's `rid` to the SAME `A${idx}` the mermaid/cytoscape graphs use.

- [ ] **Step 1.2: Implement `buildRunLineage`**

Add to `frontend/trial.js` (module scope, near the other cidflow helpers):

```javascript
// Design C2 — derive the RID run-lineage overlay model from a trial's audit
// entries, keyed to the SAME audit-node ids the CID-flow graphs use (A${idx}).
//
// Returns:
//   ridToNode:  Map<rid, "A${idx}">      — each run's node id
//   parentEdges: Array<["A${p}","A${c}"]> — resolvable parent_rid links
//   anomalyNodes: Set<"A${idx}">          — nodes with parent_rid_anomaly
//   labels:     Map<"A${idx}", shortRid>  — short rid label per run node
//
// Robust to null parent_rid (genesis/truncation/pre-CHG-26F): such links are
// simply omitted. `topoAudits` is `_buildCidFlowTopology(trial).audits` so the
// idx scheme matches exactly.
function buildRunLineage(trial, topoAudits) {
  const ridToNode = new Map();
  const labels = new Map();
  const anomalyNodes = new Set();
  const rows = [];  // {node, rid, parentRid}

  for (const a of topoAudits) {
    if (a.phase !== "llm_request") continue;
    const body = a.body || {};
    const rid = body.rid;
    if (!rid) continue;
    const node = `A${a.idx}`;
    ridToNode.set(rid, node);
    labels.set(node, _shortRid(rid));
    if (body.parent_rid_anomaly === true) anomalyNodes.add(node);
    rows.push({node, rid, parentRid: body.parent_rid || null});
  }

  const parentEdges = [];
  for (const r of rows) {
    if (!r.parentRid) continue;            // genesis / truncation / null
    const parentNode = ridToNode.get(r.parentRid);
    if (!parentNode) continue;             // parent not in this trial (skip)
    parentEdges.push([parentNode, r.node]);
  }

  return {ridToNode, parentEdges, anomalyNodes, labels};
}

function _shortRid(rid) {
  // "ibr_4a3590f66ec9" -> "ibr_…0f66ec9"; keep it compact for node labels.
  return rid.length > 11 ? `${rid.slice(0, 4)}…${rid.slice(-6)}` : rid;
}
```

NOTE: this requires `topo.audits` entries to carry `.body`. In Step 1.1, confirm whether `_buildCidFlowTopology` keeps `.body` on each audit; if NOT, add it there (one line: when building each audit topo entry, include `body: e.body`). Make that addition in this task if needed.

- [ ] **Step 1.3: Syntax check**

Run: `node --check frontend/trial.js`
Expected: no output (valid).

- [ ] **Step 1.4: Commit**

```bash
git add frontend/trial.js
git commit -m "feat(frontend): C2 buildRunLineage helper for RID overlay

Derives the run-lineage model (rid→node, parent_rid edges, anomaly nodes,
short labels) from the CID-flow topology audits, keyed to the same A\${idx}
node ids both graphs use. Pure function; null parent_rid omitted.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Task 2: `showRunLineage` toggle state + checkbox

**Files:** Modify `frontend/trial.js`.

- [ ] **Step 2.1: Add module-level state**

Near the other render-cache state vars (top of trial.js, ~line 23-40), add:

```javascript
// Design C2 — RID lineage overlay toggle (shared by both CID-flow tabs).
let showRunLineage = false;
```

- [ ] **Step 2.2: Inject the checkbox into the cidflow tab HTML**

In `renderCidFlowTab`, in the returned HTML (the `<div class="cid-flow">…` wrapper, near the `cid-flow-stats` banner ~line 1533), add a checkbox control:

```javascript
// (inside the returned template, above or beside the stats banner)
`<label class="run-lineage-toggle">
   <input type="checkbox" id="run-lineage-cb" ${showRunLineage ? "checked" : ""}>
   Show run lineage
 </label>`
```

The same control should appear on the interactive tab (Task 4 reuses the same id pattern or a sibling id `run-lineage-cb-cy`; use one shared handler).

- [ ] **Step 2.3: Wire the change handler**

Find where cidflow HTML is injected and tab listeners are bound (search `renderCidFlowTab(trial)` call ~line 610 and the tab-switch logic ~line 236). After injecting the cidflow HTML, bind:

```javascript
const cb = document.getElementById("run-lineage-cb");
if (cb) {
  cb.addEventListener("change", () => {
    showRunLineage = cb.checked;
    // Invalidate caches so both CID-flow tabs rebuild with/without overlay.
    __cidFlowNeedsMermaid = true;
    __cidFlowInteractiveNeedsMount = true;
    // Re-render the currently visible trial (reuse the existing refresh path).
    if (typeof __lastTrialForCy !== "undefined" && __lastTrialForCy) {
      renderTrial(__lastTrialForCy);   // or the established re-render entry point
    }
  });
}
```

In Step 2.1's exploration, confirm the exact re-render entry point (the function the poll/SSE loop calls — likely `renderTrial`). Use it. The cidflow render-cache hash MUST incorporate `showRunLineage` so a toggle actually re-renders — verify the cache key (search the cidflow cache hash compute) and include `showRunLineage` in it.

- [ ] **Step 2.4: Syntax check + commit**

```bash
node --check frontend/trial.js
git add frontend/trial.js
git commit -m "feat(frontend): C2 show-run-lineage toggle state + checkbox

Module-level showRunLineage (off by default) + a 'Show run lineage'
checkbox on the CID-flow tab; change handler invalidates both CID-flow
caches and re-renders. Cache key includes the toggle so it re-renders.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Task 3: Mermaid overlay in `renderCidFlowTab`

**Files:** Modify `frontend/trial.js`.

- [ ] **Step 3.1: Append rid to llm_request audit node labels (overlay on)**

In `renderCidFlowTab`, where audit nodes are emitted (~1464-1469), when `showRunLineage` is true, append the short rid to the label for llm_request nodes. Compute the lineage once at the top of the function: `const lineage = showRunLineage ? buildRunLineage(trial, tAudits) : null;`. Then:

```javascript
for (const a of tAudits) {
  const phase = a.phase.replace(/[\[\]"]/g, "");
  let label = a.sid ? `${phase}\n${a.sid}` : phase;
  if (lineage && lineage.labels.has(`A${a.idx}`)) {
    label += `\n${lineage.labels.get(`A${a.idx}`)}`;   // rid line
  }
  mer += `  A${a.idx}["${label}"]\n`;
  mer += `  class A${a.idx} auditNode\n`;
  if (lineage && lineage.anomalyNodes.has(`A${a.idx}`)) {
    mer += `  class A${a.idx} ridAnomaly\n`;
  }
}
```

- [ ] **Step 3.2: Emit dashed parent_rid run-chain edges**

After the existing edge loops (~after line 1508), when overlay on:

```javascript
if (lineage) {
  for (const [parent, child] of lineage.parentEdges) {
    mer += `  ${parent} -.->|run| ${child}\n`;
  }
}
```

(The `-.->` dotted arrow is already used for turn→audit; to distinguish, add the `|run|` edge label so run-chain edges read as "run". If clearer separation is wanted, the implementer may use a `linkStyle` on these edges — but `|run|` labels are the low-risk default.)

- [ ] **Step 3.3: Add the anomaly classDef**

In the classDef block (~1524), add:

```javascript
mer += "  classDef ridAnomaly stroke:#dc3545,stroke-width:3px,stroke-dasharray:4;\n";
```

- [ ] **Step 3.4: Syntax check + visual**

Run: `node --check frontend/trial.js`
Manual visual (browser): open a trial's CID flow tab, toggle on → llm_request nodes show a rid line, dashed `run`-labelled edges connect the chain, anomaly nodes (if any) get the red dashed border; toggle off → diagram identical to before. (Use a post-CHG-26F trial from Task 6 for meaningful parent edges.)

- [ ] **Step 3.5: Commit**

```bash
git add frontend/trial.js
git commit -m "feat(frontend): C2 mermaid RID overlay in CID flow

When 'show run lineage' is on, llm_request nodes gain a rid label, dashed
run-labelled parent_rid edges connect the run chain, and anomaly nodes get
a red dashed border (ridAnomaly classDef). Off → diagram unchanged.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Task 4: Cytoscape overlay in `_buildAndMountCytoscape`

**Files:** Modify `frontend/trial.js`.

- [ ] **Step 4.1: Read the cytoscape builder**

Run: `grep -n "_buildAndMountCytoscape\|cytoscape(\|elements\|edges:\|nodes:\|style:" frontend/trial.js | head -30`
Read `_buildAndMountCytoscape`. Identify: how nodes are added (the `A${idx}` audit nodes), the elements array construction, and the style array (selectors/classes).

- [ ] **Step 4.2: Add rid label + parent edges + anomaly class (overlay on)**

Compute `const lineage = showRunLineage ? buildRunLineage(trial, <topo audits used here>) : null;` (use the same topology the cytoscape builder uses). Then, mirroring Task 3:
- For each `A${idx}` audit node element that is in `lineage.ridToNode`'s value set, append the short rid to its `data.label` (or add a `data.rid`), and add the `rid-anomaly` class to elements in `lineage.anomalyNodes`.
- Add edge elements for each `lineage.parentEdges` pair: `{ data: { source: parent, target: child }, classes: "run-edge" }`.
- In the cytoscape `style` array, add two selectors:
  - `.run-edge` → dashed line (`'line-style': 'dashed'`, a distinct `'line-color'`, `'target-arrow-shape': 'triangle'`).
  - `.rid-anomaly` → red border (`'border-color': '#dc3545'`, `'border-width': 3`).

Keep all additions guarded by `if (lineage)` so the base graph is untouched when off.

- [ ] **Step 4.3: Syntax check + visual**

Run: `node --check frontend/trial.js`
Manual visual: open the CID flow (interactive) tab, toggle on → rid labels + dashed run edges + anomaly borders appear and are pan/zoom-interactive; toggle off → base graph unchanged. Confirm no listener/canvas leak on repeated toggles (the existing unmount path should handle it).

- [ ] **Step 4.4: Commit**

```bash
git add frontend/trial.js
git commit -m "feat(frontend): C2 cytoscape RID overlay in interactive CID flow

Mirrors the mermaid overlay in the interactive tab: rid labels on
llm_request nodes, dashed run-edge parent_rid links, rid-anomaly node
border. Guarded by show-run-lineage; base graph untouched when off.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Task 5: CSS styling

**Files:** Modify `frontend/style.css`.

- [ ] **Step 5.1: Add the toggle + (any non-mermaid/non-cytoscape) styles**

Mermaid node/edge styling lives in the mermaid classDefs (Task 3) and cytoscape styling in the cytoscape style array (Task 4), so CSS here is mainly the checkbox control affordance. Add:

```css
.run-lineage-toggle {
  display: inline-flex;
  align-items: center;
  gap: 0.4em;
  font-size: 0.9em;
  margin-left: 1em;
  cursor: pointer;
  user-select: none;
}
.run-lineage-toggle input { cursor: pointer; }
```

Match the existing `cid-flow-stats` / control styling conventions in style.css (grep for `.cid-flow` to find the neighborhood).

- [ ] **Step 5.2: Commit**

```bash
git add frontend/style.css
git commit -m "style(frontend): C2 run-lineage toggle control

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Task 6: B-Task 19 (AGW image bump + smoke) + C2 manual visual verification

This task verifies CHG-26F end-to-end AND produces the post-fix trial used for C2's manual visual check.

- [ ] **Step 6.1: Bump the AGW image tag**

In `docker-compose.yaml` (~line 40), change the agentgateway image tag to the freshly-built `v1.0.1-ib.cidgar`:
```yaml
    image: ghcr.io/agentgateway/agentgateway:v1.0.1-ib.cidgar
```

- [ ] **Step 6.2: Pull + restart + health**

```bash
docker compose pull agentgateway
make down && make up
sleep 15
curl -s http://localhost:8000/health
docker compose logs agentgateway 2>&1 | grep -iE "WARN|ERROR" | head
```
Expected: healthy; zero governance validate warnings (aiplay opts in to all RID toggles).

- [ ] **Step 6.3: Run a trial and verify CHG-26F end-to-end**

Run an MCP trial (weather, via_agw, ollama). Then assert in `/trials/{id}`:
- every `llm_request` and `llm_response` body has `rid` matching `ibr_<12hex>`;
- `llm_response` carries `provider_response_id`;
- after Run 0, `parent_rid` is populated (the CHG-26F fix — was null pre-fix);
- `tool_call` / `tool_response` carry `parent_run_rid`.

This confirms the f2→f3 and f4→f5 handoffs work in the running system.

- [ ] **Step 6.4: C2 manual visual verification against that trial**

Open `trial.html?id=<the trial>`:
- CID flow tab: toggle "Show run lineage" → rid labels + dashed run edges (now with real parent_rid links) + any anomaly nodes; toggle off → base diagram.
- CID flow (interactive) tab: same, interactive.
- Confirm verdicts l + m render in the Verdicts tab (C1) with pass/na as appropriate.

- [ ] **Step 6.5: Commit the image bump**

```bash
git add docker-compose.yaml
git commit -m "chore(aiplay): bump AGW image to v1.0.1-ib.cidgar (CHG-26 + CHG-26F)

Picks up the RID infrastructure (Design B) + the f2→f3/f4→f5 RID handoff
fix (CHG-26F). Smoke-verified: rid in llm_request/llm_response, parent_rid
chain populated, parent_run_rid on tool phases.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Deferred (carried forward)
- `strict_lineage` per-row config flag + UI toggle (verdict-behavior; needs the C1-follow-up RowConfig field — unrelated to this viz).
- verdict_n (cross-API RID continuity).
- Cytoscape-only enhancements (e.g. click-to-focus a run's subtree) — fast-follow if wanted.

## Self-review notes
- **Spec coverage:** toggle (T2), shared helper (T1), mermaid overlay (T3), cytoscape overlay (T4), CSS (T5), manual-visual verification + post-fix trial (T6). All spec sections covered.
- **No automated tests** by design (frontend has no runner) — every code task ends with `node --check` + manual visual; T6 supplies the live trial.
- **Known unknowns the implementer must resolve by reading code:** the `_buildCidFlowTopology` audit `.idx`/`.body` availability (T1.1), the exact re-render entry point + cidflow cache-key (T2.3), the cytoscape elements/style construction (T4.1). Each task names the grep to run.
- **Type/name consistency:** `buildRunLineage(trial, topoAudits)` → `{ridToNode, parentEdges, anomalyNodes, labels}`; node ids `A${idx}`; `showRunLineage`; classes `ridAnomaly` (mermaid) / `rid-anomaly` + `run-edge` (cytoscape). Used consistently across tasks.
