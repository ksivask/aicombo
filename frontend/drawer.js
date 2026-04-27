// Plan B T12 — Row drawer with an inline turn-plan JSON editor (CodeMirror 5).
//
// Opened from the matrix "📋" (Preview) button on any row. Replaces the old
// read-only plan preview modal with an editable CodeMirror editor plus:
//   * Save override   — PATCH /matrix/row/{id} with turn_plan_override
//   * Reset to default — DELETE /matrix/row/{id}/turn_plan_override
//   * Validate        — POST /templates/validate (shape check)
//
// When a trial is run next, the backend runner prefers row.turn_plan_override
// over default_turn_plan(row). See harness/api.py::trial_run.
//
// The editor is loaded via CDN (see index.html) — no bundler. Single CM5
// instance is reused across opens; we call toTextArea() before re-attaching
// so the textarea is a clean DOM node each time.

import { API_BASE } from "/config.js";

let cmInstance = null;

// Canonical turn templates surfaced as one-click buttons above the CM editor.
// Keys here are the dropdown values / button data attributes.
const TURN_TEMPLATES = {
  user_msg:                {kind: "user_msg", text: "Your prompt here"},
  compact_drop_half:       {kind: "compact", strategy: "drop_half"},
  compact_drop_tool_calls: {kind: "compact", strategy: "drop_tool_calls"},
  compact_summarize:       {kind: "compact", strategy: "summarize"},
  force_state_ref:         {kind: "force_state_ref", lookback: 2, text: "Refer back to earlier."},
  reset_context:           {kind: "reset_context"},                              // E21
  refresh_tools:           {kind: "refresh_tools"},                              // E21
};

// Append a copy of TURN_TEMPLATES[key] to plan.turns[] in the editor. If the
// editor is empty / invalid JSON, initializes a fresh {turns: [picked]}.
// Exposed on window for click handlers wired in the rendered toolbar HTML.
function addTurn(templateKey) {
  if (!cmInstance) return;
  const tmpl = TURN_TEMPLATES[templateKey];
  if (!tmpl) return;
  let plan;
  try {
    plan = JSON.parse(cmInstance.getValue());
    if (!plan || typeof plan !== "object") plan = {turns: []};
    if (!Array.isArray(plan.turns)) plan.turns = [];
  } catch {
    plan = {turns: []};
  }
  // Deep-clone the template so successive clicks don't share references.
  plan.turns.push(JSON.parse(JSON.stringify(tmpl)));
  cmInstance.setValue(JSON.stringify(plan, null, 2));
  setStatus("info", `Added ${templateKey} turn (${plan.turns.length} total) — review and Save.`);
}

