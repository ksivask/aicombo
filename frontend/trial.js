// Full-page trial view. Fetches /trials/{id}, renders row-config 1-liner + details.
// Subscribes to SSE for live updates while trial is running.

const API_BASE = "";
const params = new URLSearchParams(location.search);
const trialId = params.get("id");

const elTitle = document.getElementById("trial-title");
const elStatus = document.getElementById("trial-status-pill");
const elRowSummary = document.getElementById("row-summary");
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

if (!trialId) {
  elTitle.textContent = "Error";
  document.body.innerHTML = "<p style='padding:20px;'>No trial ID in URL. Expected <code>?id=&lt;trial_id&gt;</code>.</p>";
  throw new Error("no trial id");
}

elTitle.textContent = `Trial ${trialId.slice(0, 8)}…`;
document.title = `aiplay — ${trialId.slice(0, 8)}`;

// ── Render helpers ──

function renderHeaders(h) {
  if (!h || Object.keys(h).length === 0) return "<em>(none)</em>";
  return `<table class="kv"><tbody>${Object.entries(h).map(([k, v]) =>
    `<tr><td class="k">${escapeHtml(k)}</td><td class="v">${escapeHtml(String(v))}</td></tr>`
  ).join("")}</tbody></table>`;
}

function renderBody(b) {
  if (b === null || b === undefined) return "<em>(empty)</em>";
  return `<pre>${escapeHtml(JSON.stringify(b, null, 2))}</pre>`;
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

function renderRowSummary(trial) {
  const c = trial.config || {};
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
    `<span class="chip"><span class="chip-k">${escapeHtml(k)}</span>${escapeHtml(v)}</span>`
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

function renderTurnCard(trial, t, i) {
  const req = t.request || {};
  const resp = t.response || {};
  const audits = pickAudits(trial, t);
  return `
    <div class="turn-card">
      <h4>Turn ${i}: ${escapeHtml(t.kind)} <span class="turn-id">${escapeHtml(t.turn_id || '')}</span></h4>

      <details open><summary><strong>Request</strong> — what the adapter sent (pre-cidgar mutation)</summary>
        <div class="section">
          <div class="http-line"><strong>${escapeHtml(req.method || 'POST')}</strong> ${escapeHtml(req.url || '')}</div>
          <div class="subhead">Headers</div>
          ${renderHeaders(req.headers)}
          <div class="subhead">Body ${req.body_bytes_len ? `(${req.body_bytes_len} bytes)` : ''}</div>
          ${renderBody(req.body)}
        </div>
      </details>

      <details open><summary><strong>Response</strong> — what AGW returned (post-cidgar mutation)</summary>
        <div class="section">
          <div class="http-line"><strong>HTTP ${escapeHtml(String(resp.status || '?'))}</strong> ${resp.elapsed_ms ? `(${resp.elapsed_ms}ms)` : ''}</div>
          <div class="subhead">Headers</div>
          ${renderHeaders(resp.headers)}
          <div class="subhead">Body ${resp.body_bytes_len ? `(${resp.body_bytes_len} bytes)` : ''}</div>
          ${renderBody(resp.body)}
        </div>
      </details>

      <details open><summary><strong>Governance audit</strong> — AGW-side view of this turn (${audits.length} entries)</summary>
        <div class="section">${renderAudit(audits)}</div>
      </details>
    </div>
  `;
}

async function refresh() {
  const r = await fetch(`${API_BASE}/trials/${trialId}`);
  if (!r.ok) {
    tabContents.turns.innerHTML = `<p>Error loading trial: ${r.status} ${await r.text()}</p>`;
    return;
  }
  const trial = await r.json();

  // Header
  elStatus.textContent = trial.status || "idle";
  elStatus.className = `status-pill ${trial.status || "idle"}`;
  elRowSummary.innerHTML = renderRowSummary(trial);

  // Tabs
  tabContents.turns.innerHTML = (trial.turns || []).map((t, i) => renderTurnCard(trial, t, i)).join("")
    || "<p>No turns yet.</p>";

  // Turn Plan tab — read-only list with side-by-side "planned vs executed" note
  const plan = trial.turn_plan || {turns: []};
  const planTurns = plan.turns || [];
  const executedCount = (trial.turns || []).length;
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
  tabContents.plan.innerHTML = `
    <h3 style="margin-top:0">Planned turns</h3>
    <ol class="plan-list">${planItems || '<li><em>(empty plan)</em></li>'}</ol>
    <h3>Raw turn_plan JSON</h3>
    <pre>${escapeHtml(JSON.stringify(plan, null, 2))}</pre>
    <p class="plan-note">Read-only in Plan A. Plan B will add inline JSON editor + reset-to-default + add-turn controls per design §5.3.</p>
  `;

  const verdicts = trial.verdicts || {};
  const labels = {a: "Presence", b: "Channel structure", c: "Continuity", d: "Resilience", e: "State-mode gap"};
  tabContents.verdicts.innerHTML = ["a","b","c","d","e"].map(lvl => {
    const v = verdicts[lvl] || {verdict: "na", reason: "not computed"};
    return `
      <div class="verdict-card ${v.verdict}">
        <strong>(${lvl}) ${labels[lvl]}</strong> — <em>${v.verdict}</em><br>
        <small>${escapeHtml(v.reason)}</small>
      </div>
    `;
  }).join("");

  document.getElementById("raw-json").textContent = JSON.stringify(trial, null, 2);

  return trial.status;
}

// Initial render + SSE subscription for live updates
(async () => {
  const initialStatus = await refresh();
  if (initialStatus === "running") {
    const es = new EventSource(`${API_BASE}/trials/${trialId}/stream`);
    es.onmessage = async (e) => {
      let data;
      try { data = JSON.parse(e.data); } catch { return; }
      if (data.event === "status") {
        elStatus.textContent = data.status;
        elStatus.className = `status-pill ${data.status}`;
        if (data.status !== "running") {
          await refresh();
          if (["pass", "fail", "error", "aborted"].includes(data.status)) es.close();
        }
      } else if (data.event === "trial_done") {
        await refresh();
        es.close();
      }
    };
    es.onerror = () => {
      // Fall back to polling
      setInterval(refresh, 2000);
    };
  }
})();
