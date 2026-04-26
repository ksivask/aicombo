import { API_BASE, VALIDATE_DEBOUNCE_MS, PROVIDERS_REFRESH_MS } from "/config.js";
import { openTurnPlanDrawer } from "/drawer.js";

// Row + trial detail now opens in a new tab at /trial.html?id=X (not an inline drawer).
function openTrialTab(trialId) {
  if (!trialId) return;
  window.open(`/trial.html?id=${encodeURIComponent(trialId)}`, "_blank");
}

// Runnability — mirror of harness/validator.py rules that actually matter for the
// Run button. Anything beyond this is advisory (state/stream/provider constraints
// are auto-forced, not blocking).
const ADAPTER_CAPABILITIES_JS = {
  // Mirrors harness/validator.py::ADAPTER_CAPABILITIES.
  "langchain":   ["chat", "messages", "responses", "responses+conv"],  // E5a
  "langgraph":   ["chat", "messages", "responses", "responses+conv"],  // E5b
  "crewai":      ["chat", "messages"],              // Plan B T3
  "pydantic-ai": ["chat", "messages", "responses"], // Plan B T4
  "autogen":     ["chat", "messages", "responses", "responses+conv"], // Plan B T5
  "llamaindex":  ["chat", "responses", "responses+conv"],              // Plan B T6
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

// E9 — curated model list per provider, fetched lazily and cached
// in window.__modelsByProvider so the model column's agSelectCellEditor
// can resolve synchronously when a cell opens. Empty list for unknown
// providers (the dropdown then shows just the "Custom…" sentinel).
window.__modelsByProvider = window.__modelsByProvider || {};

async function loadModelsFor(provider) {
  if (!provider || provider === "NONE") return;
  if (window.__modelsByProvider[provider]) return;
  try {
    const r = await fetch(`${API_BASE}/providers/${provider}/models`);
    if (!r.ok) return;
    const j = await r.json();
    window.__modelsByProvider[provider] = j.models || [];
  } catch (_e) {
    // Network blip — leave the cache empty; the editor's synchronous
    // fallback list still gives the user something usable.
  }
}

async function preloadModels() {
  // Pre-fetch every known provider on grid load + cache in
  // window.__modelsByProvider. Refresh the model column once cached
  // so already-rendered rows pick up the display-name + tier formatting.
  if (!providers.length) {
    try {
      const r = await fetch(`${API_BASE}/providers`);
      if (r.ok) providers = (await r.json()).providers || [];
    } catch (_e) { /* fall through with empty providers */ }
  }
  await Promise.all(
    (providers || [])
      .filter(p => p.id && p.id !== "NONE")
      .map(p => loadModelsFor(p.id))
  );
  if (gridApi) gridApi.refreshCells({columns: ["model"], force: true});
}

// E4 — does any currently-loaded row have baseline_of === rowId? Used to
// decide whether to render the "🔁 Pairs" action button on a governed row.
// Grid data is the source of truth (matches what the user sees right now;
// no extra fetch needed).
function matrixHasBaselineFor(rowId) {
  if (!gridApi || !rowId) return false;
  let found = false;
  gridApi.forEachNode(n => {
    if (n.data?.baseline_of === rowId) found = true;
  });
  return found;
}

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
      // E9 — Model dropdown. Values come from the curated list cached in
      // window.__modelsByProvider (populated by preloadModels). The "__custom__"
      // sentinel prompts the user for a free-text model id (handled in
      // onCellValueChanged); blank/empty stays blank — runner falls back to
      // DEFAULT_<PROVIDER>_MODEL env. LLM=NONE rows ignore this column.
      headerName: "Model", field: "model", editable: true,
      cellEditor: "agSelectCellEditor",
      cellEditorParams: params => {
        const llm = params.data?.llm || "";
        const cached = window.__modelsByProvider?.[llm] || [];
        const ids = cached.map(m => m.id);
        // Always include "" (use default) + "__custom__" sentinel so the
        // user can clear the cell or supply a one-off id.
        return {values: ["", ...ids, "__custom__"]};
      },
      valueFormatter: params => {
        if (params.value === "__custom__") return "Custom…";
        if (!params.value) return "(default)";
        const llm = params.data?.llm;
        const m = (window.__modelsByProvider?.[llm] || [])
          .find(x => x.id === params.value);
        return m ? `${m.display} (${m.tier})` : params.value;
      },
      cellStyle: params => (params.data?.llm === "NONE"
        ? {color: "#bbb", fontStyle: "italic"} : null),
      width: 180,
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
      width: 160,
      flex: 1,  // absorb leftover horizontal space, no empty gap before Status
      // T13 — append a "← baseline of <short>" badge when row was cloned
      // as a direct/baseline sibling. Purely informational; helps the user
      // pair the governed row with its baseline in the grid.
      cellRenderer: params => {
        const routing = params.value || "via_agw";
        const baselineOf = params.data?.baseline_of;
        if (baselineOf) {
          const short = String(baselineOf).replace(/^row-/, "").slice(0, 8);
          return `${routing} <span class="baseline-badge" title="baseline clone of ${baselineOf}">← baseline of ${short}</span>`;
        }
        return routing;
      },
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
      headerName: "Actions", width: 200,
      cellRenderer: params => {
        const row = params.data || {};
        const running = row.status === "running";
        const runnable = isRowRunnable(row);

        let runBtn;
        if (running) {
          // T14 — cooperative abort. Disabled when the row has no trial_id
          // yet (transient window between run-click and POST /trials/run
          // response); clickable once we know which trial to abort.
          const tid = row.last_trial_id || "";
          const disabledAttr = tid ? "" : "disabled";
          runBtn = `<button class="btn-abort" data-trial-id="${tid}" ${disabledAttr} title="stop trial (current turn finishes, next turns skipped)">⏹</button>`;
        } else if (!runnable) {
          runBtn = `<button class="btn-run" data-row-id="${row.row_id}" disabled title="row is not runnable — ${invalidReason(row)}">▶</button>`;
        } else {
          runBtn = `<button class="btn-run" data-row-id="${row.row_id}" title="run trial">▶</button>`;
        }
        const previewBtn = `<button class="btn-preview" data-row-id="${row.row_id}" title="preview turn plan">📋</button>`;
        // T13 — 🔀 Baseline: clone this via_agw row as a direct/no-governance
        // sibling for A/B comparison. Hidden on already-direct rows.
        let baselineBtn = "";
        if ((row.routing || "via_agw") !== "direct") {
          baselineBtn = `<button class="btn-baseline" data-row-id="${row.row_id}" title="clone as direct/baseline row (no AGW) for A/B comparison">🔀</button>`;
        }
        // E4 — 🔁 Pairs: open /pairs.html for the governed row. Shown on
        // baseline rows (they know their pointer) and on governed rows that
        // currently have a baseline sibling in the grid.
        let pairsBtn = "";
        const governedRowId = row.baseline_of || (matrixHasBaselineFor(row.row_id) ? row.row_id : null);
        if (governedRowId) {
          pairsBtn = `<button class="btn-pairs" data-pair-row-id="${governedRowId}" title="open governed-vs-baseline diff view">🔁</button>`;
        }
        const deleteBtn = `<button class="btn-delete" data-row-id="${row.row_id}" title="delete row">✕</button>`;
        return `${runBtn}${previewBtn}${baselineBtn}${pairsBtn}${deleteBtn}`;
      },
    },
  ];
}