function escapeHtml(s) {
  return String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

function getOrCreateModal() {
  let modal = document.getElementById("drawer-modal");
  if (modal) return modal;
  modal = document.createElement("div");
  modal.id = "drawer-modal";
  modal.className = "modal";
  modal.innerHTML = `
    <div class="modal-content drawer-modal-content">
      <div class="modal-header">
        <span id="drawer-title">Row drawer</span>
        <button id="drawer-close">✕</button>
      </div>
      <div class="modal-body" id="drawer-body"></div>
    </div>
  `;
  document.body.appendChild(modal);
  modal.addEventListener("click", (e) => {
    if (e.target === modal) closeDrawer();
  });
  modal.querySelector("#drawer-close").addEventListener("click", closeDrawer);
  return modal;
}

function closeDrawer() {
  const modal = document.getElementById("drawer-modal");
  if (!modal) return;
  // Detach CodeMirror so the next open gets a fresh instance.
  if (cmInstance) {
    try { cmInstance.toTextArea(); } catch {}
    cmInstance = null;
  }
  modal.classList.add("hidden");
}

function setStatus(kind, msg) {
  const el = document.getElementById("tp-status");
  if (!el) return;
  el.textContent = msg || "";
  el.className = `tp-status tp-status-${kind || "info"}`;
}

function renderChips(row) {
  const chips = [
    ["framework", row.framework],
    ["api", row.api],
    row.stream ? ["stream", "on"] : null,
    row.state ? ["state", "on"] : null,
    ["llm", row.llm],
    ["mcp", row.mcp],
    ["routing", row.routing],
  ].filter(Boolean);
  return chips.map(([k, v]) =>
    `<span class="chip"><span class="chip-k">${escapeHtml(k)}</span>${escapeHtml(v || "")}</span>`
  ).join("");
}

export async function openTurnPlanDrawer(row) {
  if (!row || !row.row_id) return;
  const modal = getOrCreateModal();
  document.getElementById("drawer-title").textContent =
    `Row ${row.row_id} — turn plan`;

  const hasOverride = row.turn_plan_override !== undefined && row.turn_plan_override !== null;
  const overrideBadge = hasOverride
    ? '<span class="plan-executed-badge" title="row has a saved override">override</span>'
    : '<span class="plan-pending-badge" title="no override; runner will use default_turn_plan(row)">default</span>';

  // T14 — if the row's latest trial is running, surface a Stop button right
  // in the drawer header so the user doesn't have to close the drawer to
  // abort. Disabled gracefully when last_trial_id is absent.
  const runningHint = row.status === "running" && row.last_trial_id
    ? `<button id="drawer-abort-btn" class="btn-abort"
         data-trial-id="${row.last_trial_id}"
         title="stop trial (current turn finishes, next turns skipped)"
       >⏹ Stop</button>`
    : "";

  // T13 — surface baseline-pair pointer so the user can flip between
  // the governed row and its no-governance twin.
  const baselineHint = row.baseline_of
    ? `<div class="plan-note" style="background:#fff3e0; border-left:3px solid #ff9800;">
         Baseline pair — comparing against
         <code>${escapeHtml(row.baseline_of)}</code>. This row is routed
         <b>direct</b> (no AGW / no cidgar governance).
       </div>`
    : "";

  document.getElementById("drawer-body").innerHTML = `
    <div class="drawer-section">
      <div class="drawer-section-header">
        <h3 style="margin:0;">Row config</h3>
        <div>${overrideBadge}${runningHint}</div>
      </div>
      <div class="row-summary">${renderChips(row)}</div>
      ${baselineHint}
    </div>
    <div class="drawer-section">
      <div class="drawer-section-header">
        <h3 style="margin:0;">Turn Plan</h3>
        <div class="drawer-section-actions">
          <button id="tp-validate-btn" title="check plan shape">Validate</button>
          <button id="tp-reset-btn" title="discard override; revert to default template">Reset to default</button>
          <button id="tp-save-btn" title="save as per-row override">Save override</button>
        </div>
      </div>
      <div id="tp-status" class="tp-status"></div>
      <div class="tp-add-turn-bar">
        <span class="tp-add-turn-label">+ Add turn:</span>
        <button type="button" data-tpl="user_msg" class="tp-add-turn-btn"
          title='append {"kind":"user_msg","text":"…"}'>user_msg</button>
        <button type="button" data-tpl="compact_drop_half" class="tp-add-turn-btn"
          title='append {"kind":"compact","strategy":"drop_half"}'>compact (drop_half)</button>
        <button type="button" data-tpl="compact_drop_tool_calls" class="tp-add-turn-btn"
          title='append {"kind":"compact","strategy":"drop_tool_calls"}'>compact (drop_tool_calls)</button>
        <button type="button" data-tpl="compact_summarize" class="tp-add-turn-btn"
          title='append {"kind":"compact","strategy":"summarize"}'>compact (summarize)</button>
        <button type="button" data-tpl="force_state_ref" class="tp-add-turn-btn"
          title='append {"kind":"force_state_ref","lookback":2,"text":"…"}'>force_state_ref (lookback=2)</button>
        <button type="button" data-tpl="reset_context" class="tp-add-turn-btn"
          title='append {"kind":"reset_context"} — E21: wipe agent-side LLM history; AGW mints fresh CID on next turn'>reset_context</button>
        <button type="button" data-tpl="refresh_tools" class="tp-add-turn-btn"
          title='append {"kind":"refresh_tools"} — E21: force MCP tools/list re-fetch (no-op for adapters that re-fetch per call)'>refresh_tools</button>
      </div>
      <textarea id="tp-editor"></textarea>
      <p class="plan-note">
        Edits are free-form JSON. Click <code>Validate</code> before saving.
        Save persists to <code>matrix.json</code> and is used by the next trial run.
        Reset clears the override so the runner falls back to <code>default_turn_plan(row)</code>.
      </p>
    </div>
  `;

  modal.classList.remove("hidden");

  // Initial editor content: override first, else /templates/preview default.
  let initialPlan = hasOverride ? row.turn_plan_override : null;
  if (!initialPlan) {
    try {
      const r = await fetch(`${API_BASE}/templates/preview`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(row),
      });
      if (r.ok) {
        const data = await r.json();
        initialPlan = data.turn_plan || { turns: [] };
      } else {
        initialPlan = { turns: [] };
      }
    } catch {
      initialPlan = { turns: [] };
    }
  }

  const ta = document.getElementById("tp-editor");
  ta.value = JSON.stringify(initialPlan, null, 2);

  // Re-attach CodeMirror — replacement always wanted since modal content
  // was rewritten above.
  if (cmInstance) {
    try { cmInstance.toTextArea(); } catch {}
    cmInstance = null;
  }
  cmInstance = window.CodeMirror.fromTextArea(ta, {
    mode: { name: "javascript", json: true },
    theme: "dracula",
    lineNumbers: true,
    lineWrapping: true,
    gutters: ["CodeMirror-lint-markers"],
    lint: true,
  });

  // ── Wire add-turn quick-template buttons (above the editor) ──
  document.querySelectorAll(".tp-add-turn-btn").forEach(btn => {
    btn.onclick = () => addTurn(btn.dataset.tpl);
  });

  // ── Wire buttons ──
  document.getElementById("tp-validate-btn").onclick = async () => {
    let parsed;
    try {
      parsed = JSON.parse(cmInstance.getValue());
    } catch (e) {
      setStatus("error", `JSON parse error: ${e.message}`);
      return;
    }
    try {
      const r = await fetch(`${API_BASE}/templates/validate`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ turn_plan: parsed }),
      });
      const v = await r.json();
      if (v.ok) {
        setStatus("ok", "Valid turn plan");
      } else {
        setStatus("error", `Errors:\n - ${(v.errors || []).join("\n - ")}`);
      }
    } catch (e) {
      setStatus("error", `Validate request failed: ${e.message}`);
    }
  };

  document.getElementById("tp-save-btn").onclick = async () => {
    let parsed;
    try {
      parsed = JSON.parse(cmInstance.getValue());
    } catch (e) {
      setStatus("error", `JSON parse error: ${e.message}`);
      return;
    }
    try {
      const r = await fetch(`${API_BASE}/matrix/row/${row.row_id}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ turn_plan_override: parsed }),
      });
      if (r.ok) {
        row.turn_plan_override = parsed;
        setStatus("ok", "Override saved — next run will use it");
      } else {
        setStatus("error", `HTTP ${r.status}`);
      }
    } catch (e) {
      setStatus("error", `Save failed: ${e.message}`);
    }
  };

  // T14 — drawer-header Stop button (only rendered while row is running).
  const abortBtn = document.getElementById("drawer-abort-btn");
  if (abortBtn) {
    abortBtn.onclick = async () => {
      const tid = abortBtn.dataset.trialId;
      if (!tid) return;
      if (!confirm(`Abort trial ${tid}?\n\nThe currently-executing turn will finish; subsequent turns are skipped.`)) return;
      try {
        const r = await fetch(`${API_BASE}/trials/${tid}/abort`, {method: "POST"});
        const j = await r.json();
        if (j.ok) {
          setStatus("ok", `Abort requested for ${tid}`);
          abortBtn.disabled = true;
        } else {
          setStatus("info", `Trial already finished: status=${j.status || "?"}`);
          abortBtn.disabled = true;
        }
      } catch (e) {
        setStatus("error", `Abort failed: ${e.message}`);
      }
    };
  }

  document.getElementById("tp-reset-btn").onclick = async () => {
    if (!confirm("Clear override and revert to default template?")) return;
    try {
      const r = await fetch(
        `${API_BASE}/matrix/row/${row.row_id}/turn_plan_override`,
        { method: "DELETE" }
      );
      if (!r.ok) {
        setStatus("error", `Reset failed: HTTP ${r.status}`);
        return;
      }
      delete row.turn_plan_override;
      // Re-open to refresh with the fresh default — reuses this function's
      // logic end-to-end (new editor, refreshed badge).
      await openTurnPlanDrawer(row);
      setStatus("ok", "Reverted to default");
    } catch (e) {
      setStatus("error", `Reset failed: ${e.message}`);
    }
  };
}
