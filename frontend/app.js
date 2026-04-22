import { API_BASE, VALIDATE_DEBOUNCE_MS, PROVIDERS_REFRESH_MS } from "/config.js";

// Row + trial detail now opens in a new tab at /trial.html?id=X (not an inline drawer).
function openTrialTab(trialId) {
  if (!trialId) return;
  window.open(`/trial.html?id=${encodeURIComponent(trialId)}`, "_blank");
}

let gridApi;
let providers = [];

async function fetchProviders() {
  const r = await fetch(`${API_BASE}/providers`);
  const j = await r.json();
  providers = j.providers;
}

async function fetchMatrix() {
  const r = await fetch(`${API_BASE}/matrix`);
  return (await r.json()).rows || [];
}

async function validateRow(rowConfig) {
  const r = await fetch(`${API_BASE}/validate`, {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({row_config: rowConfig}),
  });
  return r.json();
}

function providerOptions() {
  return providers.map(p => p.id);
}

function buildColumnDefs() {
  return [
    {headerName: "#", valueGetter: "node.rowIndex + 1", width: 60, pinned: "left"},
    {
      headerName: "Framework", field: "framework", editable: true,
      cellEditor: "agSelectCellEditor",
      cellEditorParams: {values: ["langchain", "langgraph", "crewai", "pydantic-ai", "autogen", "llamaindex"]},
      cellStyle: params => (params.data?.llm === "NONE" ? {color: "#bbb", fontStyle: "italic"} : null),
      valueFormatter: params => (params.data?.llm === "NONE" ? "— (direct MCP)" : params.value),
      pinned: "left", width: 120,
    },
    {
      headerName: "API", field: "api", editable: true,
      cellEditor: "agSelectCellEditor",
      cellEditorParams: {values: ["chat", "responses", "responses+conv", "messages"]},
      cellStyle: params => (params.data?.llm === "NONE" ? {color: "#bbb", fontStyle: "italic"} : null),
      valueFormatter: params => (params.data?.llm === "NONE" ? "—" : params.value),
      width: 140,
    },
    {headerName: "Stream", field: "stream", editable: true, cellDataType: "boolean", width: 80},
    {headerName: "State", field: "state", editable: true, cellDataType: "boolean", width: 80},
    {
      headerName: "LLM", field: "llm", editable: true,
      cellEditor: "agSelectCellEditor",
      cellEditorParams: () => ({values: providerOptions()}),
      cellStyle: params => {
        const provider = providers.find(p => p.id === params.value);
        return provider && !provider.available ? {color: "#999", textDecoration: "line-through"} : null;
      },
      tooltipValueGetter: params => {
        const provider = providers.find(p => p.id === params.value);
        return provider && !provider.available ? provider.unavailable_reason : null;
      },
      width: 110,
    },
    {
      headerName: "MCP", field: "mcp", editable: true,
      cellEditor: "agSelectCellEditor",
      cellEditorParams: {values: ["NONE", "weather", "news", "library", "fetch"]},
      width: 110,
    },
    {
      headerName: "Routing", field: "routing", editable: true,
      cellEditor: "agSelectCellEditor",
      cellEditorParams: {values: ["via_agw", "direct"]},
      width: 100,
      flex: 1,  // absorb leftover horizontal space, no empty gap before Status
    },
    {
      headerName: "Status", field: "status", width: 110,
      cellRenderer: params => {
        const v = params.value || "idle";
        return `<span class="status-pill ${v}">${v}</span>`;
      },
    },
    {
      headerName: "Verdicts", field: "verdicts", width: 160,
      cellRenderer: params => {
        const v = params.value || {};
        const pills = ["a", "b", "c", "d", "e"].map(lvl => {
          const cls = (v[lvl]?.verdict) || "na";
          const glyph = cls === "pass" ? "✓" : cls === "fail" ? "✗" : "—";
          return `<span class="verdict-pill ${cls}" title="${v[lvl]?.reason || ""}">${glyph}</span>`;
        }).join("");
        const trialId = params.data?.last_trial_id;
        if (trialId) {
          return `<a class="verdict-link" href="/trial.html?id=${encodeURIComponent(trialId)}" target="_blank" rel="noopener" title="open trial detail in new tab">${pills}<span class="verdict-link-icon">↗</span></a>`;
        }
        return `<span class="verdict-link disabled" title="no trial yet — click ▶ to run">${pills}</span>`;
      },
    },
    {
      headerName: "Actions", width: 140,
      cellRenderer: params => {
        const running = params.data?.status === "running";
        const runBtn = running
          ? `<button class="btn-pause" data-row-id="${params.data.row_id}" disabled title="abort not implemented in Plan A">⏸</button>`
          : `<button class="btn-run" data-row-id="${params.data.row_id}" title="run trial">▶</button>`;
        return `${runBtn}<button class="btn-delete" data-row-id="${params.data.row_id}" title="delete row">✕</button>`;
      },
    },
  ];
}

