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

  // Turns tab
  tabContents.turns.innerHTML = (trial.turns || []).map((t, i) => `
    <div class="turn-card">
      <h4>Turn ${i}: ${t.kind}</h4>
      <details><summary>Request</summary><pre>${JSON.stringify(t.request, null, 2)}</pre></details>
      <details><summary>Response</summary><pre>${JSON.stringify(t.response, null, 2)}</pre></details>
      <details><summary>Audit entries (${(trial.audit_entries || []).filter(a => a.turn_id === t.turn_id).length})</summary>
        <pre>${(trial.audit_entries || []).filter(a => a.turn_id === t.turn_id).map(a => JSON.stringify(a, null, 2)).join("\n\n")}</pre>
      </details>
    </div>
  `).join("") || "<p>No turns yet.</p>";

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
