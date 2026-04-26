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
const elRowSummary = document.getElementById("row-summary");

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
  raw: document.getElementById("tab-raw"),
};
const tabBtns = document.querySelectorAll(".trial-tab-btn");

tabBtns.forEach(btn => btn.addEventListener("click", () => {
  tabBtns.forEach(b => b.classList.remove("active"));
  Object.values(tabContents).forEach(c => c.classList.remove("active"));
  btn.classList.add("active");
  tabContents[btn.dataset.tab].classList.add("active");
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
          <pre>${escapeHtml(JSON.stringify(parsed, null, 2))}</pre>
        </details>
        <details><summary><strong>Raw</strong> — wire bytes (${b.length} chars)</summary>
          <pre>${escapeHtml(b)}</pre>
        </details>`;
    }
    // Plain string, not SSE — show as raw string
    return `<pre>${escapeHtml(b)}</pre>`;
  }
  // Dict/list: pretty JSON
  return `<pre>${escapeHtml(JSON.stringify(b, null, 2))}</pre>`;
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
function renderAudit(entries) {
  if (!entries.length) return "<em>(no audit entries captured in this turn's time window)</em>";
  return entries.map(a => `
    <div class="audit-entry">
      <span class="badge ${a.phase || 'unknown'}">phase: ${a.phase || '?'}</span>
      <span class="badge">cid: ${a.cid || '∅'}</span>
      <span class="badge">backend: ${shortenBackend(a.backend)}</span>
      ${a.captured_at ? `<span class="badge">ts: ${a.captured_at.slice(11, 23)}</span>` : ''}
      <details><summary>raw governance log</summary><pre>${escapeHtml(JSON.stringify(a.raw, null, 2))}</pre></details>
    </div>
  `).join("");
}
function shortenBackend(b) {
  if (!b) return "?";
  const parts = b.split("/");
  return parts[parts.length - 2] || b;
}
function escapeHtml(s) {
  return String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
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

  tabContents.turns.innerHTML = (trial.turns || []).map((t, i) => renderTurnCard(trial, t, i)).join("")
    || "<p>Turn execution started — turn cards will appear here as turns complete.</p>";

  const plan = trial.turn_plan || {turns: []};
  tabContents.plan.innerHTML = renderPlanTab(plan, (trial.turns || []).length);

  const verdicts = trial.verdicts || {};
  tabContents.verdicts.innerHTML = renderVerdictsTab(verdicts);

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

function renderVerdictsTab(verdicts) {
  const labels = {
    a: "Presence", b: "Channel structure", c: "Continuity",
    d: "Resilience", e: "State-mode gap", f: "GAR richness"
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
    c: "Continuity — Did the SAME CID survive across consecutive turns? " +
       "Pass = ≥3 turns share at least one CID (the conversation didn't " +
       "fragment into multiple CIDs). Detects cases where Channels 1+2+3 " +
       "all fired but the agent dropped them between turns.",
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
       "compliant) OR no tool_calls in the trial."
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
  return abortedBanner + ["a","b","c","d","e","f"].map(lvl => {
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
  const initialStatus = await fetchAndRender();
  if (currentTrialId && initialStatus === "running") {
    attachPoll(currentTrialId);
  } else if (rowId && !currentTrialId) {
    startRowWatchPoll();
  }
})();