async function initGrid() {
  await fetchProviders();
  const rows = await fetchMatrix();

  const gridOptions = {
    columnDefs: buildColumnDefs(),
    rowData: rows,
    getRowId: params => params.data.row_id,
    onCellValueChanged: onCellValueChanged,
    // Row click no longer opens the trial tab — was breaking cell editing /
    // column selection. Use the Verdicts cell link to open trial detail.
    onCellClicked: onCellClicked,
  };
  const div = document.getElementById("matrix-grid");
  gridApi = agGrid.createGrid(div, gridOptions);
}

let debounceTimer = null;
async function onCellValueChanged(event) {
  clearTimeout(debounceTimer);
  debounceTimer = setTimeout(async () => {
    const row = event.data;
    const validity = await validateRow(row);
    // Apply forced values
    if (validity.forced_values) {
      for (const [k, v] of Object.entries(validity.forced_values)) {
        row[k] = v;
      }
      event.api.getRowNode(row.row_id).setData(row);
    }
    // Persist
    await fetch(`${API_BASE}/matrix/row/${row.row_id}`, {
      method: "PATCH",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify(row),
    });
  }, VALIDATE_DEBOUNCE_MS);
}

function showToast(msg) {
  let toast = document.getElementById("aiplay-toast");
  if (!toast) {
    toast = document.createElement("div");
    toast.id = "aiplay-toast";
    toast.className = "toast";
    document.body.appendChild(toast);
  }
  toast.textContent = msg;
  toast.classList.add("visible");
  clearTimeout(toast._timer);
  toast._timer = setTimeout(() => toast.classList.remove("visible"), 3500);
}

async function onCellClicked(event) {
  const target = event.event?.target;
  if (!target) return;
  if (target.classList.contains("btn-run")) {
    const rowId = target.dataset.rowId;
    await runRow(rowId);
  } else if (target.classList.contains("btn-delete")) {
    const rowId = target.dataset.rowId;
    await deleteRow(rowId);
  }
}

async function runRow(rowId) {
  const r = await fetch(`${API_BASE}/trials/${rowId}/run`, {method: "POST"});
  if (!r.ok) {
    alert(`Failed to start trial: ${r.status} ${await r.text()}`);
    return;
  }
  const j = await r.json();
  const trialId = j.trial_id;

  // Update row: status=running, last_trial_id=...
  const rowNode = gridApi.getRowNode(rowId);
  rowNode.setDataValue("status", "running");
  rowNode.setDataValue("last_trial_id", trialId);

  // Persist last_trial_id so drawer works after page reload
  await fetch(`${API_BASE}/matrix/row/${rowId}`, {
    method: "PATCH",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({last_trial_id: trialId}),
  }).catch(() => {});

  // Open trial detail page in a new tab — live updates there via SSE
  openTrialTab(trialId);

  // Grid-side SSE: just updates the row pill in the matrix view
  const es = new EventSource(`${API_BASE}/trials/${trialId}/stream`);
  es.onerror = () => {};
  es.onmessage = async (e) => {
    let data;
    try { data = JSON.parse(e.data); } catch { return; }
    if (data.event === "trial_done") {
      es.close();
      const tr = await fetch(`${API_BASE}/trials/${trialId}`);
      const trial = await tr.json();
      rowNode.setDataValue("status", trial.status);
      rowNode.setDataValue("verdicts", trial.verdicts || {});
      gridApi.refreshCells({rowNodes: [rowNode], columns: ["Actions"], force: true});
    } else if (data.event === "status") {
      rowNode.setDataValue("status", data.status);
      gridApi.refreshCells({rowNodes: [rowNode], columns: ["Actions"], force: true});
    }
  };
}

