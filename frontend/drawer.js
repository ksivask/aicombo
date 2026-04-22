import { API_BASE } from "/config.js";

const drawerEl = document.getElementById("drawer");
const drawerTitle = document.getElementById("drawer-title");
const closeBtn = document.getElementById("drawer-close");
const maximizeBtn = document.getElementById("drawer-maximize");
const resizeHandle = drawerEl.querySelector(".drawer-resize-handle");
const tabBtns = document.querySelectorAll(".tab-btn");
const tabContents = {
  turns: document.getElementById("tab-turns"),
  verdicts: document.getElementById("tab-verdicts"),
  raw: document.getElementById("tab-raw"),
};

let currentTrialId = null;
let drawerMaximized = false;
let lastDrawerHeight = null;  // remember last non-maximized size

closeBtn.addEventListener("click", () => {
  drawerEl.classList.add("hidden");
  currentTrialId = null;
});

// Maximize toggle (35vh ↔ 90vh)
maximizeBtn.addEventListener("click", () => {
  if (drawerMaximized) {
    drawerEl.style.height = lastDrawerHeight || "35vh";
    maximizeBtn.textContent = "⇱";
    maximizeBtn.title = "maximize";
    drawerMaximized = false;
  } else {
    lastDrawerHeight = drawerEl.style.height || "35vh";
    drawerEl.style.height = "90vh";
    maximizeBtn.textContent = "⇲";
    maximizeBtn.title = "restore";
    drawerMaximized = true;
  }
});

// Drag-resize (vertical) via top handle
let isResizing = false;
let resizeStartY = 0;
let resizeStartHeight = 0;
resizeHandle.addEventListener("mousedown", (e) => {
  isResizing = true;
  resizeStartY = e.clientY;
  resizeStartHeight = drawerEl.getBoundingClientRect().height;
  document.body.style.userSelect = "none";
  document.body.style.cursor = "ns-resize";
  e.preventDefault();
});
document.addEventListener("mousemove", (e) => {
  if (!isResizing) return;
  const dy = resizeStartY - e.clientY;  // drag up grows drawer
  const newH = Math.max(120, Math.min(window.innerHeight - 60, resizeStartHeight + dy));
  drawerEl.style.height = `${newH}px`;
  drawerMaximized = false;
  maximizeBtn.textContent = "⇱";
});
document.addEventListener("mouseup", () => {
  if (isResizing) {
    isResizing = false;
    document.body.style.userSelect = "";
    document.body.style.cursor = "";
  }
});

// Double-click header to toggle maximize
document.querySelector(".drawer-header").addEventListener("dblclick", (e) => {
  if (e.target.tagName === "BUTTON") return;
  maximizeBtn.click();
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
