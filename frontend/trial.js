// Full-page trial view.
//
// URL forms:
//   /trial.html?id=<trial_id>     → directly view a specific trial
//   /trial.html?row_id=<row_id>   → resolve row → its latest trial; show row config
//                                   even if no trial exists yet; auto-pick up
//                                   newly-started trials via polling.
//
// Live updates: SSE subscription refreshes the page on every status tick (1s)
// during running so turn cards appear as they execute, not only at completion.

const API_BASE = "";
const params = new URLSearchParams(location.search);
const explicitTrialId = params.get("id");
const rowId = params.get("row_id");

const elTitle = document.getElementById("trial-title");
const elStatus = document.getElementById("trial-status-pill");
const elAbortBtn = document.getElementById("trial-abort-btn");
const elExportBtn = document.getElementById("trial-export-btn");
const elRowSummary = document.getElementById("row-summary");

// CID flow render-cache: hash of the last-rendered cidflow HTML. Lets the
// poll/SSE loop skip the (expensive + race-prone) Mermaid re-render when
// the trial JSON didn't change in any way that affects the CID flow tab.
let __cidFlowLastSourceHash = null;
// Interactive (cytoscape) CID flow tab — same render-cache pattern as
// the Mermaid tab. The cy instance is held so we can `.destroy()` it
// before remounting (otherwise listeners + canvas leak on re-renders).
let __cidFlowInteractiveLastSourceHash = null;
let __cidFlowInteractiveCy = null;
let __cidFlowInteractiveNeedsMount = false;
// Services topology render-cache: same pattern as cidflow above.
let __servicesLastSourceHash = null;
// Mermaid render-deferral state. Mermaid 9.4.3's getBBox()-based label
// measurement returns 0×0 in Firefox when the parent element is
// display:none — collapsing the SVG to a 16×16 viewBox with all labels
// invisible. Solution: only run mermaid.init when the target tab is
// visible. The flag tracks whether a render is pending so the
// tab-switch click handler can trigger it on first navigation.
let __cidFlowNeedsMermaid = false;
let __servicesNeedsMermaid = false;

// Design C2 — RID lineage overlay toggle, shared by both CID-flow tabs.
// Off by default: the base CID flow is unchanged until the user opts in.
// The cidflow render-cache keys on the rendered-HTML hash, so flipping this
// changes the HTML and triggers a re-render automatically.
let showRunLineage = false;
let convShowGovInternals = false;  // wired in Task 9; declared now so Task 7's render doesn't ReferenceError.

// I-NEW-1: framework-capability cache. The NOTE-tab's framework rules
// (e.g. "crewai doesn't implement responses") used to hardcode the
// supported_apis sets, mirroring `harness/validator.py::ADAPTER_CAPABILITIES`.
// They now read from this cache, populated once at page-load via /info.
// `null` means "not fetched yet" — rules that depend on this gracefully
// no-op until it's available, then re-render on the next SSE/poll tick.
let __frameworksInfo = null;

async function ensureFrameworksInfo() {
  if (__frameworksInfo !== null) return __frameworksInfo;
  try {
    const r = await fetch(`${API_BASE}/info`);
    if (!r.ok) return null;
    const data = await r.json();
    __frameworksInfo = data.frameworks || {};
  } catch {
    // Defensive: leave as null so a later tick can retry.
  }
  return __frameworksInfo;
}

// Test helper — lets the NOTE-tab rules consult capabilities without
// caring whether the fetch has completed yet. Returns true when the
// adapter implements the API per ADAPTER_CAPABILITIES; false when it
// definitely does NOT; null when capability data isn't loaded yet
// (caller should treat as "unknown" and skip the rule).
function adapterSupportsApi(framework, api) {
  if (!__frameworksInfo) return null;
  const fw = __frameworksInfo[framework];
  if (!fw || !Array.isArray(fw.supported_apis)) return null;
  return fw.supported_apis.includes(api);
}

// Run mermaid.init on the given tab IF it's currently visible. Returns
// true on success (caller should clear the corresponding NeedsMermaid
// flag), false otherwise (tab hidden, no nodes, init threw).
function runMermaidIfVisible(tabKey) {
  const el = tabContents[tabKey];
  if (!el || !el.classList.contains("active")) return false;
  if (typeof mermaid === "undefined") return false;
  const nodes = el.querySelectorAll(".mermaid");
  if (!nodes.length) return false;
  // Reset Mermaid's own dedup mark — without this, nodes that were
  // measured 0×0 on a previous hidden-tab attempt are skipped on retry.
  for (const n of nodes) n.removeAttribute("data-processed");
  try {
    mermaid.initThrowsErrors(undefined, nodes);
    // SVG-presence sanity check — Mermaid 9.4.3 occasionally returns
    // without throwing but also without writing the SVG (e.g., a CSS
    // path collapsed it). Surface the silent-failure mode.
    for (const n of nodes) {
      if (!n.querySelector("svg")) {
        console.warn(`${tabKey}: pre.mermaid has no SVG child after init; source:`, n.textContent.slice(0, 200));
      }
    }
    // CID flow tab: inject SVG <title> children on MCP audit nodes so
    // `mcp-session-id` shows as a native browser tooltip on hover.
    // (Mermaid's own `click ... "tooltip"` directive depends on a
    // `div.mermaidTooltip` overlay that doesn't reliably surface under
    // our htmlLabels:false render config + app stylesheet.)
    if (tabKey === "cidflow" && __lastTrialForCy) {
      _injectMcpSessionTitles(el, __lastTrialForCy);
    }
    return true;
  } catch (e) {
    console.warn(`${tabKey} Mermaid render failed:`, e);
    return false;
  }
}

// Walk the rendered Mermaid SVG and prepend a <title> child to each MCP
// audit node group. Browsers render SVG <title> as a native tooltip on
// hover (same behavior as <abbr title="...">). Idempotent — re-running
// after a re-render is safe (existing <title> children are skipped).
function _injectMcpSessionTitles(tabEl, trial) {
  const svg = tabEl.querySelector("pre.mermaid svg, .mermaid svg");
  if (!svg) return;
  const topo = _buildCidFlowTopology(trial);
  for (const a of topo.audits) {
    if (!a.sidFull) continue;
    // Mermaid prefixes node group ids with `flowchart-` and may suffix
    // with `-N`. Match by id starting with the audit's `A<idx>-`.
    const node = svg.querySelector(`g.node[id^="flowchart-A${a.idx}-"]`);
    if (!node) continue;
    if (node.querySelector(":scope > title")) continue;  // already injected
    const title = document.createElementNS("http://www.w3.org/2000/svg", "title");
    title.textContent = `mcp-session-id: ${a.sidFull}`;
    node.insertBefore(title, node.firstChild);
  }
}

// Mermaid + cytoscape node id helpers — both renderers use the same
// scheme so hover/debug feel symmetric across the two tabs. CID nodes
// strip the "ibc_" prefix for compactness; snapshot nodes keep the full
// hash. Lifted to module scope to dedup the two render paths
// (renderCidFlowTab + _buildAndMountCytoscape).
const cidNodeId = cid => `C_${cid.slice(4)}`;
const ssNodeId = ss => `SS_${ss}`;

// Tiny non-cryptographic string hash (djb2-ish, sufficient for change-
// detection on rendered HTML — collisions only manifest as a missed
// re-render which the next change correct).
function _hashStr(s) {
  let h = 0;
  for (let i = 0; i < s.length; i++) h = ((h << 5) - h + s.charCodeAt(i)) | 0;
  return h.toString();
}

// Export current trial as JSON via browser blob download. Re-fetches the
// trial fresh on click so the export reflects whatever's persisted server-
// side at click time (running trials in row_id mode download a snapshot).
async function exportTrialJSON(trialId) {
  if (!trialId) return;
  const r = await fetch(`${API_BASE}/trials/${encodeURIComponent(trialId)}`);
  if (!r.ok) { alert(`Export failed: HTTP ${r.status}`); return; }
  const blob = await r.blob();
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = `trial-${trialId}.json`;
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  URL.revokeObjectURL(url);
}

if (elExportBtn) {
  elExportBtn.addEventListener("click", () => exportTrialJSON(currentTrialId));
}

// T14 — wire abort button. Only visible while status=running; hides on
// terminal status (pass/fail/error/aborted).
if (elAbortBtn) {
  elAbortBtn.addEventListener("click", async () => {
    if (!currentTrialId) return;
    if (!confirm(`Abort trial ${currentTrialId}?\n\nThe currently-executing turn will finish; subsequent turns are skipped.`)) return;
    try {
      const r = await fetch(`${API_BASE}/trials/${currentTrialId}/abort`, {method: "POST"});
      const j = await r.json();
      if (!j.ok) {
        alert(`Abort failed/no-op: ${j.reason || "unknown"}`);
      } else {
        elAbortBtn.disabled = true;
        elAbortBtn.textContent = "⏹ Aborting…";
      }
    } catch (e) {
      alert(`Abort request failed: ${e.message}`);
    }
  });
}
const tabContents = {
  conversation: document.getElementById("tab-conversation"),
  turns: document.getElementById("tab-turns"),
  plan: document.getElementById("tab-plan"),
  verdicts: document.getElementById("tab-verdicts"),
  note: document.getElementById("tab-note"),
  cidflow: document.getElementById("tab-cidflow"),
  "cidflow-interactive": document.getElementById("tab-cidflow-interactive"),
  services: document.getElementById("tab-services"),
  raw: document.getElementById("tab-raw"),
};

// Initialize Mermaid once. We render manually (per-tab refresh) via mermaid.init
// rather than letting it auto-scan, because the CID flow content is rebuilt
// every render cycle (status poll → renderTrial → tabContents.cidflow.innerHTML).
if (typeof mermaid !== "undefined") {
  mermaid.initialize({
    startOnLoad: false,
    theme: "default",
    securityLevel: "loose",
    // Mermaid 9.4.3 (vendored). v10's UMD render path collapses to
    // 16×16 in Firefox: it pre-renders into a detached/hidden DOM
    // container, calls getBBox() to measure, then attaches to the
    // visible DOM — but Firefox returns 0 from getBBox() on detached
    // elements, so layout uses 0-sized labels. This is also why
    // flowchart:{htmlLabels:false} didn't help on v10: the bug isn't
    // foreignObject-specific, it's hidden-container measurement.
    // v9.4.3 attaches before measuring, works in Firefox/Chrome/Safari.
    // htmlLabels:false retained for crisp SVG <text>/<tspan> labels
    // (no foreignObject, no HTML <br>; line breaks via \n → <tspan>).
    flowchart: { htmlLabels: false },
  });
}
const tabBtns = document.querySelectorAll(".trial-tab-btn");

tabBtns.forEach(btn => btn.addEventListener("click", () => {
  tabBtns.forEach(b => b.classList.remove("active"));
  Object.values(tabContents).forEach(c => c.classList.remove("active"));
  btn.classList.add("active");
  const tab = btn.dataset.tab;
  tabContents[tab].classList.add("active");
  // First-visibility Mermaid render — see runMermaidIfVisible comment for
  // the Firefox getBBox-on-hidden-element rationale.
  if (tab === "cidflow" && __cidFlowNeedsMermaid) {
    if (runMermaidIfVisible("cidflow")) __cidFlowNeedsMermaid = false;
  } else if (tab === "services" && __servicesNeedsMermaid) {
    if (runMermaidIfVisible("services")) __servicesNeedsMermaid = false;
  } else if (tab === "cidflow-interactive" && __cidFlowInteractiveNeedsMount) {
    // Same display:none caveat as Mermaid: cytoscape measures 0×0 on a
    // hidden container. Defer mount until the tab is actually visible.
    if (mountCytoscapeIfVisible(__lastTrialForCy)) __cidFlowInteractiveNeedsMount = false;
  }
}));

if (!explicitTrialId && !rowId) {
  elTitle.textContent = "Error";
  document.body.innerHTML = "<p style='padding:20px;'>Expected <code>?id=&lt;trial_id&gt;</code> or <code>?row_id=&lt;row_id&gt;</code> in URL.</p>";
  throw new Error("no trial id or row id");
}

// Mutable: starts with whatever was in URL; row_id mode resolves it from /matrix
let currentTrialId = explicitTrialId || null;
let currentRow = null;     // populated when row_id mode
let pollTimer = null;      // setInterval handle for the live-update poll

// ── Render helpers ──

function renderHeaders(h) {
  if (!h || Object.keys(h).length === 0) return "<em>(none)</em>";
  return `<table class="kv"><tbody>${Object.entries(h).map(([k, v]) =>
    `<tr><td class="k">${escapeHtml(k)}</td><td class="v">${escapeHtml(String(v))}</td></tr>`
  ).join("")}</tbody></table>`;
}
function renderBody(b) {
  if (b === null || b === undefined) return "<em>(empty)</em>";
  // If body is a string, it may be SSE ("data: {...}\n\n" chunks). Try to
  // extract JSON payloads from data: lines and render a "Parsed" view
  // alongside the raw string.
  if (typeof b === "string") {
    const parsed = tryParseSSE(b);
    if (parsed !== null) {
      return `
        <details open><summary><strong>Parsed</strong> — JSON payload(s) from SSE data: lines</summary>
          <div class="pre-with-copy">${copyPreBtn()}<pre>${escapeHtml(JSON.stringify(parsed, null, 2))}</pre></div>
        </details>
        <details><summary><strong>Raw</strong> — wire bytes (${b.length} chars)</summary>
          <div class="pre-with-copy">${copyPreBtn()}<pre>${escapeHtml(b)}</pre></div>
        </details>`;
    }
    // Plain string, not SSE — show as raw string
    return `<div class="pre-with-copy">${copyPreBtn()}<pre>${escapeHtml(b)}</pre></div>`;
  }
  // Dict/list: pretty JSON
  return `<div class="pre-with-copy">${copyPreBtn()}<pre>${escapeHtml(JSON.stringify(b, null, 2))}</pre></div>`;
}