async function deleteRow(rowId) {
  if (!confirm("Delete this row?")) return;
  await fetch(`${API_BASE}/matrix/row/${rowId}`, {method: "DELETE"});
  gridApi.applyTransaction({remove: [{row_id: rowId}]});
}

document.getElementById("btn-add-row").addEventListener("click", async () => {
  const newRow = {
    framework: "langchain", api: "chat",
    stream: false, state: false,
    llm: "ollama", mcp: "NONE", routing: "via_agw",
  };
  const r = await fetch(`${API_BASE}/matrix/row`, {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify(newRow),
  });
  const j = await r.json();
  gridApi.applyTransaction({add: [{row_id: j.row_id, ...newRow}]});
});

document.getElementById("btn-run-all").addEventListener("click", async () => {
  const rows = await fetchMatrix();
  for (const row of rows) {
    await runRow(row.row_id);
  }
});

// ── Settings modal ──
document.getElementById("btn-settings").addEventListener("click", async () => {
  const [info, providersResp] = await Promise.all([
    fetch(`${API_BASE}/info`).then(r => r.json()),
    fetch(`${API_BASE}/providers`).then(r => r.json()),
  ]);
  const providers = providersResp.providers;
  const body = `
    <h3>Providers (LLM key detection)</h3>
    <table class="kv"><tbody>
      ${providers.map(p => `
        <tr>
          <td class="k">${p.id}</td>
          <td class="v">${p.available ? "✓ available" : "✗ " + (p.unavailable_reason || "unavailable")}</td>
        </tr>
      `).join("")}
    </tbody></table>
    <h3>Harness info</h3>
    <pre>${JSON.stringify(info, null, 2)}</pre>
    <h3>Reload matrix state</h3>
    <p>Frontend reads env-key availability every ${PROVIDERS_REFRESH_MS/1000}s. To pick up .env changes immediately, edit .env then <code>make rotate-keys</code> on the host and click this button again.</p>
  `;
  openSettingsModal(body);
});

function openSettingsModal(bodyHtml) {
  let modal = document.getElementById("settings-modal");
  if (!modal) {
    modal = document.createElement("div");
    modal.id = "settings-modal";
    modal.className = "modal";
    modal.innerHTML = `
      <div class="modal-content">
        <div class="modal-header">
          <span>⚙ Settings</span>
          <button id="modal-close">✕</button>
        </div>
        <div class="modal-body"></div>
      </div>
    `;
    document.body.appendChild(modal);
    modal.addEventListener("click", (e) => { if (e.target === modal) modal.classList.add("hidden"); });
    modal.querySelector("#modal-close").addEventListener("click", () => modal.classList.add("hidden"));
  }
  modal.querySelector(".modal-body").innerHTML = bodyHtml;
  modal.classList.remove("hidden");
}

initGrid();
setInterval(fetchProviders, PROVIDERS_REFRESH_MS);

// Polling fallback: every 5s refresh row status + verdicts from backend
// so a dropped EventSource doesn't leave a row stuck at "running".
setInterval(async () => {
  if (!gridApi) return;
  const rows = await fetchMatrix();
  for (const row of rows) {
    const node = gridApi.getRowNode(row.row_id);
    if (!node) continue;
    if (!row.last_trial_id) continue;
    if (node.data.status === row.status && JSON.stringify(node.data.verdicts || {}) === JSON.stringify(row.verdicts || {})) continue;
    // Out-of-date: fetch the trial to get verdicts
    const tr = await fetch(`${API_BASE}/trials/${row.last_trial_id}`).catch(() => null);
    if (tr && tr.ok) {
      const trial = await tr.json();
      node.setDataValue("status", trial.status);
      node.setDataValue("verdicts", trial.verdicts || {});
      gridApi.refreshCells({rowNodes: [node], columns: ["Actions"], force: true});
    }
  }
}, 5000);