async function initGrid() {
  await fetchProviders();
  // E9 — kick off model-list preload in parallel with grid mount. Runs
  // asynchronously; the model column's editor falls back to whatever's
  // in window.__modelsByProvider when opened (empty cache → just the
  // "__custom__" sentinel, which is still a usable degraded state).
  preloadModels();
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
  // E9 — handle the model column's "__custom__" sentinel synchronously
  // BEFORE debounce + PATCH, so the sentinel never gets persisted.
  // window.prompt() blocks; if the user cancels we revert to oldValue.
  const colId = event.column?.getColId();
  if (colId === "model" && event.newValue === "__custom__") {
    const custom = window.prompt(
      "Enter a custom model id (leave blank to use default):",
      event.oldValue && event.oldValue !== "__custom__" ? event.oldValue : ""
    );
    const next = (custom == null) ? (event.oldValue || "") : custom.trim();
    // setDataValue here re-fires onCellValueChanged with the resolved
    // value; that recursive call goes through the normal PATCH path.
    event.node.setDataValue("model", next === "__custom__" ? "" : next);
    return;
  }
  // E9 — lazy-load the new provider's model list when the LLM changes
  // so the model dropdown shows the right entries on the next open.
  if (colId === "llm" && event.newValue && event.newValue !== "NONE") {
    loadModelsFor(event.newValue).then(() => {
      if (gridApi) gridApi.refreshCells({
        rowNodes: [event.node], columns: ["model"], force: true,
      });
    });
  }
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
    if (colId === "llm" || colId === "mcp") {
      // Actions + State + Model all depend on llm; redraw the whole row to
      // refresh .row-not-runnable class + State cell editability/style +
      // Model cell formatter (which keys off llm).
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
  } else if (target.classList.contains("btn-baseline")) {
    const rowId = target.dataset.rowId;
    await cloneBaseline(rowId);
  } else if (target.classList.contains("btn-abort")) {
    const trialId = target.dataset.trialId;
    await abortTrial(trialId);
  } else if (target.classList.contains("btn-pairs")) {
    const pairRowId = target.dataset.pairRowId;
    if (pairRowId) {
      window.open(`/pairs.html?row_id=${encodeURIComponent(pairRowId)}`, "_blank");
    }
  }
}