function tryParseSSE(s) {
  // Match `data: {json}` lines; tolerate multi-event streams.
  if (!s || typeof s !== "string" || !s.includes("data:")) return null;
  const payloads = [];
  for (const line of s.split(/\r?\n/)) {
    const trimmed = line.trim();
    if (!trimmed.startsWith("data:")) continue;
    const payload = trimmed.slice(5).trim();
    if (!payload) continue;
    try {
      payloads.push(JSON.parse(payload));
    } catch {
      payloads.push({ _raw: payload });
    }
  }
  if (!payloads.length) return null;
  return payloads.length === 1 ? payloads[0] : payloads;
}
// Walk possible body locations (E26: top-level body; legacy: raw.fields.body
// for shape-A JSON logs; raw.body for older fixtures). Returns the
// snapshot_hash value if AGW emitted one for this audit (E20 — present on
// tools_list and tool_call phases when channels.snapshot_correlation is on).
function _auditSnapshotHash(a) {
  const body = a.body || a.raw?.fields?.body || a.raw?.body || {};
  return body.snapshot_hash || null;
}

function renderAudit(entries) {
  if (!entries.length) return "<em>(no audit entries captured in this turn's time window)</em>";
  return entries.map(a => {
    const phase = a.phase || '?';
    const ss = _auditSnapshotHash(a);
    // Per-phase header badges:
    //   tools_list  → snapshot_hash (no cid; tools/list happens before any
    //                 conversation context, so cid is typically ∅)
    //   tool_call   → cid AND snapshot_hash (the call carries both — cid for
    //                 conversation tracking, ss for snapshot correlation E20)
    //   other       → cid only (legacy behavior)
    let idBadges = '';
    if (phase === 'tools_list') {
      idBadges = `<span class="badge">ss: ${ss || '∅'}</span>`;
    } else if (phase === 'tool_call') {
      idBadges = `<span class="badge">cid: ${a.cid || '∅'}</span>` +
                 `<span class="badge">ss: ${ss || '∅'}</span>`;
    } else {
      idBadges = `<span class="badge">cid: ${a.cid || '∅'}</span>`;
    }
    return `
    <div class="audit-entry">
      <span class="badge ${phase}">phase: ${phase}</span>
      ${idBadges}
      <span class="badge">backend: ${shortenBackend(a.backend)}</span>
      ${a.captured_at ? `<span class="badge">ts: ${a.captured_at.slice(11, 23)}</span>` : ''}
      <details><summary>raw governance log</summary><div class="pre-with-copy">${copyPreBtn()}<pre>${escapeHtml(JSON.stringify(a.raw, null, 2))}</pre></div></details>
    </div>
    `;
  }).join("");
}
function shortenBackend(b) {
  if (!b) return "?";
  const parts = b.split("/");
  return parts[parts.length - 2] || b;
}
function escapeHtml(s) {
  return String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

// ── Copy-to-clipboard helpers ──
// copyPreBtn() returns the HTML for a small icon button; place it inside a
// <div class="pre-with-copy"> alongside a <pre>. The global click handler
// (installed once below) finds the sibling <pre> and copies its textContent.
// We use this pre-relative pattern (rather than embedding the text in a data
// attribute) because some snippets (raw trial JSON) can be hundreds of KB
// and would balloon the DOM.
function copyPreBtn(label = "📋") {
  return `<button class="copy-btn copy-pre-btn" title="copy contents to clipboard">${label}</button>`;
}

// navigator.clipboard.writeText requires a secure context — HTTPS, OR
// http://localhost / http://127.0.0.1. The aiplay UI is typically served
// over http://<host-IP>:8000 (multipass/UTM/Docker host IP), which is
// NOT a secure context. Modern Firefox/Chrome reject the modern API on
// HTTP+IP origins (NotAllowedError / undefined). Fall back to the legacy
// textarea+execCommand("copy") path which works on HTTP+IP.
async function copyTextToClipboard(text) {
  if (window.isSecureContext && navigator.clipboard?.writeText) {
    try {
      await navigator.clipboard.writeText(text);
      return true;
    } catch {
      // fall through to legacy
    }
  }
  // Legacy path: create off-screen textarea, select, execCommand("copy"),
  // remove. Requires the click event to still be in the call stack
  // (browsers gate execCommand on user-activation).
  const ta = document.createElement("textarea");
  ta.value = text;
  ta.setAttribute("readonly", "");
  ta.style.cssText = "position:fixed;top:0;left:0;opacity:0;pointer-events:none;";
  document.body.appendChild(ta);
  ta.select();
  ta.setSelectionRange(0, text.length);
  let ok = false;
  try {
    ok = document.execCommand("copy");
  } catch {
    ok = false;
  }
  document.body.removeChild(ta);
  return ok;
}

if (!window.__copyBtnInstalled) {
  document.addEventListener("click", async (e) => {
    if (!e.target.matches?.(".copy-pre-btn")) return;
    const wrapper = e.target.closest(".pre-with-copy");
    const pre = wrapper?.querySelector("pre");
    if (!pre) return;
    const orig = e.target.textContent;
    const ok = await copyTextToClipboard(pre.textContent);
    e.target.textContent = ok ? "✓" : "✗";
    setTimeout(() => { e.target.textContent = orig; }, 800);
  });
  window.__copyBtnInstalled = true;
}
function renderRowChips(c) {
  const chips = [
    ["framework", c.framework],
    ["api", c.api],
    c.stream ? ["stream", "on"] : null,
    c.state ? ["state", "on"] : null,
    ["llm", c.llm],
    ["mcp", c.mcp],
    ["routing", c.routing],
  ].filter(Boolean);
  return chips.map(([k, v]) =>
    `<span class="chip"><span class="chip-k">${escapeHtml(k)}</span>${escapeHtml(v || "")}</span>`
  ).join("");
}
function pickAudits(trial, turn) {
  const all = trial.audit_entries || [];
  const headerDemux = all.some(a => a.turn_id);
  if (headerDemux) {
    return all.filter(a => a.turn_id === turn.turn_id);
  }
  const start = turn.started_at || "";
  const end = turn.finished_at || "9999";
  return all.filter(a => {
    const ts = a.captured_at || "";
    return ts >= start && ts <= end;
  });
}
// Known-benign event types with explanatory tooltips.
// Shown as a small ⓘ info badge next to the phase label; hover shows the note.
const BENIGN_EVENT_NOTES = {
  "mcp_sse_open":
    "Optional MCP server→client SSE push channel (streamable-http protocol). " +
    "The fastmcp servers in this harness don't push events, so the stream " +
    "closes idle and AGW wraps the close in a cosmetic JSON-RPC -32603 error. " +
    "The POST channel — where tool calls actually execute — is unaffected.",
  "mcp_session_close":
    "MCP session-close (DELETE) at end of tool execution. The response body " +
    "may contain an SSE-framed JSON-RPC error from AGW wrapping the stream " +
    "termination — benign. The POST tool_call before this event carried the " +
    "actual result.",
};

function renderEventStepCard(ev, idx) {
  // ev is a single framework_events[i] dict. See BENIGN_EVENT_NOTES for types
  // where the "error" in the response body is expected.
  const phase = ev.t || ev.kind || "unknown";
  const req = ev.request || {};
  const resp = ev.response || {};
  const benignNote = BENIGN_EVENT_NOTES[phase];
  const detailBits = [];
  if (ev.tool_name) detailBits.push(`tool=${escapeHtml(ev.tool_name)}`);
  if (ev.tool_count !== undefined) detailBits.push(`tools=${ev.tool_count}`);
  if (ev.args !== undefined) detailBits.push(`args=${escapeHtml(JSON.stringify(ev.args))}`);
  if (ev.error) detailBits.push(`<span class="step-error">err=${escapeHtml(ev.error)}</span>`);
  if (ev.max_hops !== undefined) detailBits.push(`max_hops=${ev.max_hops}`);
  const detailStr = detailBits.length ? ` <span class="step-detail">${detailBits.join(" · ")}</span>` : "";
  const benignBadge = benignNote
    ? ` <span class="step-benign" title="${escapeHtml(benignNote)}">ⓘ benign</span>`
    : "";

  const reqBlock = req.url ? `
    <details><summary>Step request — <code>${escapeHtml(req.method || 'POST')}</code> ${escapeHtml(req.url || '')}</summary>
      <div class="section">
        <div class="subhead">Headers</div>
        ${renderHeaders(req.headers)}
        <div class="subhead">Body ${req.body_bytes_len ? `(${req.body_bytes_len} bytes)` : ''}</div>
        ${renderBody(req.body)}
      </div>
    </details>` : "";
  const respBlock = (resp.status !== undefined) ? `
    <details><summary>Step response — <strong>HTTP ${escapeHtml(String(resp.status || '?'))}</strong> ${resp.elapsed_ms ? `(${resp.elapsed_ms}ms)` : ''}</summary>
      <div class="section">
        <div class="subhead">Headers</div>
        ${renderHeaders(resp.headers)}
        <div class="subhead">Body ${resp.body_bytes_len ? `(${resp.body_bytes_len} bytes)` : ''}</div>
        ${renderBody(resp.body)}
      </div>
    </details>` : "";
  const summaryBlock = ev.result_summary ? `
    <div class="step-summary"><strong>Result preview</strong>: <code>${escapeHtml(ev.result_summary)}</code></div>` : "";
  return `
    <div class="step-card">
      <div class="step-head"><span class="step-idx">#${idx}</span> <span class="step-phase">${escapeHtml(phase)}</span>${benignBadge}${detailStr}</div>
      ${summaryBlock}
      ${reqBlock}
      ${respBlock}
    </div>
  `;
}

function renderTurnCard(trial, t, i) {
  const req = t.request || {};
  const resp = t.response || {};
  const audits = pickAudits(trial, t);
  const events = Array.isArray(t.framework_events) ? t.framework_events : [];
  // All sections collapsed by default — user expands what they need. Reduces
  // visual noise on multi-turn / MCP-heavy trials while still letting
  // single-turn trials drill in with one click.
  const stepsBlock = events.length ? `
      <details><summary><strong>Steps</strong> — multi-step framework flow (${events.length} events)</summary>
        <div class="section steps-list">
          ${events.map((ev, idx) => renderEventStepCard(ev, idx)).join("")}
        </div>
      </details>` : "";
  return `
    <div class="turn-card">
      <h4>Turn ${i}: ${escapeHtml(t.kind)} <span class="turn-id">${escapeHtml(t.turn_id || '')}</span></h4>
      <details><summary><strong>Summary: First request</strong> — what the adapter sent on the first HTTP call of this turn (pre-cidgar mutation)</summary>
        <div class="section">
          <div class="http-line"><strong>${escapeHtml(req.method || 'POST')}</strong> ${escapeHtml(req.url || '')}</div>
          <div class="subhead">Headers</div>
          ${renderHeaders(req.headers)}
          <div class="subhead">Body ${req.body_bytes_len ? `(${req.body_bytes_len} bytes)` : ''}</div>
          ${renderBody(req.body)}
        </div>
      </details>
      <details><summary><strong>Summary: Final response</strong> — what AGW returned on the last HTTP call of this turn (post-cidgar mutation)</summary>
        <div class="section">
          <div class="http-line"><strong>HTTP ${escapeHtml(String(resp.status || '?'))}</strong> ${resp.elapsed_ms ? `(${resp.elapsed_ms}ms)` : ''}</div>
          <div class="subhead">Headers</div>
          ${renderHeaders(resp.headers)}
          <div class="subhead">Body ${resp.body_bytes_len ? `(${resp.body_bytes_len} bytes)` : ''}</div>
          ${renderBody(resp.body)}
        </div>
      </details>
      ${stepsBlock}
      <details><summary><strong>Governance audit</strong> — AGW-side view of this turn (${audits.length} entries)</summary>
        <div class="section">${renderAudit(audits)}</div>
      </details>
    </div>
  `;
}

// ── State machine for the page ──
//
// On load:
//   1. If explicitTrialId → render trial; subscribe SSE if running.
//   2. If row_id only → fetch row; if last_trial_id present → switch to that
//      trial; else render row-config-only page; poll /matrix/row/{id} every
//      2s for a newly-started trial, switch to it when one appears.

async function fetchAndRender() {
  if (currentTrialId) {
    return renderTrial(currentTrialId);
  } else if (rowId) {
    return renderRowOnly();
  }
}

async function renderTrial(tid) {
  const r = await fetch(`${API_BASE}/trials/${tid}`);
  if (!r.ok) {
    tabContents.turns.innerHTML = `<p>Error loading trial: ${r.status} ${await r.text()}</p>`;
    return null;
  }
  const trial = await r.json();

  elTitle.textContent = `Trial ${tid.slice(0, 8)}…`;
  document.title = `aiplay — ${tid.slice(0, 8)}`;
  elStatus.textContent = trial.status || "idle";
  elStatus.className = `status-pill ${trial.status || "idle"}`;
  elRowSummary.innerHTML = renderRowChips(trial.config || {});

  // T14 — show Stop only while running.
  if (elAbortBtn) {
    elAbortBtn.style.display = (trial.status === "running") ? "" : "none";
    if (trial.status !== "running") {
      elAbortBtn.disabled = false;
      elAbortBtn.textContent = "⏹ Stop";
    }
  }

  // Export button: show whenever we have a resolved trial id.
  if (elExportBtn) {
    elExportBtn.style.display = "";
  }

  tabContents.conversation.innerHTML = renderConversationTab(trial);
  tabContents.turns.innerHTML = (trial.turns || []).map((t, i) => renderTurnCard(trial, t, i)).join("")
    || "<p>Turn execution started — turn cards will appear here as turns complete.</p>";

  const plan = trial.turn_plan || {turns: []};
  tabContents.plan.innerHTML = renderPlanTab(plan, (trial.turns || []).length);

  const verdicts = trial.verdicts || {};
  tabContents.verdicts.innerHTML = renderVerdictsTab(verdicts, trial);

  tabContents.note.innerHTML = renderNoteTab(trial);

  // Render-cache: the SSE/poll cycle re-enters renderTrial() every ~1-2s.
  // Without this guard, every cycle nukes innerHTML and re-runs Mermaid,
  // which races with mermaid.init (Firefox can end up with the
  // newly-injected <pre> still empty if we re-inject before the
  // previous init finished writing the SVG). Hash-of-HTML lets us skip the
  // re-render entirely when the trial state hasn't changed in a way that
  // affects the CID flow tab.
  const cidflowHtml = renderCidFlowTab(trial);
  const sourceHash = _hashStr(cidflowHtml);
  if (sourceHash !== __cidFlowLastSourceHash) {
    tabContents.cidflow.innerHTML = cidflowHtml;
    __cidFlowLastSourceHash = sourceHash;
    // setTimeout(0) defers mermaid.run until after the browser commits the
    // innerHTML write. Firefox is stricter than Chrome about running
    // mermaid.run synchronously after innerHTML — it can measure the pre
    // before layout settles and produce a 0×0 SVG.
    // Mark for render. If the cidflow tab is currently visible, init runs
    // immediately (deferred via setTimeout(0) so the browser commits the
    // innerHTML write first). If hidden, the tab-switch click handler
    // will pick it up on first navigation.
    __cidFlowNeedsMermaid = true;
    setTimeout(() => {
      if (runMermaidIfVisible("cidflow")) __cidFlowNeedsMermaid = false;
    }, 0);
  }

  // Interactive (cytoscape) CID flow tab — same render-cache pattern as
  // the Mermaid tab. Mount is deferred to the tab-switch handler when the
  // tab is hidden (cytoscape measures 0×0 on a display:none parent — same
  // root cause as Mermaid's getBBox issue).
  __lastTrialForCy = trial;
  const cyHtml = renderCidFlowInteractiveTab(trial);
  const cyHash = _hashStr(cyHtml);
  if (cyHash !== __cidFlowInteractiveLastSourceHash) {
    tabContents["cidflow-interactive"].innerHTML = cyHtml;
    __cidFlowInteractiveLastSourceHash = cyHash;
    __cidFlowInteractiveNeedsMount = true;
    setTimeout(() => {
      if (mountCytoscapeIfVisible(trial)) __cidFlowInteractiveNeedsMount = false;
    }, 0);
  }

  // Design C2 — (re)bind the run-lineage toggle on both CID-flow tabs after
  // their (re)injection. Uses onchange (not addEventListener) so re-running
  // this each render cycle is idempotent (no duplicate listeners).
  _wireRunLineageToggle(trial);

  // Services topology tab — same render-cache + visibility-deferred init
  // as cidflow above.
  const servicesHtml = renderServicesTab(trial);
  const servicesHash = _hashStr(servicesHtml);
  if (servicesHash !== __servicesLastSourceHash) {
    tabContents.services.innerHTML = servicesHtml;
    __servicesLastSourceHash = servicesHash;
    __servicesNeedsMermaid = true;
    setTimeout(() => {
      if (runMermaidIfVisible("services")) __servicesNeedsMermaid = false;
    }, 0);
  }

  document.getElementById("raw-json").textContent = JSON.stringify(trial, null, 2);
  return trial.status;
}

async function renderRowOnly() {
  // No trial yet — show the row's planned config and an explanation.
  const r = await fetch(`${API_BASE}/matrix/row/${rowId}`);
  if (!r.ok) {
    tabContents.turns.innerHTML = `<p>Error loading row: ${r.status} ${await r.text()}</p>`;
    return null;
  }
  currentRow = await r.json();

  elTitle.textContent = `Row ${rowId.slice(0, 12)}… (no trial yet)`;
  document.title = `aiplay — ${rowId.slice(0, 12)}`;
  elStatus.textContent = "no trial";
  elStatus.className = `status-pill idle`;
  elRowSummary.innerHTML = renderRowChips(currentRow);

  tabContents.turns.innerHTML = `
    <div class="empty-state">
      <p>This row has no completed trial yet.</p>
      <p>Click <strong>▶ Run</strong> on this row in the matrix; this page will auto-update once the trial starts.</p>
      <p>Watching <code>/matrix/row/${rowId}</code> for a new trial every 2s…</p>
    </div>`;

  // Render the planned turn template using /templates/preview
  try {
    const presp = await fetch(`${API_BASE}/templates/preview`, {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify(currentRow),
    });
    if (presp.ok) {
      const data = await presp.json();
      tabContents.plan.innerHTML = renderPlanTab(data.turn_plan || {turns: []}, 0);
    }
  } catch {}

  tabContents.verdicts.innerHTML = "<p><em>No trial run yet — verdicts will appear after the first run.</em></p>";
  // Note tab: registry depends only on the row config (axes), so it can be
  // rendered before any trial runs. Useful preview of "what to expect".
  tabContents.note.innerHTML = renderNoteTab({config: currentRow});
  document.getElementById("raw-json").textContent = JSON.stringify(currentRow, null, 2);
  return null;
}

function renderPlanTab(plan, executedCount) {
  const planTurns = plan.turns || [];
  const planItems = planTurns.map((t, i) => {
    const kind = t.kind || "user_msg";
    const isExecuted = i < executedCount;
    const executedBadge = isExecuted
      ? '<span class="plan-executed-badge">✓ executed</span>'
      : '<span class="plan-pending-badge">pending</span>';
    if (kind === "user_msg") {
      return `<li class="plan-turn">
        <span class="plan-turn-idx">${i}</span>
        <span class="plan-turn-kind">user_msg</span>
        <span class="plan-turn-content">${escapeHtml(t.content || "")}</span>
        ${executedBadge}
      </li>`;
    }
    const extra = Object.entries(t).filter(([k]) => k !== "kind")
      .map(([k, v]) => `<code>${escapeHtml(k)}=${escapeHtml(JSON.stringify(v))}</code>`).join(" ");
    return `<li class="plan-turn">
      <span class="plan-turn-idx">${i}</span>
      <span class="plan-turn-kind control">${escapeHtml(kind)}</span>
      <span class="plan-turn-content">${extra}</span>
      ${executedBadge}
    </li>`;
  }).join("");
  return `
    <h3 style="margin-top:0">Planned turns</h3>
    <ol class="plan-list">${planItems || '<li><em>(empty plan)</em></li>'}</ol>
    <h3>Raw turn_plan JSON</h3>
    <pre>${escapeHtml(JSON.stringify(plan, null, 2))}</pre>
    <p class="plan-note">Read-only here — edit the plan via the matrix row's drawer (CodeMirror editor) before run.</p>
  `;
}

// Walk all audit entries + collect unique CIDs and snapshot hashes for
// the verdicts-tab banner. Source of truth is trial.audit_entries (live
// AGW log) — not the wire bodies, since channels can be disabled and the
// audit stream is the authoritative record of what AGW saw.
//
// MCP session ids live trial-side only (in turns[*].framework_events
// headers — AGW audits don't carry them; verified across 103 trials),
// so we walk a different source for those.
function _collectIdentifiers(trial) {
  const cids = new Set();
  const snapshots = new Set();
  for (const a of (trial.audit_entries || [])) {
    if (a.cid) cids.add(a.cid);
    const ss = _auditSnapshotHash(a);
    if (ss) snapshots.add(ss);
  }
  // Sessions: dedupe by full header value; carry the alias for compact
  // display (full id available on hover via the banner).
  const sessionsByFull = new Map();  // full → alias
  for (const t of (trial.turns || [])) {
    for (const fe of (t.framework_events || [])) {
      const reqH = (fe.request  && fe.request.headers)  || {};
      const resH = (fe.response && fe.response.headers) || {};
      const raw = reqH["mcp-session-id"] || resH["mcp-session-id"] || null;
      const dec = _decodeMcpSessionAlias(raw);
      if (dec && !sessionsByFull.has(dec.full)) sessionsByFull.set(dec.full, dec.alias);
    }
  }
  const sessions = [...sessionsByFull].map(([full, alias]) => ({full, alias}))
    .sort((a, b) => a.alias < b.alias ? -1 : a.alias > b.alias ? 1 : 0);
  // RIDs in RUN ORDER — one per llm_request (each is an LLM run), ordered by
  // capture time. Design B/C: shows the run sequence the parent_rid chain
  // links. NOT sorted alphabetically — run order is the meaningful axis.
  const rids = (trial.audit_entries || [])
    .filter(a => a.phase === "llm_request" && a.body && a.body.rid)
    .slice()
    .sort((x, y) => (x.captured_at || "") < (y.captured_at || "") ? -1
                  : (x.captured_at || "") > (y.captured_at || "") ? 1 : 0)
    .map(a => a.body.rid);
  return {cids: [...cids].sort(), snapshots: [...snapshots].sort(), sessions, rids};
}

function renderIdentifiersBanner(trial) {
  const {cids, snapshots, sessions, rids} = _collectIdentifiers(trial);
  // CIDs and snapshot hashes are pure-hex by construction (XSS-safe
  // today), but defensive escapeHtml is cheap and survives any future
  // identifier-format change.
  const cidList = cids.length ? escapeHtml(cids.join(", ")) : "<em>(none observed)</em>";
  const ssList = snapshots.length ? escapeHtml(snapshots.join(", ")) : "<em>(none observed)</em>";
  // Sessions: render each alias as its own <code> with the full id in a
  // title attr — same hover-for-full UX as the CID flow tabs.
  const sessList = sessions.length
    ? sessions.map(s =>
        `<code title="mcp-session-id: ${escapeHtml(s.full)}">${escapeHtml(s.alias)}</code>`
      ).join(", ")
    : "<em>(none observed)</em>";
  // RIDs as an arrow chain to convey run order (run0 → run1 → …).
  const ridList = rids.length
    ? rids.map(r => `<code>${escapeHtml(r)}</code>`).join(" → ")
    : "<em>(none observed)</em>";
  return `
    <div class="identifiers-banner">
      <div><strong>CIDs (${cids.length}):</strong> <code>${cidList}</code></div>
      <div><strong>RIDs in run order (${rids.length}):</strong> ${ridList}</div>
      <div><strong>mcp-tools/list-snapshots (${snapshots.length}):</strong> <code>${ssList}</code></div>
      <div><strong>mcp-sessions (${sessions.length}):</strong> ${sessList}</div>
    </div>
  `;
}

function renderVerdictsTab(verdicts, trial) {
  const labels = {
    a: "Presence", b: "Channel structure", c: "Continuity",
    d: "Resilience", e: "State-mode gap", f: "GAR richness",
    h: "Latency overhead",                                  // governance per-turn cost
    i: "Snapshot correlation",                              // E20 _ib_ss rate
    k: "Cross-API continuity",                              // E24 combo across LLM routes
    l: "Run lineage",                                       // parent_rid chain integrity
    m: "Turn boundary",                                     // is_turn_boundary correctness
  };
  // Hover help (title attr) explaining what each verdict actually checks.
  // Pairs with the explanations in docs/agw-governance-spec.md §4.2 + §13.
  const tips = {
    a: "Presence — Did AGW actually generate or observe a CID? " +
       "Pass = each user_msg turn has at least one audit entry whose 'cid' " +
       "field is populated. This is the AGW-side log evidence that " +
       "governance fired at all.",
    b: "Channel structure — Did the CID show up in the right WIRE " +
       "LOCATIONS per spec? Pass = each turn's response body carries the " +
       "matching CID via Channel 1 (tool_calls / tool_use args) OR " +
       "Channel 2 (text marker '<!-- ib:cid=… -->'). This is the wire-side " +
       "verification that what AGW logged actually got injected per the " +
       "channel spec — not just that some CID exists somewhere.",
    c: "Continuity (bracket-aware, E21) — Turns are split into segments by " +
       "reset_context turns. Pass = (a) per-segment continuity holds AND " +
       "(b) NO cross-segment CID leak (a CID appearing in two distinct " +
       "segments is treated as governance failure — agent should have minted " +
       "fresh CID after reset). Single-segment trials use the legacy " +
       "consecutive-pair check. Detects both 'agent dropped CID between " +
       "turns' and 'reset boundary failed to isolate'.",
    d: "Resilience — Did the CID survive a 'compact' turn? " +
       "Pass = the CIDs observed BEFORE a compact turn intersect with " +
       "those observed AFTER. Tests whether at least one channel " +
       "(Channel 3 MCP resource block being the most resilient) carries " +
       "CID through history-trimming or summarization. NA when no " +
       "compact turn is in the trial plan.",
    e: "State-mode gap — Does the CID survive a 'force_state_ref' jump? " +
       "Only meaningful for api=responses + state=T (chain mode) or " +
       "responses+conv (container mode). Pass = the forced jump back to " +
       "an OLDER previous_response_id / conversation reference still " +
       "carries the same CID. Tests whether server-side state-mode " +
       "(where cidgar can't re-inspect prior turns) preserves CID.",
    f: "GAR richness — Did the LLM populate _ib_gar with all 5 keys " +
       "(goal, need, impact, dspm, alt)? Pass = at least one tool_call " +
       "carries a well-formed GAR object. Fail = present but malformed " +
       "(missing keys, not JSON). NA = LLM omitted GAR (spec §9.2 " +
       "compliant) OR no tool_calls in the trial.",
    h: "Latency overhead — Per-turn governance cost vs paired baseline " +
       "(routing=direct twin via E4 clone-baseline). Pass = mean overhead " +
       "<20% across turns. NA when no baseline twin exists. Higher = AGW " +
       "is meaningfully slowing requests; investigate channel mutations + " +
       "audit emission cost.",
    i: "Snapshot correlation (E20) — For each tools/call audit, did the " +
       "request carry a valid _ib_ss matching a known tools/list snapshot " +
       "hash? Aggregates correlation_lost flags across all tools_calls. " +
       "Pass = ≥80% correlated. Drops below 80% indicate the LLM (often " +
       "smaller models — ollama qwen/llama base) is omitting the required " +
       "_ib_ss param. NA = no tools_call audits in the trial.",
    k: "Cross-API continuity (E24, combo only) — For trials hitting ≥2 " +
       "LLM routes, does at least one CID appear on every route touched? " +
       "Pass = cross-route intersection non-empty (CID survived all LLM " +
       "switches). Fail modes: (A) some route had LLM traffic but no CID " +
       "(agent-side propagation gap), (B) all routes have CIDs but no " +
       "overlap (AGW minted distinct per route — possible isolation " +
       "breach), (C) marker text present in bodies on multiple routes but " +
       "AGW didn't extract — MARKER_RE format mismatch / adapter dropped " +
       "marker / cidgar config inconsistent across routes. NA for " +
       "single-route trials.",
    l: "Run-lineage integrity — parent_rid chain reconstructs across runs.",
    m: "Turn-boundary correctness — is_turn_boundary lands on each turn's first run."
  };
  // T14 — render the `_aborted` marker at the top if present so the user
  // immediately sees that the verdicts below are partial.
  let abortedBanner = "";
  const ab = verdicts["_aborted"];
  if (ab) {
    abortedBanner = `
      <div class="verdict-card aborted verdict-aborted">
        <strong>⏹ Trial aborted</strong> — <em>${escapeHtml(ab.verdict || "aborted")}</em><br>
        <small>${escapeHtml(ab.reason || "abort requested by user")}</small>
        <br><small style="color:#999;">Verdicts below are computed on the turns that completed before abort — treat as partial.</small>
      </div>
    `;
  }
  const verr = verdicts["_verdict_error"];
  if (verr) {
    abortedBanner += `
      <div class="verdict-card error">
        <strong>⚠ Verdict computation error</strong> — <em>${escapeHtml(verr.verdict || "error")}</em><br>
        <small>${escapeHtml(verr.reason || "")}</small>
      </div>
    `;
  }
  return renderIdentifiersBanner(trial) + abortedBanner + ["a","b","c","d","e","f","h","i","k","l","m"].map(lvl => {
    const v = verdicts[lvl] || {verdict: "na", reason: "not computed"};
    const tip = tips[lvl] || "";
    return `
      <div class="verdict-card ${v.verdict}" title="${escapeHtml(tip)}">
        <strong>(${lvl}) ${labels[lvl]}</strong> <span class="verdict-help" title="${escapeHtml(tip)}">ⓘ</span> — <em>${v.verdict}</em><br>
        <small>${escapeHtml(v.reason)}</small>
      </div>
    `;
  }).join("");
}

// ── Note tab ──
//
// Per-axis registry of known limitations / unimplemented features /
// dependencies, evaluated against this trial's config (framework, api,
// llm, mcp, routing, state, stream). Replaces the single E6 rubber
// stamp on the header — that only flagged one of many config-dependent
// gotchas. Notes are grouped by category, severity-coded
// (warn / info / ok), and may carry a docref to the relevant
// enhancements.md / change-ledger / source path.

function renderNoteTab(trial) {
  const c = trial.config || {};
  const notes = collectNotes(c);
  if (!notes.length) {
    return `<p>✓ No known issues for this configuration. Cidgar should govern this trial fully.</p>`;
  }
  // Group by category — sort order in collectNotes() determines section order.
  const byCategory = {};
  for (const n of notes) {
    (byCategory[n.category] || (byCategory[n.category] = [])).push(n);
  }
  let html = `<p style="color:#666; font-size:13px;">Known limitations / unimplemented features / dependencies for this trial's (${escapeHtml(c.framework)} + ${escapeHtml(c.api)} + ${escapeHtml(c.llm)} + ${escapeHtml(c.mcp || 'NONE')}) configuration:</p>`;
  for (const cat of Object.keys(byCategory)) {
    html += `<h3 class="note-section">${escapeHtml(cat)}</h3>`;
    for (const n of byCategory[cat]) {
      html += `
        <div class="note-card note-${n.severity}">
          <strong>${n.icon} ${escapeHtml(n.title)}</strong>
          <div class="note-body">${escapeHtml(n.body)}</div>
          ${n.docref ? `<div class="note-docref">→ <code>${escapeHtml(n.docref)}</code></div>` : ''}
        </div>`;
    }
  }
  return html;
}

function collectNotes(c) {
  // Returns a flat list of note objects:
  //   {category, severity, icon, title, body, docref?}
  // Sort order: AGW gaps first (most important for verdict interpretation),
  // then framework, LLM, state, routing, MCP. Within a category, order
  // matches the sequence below.
  const notes = [];
  const api = c.api;
  const llm = c.llm;
  const framework = c.framework;
  const mcp = c.mcp;
  const routing = c.routing;
  const state = c.state;
  const stream = c.stream;

  // ── AGW / cidgar dependencies (most important for verdict interpretation) ──
  if (api === "responses" || api === "responses+conv") {
    notes.push({
      category: "AGW cidgar gaps",
      severity: "warn",
      icon: "⚠",
      title: "Responses-API governance not implemented (E6)",
      body: "AGW's cidgar pipeline has no LlmRequest::Responses variant in governance/mod.rs:23. f2/f3/f6/f7 hooks silently no-op for /v1/responses traffic. Channels 1 (tool args CID) and 2 (text marker) WILL NOT FIRE on the LLM path. Channel 3 (MCP tool-result resource block) still works because the MCP session handler is format-agnostic. Verdicts (a) presence + (b) channel structure will show partial pass/fail with mostly empty audit on the LLM side.",
      docref: "docs/enhancements.md#e6 + agw-governance-spec.md §1 out-of-scope",
    });
  }
  if (api === "responses" && state === false) {
    notes.push({
      category: "AGW cidgar gaps",
      severity: "info",
      icon: "ℹ",
      title: "Stateless multi-turn requires AGW E14 (InputCompat)",
      body: "Stateless multi-turn responses re-send prior assistant content with output-only fields (status, structured output_text). AGW's strict InputParam parser used to 503 on these (commit 145df46 + 56f1182e). Requires AGW image built from ibfork/feat/cidgar commit 56f1182e or later. If you see 'data did not match any variant of untagged enum InputParam' in logs, the AGW image is older than E14.",
      docref: "agw-governance-spec.md §14.7 + change-ledger CHG-241",
    });
  }
  if (api === "responses+conv") {
    notes.push({
      category: "AGW cidgar gaps",
      severity: "info",
      icon: "ℹ",
      title: "Conversations API setup requires /v1/conversations passthrough route (E13c)",
      body: "responses+conv uses POST /v1/conversations on first turn to mint conv_xxx. AGW chatgpt route needs `/v1/conversations: passthrough` in ai.routes map (added in agw/config.yaml as part of E13c). Without it, setup call 503s with 'missing field messages' (default Completions parser). No governance instrumentation on setup (passthrough byte-forwards).",
      docref: "docs/enhancements.md#e13c + agw/config.yaml llm-chatgpt route",
    });
  }
  if (stream === true) {
    notes.push({
      category: "AGW cidgar gaps",
      severity: "warn",
      icon: "⚠",
      title: "Streaming bypasses Channels 1 + 2 (E8)",
      body: "AGW's process_streaming (llm/mod.rs:871) skips cidgar's on_llm_response per V5/Plan Addendum Delta D ('non-streaming only'). Streaming trials lose Ch1+Ch2 silently. Only Channel 3 (MCP) and audit-tail capture work. Verdicts (a)/(b) on streaming will reflect this gap.",
      docref: "docs/enhancements.md#e8",
    });
  }

  // ── Per-framework limitations ──
  // I-NEW-1: framework-capability rules consult /info.frameworks
  // (mirror of harness/validator.py::ADAPTER_CAPABILITIES) instead of
  // duplicating the supported-API sets in JS. If a contributor extends
  // ADAPTER_CAPABILITIES, this code automatically picks it up — no
  // parallel JS edit required.
  const FRAMEWORK_CAPABILITY_NOTES = [
    {
      framework: "crewai", apis: ["responses", "responses+conv"],
      title: "crewai adapter does not implement responses/responses+conv (E5c)",
      body: "crewai 1.14 has no first-class Responses API support. Validator may have allowed this combo but the adapter will reject.",
      docref: "harness/validator.py + docs/enhancements.md#e5c",
    },
    {
      framework: "pydantic-ai", apis: ["responses+conv"],
      title: "pydantic-ai adapter does not implement responses+conv (E5d)",
      body: "pydantic-ai 1.86 doesn't first-class-support previous_response_id chaining. Validator-blocked.",
      docref: "docs/enhancements.md#e5d",
    },
    {
      framework: "llamaindex", apis: ["messages"],
      title: "llamaindex adapter does not implement messages (E5e)",
      body: "llamaindex's OpenAI wrapper is OpenAI-only. Messages support would require llama-index-llms-anthropic package.",
      docref: "docs/enhancements.md#e5e",
    },
  ];
  for (const rule of FRAMEWORK_CAPABILITY_NOTES) {
    if (framework !== rule.framework) continue;
    if (!rule.apis.includes(api)) continue;
    // Only fire when /info.frameworks confirms the adapter genuinely
    // does NOT implement this api. `null` (cache cold) → skip silently;
    // `true` (capability surfaced after the JS rule was authored) →
    // also skip (the rule no longer applies).
    if (adapterSupportsApi(framework, api) === false) {
      notes.push({
        category: "Framework limitations",
        severity: "warn",
        icon: "⚠",
        title: rule.title,
        body: rule.body,
        docref: rule.docref,
      });
    }
  }
  if (framework === "langgraph") {
    notes.push({
      category: "Framework limitations",
      severity: "info",
      icon: "ℹ",
      title: "langgraph deprecation warning",
      body: "langgraph.prebuilt.create_react_agent is deprecated since LangGraph V1.0; will be removed in V2.0. Migration to langchain.agents.create_agent tracked as E16. Today emits a DeprecationWarning in pytest output; harmless.",
      docref: "docs/enhancements.md#e16",
    });
  }
  if ((framework === "langchain" || framework === "langgraph") && api === "messages") {
    notes.push({
      category: "Framework limitations",
      severity: "info",
      icon: "ℹ",
      title: "ChatAnthropic httpx hook via cached_property override",
      body: "langchain-anthropic 1.4.1 doesn't accept http_client kwarg. Adapter overrides @cached_property _client / _async_client on the instance before first read (E5a hack). Fragile to library upgrades — if a future langchain-anthropic version changes those attribute names, wire-byte capture silently breaks.",
      docref: "adapters/langchain/framework_bridge.py:_install_anthropic_hooked_clients",
    });
  }
  if (framework === "autogen" && (api === "responses" || api === "responses+conv")) {
    notes.push({
      category: "Framework limitations",
      severity: "info",
      icon: "ℹ",
      title: "autogen bypasses framework for Responses API",
      body: "autogen-ext has no Responses client. Adapter calls openai.AsyncOpenAI(...).responses.create() directly, bypassing AssistantAgent entirely for responses-mode trials. Verdict (e) state-mode tests work but go through the bypass path, not the autogen agent loop.",
      docref: "adapters/autogen/framework_bridge.py:_turn_responses_direct",
    });
  }
  if (framework === "llamaindex" && (api === "responses" || api === "responses+conv")) {
    notes.push({
      category: "Framework limitations",
      severity: "info",
      icon: "ℹ",
      title: "llamaindex bypasses OpenAIResponses for state mode",
      body: "Adapter bypasses llama_index.llms.openai.OpenAIResponses for /responses, going direct to openai SDK for full control over previous_response_id chaining. Same trade-off as autogen.",
      docref: "adapters/llamaindex/framework_bridge.py",
    });
  }

  // ── Per-LLM caveats ──
  if (llm === "chatgpt") {
    notes.push({
      category: "LLM provider",
      severity: "info",
      icon: "ℹ",
      title: "OpenAI: requires OPENAI_API_KEY + credits",
      body: "If account is out of quota, requests return 429 'insufficient_quota' which adapter surfaces as a 500. Verify with: curl -H 'Authorization: Bearer $OPENAI_API_KEY' https://api.openai.com/v1/chat/completions -d '...'.",
    });
  }
  if (llm === "claude") {
    notes.push({
      category: "LLM provider",
      severity: "info",
      icon: "ℹ",
      title: "Anthropic: requires ANTHROPIC_API_KEY + credits",
      body: "Out of credits → 400 'Your credit balance is too low to access the Anthropic API'. Newer accounts may have access only to claude-haiku-4-5 family; older claude-3-5-haiku-20241022 returns 404 'model not found'. Set DEFAULT_CLAUDE_MODEL=claude-haiku-4-5 in .env.",
    });
  }
  if (llm === "gemini") {
    notes.push({
      category: "LLM provider",
      severity: "warn",
      icon: "⚠",
      title: "Gemini: requires GOOGLE_API_KEY (often empty in dev)",
      body: "GOOGLE_API_KEY=\"\" in .env results in adapter ValueError before any HTTP. Set the key OR pick another LLM provider. Gemini routes through /v1beta/openai/ compat endpoint.",
    });
  }
  if (llm === "ollama") {
    notes.push({
      category: "LLM provider",
      severity: "info",
      icon: "ℹ",
      title: "Ollama: model must be pulled on host",
      body: "Adapter calls into host Ollama at host.docker.internal:11434. If the model name (DEFAULT_OLLAMA_MODEL or row override) isn't pulled, returns 404 'model not found'. Run `ollama list` on host to see available; `ollama pull <name>` to add. The dropdown's curated list does NOT reflect what's actually pulled (E10 dynamic discovery filed).",
      docref: "docs/enhancements.md#e10",
    });
  }
  if (llm === "mock") {
    notes.push({
      category: "LLM provider",
      severity: "ok",
      icon: "✓",
      title: "Mock LLM: deterministic, no auth needed",
      body: "mock-llm container in compose. Returns canned responses. Useful for cidgar testing without real-LLM cost or non-determinism.",
    });
  }

  // ── State + stream caveats ──
  if (api === "responses" && state === true) {
    notes.push({
      category: "State semantics",
      severity: "info",
      icon: "ℹ",
      title: "responses + state=T = chain mode (previous_response_id)",
      body: "Adapter threads previous_response_id from prior turn's response. Chain mode is wire-distinct from responses+conv (which uses conversation:{id} container). compact() in chain mode is currently a no-op pending E15 (server-side /v1/responses/{id}/compact endpoint).",
      docref: "docs/enhancements.md#e15",
    });
  }
  if (api === "responses+conv") {
    notes.push({
      category: "State semantics",
      severity: "info",
      icon: "ℹ",
      title: "responses+conv = Conversations API container",
      body: "Adapter mints a conv_xxx via POST /v1/conversations on first turn (E13b), then references it via conversation:{id} on each /v1/responses call. compact() is a no-op (no Conversations-API container-level compact exists).",
      docref: "docs/enhancements.md#e13b",
    });
  }

  // ── Routing ──
  if (routing === "direct") {
    notes.push({
      category: "Routing",
      severity: "warn",
      icon: "⚠",
      title: "Direct routing bypasses AGW entirely",
      body: "All HTTP goes direct to provider (api.openai.com, api.anthropic.com, etc.). NO cidgar governance applies. NO audit entries collected. Used as A/B baseline for governed trials — see Pairs view (🔁 button on matrix row). Verdicts on direct trials will all be 'na' or 'fail' due to absent audit.",
    });
  }

  // ── MCP-specific ──
  if (mcp === "fetch") {
    notes.push({
      category: "MCP server",
      severity: "info",
      icon: "ℹ",
      title: "fetch route uses mcp_marker_kind=both (E7)",
      body: "AGW's mcp-fetch route opts into emitting BOTH a Channel-3 resource block AND a text-content block carrying the same marker. Defense-in-depth for agents that flatten tool_result.content. Other MCP routes (weather/news/library) use the default 'resource' only.",
      docref: "agw/config.yaml mcp-fetch route + docs/enhancements.md#e7",
    });
  }
  if ((mcp === "NONE" || !mcp) && llm && llm !== "NONE") {
    notes.push({
      category: "MCP server",
      severity: "info",
      icon: "ℹ",
      title: "No MCP — Channel 3 won't fire",
      body: "Without MCP tools, no /tools/call requests, so cidgar's f4/f5 (Channel 3 resource block) never fires. Only Channels 1 + 2 (LLM-side) and audit-log entries are available for verdict computation.",
    });
  }

  return notes;
}

// Decode an mcp-session-id header value into a short alias + full id pair.
// Header format observed in framework_events: base64url-encoded JSON of shape
//   {"t":"mcp","s":[{"t":"mutable","s":"<hex>"}]}
// `raw` is expected to be base64url (no `=` padding); the fallback alias
// path assumes that, so a real header never produces padding chars in the
// alias. Returns {alias: <last-6-hex>, full: <raw>} on success; falls back
// to {alias: <last-6-of-raw>, full: <raw>} on decode/parse failure or
// unexpected JSON shape. Returns null when the input is empty/null.
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
  } catch (_e) { /* ignore — handled by the shape-check fallback below */ }
  // Reached on decode/parse failure OR when the JSON shape lacks s[0].s
  // OR when the inner hash is shorter than 6 chars.
  return {alias: raw.slice(-6), full: raw};
}

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
    const phase = audit && audit.phase;   // null entries are tolerated (silent skip below)
    const feType = phaseToType[phase];
    if (!feType) continue;
    // Look up before incrementing — a missing event must NOT advance the
    // counter, otherwise a mid-sequence gap would corrupt every later
    // audit's ordinal binding for this phase.
    const fe = eventsByType[feType][counters[phase]];
    if (!fe) continue;
    counters[phase]++;
    // For mcp_tools_list / mcp_tools_call the session id is always on the
    // request; the response fallback is future-proofing for additional
    // MCP types that might set the header on the response (e.g. mcp_initialize).
    const reqH = (fe.request  && fe.request.headers)  || {};
    const resH = (fe.response && fe.response.headers) || {};
    const raw = reqH["mcp-session-id"] || resH["mcp-session-id"] || null;
    const decoded = _decodeMcpSessionAlias(raw);
    // Silent skip when there's no header — no chip, no console noise; an
    // absent session id is normal for some adapter/MCP combinations.
    if (decoded) out.set(globalIdx, decoded);
  }
  return out;
}

