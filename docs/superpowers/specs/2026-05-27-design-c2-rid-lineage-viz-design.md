---
status: proposed
drafted: 2026-05-27
design: C2 (second sub-project of Design C)
depends_on:
  - Design B (RID audit fields) + CHG-26F (f2→f3 handoff, so parent_rid populates)
  - served trial JSON exposes audit_entries[].body.{rid, parent_rid, parent_rid_anomaly, is_turn_boundary}
---

# Design C2 — RID lineage overlay in CID flow

## Summary

Design B emits run-identity audit fields; C1 added the scoring verdicts (l, m).
C2 is the dashboard visualization: render the `rid → parent_rid` run lineage as
an **opt-in overlay on the existing CID flow view**, rather than a separate
tab. CID (conversation) and RID (run) are related correlators, so layering the
run chain onto the conversation flow gives one unified picture.

Frontend-only. No harness/AGW/backend change — the served trial JSON already
carries the RID fields the overlay needs.

## Scope

In scope:
- A "Show run lineage" toggle (off by default) added to both CID-flow tabs:
  `cidflow` (Mermaid) and `cidflow-interactive` (Cytoscape).
- A shared `buildRunLineage(trial)` helper that derives the lineage model from
  `audit_entries[].body`.
- Mermaid overlay: rid labels on `llm_request` nodes, dashed `parent_rid`
  run-chain edges, anomaly node styling.
- Cytoscape overlay: same augmentation in the interactive graph.
- CSS for the new node/edge styles.

Out of scope (deferred):
- `strict_lineage` per-row config flag + its UI toggle (verdict-behavior knob;
  needs the C1-follow-up RowConfig field first — unrelated to this viz).
- A standalone "Run lineage" tab (rejected in favor of the CID-flow overlay).
- verdict_n.
- Any backend/harness change.

## Background — how CID flow is built today

`frontend/trial.html` has tabs including **CID flow** (`#tab-cidflow`, Mermaid
`graph LR`) and **CID flow (interactive)** (`#tab-cidflow-interactive`,
Cytoscape). `frontend/trial.js`:
- builds the Mermaid text in a `renderCidFlowTab(trial)`-style path: a
  tripartite graph of **turn** nodes ↔ **CID** nodes (central pivot, colored by
  reuse) ↔ **audit-entry** nodes; solid turn→CID / audit→CID edges, dotted
  turn→audit edges;
- defers Mermaid init until the tab is visible (`runMermaidIfVisible`, guarding
  the Mermaid 9.4.3 getBBox label-measurement quirk) and uses a render-cache
  (hash of last-rendered HTML) to skip re-render when nothing changed;
- mounts/unmounts the Cytoscape graph with its own render-cache and listener
  cleanup.

The `llm_request` audit-entry nodes the RID overlay annotates already exist in
that graph.

## Data source

The served trial JSON exposes, per audit entry, a parsed `body` dict:
`audit_entries[i].body.{rid, parent_rid, parent_rid_anomaly, is_turn_boundary}`
(verified live). The overlay reads these directly — no regex on raw log lines,
no backend change. (`parent_rid` is `null` on genesis runs, truncation, and on
pre-CHG-26F trials; the overlay handles `null` by drawing no edge.)

## Architecture

### Toggle state
A module-level `let showRunLineage = false;` in `trial.js`. A "Show run lineage"
checkbox is rendered at the top of each CID-flow tab's content, bound to this
shared state. Flipping it:
1. invalidates the affected tab's render-cache (so the next render rebuilds),
2. triggers a re-render of the currently-visible CID-flow tab.

Off by default — the base CID flow is unchanged until the user opts in.

### Shared helper
```
buildRunLineage(trial) -> {
  // node-key (matching the cidflow audit-node id scheme) -> short rid string
  ridByNode: Map<nodeKey, string>,
  // [ [parentNodeKey, childNodeKey], ... ] for resolvable parent_rid links
  parentEdges: Array<[string, string]>,
  // set of node-keys whose entry had parent_rid_anomaly === true
  anomalyNodes: Set<string>,
}
```
It walks `trial.audit_entries`, selects `llm_request` entries with a `body.rid`,
maps each `rid` to the cidflow audit-node id, and resolves `parent_rid` to the
node of the run that owns that rid (skipping links whose parent rid isn't found
— genesis/truncation/null). This is the one piece of logic shared by both
renderers; it is a pure function of `trial`.

### Mermaid overlay (`cidflow` tab)
When `showRunLineage` is true, the `graph LR` builder additionally:
- appends the short rid (e.g. last 6 hex of `ibr_…`) to each `llm_request`
  node's label;
- emits dashed run-chain edges `parent -.-> child` for each `parentEdges` entry,
  using a distinct Mermaid `linkStyle`/class so they read differently from the
  solid CID edges;
- applies an anomaly class (warning border/fill) to `anomalyNodes`.
When false, the builder emits exactly today's diagram (byte-identical → render
cache unaffected for users who never toggle).

### Cytoscape overlay (`cidflow-interactive` tab)
When `showRunLineage` is true, the graph additionally carries: rid in each
`llm_request` node's data/label; `parent_rid` edges as a distinct edge class
(dashed style); anomaly node class. Reuses the existing mount/unmount lifecycle
and render-cache.

### Styling (`style.css`)
New classes for: the dashed run-chain edge, the rid label text, and the anomaly
node (warning color). Mirror the existing cidflow node/edge class conventions.

## Robustness

- `parent_rid: null` → no edge (rooted node). Covers genesis, truncated history,
  and pre-CHG-26F trials uniformly.
- No `rid` on a trial (non-Design-B / chat-only) → overlay adds nothing; toggle
  is a no-op visually.
- The overlay is strictly additive: with the toggle off, the diagram is
  identical to today's; with it on, it only adds nodes-labels/edges/classes —
  it never removes or restyles the base CID structure.

## Verification

The frontend has no automated test harness (the CID flow and Cytoscape views
are not unit-tested), and per decision C2 does not introduce one. Verification:
1. `node --check frontend/trial.js` (and any other edited JS) — syntax.
2. **Manual visual** against a live trial: load `trial.html?id=<trial>`, open
   the CID flow tab, toggle "Show run lineage" on/off, confirm rid labels +
   dashed parent_rid edges + anomaly highlight appear (and the base diagram is
   unchanged with the toggle off); repeat on the interactive tab.
3. The live trial should be a **post-CHG-26F** run (populated `parent_rid`) —
   produced by B-Task 19 (AGW image bump to `v1.0.1-ib.cidgar` + smoke), which
   is sequenced immediately before this manual-visual step.

## Cross-references

- C1 spec (verdicts): `docs/superpowers/specs/2026-05-27-design-c1-rid-verdicts-design.md`
- Design B spec: `docs/superpowers/specs/2026-05-20-run-identity-design.md`
- CHG-26F (RID handoff): AGW `ibfork/feat/cidgar` commit `c733818`
- Existing CID-flow render + Cytoscape mount: `frontend/trial.js`
