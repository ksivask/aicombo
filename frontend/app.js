import { API_BASE, VALIDATE_DEBOUNCE_MS, PROVIDERS_REFRESH_MS } from "/config.js";

// Row + trial detail now opens in a new tab at /trial.html?id=X (not an inline drawer).
function openTrialTab(trialId) {
  if (!trialId) return;
  window.open(`/trial.html?id=${encodeURIComponent(trialId)}`, "_blank");
}

// Runnability — mirror of harness/validator.py rules that actually matter for the
// Run button. Anything beyond this is advisory (state/stream/provider constraints
// are auto-forced, not blocking).
const ADAPTER_CAPABILITIES_JS = {
  // Plan A: only langchain (chat-only). direct-mcp routes via llm=NONE.
  // Plan B will expand this as adapters are added.
  "langchain":   ["chat"],
  "langgraph":   ["chat"],                          // Plan B T2
  "crewai":      ["chat", "messages"],              // Plan B T3
  "pydantic-ai": ["chat", "messages", "responses"], // Plan B T4
  "autogen":     ["chat", "messages", "responses", "responses+conv"], // Plan B T5
};

function isRowRunnable(row) {
  const llm = row.llm || "NONE";
  const mcp = row.mcp || "NONE";
  const api = row.api || "chat";
  const framework = row.framework || "langchain";
  if (llm === "NONE" && mcp === "NONE") return false;
  if (llm === "NONE") return true;  // routes to direct-mcp adapter
  // Provider must be allowed for the API
  const apiProviders = API_TO_PROVIDERS[api] || [];
  if (apiProviders.length && !apiProviders.includes(llm)) return false;
  // Adapter must implement the API
  const adapterApis = ADAPTER_CAPABILITIES_JS[framework] || [];
  if (!adapterApis.includes(api)) return false;
  return true;
}

