# CID flow tabs — `mcp-session-id` labels on MCP audit nodes

**Date:** 2026-04-30
**Scope:** Frontend-only (`frontend/trial.js`, both Mermaid and cytoscape renderers).
**Goal:** Show which `mcp-session-id` was in use for each MCP-related audit node in the CID flow tabs.

## Problem

The CID flow tabs render audit nodes with a phase label only (`tools_list`, `tool_call`, etc.). When a trial spans multiple MCP sessions across turns, there is no way to tell from the graph which call belonged to which session, or where session boundaries fall.

The `mcp-session-id` header is present on the trial side in `turns[*].framework_events[*].{request,response}.headers["mcp-session-id"]`. AGW governance audit entries do **not** carry it (verified across 103 trials: zero hits in any audit field — top-level, body, or raw line).

## Approach

Augment the `tools_list` and `tool_call` audit nodes with a session label, derived by correlating each audit to the same-turn framework_event of the matching MCP type. Frontend-only join; no backend, schema, or extra graph elements.

## Correlation

For each turn, in `captured_at` order:

1. Build an ordered list of MCP framework_events of each relevant type:
   - `mcp_tools_list` events
   - `mcp_tools_call` events
2. Walk that turn's audits in order. For an audit with phase `tools_list`, bind to the *k*th `mcp_tools_list` event in the same turn (where *k* is the audit's ordinal position among `tools_list` audits in that turn). Same rule for `tool_call` ↔ `mcp_tools_call`.
3. Read the session id from `event.request.headers["mcp-session-id"]`; if absent, try `event.response.headers["mcp-session-id"]`.
4. If counts don't match (more audits than events of that type in the turn, or vice versa) or the header is missing, the unmatched audits get no chip — silent skip, no UI noise.

Audits whose phase is not `tools_list` or `tool_call` (`llm_request`, `tool_planned`, `tool_response`, `terminal`) are unchanged.

## Aliasing

The visible label is the last 6 hex chars of the *inner mutable hash* extracted from the decoded `mcp-session-id`.

Decoding:
1. Treat the raw header value as base64url, decode to UTF-8.
2. Parse as JSON; expect shape `{"t":"mcp","s":[{"t":"mutable","s":"<hex>"}]}`.
3. Take `s[0].s` and use its last 6 characters.

Fallback: if any step fails (decode error, unexpected shape), use the last 6 characters of the raw base64 header value. The fallback yields a stable, distinguishing label even when the structure differs from the expected shape.

The full raw header value (not the decoded form) is what appears in the tooltip.

## Visual treatment

**Mermaid (`renderCidFlowTab`):**
- Audit node label becomes two lines: existing phase label, then the alias (e.g. `tool_call\nc967c`). Existing label-escaping logic still applies.
- Hover tooltip via Mermaid's `click <nodeId> <callback> "<text>"` directive. Register one no-op global callback (`window.__cidFlowNoop = () => {};`) at module load and emit `click A<idx> __cidFlowNoop "<full-id>"` for each audit that has a session id.
- No tinting.

**Cytoscape (`renderCidFlowInteractiveTab`):**
- Same two-line label appended to the node label.
- Tooltip: store the full id on the node's `data('sidFull')`. Register `mouseover` / `mouseout` handlers on the `node[sidFull]` selector that set / clear the `title` attribute on the cytoscape container element to `"mcp-session-id: <full-id>"`. Browser renders the native tooltip after the usual hover delay. No new vendor libs (`cytoscape-popper`, `tippy.js`, `qtip` not vendored).
- No tinting.

The tooltip text is the raw `mcp-session-id` header value verbatim.

## Topology helper

`_buildCidFlowTopology(trial)` already returns per-audit objects. Extend each audit object with a new field, `sid: string | null`:
- `sid` = the alias (last 6 chars per the rules above) when correlation succeeds.
- `sid` = `null` when there is no match.

Add a sibling field, `sidFull: string | null`, carrying the raw header for tooltip use. Keeping the helper renderer-agnostic preserves the property the file already documents (Mermaid and cytoscape share IDs and topology).

## Legend

Add one line to the existing CID flow legend block (used by both tabs):

> **Audit node second line** (e.g. `c967c` under `tool_call`) — last 6 hex chars of the `mcp-session-id` for that MCP call. Hover for the full id.

## Caching

The session-id derivation is deterministic from the trial JSON. The existing render-cache hash (`__cidFlowLastSourceHash`, `__cidFlowInteractiveLastSourceHash`) already covers any field that affects the rendered output, so no cache-key changes are needed.

## Empty / degenerate cases

- Trial with no MCP framework_events → no audits get a chip; render is byte-identical to today.
- Audit count exceeds matching event count in a turn → unmatched audits get no chip.
- Audit phase has no corresponding framework_event type (current scope: only `tools_list` and `tool_call` are handled) → no chip.
- `mcp-session-id` header missing on the matched event → no chip.
- Decode failure → fallback to last 6 of raw base64 (chip still appears).

## Out of scope (deferred)

- Session nodes (first-class graph nodes for each session).
- Framework_event-derived MCP-call nodes (initialize / notif / sse_open / session_close not currently in the graph).
- Per-session border tinting on audit nodes.
- Hover-to-highlight-all-same-session interaction.
- Propagating the session label to turn nodes.
- Retroactive Mermaid `click`-callback tooltips for existing CID nodes (the in-code comment claims auto-tooltips that don't exist; cleanup tracked separately).

## Testing approach

- Sample trial with multiple sessions across turns: verify each `tool_call` and `tools_list` audit node shows the expected last-6 alias and the tooltip reveals the full base64 id.
- Trial with no MCP framework_events: verify render is unchanged from baseline.
- Trial where a turn has more `tool_call` audits than `mcp_tools_call` events (synthetic): verify unmatched audits render without a chip and no error.
- Trial where the header decode fails (synthetic, malformed base64): verify fallback alias appears.
- Both tabs (static Mermaid and interactive cytoscape) display the chip and tooltip consistently.
