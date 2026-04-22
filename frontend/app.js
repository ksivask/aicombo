import { API_BASE, VALIDATE_DEBOUNCE_MS, PROVIDERS_REFRESH_MS } from "/config.js";
import { openDrawer, refreshDrawer } from "/drawer.js";

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
      cellEditorParams: {values: ["langchain", "langgraph", "crewai", "pydantic-ai", "autogen", "llamaindex", "NONE"]},
      pinned: "left", width: 120,
    },
    {
      headerName: "API", field: "api", editable: true,
      cellEditor: "agSelectCellEditor",
      cellEditorParams: {values: ["chat", "responses", "responses+conv", "messages", "NONE"]},
      width: 140,
    },
    {headerName: "Stream", field: "stream", editable: true, cellDataType: "boolean", width: 80},
    {headerName: "State", field: "state", editable: true, cellDataType: "boolean", width: 80},
    {
      headerName: "LLM", field: "llm", editable: true,
      cellEditor: "agSelectCellEditor",
      cellEditorParams: {values: providerOptions()},
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
    },
    {
      headerName: "Status", field: "status", pinned: "right", width: 100,
      cellRenderer: params => {
        const v = params.value || "idle";
        return `<span class="status-pill ${v}">${v}</span>`;
      },
    },
    {
      headerName: "Verdicts", field: "verdicts", pinned: "right", width: 140,
      cellRenderer: params => {
        const v = params.value || {};
        return ["a", "b", "c", "d", "e"].map(lvl => {
          const cls = (v[lvl]?.verdict) || "na";
          const glyph = cls === "pass" ? "✓" : cls === "fail" ? "✗" : "—";
          return `<span class="verdict-pill ${cls}" title="${v[lvl]?.reason || ""}">${glyph}</span>`;
        }).join("");
      },
    },
    {
      headerName: "Actions", pinned: "right", width: 140,
      cellRenderer: params => `
        <button class="btn-run" data-row-id="${params.data.row_id}">▶</button>
        <button class="btn-delete" data-row-id="${params.data.row_id}">✕</button>
      `,
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
    onRowClicked: onRowClicked,
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

async function onRowClicked(event) {
  // If click was on a button, don't open drawer
  if (event.event?.target?.tagName === "BUTTON") return;
  const trialId = event.data.last_trial_id;
  if (trialId) openDrawer(trialId);
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
  const j = await r.json();
  const trialId = j.trial_id;

  // Update row: status=running, last_trial_id=...
  const rowNode = gridApi.getRowNode(rowId);
  rowNode.setDataValue("status", "running");
  rowNode.setDataValue("last_trial_id", trialId);

  // Subscribe to SSE
  const es = new EventSource(`${API_BASE}/trials/${trialId}/stream`);
  es.onmessage = async (e) => {
    const data = JSON.parse(e.data);
    if (data.event === "trial_done") {
      es.close();
      // Reload trial to pull verdicts
      const tr = await fetch(`${API_BASE}/trials/${trialId}`);
      const trial = await tr.json();
      rowNode.setDataValue("status", trial.status);
      rowNode.setDataValue("verdicts", trial.verdicts || {});
      refreshDrawer(trialId);
    } else if (data.event === "status") {
      rowNode.setDataValue("status", data.status);
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

initGrid();
setInterval(fetchProviders, PROVIDERS_REFRESH_MS);
