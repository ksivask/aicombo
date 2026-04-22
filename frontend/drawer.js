import { API_BASE } from "/config.js";

const drawerEl = document.getElementById("drawer");
const drawerTitle = document.getElementById("drawer-title");
const closeBtn = document.getElementById("drawer-close");
const tabBtns = document.querySelectorAll(".tab-btn");
const tabContents = {
  turns: document.getElementById("tab-turns"),
  verdicts: document.getElementById("tab-verdicts"),
  raw: document.getElementById("tab-raw"),
};

let currentTrialId = null;

closeBtn.addEventListener("click", () => {
  drawerEl.classList.add("hidden");
  currentTrialId = null;
});

tabBtns.forEach(btn => {
  btn.addEventListener("click", () => {
    tabBtns.forEach(b => b.classList.remove("active"));
    Object.values(tabContents).forEach(c => c.classList.remove("active"));
    btn.classList.add("active");
    tabContents[btn.dataset.tab].classList.add("active");
  });
});

export async function openDrawer(trialId, rowData) {
  currentTrialId = trialId || null;
  drawerEl.classList.remove("hidden");
  if (trialId) {
    drawerTitle.textContent = `Trial ${trialId.slice(0, 8)}…`;
    await refreshDrawer(trialId);
  } else {
    drawerTitle.textContent = "No trial yet";
    const cfg = rowData ? `<pre>${JSON.stringify(rowData, null, 2)}</pre>` : "";
    tabContents.turns.innerHTML = `<p>No trial has been run for this row yet. Click ▶ to start one.</p>${cfg}`;
    tabContents.verdicts.innerHTML = "<p>Run a trial to compute verdicts.</p>";
    document.getElementById("raw-json").textContent = rowData ? JSON.stringify(rowData, null, 2) : "{}";
  }
}

export async function refreshDrawer(trialId) {
  if (currentTrialId !== trialId) return;
  const r = await fetch(`${API_BASE}/trials/${trialId}`);
  if (!r.ok) {
    tabContents.turns.innerHTML = `<p>Error loading trial: ${r.status}</p>`;
    return;
  }
  const trial = await r.json();

  // Turns tab — pedagogical rendering with headers table + body pretty-print
  const renderHeaders = (h) => {
    if (!h || Object.keys(h).length === 0) return "<em>(none)</em>";
    return `<table class="kv"><tbody>${Object.entries(h).map(([k, v]) =>
      `<tr><td class="k">${k}</td><td class="v">${String(v).replace(/</g, "&lt;")}</td></tr>`
    ).join("")}</tbody></table>`;
  };
  const renderBody = (b) => {
    if (b === null || b === undefined) return "<em>(empty)</em>";
    return `<pre>${JSON.stringify(b, null, 2)}</pre>`;
  };
  const renderAudit = (entries) => {
    if (!entries.length) return "<em>(no audit entries for this turn — AGW governance may not have fired)</em>";
    return entries.map(a => `
      <div class="audit-entry">
        <span class="badge ${a.phase || 'unknown'}">phase: ${a.phase || '?'}</span>
        <span class="badge">cid: ${a.cid || '∅'}</span>
        <span class="badge">backend: ${a.backend || '?'}</span>
        <details><summary>raw governance log</summary><pre>${JSON.stringify(a.raw, null, 2)}</pre></details>
      </div>
    `).join("");
  };

  tabContents.turns.innerHTML = (trial.turns || []).map((t, i) => {
    const req = t.request || {};
    const resp = t.response || {};
    const audits = (trial.audit_entries || []).filter(a => a.turn_id === t.turn_id);
    return `
    <div class="turn-card">
      <h4>Turn ${i}: ${t.kind} <span class="turn-id">${t.turn_id || ''}</span></h4>

      <details open><summary><strong>Request</strong> — what the adapter sent (pre-cidgar mutation)</summary>
        <div class="section">
          <div class="http-line"><strong>${req.method || 'POST'}</strong> ${req.url || ''}</div>
          <div class="subhead">Headers</div>
          ${renderHeaders(req.headers)}
          <div class="subhead">Body ${req.body_bytes_len ? `(${req.body_bytes_len} bytes)` : ''}</div>
          ${renderBody(req.body)}
        </div>
      </details>

      <details open><summary><strong>Response</strong> — what AGW returned (post-cidgar mutation)</summary>
        <div class="section">
          <div class="http-line"><strong>HTTP ${resp.status || '?'}</strong> ${resp.elapsed_ms ? `(${resp.elapsed_ms}ms)` : ''}</div>
          <div class="subhead">Headers</div>
          ${renderHeaders(resp.headers)}
          <div class="subhead">Body ${resp.body_bytes_len ? `(${resp.body_bytes_len} bytes)` : ''}</div>
          ${renderBody(resp.body)}
        </div>
      </details>

      <details><summary><strong>Governance audit</strong> — AGW-side view of this turn (${audits.length} entries)</summary>
        <div class="section">
          ${renderAudit(audits)}
        </div>
      </details>
    </div>
  `;
  }).join("") || "<p>No turns yet.</p>";

  // Verdicts tab
  const verdicts = trial.verdicts || {};
  const labels = {a: "Presence", b: "Channel structure", c: "Continuity", d: "Resilience", e: "State-mode gap"};
  tabContents.verdicts.innerHTML = ["a","b","c","d","e"].map(lvl => {
    const v = verdicts[lvl] || {verdict: "na", reason: "not computed"};
    return `
      <div class="verdict-card ${v.verdict}">
        <strong>(${lvl}) ${labels[lvl]}</strong> — <em>${v.verdict}</em><br>
        <small>${v.reason}</small>
      </div>
    `;
  }).join("");

  // Raw JSON tab
  document.getElementById("raw-json").textContent = JSON.stringify(trial, null, 2);
}