async function abortTrial(trialId) {
  // T14 — cooperative abort. We don't kill the trial mid-turn (would
  // corrupt framework/HTTP state); the backend runner checks a flag
  // between turns and transitions to status=aborted.
  if (!trialId) {
    showToast("No trial id on this row yet — try again in a moment.");
    return;
  }
  if (!confirm(`Abort trial ${trialId}?\n\nThe currently-executing turn will finish; subsequent turns are skipped.`)) {
    return;
  }
  try {
    const r = await fetch(`${API_BASE}/trials/${trialId}/abort`, {method: "POST"});
    if (!r.ok) {
      showToast(`Abort failed: HTTP ${r.status}`);
      return;
    }
    const j = await r.json();
    if (j.ok) {
      showToast(`Abort requested for ${trialId}`);
    } else {
      // Already finished — refresh so the stale "running" pill updates.
      showToast(`Trial already finished (status=${j.status}). Refreshing row.`);
      const rows = await fetchMatrix();
      const row = rows.find(r => r.last_trial_id === trialId);
      if (row && gridApi) {
        const node = gridApi.getRowNode(row.row_id);
        if (node) {
          node.setDataValue("status", row.status);
          gridApi.refreshCells({rowNodes: [node], force: true});
        }
      }
    }
  } catch (e) {
    showToast(`Abort request failed: ${e.message}`);
  }
}

async function cloneBaseline(rowId) {
  // T13 — POST /matrix/row/{id}/clone-baseline, then insert the new row
  // into the grid so the user sees the A/B pair immediately without
  // a full page reload.
  const r = await fetch(`${API_BASE}/matrix/row/${rowId}/clone-baseline`, {
    method: "POST",
  });
  if (!r.ok) {
    showToast(`Clone failed: HTTP ${r.status}`);
    return;
  }
  const j = await r.json();
  // Fetch the freshly-created row so we render the full config (matrix
  // response returns only the row_id + baseline_of pointer).
  const rr = await fetch(`${API_BASE}/matrix/row/${j.row_id}`);
  if (rr.ok) {
    const newRow = await rr.json();
    gridApi.applyTransaction({add: [newRow]});
    // E4 — the newly-added baseline makes the governed row eligible for the
    // 🔁 Pairs action button. Redraw the source row so Actions re-renders.
    const srcNode = gridApi.getRowNode(rowId);
    if (srcNode) {
      gridApi.refreshCells({rowNodes: [srcNode], force: true});
    }
    showToast(`Baseline created: ${j.row_id} (← ${j.baseline_of})`);
  } else {
    showToast(`Baseline created but fetch failed: HTTP ${rr.status}`);
  }
}