// ── CID flow tab ──
//
// Renders a Mermaid `graph LR` diagram showing the relationship between
// turns, CIDs, and audit entries. CID nodes are the central pivot; turn
// nodes connect to CIDs via channel evidence in the response body, and
// audit-entry nodes connect to CIDs via the audit log's `cid` field.
//
// Visual cues:
// - CID node fill: green if seen in ≥2 turns (chain preserved), yellow if
//   seen in only one turn (single use), red if seen only in audit entries
//   (channel injection broke — governance logged a CID but no turn body
//   carried it on the wire).
// - Turn node border: red if turn.error is truthy.
// - Edges: solid for turn→CID and audit→CID; dotted for turn→audit
//   (time-window or header-demux correlation).

const CID_RE = /ibc_[a-f0-9]{12}/g;

function _scanBodyForCids(t) {
  // Mirrors the rough shape of harness/efficacy.py's CID extractors: scan
  // the JSON-stringified response + framework_events. Catches Channel 1
  // (tool_calls args), Channel 2 (text marker), and Channel 3 (MCP resource
  // block embedded inside framework_events) in one regex pass. Looser than
  // the channel-specific extractors, but appropriate for visualization
  // (we just need "did this turn body contain this CID anywhere?").
  const parts = [];
  if (t.response !== undefined) parts.push(JSON.stringify(t.response));
  if (t.framework_events !== undefined) parts.push(JSON.stringify(t.framework_events));
  const blob = parts.join("");
  const found = new Set();
  for (const m of blob.matchAll(CID_RE)) found.add(m[0]);
  return found;
}

