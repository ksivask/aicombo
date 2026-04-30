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
    return true;
  } catch (e) {
    console.warn(`${tabKey} Mermaid render failed:`, e);
    return false;
  }
}

// Mermaid + cytoscape node id helpers — both renderers use the same
// scheme so hover/debug feel symmetric across the two tabs. CID nodes
// strip the "ib_" prefix for compactness; snapshot nodes keep the full
// hash. Lifted to module scope to dedup the two render paths
// (renderCidFlowTab + _buildAndMountCytoscape).
const cidNodeId = cid => `C_${cid.slice(3)}`;
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
function _collectIdentifiers(trial) {
  const cids = new Set();
  const snapshots = new Set();
  for (const a of (trial.audit_entries || [])) {
    if (a.cid) cids.add(a.cid);
    const ss = _auditSnapshotHash(a);
    if (ss) snapshots.add(ss);
  }
  return {cids: [...cids].sort(), snapshots: [...snapshots].sort()};
}

function renderIdentifiersBanner(trial) {
  const {cids, snapshots} = _collectIdentifiers(trial);
  // CIDs and snapshot hashes are pure-hex by construction (XSS-safe
  // today), but defensive escapeHtml is cheap and survives any future
  // identifier-format change.
  const cidList = cids.length ? escapeHtml(cids.join(", ")) : "<em>(none observed)</em>";
  const ssList = snapshots.length ? escapeHtml(snapshots.join(", ")) : "<em>(none observed)</em>";
  return `
    <div class="identifiers-banner">
      <div><strong>CIDs (${cids.length}):</strong> <code>${cidList}</code></div>
      <div><strong>Snapshots (${snapshots.length}):</strong> <code>${ssList}</code></div>
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
       "single-route trials."
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
  return renderIdentifiersBanner(trial) + abortedBanner + ["a","b","c","d","e","f","h","i","k"].map(lvl => {
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
    const phase = audit && audit.phase;
    const feType = phaseToType[phase];
    if (!feType) continue;
    const k = counters[phase]++;
    const fe = eventsByType[feType][k];
    if (!fe) continue;
    // Try request headers first, then response headers, for the session id.
    const reqH = (fe.request  && fe.request.headers)  || {};
    const resH = (fe.response && fe.response.headers) || {};
    const raw = reqH["mcp-session-id"] || resH["mcp-session-id"] || null;
    const decoded = _decodeMcpSessionAlias(raw);
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

const CID_RE = /ib_[a-f0-9]{12}/g;

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
    audits: audits.map((a, i) => ({
      idx: i, phase: a.phase || "audit", cid: a.cid || null,
      ss: _auditSnapshotHash(a),
    })),
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
  for (const a of tAudits) {
    const phase = a.phase.replace(/[\[\]"]/g, "");
    mer += `  A${a.idx}["${phase}"]\n`;
    mer += `  class A${a.idx} auditNode\n`;
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
      </div>
      <div class="cid-flow-legend">
        <div><span class="legend-glyph solid">━</span> <strong>Solid</strong> — CID was OBSERVED on the wire (turn body) or in the governance log (audit entry).</div>
        <div><span class="legend-glyph dotted">┄</span> <strong>Dotted</strong> — turn↔audit correlation only (which audits belong to which turn). No CID claim.</div>
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
            </ul>
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
            </ul>
            <p>Edges: thin solid for turn→CID and audit→CID; dotted for turn→audit correlation; thick purple for SS→tool_call (E20 correlation).</p>
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
  // Audit nodes
  for (const a of topo.audits) {
    elements.push({
      data: {id: `A${a.idx}`, label: a.phase},
      classes: "node-audit",
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