function invalidReason(row) {
  const llm = row.llm || "NONE";
  const mcp = row.mcp || "NONE";
  const api = row.api || "chat";
  const framework = row.framework || "langchain";
  if (llm === "NONE" && mcp === "NONE") {
    return "LLM=NONE + MCP=NONE: nothing to exercise. Pick at least one.";
  }
  if (llm !== "NONE") {
    const apiProviders = API_TO_PROVIDERS[api] || [];
    if (apiProviders.length && !apiProviders.includes(llm)) {
      return `api=${api} not supported by llm=${llm} (supported: ${apiProviders.join(", ")})`;
    }
    const adapterApis = ADAPTER_CAPABILITIES_JS[framework] || [];
    if (!adapterApis.includes(api)) {
      return `Plan A's ${framework} adapter doesn't implement api=${api} ` +
             `(available: ${adapterApis.join(", ") || "none"}). Plan B adds the missing adapters.`;
    }
  }
  return "invalid config";
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

// Client-side mirror of harness/validator.py::API_TO_PROVIDERS.
// Filters the LLM dropdown so users only see compatible providers
// for the row's current API.
const API_TO_PROVIDERS = {
  "chat": ["ollama", "mock", "chatgpt", "gemini"],
  "responses": ["chatgpt"],
  "responses+conv": ["chatgpt"],
  "messages": ["claude"],
};

function llmOptionsForRow(row) {
  const allowed = API_TO_PROVIDERS[row?.api] || providerOptions();
  // Always keep NONE at the top (direct-MCP mode — valid with any api since
  // LLM is ignored in that case).
  return ["NONE", ...allowed];
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
    {
      headerName: "State", field: "state", cellDataType: "boolean", width: 80,
      // Editable only when (api, llm) supports state (chatgpt + responses). All
      // other combos disable + force F via validator. Reflects the rule client-side.
      editable: params => {
        const r = params.data || {};
        const api = r.api;
        const llm = r.llm;
        if (api === "responses" && llm === "chatgpt") return true;
        return false;  // chat / messages / responses+conv (forced) / non-chatgpt
      },
      cellStyle: params => {
        const r = params.data || {};
        const editable = (r.api === "responses" && r.llm === "chatgpt");
        return editable ? null : {color: "#bbb", background: "#f5f5f5"};
      },
      tooltipValueGetter: params => {
        const r = params.data || {};
        if (r.api === "responses+conv") return "responses+conv forces state=true";
        if (r.api !== "responses") return `state only meaningful for api=responses (current api=${r.api})`;
        if (r.llm !== "chatgpt") return `llm=${r.llm} does not implement Responses-API state — pick chatgpt`;
        return null;
      },
    },
    {
      headerName: "LLM", field: "llm", editable: true,
      cellEditor: "agSelectCellEditor",
      // Filter by current API — claude doesn't appear when api=chat,
      // ollama doesn't appear when api=responses, etc.
      cellEditorParams: params => ({values: llmOptionsForRow(params.data)}),
      cellStyle: params => {
        const row = params.data || {};
        const allowed = API_TO_PROVIDERS[row.api] || [];
        const provider = providers.find(p => p.id === params.value);
        // Red/strikethrough: LLM not compatible with current API (user needs to change)
        if (params.value !== "NONE" && !allowed.includes(params.value)) {
          return {color: "#c62828", textDecoration: "line-through", background: "#ffebee"};
        }
        // Grey/strikethrough: LLM compatible with API but no API key set
        if (provider && !provider.available) {
          return {color: "#999", textDecoration: "line-through"};
        }
        return null;
      },
      tooltipValueGetter: params => {
        const row = params.data || {};
        const allowed = API_TO_PROVIDERS[row.api] || [];
        if (params.value !== "NONE" && !allowed.includes(params.value)) {
          return `api=${row.api} does not support llm=${params.value}. ` +
                 `Supported: ${allowed.join(", ")}`;
        }
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
      headerName: "Verdicts", field: "verdicts", width: 180,
      cellRenderer: params => {
        const v = params.value || {};
        const pills = ["a", "b", "c", "d", "e", "f"].map(lvl => {
          const cls = (v[lvl]?.verdict) || "na";
          const glyph = cls === "pass" ? "✓" : cls === "fail" ? "✗" : "—";
          return `<span class="verdict-pill ${cls}" title="${v[lvl]?.reason || ""}">${glyph}</span>`;
        }).join("");
        // Always link by row_id (works from row creation; trial page handles
        // both "no trial yet" and "live streaming during run" cases).
        const rowId = params.data?.row_id;
        if (rowId) {
          return `<a class="verdict-link" href="/trial.html?row_id=${encodeURIComponent(rowId)}" target="_blank" rel="noopener" title="open trial detail in new tab">${pills}<span class="verdict-link-icon">↗</span></a>`;
        }
        return `<span class="verdict-link disabled">${pills}</span>`;
      },
    },
    {
      headerName: "Actions", width: 170,
      cellRenderer: params => {
        const row = params.data || {};
        const running = row.status === "running";
        const runnable = isRowRunnable(row);

        let runBtn;
        if (running) {
          runBtn = `<button class="btn-pause" data-row-id="${row.row_id}" disabled title="abort not implemented in Plan A">⏸</button>`;
        } else if (!runnable) {
          runBtn = `<button class="btn-run" data-row-id="${row.row_id}" disabled title="row is not runnable — ${invalidReason(row)}">▶</button>`;
        } else {
          runBtn = `<button class="btn-run" data-row-id="${row.row_id}" title="run trial">▶</button>`;
        }
        const previewBtn = `<button class="btn-preview" data-row-id="${row.row_id}" title="preview turn plan">📋</button>`;
        const deleteBtn = `<button class="btn-delete" data-row-id="${row.row_id}" title="delete row">✕</button>`;
        return `${runBtn}${previewBtn}${deleteBtn}`;
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
    // Grey-out unrunnable rows (LLM=NONE + MCP=NONE)
    getRowClass: params => isRowRunnable(params.data) ? null : "row-not-runnable",
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
    // Cells whose styling/editability depends on other columns must redraw
    // when those source columns change.
    const colId = event.column?.getColId();
    if (colId === "llm" || colId === "mcp") {
      // Actions + State both depend on llm; redraw the whole row to refresh
      // .row-not-runnable class + State cell editability/style.
      event.api.redrawRows({rowNodes: [event.api.getRowNode(row.row_id)]});
    }
    if (colId === "api") {
      // State editability + LLM options both depend on api → redraw the row
      // so cellStyle + cellEditorParams re-evaluate.
      event.api.redrawRows({rowNodes: [event.api.getRowNode(row.row_id)]});
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
  } else if (target.classList.contains("btn-preview")) {
    const rowId = target.dataset.rowId;
    await previewPlan(rowId);
  }
}

async function previewPlan(rowId) {
  const rows = await fetchMatrix();
  const row = rows.find(r => r.row_id === rowId);
  if (!row) return;
  const r = await fetch(`${API_BASE}/templates/preview`, {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify(row),
  });
  if (!r.ok) {
    showToast(`Preview failed: ${r.status}`);
    return;
  }
  const data = await r.json();
  const plan = data.turn_plan || {turns: []};
  const turns = plan.turns || [];
  const escape = s => String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");

  const turnItems = turns.map((t, i) => {
    const kind = t.kind || "user_msg";
    if (kind === "user_msg") {
      return `<li class="plan-turn">
        <span class="plan-turn-idx">${i}</span>
        <span class="plan-turn-kind">user_msg</span>
        <span class="plan-turn-content">${escape(t.content || "")}</span>
      </li>`;
    }
    // compact / force_state_ref / inject_ambient_cid / direct_mcp_*
    const params_summary = Object.entries(t).filter(([k]) => k !== "kind")
      .map(([k, v]) => `<code>${escape(k)}=${escape(JSON.stringify(v))}</code>`).join(" ");
    return `<li class="plan-turn">
      <span class="plan-turn-idx">${i}</span>
      <span class="plan-turn-kind control">${escape(kind)}</span>
      <span class="plan-turn-content">${params_summary}</span>
    </li>`;
  }).join("");

  const body = `
    <h3>Row config</h3>
    <div class="row-summary">
      ${["framework","api","llm","mcp","routing"].map(k =>
        `<span class="chip"><span class="chip-k">${k}</span>${escape(row[k] || "")}</span>`
      ).join("")}
      ${row.stream ? '<span class="chip"><span class="chip-k">stream</span>on</span>' : ""}
      ${row.state  ? '<span class="chip"><span class="chip-k">state</span>on</span>'  : ""}
    </div>
    <h3>Turn plan (${turns.length} turn${turns.length === 1 ? "" : "s"})</h3>
    <ol class="plan-list">${turnItems || '<li><em>(empty plan)</em></li>'}</ol>
    <h3>Raw JSON</h3>
    <pre>${escape(JSON.stringify(plan, null, 2))}</pre>
    <p class="plan-note">Read-only in Plan A. Plan B will add an inline JSON editor here + a per-row <code>[Reset to default]</code> + <code>[+ Add turn]</code> actions per design §5.3.</p>
  `;
  openSettingsModal(body);
}

async function runRow(rowId) {
  const rowNode = gridApi.getRowNode(rowId);
  if (rowNode && !isRowRunnable(rowNode.data)) {
    showToast(invalidReason(rowNode.data));
    return;
  }
  const r = await fetch(`${API_BASE}/trials/${rowId}/run`, {method: "POST"});
  if (!r.ok) {
    alert(`Failed to start trial: ${r.status} ${await r.text()}`);
    return;
  }
  const j = await r.json();
  const trialId = j.trial_id;

  // Update row: status=running, last_trial_id=... (rowNode already bound above)
  rowNode.setDataValue("status", "running");
  // last_trial_id is NOT a declared column — AG-Grid setDataValue throws
  // 'getColDef null' for non-column fields. Write directly to rowNode.data
  // and refresh the cells that depend on it (Verdicts cell reads it).
  rowNode.data.last_trial_id = trialId;
  gridApi.refreshCells({rowNodes: [rowNode], force: true});

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
  // Optimistic delete: no browser confirm() (users can block it permanently).
  // The matrix row JSON file is the only state lost; trivial to re-add.
  const r = await fetch(`${API_BASE}/matrix/row/${rowId}`, {method: "DELETE"});
  if (r.ok) {
    gridApi.applyTransaction({remove: [{row_id: rowId}]});
    showToast(`Row ${rowId} deleted`);
  } else {
    showToast(`Delete failed: ${r.status}`);
  }
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

// Delete-all uses click-twice-to-confirm (browser confirm() may be blocked)
let _deleteAllArmed = false;
let _deleteAllTimer = null;
document.getElementById("btn-delete-all").addEventListener("click", async (e) => {
  const btn = e.currentTarget;
  if (!_deleteAllArmed) {
    _deleteAllArmed = true;
    btn.textContent = "⚠ Click again to confirm";
    btn.classList.add("armed");
    clearTimeout(_deleteAllTimer);
    _deleteAllTimer = setTimeout(() => {
      _deleteAllArmed = false;
      btn.textContent = "⛌ Delete All";
      btn.classList.remove("armed");
    }, 3000);
    return;
  }
  // Confirmed
  clearTimeout(_deleteAllTimer);
  _deleteAllArmed = false;
  btn.textContent = "⛌ Delete All";
  btn.classList.remove("armed");
  const r = await fetch(`${API_BASE}/matrix`, {method: "DELETE"});
  if (r.ok) {
    const j = await r.json();
    showToast(`Deleted ${j.deleted_count} row${j.deleted_count === 1 ? "" : "s"}`);
    // Clear grid client-side
    const allIds = [];
    gridApi.forEachNode(n => allIds.push({row_id: n.data.row_id}));
    gridApi.applyTransaction({remove: allIds});
  } else {
    showToast(`Delete-all failed: ${r.status}`);
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