// Shared topology extractor for both CID flow tabs (Mermaid + cytoscape).
// Returns an empty-ish topology when there's no data to visualize; the
// callers decide what empty-state UI to show. Mermaid renderer composes
// IDs/strings on top of this; cytoscape renderer maps directly to its
// elements/classes shape. Keeping IDs out of this helper lets each renderer
// own its own ID conventions (Mermaid: T0, A0, C_<hex>, SS_<hash>;
// cytoscape: same identifiers reused — happens to match for free).
function _buildCidFlowTopology(trial) {
  const turns = trial.turns || [];
  const audits = trial.audit_entries || [];

  // Per-turn CID sets + global universe.
  const turnCids = turns.map(t => _scanBodyForCids(t));
  const auditCids = new Set();
  for (const a of audits) if (a.cid) auditCids.add(a.cid);
  const allCids = new Set();
  for (const s of turnCids) for (const c of s) allCids.add(c);
  for (const c of auditCids) allCids.add(c);

  // Per-CID survivability classification (preserved/single/auditonly).
  const cidClass = {};
  for (const cid of allCids) {
    let inTurns = 0;
    for (const s of turnCids) if (s.has(cid)) inTurns += 1;
    if (inTurns >= 2) cidClass[cid] = "preserved";
    else if (inTurns === 1) cidClass[cid] = "single";
    else cidClass[cid] = "auditonly";
  }

  // Snapshot (E20) audits + classification.
  const ssAudits = audits.map((a, i) => ({
    i, phase: a.phase || "", ss: _auditSnapshotHash(a),
  })).filter(x => x.ss);
  const allSnapshots = new Set(ssAudits.map(x => x.ss));
  const ssClass = {};
  for (const ss of allSnapshots) {
    const hasCall = ssAudits.some(x => x.ss === ss && x.phase === "tool_call");
    ssClass[ss] = hasCall ? "snapshotconsumed" : "snapshotorphan";
  }

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

  // turn → CID edges (one per (turn, cid) pair).
  const turnToCid = [];
  turns.forEach((_, i) => {
    for (const cid of turnCids[i]) turnToCid.push({turnIdx: i, cid});
  });
  // audit → CID edges (only when the audit's cid is in allCids).
  const auditToCid = [];
  audits.forEach((a, i) => {
    if (a.cid && allCids.has(a.cid)) auditToCid.push({auditIdx: i, cid: a.cid});
  });

  return {
    turns: turns.map((t, i) => ({
      idx: i, kind: (t.kind || "?"), errored: !!t.error,
    })),
    audits: audits.map((a, i) => {
      const s = auditSessions.get(i) || null;
      return {
        idx: i, phase: a.phase || "audit", cid: a.cid || null,
        ss: _auditSnapshotHash(a),
        sid:     s ? s.alias : null,
        sidFull: s ? s.full  : null,
      };
    }),
    cids: [...allCids].map(cid => ({
      cid, klass: cidClass[cid],
    })),
    snapshots: [...allSnapshots].map(ss => ({
      hash: ss, klass: ssClass[ss],
    })),
    edges: {
      turnToCid, auditToCid, turnToAudit,
      ssAudits,   // [{i, phase, ss}] — used for SS→list/SS→call edges
    },
    counts: {
      turns: turns.length, audits: audits.length, cids: allCids.size,
      preserved: Object.values(cidClass).filter(c => c === "preserved").length,
      single: Object.values(cidClass).filter(c => c === "single").length,
      auditOnly: Object.values(cidClass).filter(c => c === "auditonly").length,
    },
  };
}