async function previewPlan(rowId) {
  // Plan B T12 — the old read-only "preview" modal is replaced by an editable
  // CodeMirror drawer (see drawer.js). Same entry point (📋 row button), same
  // GET of /templates/preview when no override exists, but now the user can
  // edit + save the turn_plan_override on the row.
  const rows = await fetchMatrix();
  const row = rows.find(r => r.row_id === rowId);
  if (!row) return;
  await openTurnPlanDrawer(row);
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

  // Open trial detail page in a new tab — live updates there via polling.
  openTrialTab(trialId);

  // Row pill updates (status, verdicts) flow through the 5s polling loop
  // at the bottom of this file — simpler and more robust than SSE, which
  // only emitted status pings anyway.
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

// + Add Bulk — enumerates every (framework × supported_api) combo and
// creates one row per combo. LLM = first API-compatible provider from
// API_TO_PROVIDERS (chat→ollama, messages→claude, responses/+conv→chatgpt);
// MCP = randomly picked from {weather, news, library, fetch}; routing =
// via_agw; model = null (lets the runner use DEFAULT_<PROVIDER>_MODEL env).
//
// Source of truth: /info.frameworks (the I-NEW-1 endpoint backed by
// validator.py::ADAPTER_CAPABILITIES). Falls back to the in-JS
// ADAPTER_CAPABILITIES_JS mirror when /info doesn't expose frameworks
// (older harness builds — pre-b0e0aca).
const BULK_API_TO_FIRST_LLM = {
  "chat":           "ollama",
  "messages":       "claude",
  "responses":      "chatgpt",
  "responses+conv": "chatgpt",
};
const BULK_MCPS = ["weather", "news", "library", "fetch"];

async function getBulkSupportedApis() {
  try {
    const info = await fetch(`${API_BASE}/info`).then(r => r.json());
    if (info?.frameworks) {
      return Object.fromEntries(
        Object.entries(info.frameworks).map(([k, v]) => [k, v.supported_apis || []])
      );
    }
  } catch (e) {
    console.warn("Add Bulk: /info.frameworks unavailable, falling back to in-JS map:", e);
  }
  // Fallback for harness builds pre-I-NEW-1
  return {...ADAPTER_CAPABILITIES_JS, "direct-mcp": []};
}

document.getElementById("btn-add-bulk").addEventListener("click", async () => {
  const supported = await getBulkSupportedApis();
  // Build the combo list
  const combos = [];
  for (const [fw, apis] of Object.entries(supported)) {
    if (fw === "direct-mcp") continue;  // handled as a single llm=NONE row below
    for (const api of apis) {
      const llm = BULK_API_TO_FIRST_LLM[api];
      if (!llm) continue;  // unknown api → skip
      combos.push({
        framework: fw, api,
        stream: false, state: false,
        llm, mcp: BULK_MCPS[Math.floor(Math.random() * BULK_MCPS.length)],
        routing: "via_agw", model: null,
      });
    }
  }
  // Always add one direct-mcp row (mcp-only, llm=NONE)
  combos.push({
    framework: "direct-mcp", api: "chat",  // api ignored when llm=NONE
    stream: false, state: false,
    llm: "NONE", mcp: BULK_MCPS[Math.floor(Math.random() * BULK_MCPS.length)],
    routing: "via_agw", model: null,
  });

  if (!confirm(`Add ${combos.length} rows (${Object.keys(supported).length} frameworks × supported APIs + 1 direct-mcp)?`)) return;

  // POST each. Sequential keeps row order deterministic and avoids
  // hammering the harness's _save_matrix lock.
  let ok = 0, fail = 0;
  const newRows = [];
  for (const r of combos) {
    try {
      const resp = await fetch(`${API_BASE}/matrix/row`, {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify(r),
      });
      if (resp.ok) {
        const j = await resp.json();
        newRows.push({row_id: j.row_id, ...r});
        ok++;
      } else {
        fail++;
      }
    } catch {
      fail++;
    }
  }
  if (newRows.length) gridApi.applyTransaction({add: newRows});
  alert(`Added ${ok} row${ok === 1 ? "" : "s"}${fail ? ` (${fail} failed — check console)` : ""}.`);
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
  const [info, providersResp, settings] = await Promise.all([
    fetch(`${API_BASE}/info`).then(r => r.json()),
    fetch(`${API_BASE}/providers`).then(r => r.json()),
    fetch(`${API_BASE}/settings`).then(r => r.json()).catch(() => ({default_turn_count: 3})),
  ]);
  const providers = providersResp.providers;
  const turnCount = settings.default_turn_count || 3;
  const body = `
    <h3>Default turn count</h3>
    <p style="font-size:12px;color:#666;margin:0 0 6px 0;">
      Number of turns generated by the default turn plan template
      (per-row drawer overrides still win). Plans with fixed semantics
      (compact, force_state_ref) are not resized — verdicts (d) and (e)
      need exact turn counts.
    </p>
    <div style="display:flex;align-items:center;gap:10px;margin-bottom:12px;">
      <label for="setting-turn-count" style="font-size:13px;">Default turns:</label>
      <select id="setting-turn-count" style="padding:4px 8px;font-size:13px;">
        ${[1,2,3,4,5,6,8,10].map(n =>
          `<option value="${n}"${n===turnCount?" selected":""}>${n}</option>`
        ).join("")}
      </select>
      <button id="setting-turn-count-save" style="padding:4px 12px;font-size:12px;">Save</button>
      <span id="setting-turn-count-status" style="font-size:11px;color:#666;"></span>
    </div>

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
  // Wire the turn-count save button after modal-body innerHTML is set
  document.getElementById("setting-turn-count-save").addEventListener("click", async () => {
    const sel = document.getElementById("setting-turn-count");
    const status = document.getElementById("setting-turn-count-status");
    const n = parseInt(sel.value, 10);
    try {
      const r = await fetch(`${API_BASE}/settings`, {
        method: "PUT",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({default_turn_count: n}),
      });
      const j = await r.json();
      status.textContent = `✓ saved (${j.default_turn_count})`;
      status.style.color = "#28a745";
    } catch (e) {
      status.textContent = `✗ ${e}`;
      status.style.color = "#dc3545";
    }
    setTimeout(() => { status.textContent = ""; }, 2500);
  });
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

// Row pill refresh: every 5s sync status + verdicts from backend so
// running rows transition to their final pill once the background trial
// completes. Canonical update path (the grid has no SSE subscription).
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