// Design C2 — derive the RID run-lineage overlay model from a trial's audit
// entries, keyed to the SAME audit-node ids the CID-flow graphs use. Both
// renderers number audit nodes `A${i}` where i is the audit_entries index
// (see `idx: i` in _buildCidFlowTopology and `A${a.idx}` in the renderers), so
// reading audit_entries directly here keeps the ids aligned with no coupling.
//
// Returns:
//   ridToNode:    Map<rid, "A${i}">          — each run's node id
//   parentEdges:  Array<["A${p}","A${c}"]>   — resolvable parent_rid links
//   anomalyNodes: Set<"A${i}">               — nodes with parent_rid_anomaly
//   labels:       Map<"A${i}", shortRid>     — short rid label per run node
//
// Robust to null parent_rid (genesis / truncation / pre-CHG-26F): such links
// are simply omitted (the run node stands rooted).
function buildRunLineage(trial) {
  const audits = trial.audit_entries || [];
  const ridToNode = new Map();
  const labels = new Map();
  const anomalyNodes = new Set();
  const rows = [];  // {node, rid, parentRid}

  audits.forEach((e, i) => {
    if ((e.phase || "") !== "llm_request") return;
    const body = e.body || {};
    const rid = body.rid;
    if (!rid) return;
    const node = `A${i}`;
    ridToNode.set(rid, node);
    labels.set(node, _shortRid(rid));
    if (body.parent_rid_anomaly === true) anomalyNodes.add(node);
    rows.push({node, rid, parentRid: body.parent_rid || null});
  });

  const parentEdges = [];
  for (const r of rows) {
    if (!r.parentRid) continue;            // genesis / truncation / null
    const parentNode = ridToNode.get(r.parentRid);
    if (!parentNode) continue;             // parent not in this trial — skip
    parentEdges.push([parentNode, r.node]);
  }

  // originatedEdges: [runNode, toolNode] — the LLM run that REQUESTED each
  // tool_call/tool_response, via parent_run_rid (the direct f3-stamp/f4-pop
  // association, NOT the parent_rid chain). This is the primary RID use case:
  // "which run originated this MCP call." Direction run -> tool (run produced
  // the call). Skips tool phases whose originating run isn't in this trial.
  const originatedEdges = [];
  audits.forEach((e, i) => {
    const ph = e.phase || "";
    if (ph !== "tool_call" && ph !== "tool_response") return;
    const prr = (e.body || {}).parent_run_rid;
    if (!prr) return;
    const runNode = ridToNode.get(prr);
    if (!runNode) return;
    originatedEdges.push([runNode, `A${i}`]);
  });

  return {ridToNode, parentEdges, originatedEdges, anomalyNodes, labels};
}

function _shortRid(rid) {
  // "ibr_4a3590f66ec9" -> "ibr_…0f66ec9": compact for node labels.
  return rid.length > 11 ? `${rid.slice(0, 4)}…${rid.slice(-6)}` : rid;
}

// Design C2 — bind the "Show run lineage" checkbox(es) on the CID-flow tabs.
// Both tabs render their own checkbox (#run-lineage-cb mermaid,
// #run-lineage-cb-cy interactive); flipping either updates the shared
// showRunLineage state and re-renders the trial so both tabs reflect it.
// Idempotent via onchange assignment — safe to call every render cycle.
function _wireRunLineageToggle(trial) {
  for (const id of ["run-lineage-cb", "run-lineage-cb-cy"]) {
    const cb = document.getElementById(id);
    if (!cb) continue;
    cb.onchange = () => {
      showRunLineage = cb.checked;
      renderTrial(trial.trial_id);
    };
  }
}

function renderCidFlowTab(trial) {
  const topo = _buildCidFlowTopology(trial);
  const {turns: tTurns, audits: tAudits, cids: tCids, snapshots: tSnaps,
         edges: tEdges, counts} = topo;

  // Empty state: no turns, no audits, no CIDs.
  if (counts.turns === 0 && counts.audits === 0 && counts.cids === 0) {
    return `<div class="cid-flow"><p class="empty-state-msg">No CID flow to visualize — run the trial first or check verdict (a) for why no CIDs were observed.</p></div>`;
  }
  if (counts.cids === 0) {
    return `
      <div class="cid-flow">
        <p class="empty-state-msg">
          No CIDs found in any turn body or audit entry. Check verdict (a)
          for whether AGW governance fired. Trial has ${counts.turns}
          turn(s) and ${counts.audits} audit entry(ies).
        </p>
      </div>
    `;
  }

  // cidNodeId / ssNodeId are defined at module scope — the cytoscape
  // builder uses the same helpers so the two graphs share node ids.

  let mer = "graph LR\n";

  // Design C2 — RID run-lineage overlay model (null when the toggle is off,
  // so the base diagram is byte-identical to pre-C2).
  const lineage = showRunLineage ? buildRunLineage(trial) : null;

  // Turn nodes — kind is the user-visible label. Errored turns get the
  // erroredTurn class for a red border (see classDef below).
  for (const t of tTurns) {
    const kind = t.kind.replace(/[\[\]"]/g, "");
    const label = `Turn ${t.idx}\n${kind}`;
    mer += `  T${t.idx}["${label}"]\n`;
    if (t.errored) mer += `  class T${t.idx} erroredTurn\n`;
  }

  // CID nodes — assign survivability class for fill color.
  for (const c of tCids) {
    const nid = cidNodeId(c.cid);
    // Show only the last 8 hex chars in the label to keep nodes compact;
    // the full CID is recoverable from hover (Mermaid renders title attrs).
    const shortLabel = `${c.cid.slice(0, 6)}…${c.cid.slice(-4)}`;
    mer += `  ${nid}(["${shortLabel}"])\n`;
    mer += `  class ${nid} cid${c.klass}\n`;
  }

  // Audit entry nodes — phase is the most useful label; fall back to "audit".
  // When the audit corresponds to an MCP call we joined to a framework_event,
  // append the mcp-session-id alias on a second line. The full id is
  // surfaced as an SVG <title> tooltip injected by _injectMcpSessionTitles
  // post-render (Mermaid's own click-directive tooltip overlay isn't
  // reliable under our htmlLabels:false config).
  for (const a of tAudits) {
    const phase = a.phase.replace(/[\[\]"]/g, "");
    let label = a.sid ? `${phase}\n${a.sid}` : phase;
    if (lineage && lineage.labels.has(`A${a.idx}`)) {
      label += `\n${lineage.labels.get(`A${a.idx}`)}`;   // C2 — rid line
    }
    mer += `  A${a.idx}["${label}"]\n`;
    mer += `  class A${a.idx} auditNode\n`;
    if (lineage && lineage.anomalyNodes.has(`A${a.idx}`)) {
      mer += `  class A${a.idx} ridAnomaly\n`;
    }
  }

  // E20 — snapshot (ib_ss) nodes.
  for (const s of tSnaps) {
    // Show first 8 chars (full hash by design); braces around for round-rect
    mer += `  ${ssNodeId(s.hash)}>"_ib_ss=${s.hash}"]\n`;     // asymmetric shape distinguishes from CID
    mer += `  class ${ssNodeId(s.hash)} ${s.klass}\n`;
  }

  // Edges: turn → CID (solid). One edge per (turn, cid) pair where the
  // turn body contained that CID.
  for (const e of tEdges.turnToCid) {
    mer += `  T${e.turnIdx} --> ${cidNodeId(e.cid)}\n`;
  }

  // Edges: audit → CID (solid).
  for (const e of tEdges.auditToCid) {
    mer += `  A${e.auditIdx} --> ${cidNodeId(e.cid)}\n`;
  }

  // E20 — Edges: SS → audit. Direction inverted so SS nodes appear LEFT
  // of the turn/audit/CID column in Mermaid's LR auto-layout (otherwise
  // they end up far right and visually outweigh the rest of the graph).
  // Semantic reads naturally either way:
  //   solid: SS → tools_list  ("this snapshot belongs to this list call")
  //   thick: SS ==> tool_call ("this snapshot was carried into this call")
  for (const x of tEdges.ssAudits) {
    if (x.phase === "tools_list") {
      mer += `  ${ssNodeId(x.ss)} --> A${x.i}\n`;
    } else if (x.phase === "tool_call") {
      mer += `  ${ssNodeId(x.ss)} ==> A${x.i}\n`;     // ==> = thick edge
    }
  }

  // Edges: turn → audit (dotted). Header-demux vs time-window correlation
  // is computed in _buildCidFlowTopology (mirrors pickAudits() above so the
  // graph and the per-turn governance audit panel agree).
  for (const e of tEdges.turnToAudit) {
    mer += `  T${e.turnIdx} -.-> A${e.auditIdx}\n`;
  }

  // Design C2 — dashed, "run"-labelled parent_rid edges (the run chain).
  // Distinct from the turn→audit dotted edges via the |run| edge label.
  if (lineage) {
    for (const [parent, child] of lineage.parentEdges) {
      mer += `  ${parent} -.->|run| ${child}\n`;
    }
    // "originated" edges: run → tool_call/tool_response it requested
    // (parent_run_rid). Labelled |calls| to distinguish from the |run| chain.
    for (const [run, tool] of lineage.originatedEdges) {
      mer += `  ${run} -.->|calls| ${tool}\n`;
    }
  }

  // Style classes. Mermaid classDef syntax: classDef <name> <style;style;...>
  // - cidpreserved: green fill (CID survived ≥2 turns — chain preserved)
  // - cidsingle:    yellow fill (CID seen in exactly one turn)
  // - cidauditonly: red fill (CID in audit but no turn body — channels broke)
  // - erroredTurn:  red border on turn nodes that errored
  // - auditNode:    subtle gray fill so audit nodes don't compete visually
  mer += "\n";
  mer += "  classDef cidpreserved fill:#d4edda,stroke:#28a745,stroke-width:2px,color:#155724;\n";
  mer += "  classDef cidsingle fill:#fff3cd,stroke:#ffc107,stroke-width:2px,color:#856404;\n";
  mer += "  classDef cidauditonly fill:#f8d7da,stroke:#dc3545,stroke-width:2px,color:#721c24;\n";
  // E20 snapshot nodes — purple family (distinct from CID green/yellow/red)
  mer += "  classDef snapshotconsumed fill:#e9d8fd,stroke:#6f42c1,stroke-width:2px,color:#3d1a78;\n";
  mer += "  classDef snapshotorphan fill:#f5f0ff,stroke:#a78bda,stroke-width:1px,stroke-dasharray:3,color:#6c5ba0;\n";
  mer += "  classDef erroredTurn stroke:#dc3545,stroke-width:3px;\n";
  mer += "  classDef auditNode fill:#f0f0f0,stroke:#999,color:#333;\n";
  // Design C2 — anomaly run nodes (parent_rid_anomaly): red dashed border.
  mer += "  classDef ridAnomaly stroke:#dc3545,stroke-width:3px,stroke-dasharray:4;\n";

  // Counts banner above the graph for quick orientation.
  const preservedCount = counts.preserved;
  const singleCount = counts.single;
  const auditOnlyCount = counts.auditOnly;

  return `
    <div class="cid-flow">
      <div class="cid-flow-stats">
        <span><strong>${counts.cids}</strong> unique CID${counts.cids === 1 ? "" : "s"}</span>
        <span> · </span>
        <span><strong>${counts.turns}</strong> turn${counts.turns === 1 ? "" : "s"}</span>
        <span> · </span>
        <span><strong>${counts.audits}</strong> audit entr${counts.audits === 1 ? "y" : "ies"}</span>
        ${preservedCount ? `<span class="cid-legend-chip preserved">${preservedCount} preserved (≥2 turns)</span>` : ""}
        ${singleCount ? `<span class="cid-legend-chip single">${singleCount} single-use</span>` : ""}
        ${auditOnlyCount ? `<span class="cid-legend-chip auditonly">${auditOnlyCount} audit-only (channels broke)</span>` : ""}
        <label class="run-lineage-toggle" title="Overlay the RID run-lineage (parent_rid chain) on this CID flow">
          <input type="checkbox" id="run-lineage-cb" ${showRunLineage ? "checked" : ""}>
          Show run lineage
        </label>
      </div>
      <div class="cid-flow-legend">
        <div><span class="legend-glyph solid">━</span> <strong>Solid</strong> — CID was OBSERVED on the wire (turn body) or in the governance log (audit entry).</div>
        <div><span class="legend-glyph dotted">┄</span> <strong>Dotted</strong> — turn↔audit correlation only (which audits belong to which turn). No CID claim.</div>
        <div><span class="legend-glyph">↪</span> <strong>Audit node second line</strong> (e.g. <code>c967c</code> under <code>tool_call</code>) — last 6 chars of the <code>mcp-session-id</code> for that MCP call. Hover for the full id.</div>
        <details class="cid-flow-help">
          <summary>What each edge means in practice</summary>
          <div class="cid-flow-help-body">
            <p><strong>Solid <code>turn → CID</code></strong> — the harness scanned that turn's request/response bodies and FOUND the CID. This is the strongest signal that AGW's channels did their job:</p>
            <ul>
              <li><strong>C1</strong> (tool args <code>_ib_cid</code>) — AGW injected the CID into the MCP <code>tools/call</code> arguments and the agent passed it through to the next turn.</li>
              <li><strong>C2</strong> (text marker <code>&lt;!-- ib:cid=… --&gt;</code>) — AGW appended an HTML comment to the LLM's text reply and the agent kept it in subsequent turns.</li>
              <li><strong>C3</strong> (MCP resource block) — AGW added a <code>resource</code> content block at <code>gateway-meta://conv/{cid}</code> to the tool result and the agent didn't strip it.</li>
            </ul>
            <p><strong>Solid <code>audit → CID</code></strong> — AGW's governance pipeline emitted a structured log entry tagged with that CID (read off the agentgateway container's stdout via <code>docker logs -f</code>). This is what AGW SAW; pair with the turn→CID edges to see if the channels actually delivered it back to the agent.</p>
            <p><strong>Dotted <code>turn → audit</code></strong> — the harness needs to know which audits "belong" to which turn (e.g., for verdict (h) latency overhead). Two correlation strategies, in order:</p>
            <ol>
              <li><strong>Header-demux</strong> — if the adapter propagated <code>X-Aiplay-Trial-Id</code>/<code>X-Aiplay-Turn-Id</code> headers to AGW, the audit entry carries them and matches exactly.</li>
              <li><strong>Time-window fallback</strong> — when headers aren't present, the harness falls back to "audits whose timestamp lies within [turn.started_at, turn.finished_at]". Less precise (cross-turn races possible) but always available.</li>
            </ol>
            <p><strong>What the colors on CID nodes mean</strong></p>
            <ul>
              <li><span class="legend-color preserved">●</span> <strong>green</strong> — CID appears in <strong>≥2 turns</strong>. Channels worked AND the agent kept the CID across turn boundaries. Verdict (c) <em>continuity</em> passes.</li>
              <li><span class="legend-color single">●</span> <strong>yellow</strong> — CID appears in <strong>1 turn</strong> only. Fine for trivial single-turn trials; suspect for multi-turn ones.</li>
              <li><span class="legend-color auditonly">●</span> <strong>red</strong> — CID appears <strong>only in audit logs</strong>, never on the wire. AGW emitted it but no channel landed it back into the agent's context. Channels broke; verdict (a) <em>presence</em> typically fails.</li>
            </ul>
            <p><strong>E20 snapshot nodes (<code>_ib_ss=…</code>)</strong></p>
            <p>Asymmetric flag-shaped nodes representing tools/list snapshot identities (8-hex SHA-256 prefix), positioned LEFT of the turn column. Edges from each snapshot point AWAY: thin to its <code>tools_list</code> audit ("this snapshot belongs to this list call"); thick to each consuming <code>tool_call</code> audit ("this snapshot was carried into this call" — E20 correlation succeeded).</p>
            <ul>
              <li><span class="legend-color snapshotconsumed">●</span> <strong>solid purple</strong> — snapshot has at least one matching <code>tool_call</code> edge (correlation worked; verdict (i) counts this).</li>
              <li><span class="legend-color snapshotorphan">●</span> <strong>dashed purple</strong> — snapshot generated by AGW but NO downstream <code>tool_call</code> referenced it. Either the agent never invoked tools, or LLM dropped <code>_ib_ss</code> (verdict (i) <code>correlation_lost</code> case — see verdict (i) reason for the rate).</li>
              <li><span style="font-weight:bold">╌▶ |run|</span> <strong>run-lineage</strong> — dashed <code>|run|</code> edge = <code>parent_rid</code> link (run → its parent run).</li>
              <li><span style="font-weight:bold">╌▶ |calls|</span> <strong>run-lineage</strong> — dashed <code>|calls|</code> edge = <code>parent_run_rid</code>: the run that ORIGINATED this <code>tool_call</code>/<code>tool_response</code> (primary MCP-call↔run link).</li>
              <li><span style="color:#dc3545;font-weight:bold">▢</span> <strong>run-lineage</strong> — red dashed node border = <code>parent_rid_anomaly</code> (carriers disagreed on the parent). Each <code>llm_request</code> node also gains its <code>rid</code> on a second line.</li>
            </ul>
            <p class="legend-note">The run-lineage items above render only when the <strong>Show run lineage</strong> toggle is on. (Mermaid draws all overlay edges dashed; the <code>|run|</code> vs <code>|calls|</code> edge labels distinguish them.)</p>
          </div>
        </details>
      </div>
      <pre class="mermaid">${escapeHtml(mer)}</pre>
      <details class="cid-flow-source">
        <summary>Mermaid source (debug)</summary>
        <div class="pre-with-copy">
          ${copyPreBtn()}
          <pre>${escapeHtml(mer)}</pre>
        </div>
      </details>
    </div>
  `;
}

// ── CID flow tab — interactive (cytoscape.js) ──
//
// Same topology as the Mermaid tab (via _buildCidFlowTopology) but
// rendered with cytoscape.js so users can drag nodes around and switch
// auto-layouts. Keeps the static Mermaid tab as the canonical "screenshot
// + copy-the-source" view; this is the explore-the-graph view.
//
// We split rendering in two so the visibility-deferred mount works:
//   renderCidFlowInteractiveTab(trial)  → returns the toolbar+container HTML
//   _buildAndMountCytoscape(trial, el)  → instantiates cytoscape into el
// (Mounting on a display:none parent makes cytoscape measure 0×0 and
// dump every node at the origin — same root cause as Mermaid's getBBox
// problem. Defer until the tab is visible.)

// Last trial reference held so the tab-switch handler can mount
// cytoscape on first navigation (we don't have `trial` in that handler's
// scope, so stash it from the most recent renderTrial pass).
let __lastTrialForCy = null;

function renderCidFlowInteractiveTab(trial) {
  const topo = _buildCidFlowTopology(trial);
  if (topo.counts.turns === 0 && topo.counts.audits === 0 && topo.counts.cids === 0) {
    return `<div class="cid-flow-cy"><p class="empty-state-msg">No CID flow to visualize — run the trial first or check verdict (a) for why no CIDs were observed.</p></div>`;
  }
  if (topo.counts.cids === 0) {
    return `
      <div class="cid-flow-cy">
        <p class="empty-state-msg">
          No CIDs found in any turn body or audit entry. Trial has
          ${topo.counts.turns} turn(s) and ${topo.counts.audits} audit entry(ies).
        </p>
      </div>
    `;
  }
  return `
    <div class="cid-flow-cy">
      <div class="cy-toolbar">
        <label>Layout:
          <select id="cy-layout">
            <option value="dagre" selected>dagre (left-to-right flow)</option>
            <option value="cose">cose (force-directed)</option>
            <option value="breadthfirst">breadthfirst</option>
            <option value="grid">grid</option>
            <option value="circle">circle</option>
          </select>
        </label>
        <button id="cy-fit">Fit</button>
        <button id="cy-reset">Reset positions</button>
        <label class="run-lineage-toggle" title="Overlay the RID run-lineage (parent_rid chain) on this CID flow">
          <input type="checkbox" id="run-lineage-cb-cy" ${showRunLineage ? "checked" : ""}>
          Show run lineage
        </label>
        <span class="cy-hint">Drag nodes to rearrange · scroll to zoom · drag background to pan</span>
      </div>
      <div id="cy-container" class="cy-container"></div>
      <div class="cid-flow-legend">
        <details class="cid-flow-help">
          <summary>Legend + interactions</summary>
          <div class="cid-flow-help-body">
            <p>Same color coding as the static <strong>CID flow</strong> tab. Drag any node to rearrange; scroll-wheel to zoom; drag the background to pan. Use the <strong>Layout</strong> selector to try different auto-arrangements; <strong>Reset positions</strong> reapplies the dagre LR layout (discards your drag).</p>
            <ul>
              <li><span class="legend-color preserved">●</span> green CID — preserved (≥2 turns)</li>
              <li><span class="legend-color single">●</span> yellow CID — single-use (1 turn)</li>
              <li><span class="legend-color auditonly">●</span> red CID — audit-only (channels broke)</li>
              <li><span class="legend-color snapshotconsumed">●</span> solid purple SS — snapshot consumed by a tool_call</li>
              <li><span class="legend-color snapshotorphan">●</span> dashed purple SS — snapshot orphan (no consuming tool_call)</li>
              <li><span style="color:#fd7e14;font-weight:bold">╌▶</span> orange dashed edge — <code>parent_rid</code> run chain (run → parent run)</li>
              <li><span style="color:#20c997;font-weight:bold">╌▶</span> teal "calls" edge — <code>parent_run_rid</code>: the run that ORIGINATED a tool_call/response (primary MCP-call↔run link)</li>
              <li><span style="color:#dc3545;font-weight:bold">▢</span> red dashed node border — <code>parent_rid_anomaly</code> (carriers disagreed on the parent)</li>
            </ul>
            <p><strong>Audit node second line</strong> (e.g. <code>c967c</code> under <code>tool_call</code>) — last 6 chars of the <code>mcp-session-id</code> for that MCP call. Hover for the full id.</p>
            <p>Edges: thin solid for turn→CID and audit→CID; dotted for turn→audit correlation; thick purple for SS→tool_call (E20 correlation). The orange/teal/red <strong>run-lineage</strong> items above render only when the <strong>Show run lineage</strong> toggle is on; each <code>llm_request</code> node also gains its <code>rid</code> in its label.</p>
          </div>
        </details>
      </div>
    </div>
  `;
}

// Mount cytoscape into the tab's container IF the tab is currently visible.
// Returns true on successful mount (caller should clear the
// __cidFlowInteractiveNeedsMount flag), false if hidden or lib missing.
function mountCytoscapeIfVisible(trial) {
  if (!trial) return false;
  const tab = tabContents["cidflow-interactive"];
  if (!tab || !tab.classList.contains("active")) return false;
  const container = tab.querySelector("#cy-container");
  if (!container) return false;
  if (typeof cytoscape === "undefined") {
    console.warn("cytoscape lib not loaded; interactive CID flow tab will be empty");
    container.innerHTML = `<div class="cy-offline">⚠ Cytoscape library unavailable (CDN blocked or offline). Use the static CID flow tab instead.</div>`;
    return false;
  }
  if (__cidFlowInteractiveCy) {
    // Tear down prior instance so we don't leak listeners + canvas.
    try { __cidFlowInteractiveCy.destroy(); } catch {}
    __cidFlowInteractiveCy = null;
  }
  __cidFlowInteractiveCy = _buildAndMountCytoscape(trial, container);
  return true;
}

function _buildAndMountCytoscape(trial, container) {
  const topo = _buildCidFlowTopology(trial);
  // cidNodeId / ssNodeId — module-scope helpers.

  // Design C2 — RID run-lineage overlay model (null when the toggle is off).
  const lineage = showRunLineage ? buildRunLineage(trial) : null;

  const elements = [];
  // Turn nodes
  for (const t of topo.turns) {
    elements.push({
      data: {id: `T${t.idx}`, label: `Turn ${t.idx}\n${t.kind}`},
      classes: t.errored ? "node-turn errored" : "node-turn",
    });
  }
  // CID nodes
  for (const c of topo.cids) {
    const shortLabel = `${c.cid.slice(0, 6)}…${c.cid.slice(-4)}`;
    elements.push({
      data: {id: cidNodeId(c.cid), label: shortLabel, fullCid: c.cid},
      classes: `node-cid cid-${c.klass}`,
    });
  }
  // Audit nodes — when the audit corresponds to an MCP call we joined to
  // a framework_event, append the mcp-session-id alias on a second line
  // and stash the full id on data.sidFull for the hover-tooltip handler.
  for (const a of topo.audits) {
    let label = a.sid ? `${a.phase}\n${a.sid}` : a.phase;
    if (lineage && lineage.labels.has(`A${a.idx}`)) {
      label += `\n${lineage.labels.get(`A${a.idx}`)}`;   // C2 — rid line
    }
    const cls = (lineage && lineage.anomalyNodes.has(`A${a.idx}`))
      ? "node-audit rid-anomaly" : "node-audit";
    elements.push({
      data: {
        id: `A${a.idx}`,
        label,
        sidFull: a.sidFull,
      },
      classes: cls,
    });
  }
  // Snapshot nodes
  for (const s of topo.snapshots) {
    elements.push({
      data: {id: ssNodeId(s.hash), label: `_ib_ss=${s.hash}`},
      classes: `node-ss ss-${s.klass}`,
    });
  }
  // Edges
  for (const e of topo.edges.turnToCid) {
    elements.push({
      data: {source: `T${e.turnIdx}`, target: cidNodeId(e.cid)},
      classes: "edge-turn-cid",
    });
  }
  for (const e of topo.edges.auditToCid) {
    elements.push({
      data: {source: `A${e.auditIdx}`, target: cidNodeId(e.cid)},
      classes: "edge-audit-cid",
    });
  }
  for (const e of topo.edges.turnToAudit) {
    elements.push({
      data: {source: `T${e.turnIdx}`, target: `A${e.auditIdx}`},
      classes: "edge-turn-audit dotted",
    });
  }
  for (const x of topo.edges.ssAudits) {
    if (x.phase === "tools_list") {
      elements.push({
        data: {source: ssNodeId(x.ss), target: `A${x.i}`},
        classes: "edge-ss-list",
      });
    } else if (x.phase === "tool_call") {
      elements.push({
        data: {source: ssNodeId(x.ss), target: `A${x.i}`},
        classes: "edge-ss-call thick",
      });
    }
  }

  // Design C2 — run-chain (parent_rid) edges + run→tool origination
  // (parent_run_rid) edges, when the overlay is on.
  if (lineage) {
    for (const [parent, child] of lineage.parentEdges) {
      elements.push({
        data: {source: parent, target: child},
        classes: "edge-run",
      });
    }
    for (const [run, tool] of lineage.originatedEdges) {
      elements.push({
        data: {source: run, target: tool, label: "calls"},
        classes: "edge-origin",
      });
    }
  }

  // Register dagre layout extension. cytoscape-dagre's UMD build exposes
  // itself as window.cytoscapeDagre; gate registration so cytoscape.use
  // is idempotent (cytoscape throws if you re-register the same name).
  if (typeof window !== "undefined" && window.cytoscapeDagre && !cytoscape.__dagreRegistered) {
    try {
      cytoscape.use(window.cytoscapeDagre);
      cytoscape.__dagreRegistered = true;
    } catch (e) {
      console.warn("cytoscape-dagre registration failed:", e);
    }
  }

  const cy = cytoscape({
    container,
    elements,
    layout: cytoscape.__dagreRegistered
      ? {name: "dagre", rankDir: "LR", spacingFactor: 1.2}
      : {name: "breadthfirst", directed: true},
    style: [
      {selector: "node", style: {
        label: "data(label)",
        "text-wrap": "wrap",
        "text-valign": "center",
        "text-halign": "center",
        "font-size": 10,
        "padding": 6,
        "shape": "round-rectangle",
        "background-color": "#ECECFF",
        "border-color": "#9370DB",
        "border-width": 1,
        "color": "#333",
        "width": "label",
        "height": "label",
      }},
      {selector: "node.node-turn", style: {
        "background-color": "#ECECFF", "shape": "rectangle",
      }},
      {selector: "node.node-turn.errored", style: {
        "border-color": "#dc3545", "border-width": 3,
      }},
      {selector: "node.cid-preserved", style: {
        "background-color": "#d4edda", "border-color": "#28a745",
        "border-width": 2, "shape": "round-rectangle", "color": "#155724",
      }},
      {selector: "node.cid-single", style: {
        "background-color": "#fff3cd", "border-color": "#ffc107",
        "border-width": 2, "shape": "round-rectangle", "color": "#856404",
      }},
      {selector: "node.cid-auditonly", style: {
        "background-color": "#f8d7da", "border-color": "#dc3545",
        "border-width": 2, "shape": "round-rectangle", "color": "#721c24",
      }},
      {selector: "node.node-audit", style: {
        "background-color": "#f0f0f0", "border-color": "#999", "color": "#333",
      }},
      {selector: "node.node-ss", style: {
        "background-color": "#e9d8fd", "border-color": "#6f42c1",
        "border-width": 2, "shape": "tag", "color": "#3d1a78",
      }},
      {selector: "node.ss-snapshotorphan", style: {
        "background-color": "#f5f0ff", "border-color": "#a78bda",
        "border-style": "dashed", "color": "#6c5ba0",
      }},
      {selector: "edge", style: {
        "width": 1.5, "line-color": "#999",
        "target-arrow-shape": "triangle", "target-arrow-color": "#999",
        "curve-style": "bezier",
      }},
      {selector: "edge.dotted", style: {"line-style": "dotted"}},
      {selector: "edge.thick", style: {
        "width": 3.5, "line-color": "#6f42c1", "target-arrow-color": "#6f42c1",
      }},
      // Design C2 — run-chain (parent_rid) edges + anomaly run nodes.
      {selector: "edge.edge-run", style: {
        "line-style": "dashed", "line-color": "#fd7e14",
        "target-arrow-color": "#fd7e14", "width": 2,
      }},
      {selector: "edge.edge-origin", style: {
        "line-style": "dashed", "line-color": "#20c997",
        "target-arrow-color": "#20c997", "width": 2,
        "label": "data(label)", "font-size": 8, "color": "#0c7a5b",
        "text-rotation": "autorotate",
      }},
      {selector: "node.rid-anomaly", style: {
        "border-color": "#dc3545", "border-width": 3, "border-style": "dashed",
      }},
    ],
  });

  // Toolbar wiring. Use closest()-rooted lookups so we don't depend on
  // the toolbar living anywhere specific in the DOM tree.
  const toolbarRoot = container.parentElement;
  const layoutSel = toolbarRoot && toolbarRoot.querySelector("#cy-layout");
  const fitBtn    = toolbarRoot && toolbarRoot.querySelector("#cy-fit");
  const resetBtn  = toolbarRoot && toolbarRoot.querySelector("#cy-reset");
  if (layoutSel) {
    layoutSel.addEventListener("change", e => {
      const name = e.target.value;
      let opts;
      if (name === "dagre") {
        opts = cytoscape.__dagreRegistered
          ? {name: "dagre", rankDir: "LR", spacingFactor: 1.2}
          : {name: "breadthfirst", directed: true};
      } else {
        opts = {name};
      }
      cy.layout(opts).run();
    });
  }
  if (fitBtn)   fitBtn.addEventListener("click", () => cy.fit(undefined, 30));
  if (resetBtn) resetBtn.addEventListener("click", () => {
    const opts = cytoscape.__dagreRegistered
      ? {name: "dagre", rankDir: "LR", spacingFactor: 1.2}
      : {name: "breadthfirst", directed: true};
    cy.layout(opts).run();
  });

  // Native-browser tooltip for audits that carry a session id. Set the
  // container's `title` attr on hover; clear on mouseout. Browser shows
  // its default tooltip after the usual hover delay. No vendor lib needed.
  // Selector `node[sidFull]` is cytoscape's "data attr is truthy" form;
  // `null` (audits with no session match) and `undefined` are filtered.
  // The comparison form `node[sidFull != null]` is NOT valid cytoscape
  // selector syntax — `null` isn't a recognized literal there.
  cy.on("mouseover", "node[sidFull]", evt => {
    container.setAttribute("title", `mcp-session-id: ${evt.target.data("sidFull")}`);
  });
  cy.on("mouseout", "node[sidFull]", () => {
    container.removeAttribute("title");
  });

  return cy;
}

// ── Services topology tab ──
//
// Renders a Mermaid network/services map for ONE trial: agent ↔ AGW ↔
// {LLM providers, MCP servers}. Identity extracted strictly from data
// AGW would see directly or indirectly on the wire (MCP initialize
// handshake clientInfo/serverInfo, LLM User-Agent header, request body
// model fields, AGW route paths). Falls back to framework name + route
// hostname when richer identity is unavailable.
//
// Wire-shape note: MCP responses (initialize, tools/list) come through
// AGW as text/event-stream — `response.body` is an SSE STRING like
// `"data: {jsonrpc..., result.serverInfo}\n\n"`. We SSE-parse the body
// here to recover the JSON-RPC payload. LLM responses are plain JSON
// dicts (Anthropic /v1/messages, OpenAI /v1/chat/completions).
function renderServicesTab(trial) {
  const topo = extractServicesTopology(trial);

  if (!topo.llms.size && !topo.mcps.size) {
    return `<p>No service topology to map — no LLM or MCP traffic captured in this trial's framework_events.</p>`;
  }

  // Build Mermaid graph LR definition. Use \n (not <br>) for line breaks
  // because the trial-level mermaid.initialize sets htmlLabels:false
  // (renders labels as <text>/<tspan> for Firefox SVG correctness).
  let mer = "graph LR\n";

  // Agent cluster
  mer += `  subgraph Agent["Agent (framework: ${escapeMermaid(trial.config?.framework || '?')})"]\n`;
  if (topo.agentMcpClient) {
    mer += `    A_MCP["MCP client\n${escapeMermaid(topo.agentMcpClient.name)}\n${escapeMermaid(topo.agentMcpClient.version || '(no version)')}"]\n`;
  }
  if (topo.agentLlmClient) {
    mer += `    A_LLM["LLM client\nUA: ${escapeMermaid(truncate(topo.agentLlmClient, 50))}"]\n`;
  }
  if (!topo.agentMcpClient && !topo.agentLlmClient) {
    mer += `    A_DEFAULT["${escapeMermaid(trial.config?.framework || 'agent')}"]\n`;
  }
  mer += `  end\n`;

  // AGW pivot
  mer += `  AGW["AGW cidgar\n(observer)"]\n`;

  // LLM cluster
  if (topo.llms.size > 0) {
    mer += `  subgraph LLMs["LLM providers"]\n`;
    for (const [route, info] of topo.llms) {
      const modelStr = info.models.size ? Array.from(info.models).join(", ") : "(unknown)";
      const tokenStr = info.totalTokens ? `${info.totalTokens} tokens` : "";
      mer += `    L_${sanitizeId(route)}["${escapeMermaid(route)}\n${escapeMermaid(modelStr)}\n${info.calls} calls${tokenStr ? '\n' + escapeMermaid(tokenStr) : ''}"]\n`;
    }
    mer += `  end\n`;
  }

  // MCP cluster
  if (topo.mcps.size > 0) {
    mer += `  subgraph MCPs["MCP servers"]\n`;
    for (const [route, info] of topo.mcps) {
      const serverLabel = info.serverInfo
        ? `${info.serverInfo.name} ${info.serverInfo.version || ''}`
        : `(no serverInfo)`;
      const toolStr = info.toolNames?.size
        ? Array.from(info.toolNames).slice(0, 3).join(", ") + (info.toolNames.size > 3 ? '...' : '')
        : "no tools called";
      mer += `    M_${sanitizeId(route)}["${escapeMermaid(route)}\n${escapeMermaid(serverLabel)}\n${info.callCount} calls (${escapeMermaid(toolStr)})"]\n`;
    }
    mer += `  end\n`;
  }

  // Edges agent → AGW
  if (topo.agentMcpClient) mer += `  A_MCP --> AGW\n`;
  if (topo.agentLlmClient) mer += `  A_LLM --> AGW\n`;
  if (!topo.agentMcpClient && !topo.agentLlmClient) mer += `  A_DEFAULT --> AGW\n`;

  // Edges AGW → LLMs
  for (const route of topo.llms.keys()) {
    mer += `  AGW --> L_${sanitizeId(route)}\n`;
  }

  // Edges AGW → MCPs
  for (const route of topo.mcps.keys()) {
    mer += `  AGW --> M_${sanitizeId(route)}\n`;
  }

  return `
    <div class="services-topology">
      <p>Service topology for this trial — derived from AGW-observable wire data
         (MCP initialize handshakes, LLM User-Agent + body.model, route paths).
         All extraction happens harness-side from <code>framework_events</code>;
         no data here would be unavailable to AGW itself.</p>
      <pre class="mermaid">${escapeHtml(mer)}</pre>
      <details class="services-source">
        <summary>Mermaid source (debug)</summary>
        <div class="pre-with-copy">
          ${copyPreBtn()}
          <pre>${escapeHtml(mer)}</pre>
        </div>
      </details>
      <details class="services-raw">
        <summary>Extracted topology (raw)</summary>
        <div class="pre-with-copy">
          ${copyPreBtn()}
          <pre>${escapeHtml(JSON.stringify(serializeTopo(topo), null, 2))}</pre>
        </div>
      </details>
    </div>`;
}

function extractServicesTopology(trial) {
  // Output:
  //   { agentMcpClient: {name, version} | null,
  //     agentLlmClient: "User-Agent string" | null,
  //     llms: Map<routeKey, {calls, models: Set<string>, totalTokens}>,
  //     mcps: Map<routeKey, {callCount, serverInfo: {name, version} | null,
  //                          toolNames: Set<string>}> }
  const topo = {
    agentMcpClient: null,
    agentLlmClient: null,
    llms: new Map(),
    mcps: new Map(),
  };

  for (const turn of (trial.turns || [])) {
    for (const ev of (turn.framework_events || [])) {
      const phase = ev.t || ev.kind || "";
      const req = ev.request || {};
      const resp = ev.response || {};
      const reqBody = req.body && typeof req.body === "object" ? req.body : {};
      // MCP responses are SSE strings; LLM responses are JSON dicts.
      // Normalize: try parsed-dict first, else SSE-parse the string.
      const respBody = _coerceRespBody(resp.body);
      const reqHeaders = req.headers || {};

      if (phase === "mcp_initialize") {
        const ci = reqBody.params?.clientInfo;
        if (ci?.name && !topo.agentMcpClient) {
          topo.agentMcpClient = {name: ci.name, version: ci.version || null};
        }
        const si = respBody?.result?.serverInfo;
        const route = mcpRouteFromUrl(req.url);
        if (route) {
          const entry = topo.mcps.get(route) || {callCount: 0, serverInfo: null, toolNames: new Set()};
          if (si?.name) entry.serverInfo = {name: si.name, version: si.version || null};
          topo.mcps.set(route, entry);
        }
      } else if (phase === "mcp_tools_list") {
        const route = mcpRouteFromUrl(req.url);
        if (route) {
          const entry = topo.mcps.get(route) || {callCount: 0, serverInfo: null, toolNames: new Set()};
          // Don't bump callCount for tools_list (it's a discovery, not an invocation)
          topo.mcps.set(route, entry);
        }
      } else if (phase === "mcp_tools_call") {
        const route = mcpRouteFromUrl(req.url);
        if (route) {
          const entry = topo.mcps.get(route) || {callCount: 0, serverInfo: null, toolNames: new Set()};
          entry.callCount += 1;
          const toolName = ev.tool_name || reqBody.params?.name;
          if (toolName) entry.toolNames.add(toolName);
          topo.mcps.set(route, entry);
        }
      } else if (phase.startsWith?.("llm_hop") || phase === "llm_request") {
        const route = llmRouteFromUrl(req.url);
        if (route) {
          const entry = topo.llms.get(route) || {calls: 0, models: new Set(), totalTokens: 0};
          entry.calls += 1;
          if (reqBody.model) entry.models.add(reqBody.model);
          // Pull token count from response if present. OpenAI
          // /v1/chat/completions has usage.total_tokens. Anthropic
          // /v1/messages has usage.input_tokens + usage.output_tokens
          // (no total_tokens). Handle both shapes.
          const usage = respBody?.usage || respBody?.usage_metadata;
          if (usage) {
            let tt = usage.total_tokens;
            if (tt == null && (usage.input_tokens != null || usage.output_tokens != null)) {
              tt = (usage.input_tokens || 0) + (usage.output_tokens || 0);
            }
            if (tt) entry.totalTokens += tt;
          }
          topo.llms.set(route, entry);
        }
        // First-seen User-Agent wins (later hops are typically same client)
        if (!topo.agentLlmClient) {
          const ua = reqHeaders["user-agent"] || reqHeaders["User-Agent"];
          if (ua) topo.agentLlmClient = ua;
        }
      }
    }
  }
  return topo;
}

// Coerce a framework_events response.body into a JSON-RPC/JSON dict if
// possible. MCP comes through as SSE (`"data: {...}\n\n"` strings);
// LLM bodies are already dicts.
function _coerceRespBody(b) {
  if (b && typeof b === "object") return b;
  if (typeof b !== "string" || !b) return null;
  // Reuse the same SSE-data-line tolerance as tryParseSSE() above, but
  // unwrap the single-payload case for serverInfo extraction.
  if (b.includes("data:")) {
    for (const line of b.split(/\r?\n/)) {
      const trimmed = line.trim();
      if (!trimmed.startsWith("data:")) continue;
      const payload = trimmed.slice(5).trim();
      if (!payload) continue;
      try { return JSON.parse(payload); } catch { /* skip non-JSON */ }
    }
  }
  // Fallback: maybe a bare JSON string
  try { return JSON.parse(b); } catch { return null; }
}

function mcpRouteFromUrl(url) {
  if (!url) return null;
  // e.g. http://agentgateway:8080/mcp/fetch → "mcp-fetch"
  const m = url.match(/\/mcp\/([^/?]+)/);
  return m ? `mcp-${m[1]}` : null;
}

function llmRouteFromUrl(url) {
  if (!url) return null;
  // e.g. http://agentgateway:8080/llm/chatgpt/v1/chat/completions → "chatgpt"
  const m = url.match(/\/llm\/([^/?]+)/);
  return m ? m[1] : null;
}

function sanitizeId(s) {
  return s.replace(/[^a-zA-Z0-9_]/g, "_");
}

function escapeMermaid(s) {
  // Mermaid label-text safe-encoding for ["..."] node syntax. The outer
  // Mermaid grammar mis-parses bare quotes, brackets, AND parens inside
  // labels (rejecting on the first ")" that closes "[" prematurely).
  // Encode via HTML numeric entities — Mermaid decodes them at label-
  // render time. Without paren-escape, labels like "MCP server (fetch)"
  // throw inside mermaid.run() and the diagram silently stays as source.
  return String(s ?? "")
    .replace(/&/g, "&amp;")
    .replace(/"/g, "&quot;")
    .replace(/\[/g, "&#91;")
    .replace(/\]/g, "&#93;")
    .replace(/\(/g, "&#40;")
    .replace(/\)/g, "&#41;");
}

function truncate(s, n) {
  if (!s) return "";
  return s.length > n ? s.slice(0, n - 1) + "…" : s;
}

function serializeTopo(topo) {
  // Convert Sets/Maps to plain objects for JSON.stringify in the debug view
  return {
    agentMcpClient: topo.agentMcpClient,
    agentLlmClient: topo.agentLlmClient,
    llms: Object.fromEntries(
      Array.from(topo.llms.entries()).map(([k, v]) => [k, {
        calls: v.calls,
        models: Array.from(v.models),
        totalTokens: v.totalTokens,
      }])
    ),
    mcps: Object.fromEntries(
      Array.from(topo.mcps.entries()).map(([k, v]) => [k, {
        callCount: v.callCount,
        serverInfo: v.serverInfo,
        toolNames: Array.from(v.toolNames || []),
      }])
    ),
  };
}

function attachPoll(tid) {
  // Poll the regular trial GET every 2s so turn cards + verdicts appear
  // as they get persisted. Clears itself once the trial reaches a
  // terminal status. Replaced the SSE subscriber — the server-side
  // /trials/{id}/stream endpoint only emitted status pings, so polling
  // the same data directly is strictly simpler.
  if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
  pollTimer = setInterval(async () => {
    const status = await renderTrial(tid);
    if (status && !["running"].includes(status)) {
      clearInterval(pollTimer);
      pollTimer = null;
    }
  }, 2000);
}

// Row-id mode: poll /matrix/row/{id} for a newly-attached trial.
function startRowWatchPoll() {
  if (!rowId || currentTrialId) return;
  const t = setInterval(async () => {
    try {
      const r = await fetch(`${API_BASE}/matrix/row/${rowId}`);
      if (!r.ok) return;
      const row = await r.json();
      if (row.last_trial_id && row.last_trial_id !== currentTrialId) {
        currentTrialId = row.last_trial_id;
        clearInterval(t);
        const status = await renderTrial(currentTrialId);
        if (status === "running") attachPoll(currentTrialId);
      }
    } catch {}
  }, 2000);
}

// ── Bootstrap ──
(async () => {
  // I-NEW-1: prime the framework-capability cache before first render so
  // NOTE-tab rules that consult it get accurate answers on the first paint
  // (rather than null → skip → reappear on the second SSE tick).
  await ensureFrameworksInfo();
  const initialStatus = await fetchAndRender();
  if (currentTrialId && initialStatus === "running") {
    attachPoll(currentTrialId);
  } else if (rowId && !currentTrialId) {
    startRowWatchPoll();
  }
})();

// ── Customer Conversation View ──
// Customer-value-oriented rendering of a trial: CID → turn → llm/tool tree
// with anomaly callouts. See docs/superpowers/specs/2026-05-31-customer-
// conversation-view-design.md for the full design.

function renderConversationTab(trial) {
  const tree = buildConversationTree(trial);
  if (!tree) {
    return `<p style="padding:16px;">No conversation data for this trial.</p>`;
  }
  detectTurnAnomalies(tree);
  const pitch = generateElevatorPitch(trial, tree);

  const header = `<header class="conv-header" id="conv-header">
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
 * Last-5-hex of an mcp-session-id, or "?????" if absent. Matches the
 * abbreviation used in the CID flow tab's audit node second line.
 */
function _shortMcpSession(sid) {
  if (!sid || typeof sid !== "string") return "?????";
  return sid.length >= 6 ? sid.slice(-5) : sid;
}

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

  for (let ti = 0; ti < turns.length; ti++) {
    const t = turns[ti];
    const {inWindow} = _partitionAuditsByWindow(
      audits, t.started_at, t.finished_at
    );

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
        // Strict orphan rule (§6.1): orphan if parent_run_rid is missing OR
        // doesn't resolve to an llm_request seen IN THIS TURN. `ridToNode`
        // is the per-turn map; a hit here implies the rid is also in the
        // trial-level `rid2Run` index by construction.
        const ownerRun = prr ? ridToNode.get(prr) : null;
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
        if (ownerRun) {
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
  // Idempotency: clear Task-5-added anomalies/findings before re-walking.
  // Preserve build-time anomalies (source starts with "audit#") that
  // buildConversationTree placed on turn.anomalies — e.g. snapshot_orphan.
  for (const cid of tree.cids) {
    cid.badge = "pass";
    for (const t of cid.turns) {
      t.badge = "pass";
      t.anomalies = (t.anomalies || []).filter(
        a => a && typeof a.source === "string" && a.source.startsWith("audit#")
      );
      for (const tn of t.orphanToolCalls) tn.anomalies = [];
    }
  }

  // Compute once — used inside the per-turn turn-boundary check below.
  const anyHasBoundary = tree.cids.some(
    c => c.turns.some(t => t.llmRuns.some(r => r.isTurnBoundary))
  );

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
    if (tree.header.status === "fail" || tree.header.status === "error") return "fail";
    if (tree.multiCidAnomaly) return "fail";
    if (tree.cids.some(c => c.badge === "fail")) return "fail";
    if (tree.cids.some(c => c.badge !== "pass")) return "warn";
    return "pass";
  })();

  tree.findings = findings;
}

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
