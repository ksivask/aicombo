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
  user_msg:                {kind: "user_msg", content: "Your prompt here"},
  compact_drop_half:       {kind: "compact", strategy: "drop_half"},
  compact_drop_tool_calls: {kind: "compact", strategy: "drop_tool_calls"},
  compact_summarize:       {kind: "compact", strategy: "summarize"},
  force_state_ref:         {kind: "force_state_ref", lookback: 2, content: "Refer back to earlier."},
  reset_context:           {kind: "reset_context"},                              // E21
  refresh_tools:           {kind: "refresh_tools"},                              // E21
};

// Insert a copy of TURN_TEMPLATES[key] into plan.turns[] at the cursor's
// current position. Walks the editor's live text to find each turn-object's
// "naked" opening-brace line and maps cursor.line → turn index. Cursor at/
// before turn N's opener inserts BEFORE turn N (so the new turn becomes N
// and the rest shift down). Cursor past the last opener appends. Cursor
// outside the turns array → append (sane fallback). Empty editor / invalid
// JSON / no openers → append.
//
// The opener regex `/^\s*\{\s*$/` is whole-line so it only matches "naked"
// turn-opener lines (the opening brace on its own line, as JSON.stringify
// formats it with `null, 2` indent). Inline braces inside string literals
// or single-line objects don't match — sufficient for the formatted shape
// the editor produces.
function _cursorToTurnIndex(plan) {
  if (!cmInstance || !plan.turns || plan.turns.length === 0) return 0;
  const cursor = cmInstance.getCursor();
  const lines = cmInstance.getValue().split("\n");
  const liveOpenerLines = [];
  let inArray = false;
  for (let i = 0; i < lines.length; i++) {
    const ln = lines[i];
    if (!inArray) {
      if (/"turns":\s*\[/.test(ln)) inArray = true;
      continue;
    }
    if (/^\s*\{\s*$/.test(ln)) liveOpenerLines.push(i);
  }
  // Cursor is BEFORE the first turn's opener → insert at 0.
  // Cursor is AT or PAST the last opener → append.
  // Cursor between opener[i] and opener[i+1] → insert at i+1 (after turn i).
  if (liveOpenerLines.length === 0) return plan.turns.length;
  if (cursor.line < liveOpenerLines[0]) return 0;
  for (let idx = 0; idx < liveOpenerLines.length - 1; idx++) {
    if (cursor.line >= liveOpenerLines[idx] && cursor.line < liveOpenerLines[idx + 1]) {
      return idx + 1;
    }
  }
  return plan.turns.length;
}

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
    // Invalid JSON — start fresh; cursor mapping is moot.
    plan = {turns: []};
  }
  // Deep-clone the template so successive clicks don't share references.
  const newTurn = JSON.parse(JSON.stringify(tmpl));
  const insertAt = _cursorToTurnIndex(plan);
  plan.turns.splice(insertAt, 0, newTurn);
  cmInstance.setValue(JSON.stringify(plan, null, 2));
  setStatus(
    "info",
    `Inserted ${templateKey} turn at position ${insertAt} (${plan.turns.length} total) — review and Save.`
  );
}

function escapeHtml(s) {
  return String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

// E37 — surface which plan flags are currently set. The drawer-paragraph
// hint below the checkboxes lists a precedence order that is intentionally
// wrong (per code-review B1, awaiting user decision); the warning here
// reports the ACTUAL templates.py order so the message is accurate even
// while the hint is stale.
function _activePlanFlags(row) {
  return [
    row.with_force_state_ref && "with_force_state_ref",
    row.with_reset            && "with_reset",
    row.with_e20_verification && "with_e20_verification",
    row.with_compact          && "with_compact",
  ].filter(Boolean);
}

function _renderFlagWarning(row) {
  const active = _activePlanFlags(row);
  if (active.length < 2) return "";
  // Precedence per templates.py — reset BEFORE e20_verification.
  const order = ["with_force_state_ref", "with_reset", "with_e20_verification", "with_compact"];
  const winner = order.find(f => active.includes(f));
  return `<div class="drawer-flag-warning">⚠ Multiple plan flags set (${escapeHtml(active.join(", "))}). Only <code>${escapeHtml(winner)}</code> will execute (per templates.py precedence). Other flags ignored at runtime.</div>`;
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

  // E37 — verdict-purposed plan flags. Each toggle PATCHes the row +
  // surfaces a hint that the editor still shows the OLD plan (user must
  // click 'Reset to default' to load the new template). Precedence per
  // templates.py::default_turn_plan: with_force_state_ref >
  // with_e20_verification > with_reset > with_compact.
  const flagsRow = `
    <div class="drawer-flag" title="Verdict (d) — append a compact turn between user_msg turns">
      <label><input type="checkbox" data-flag="with_compact" ${row.with_compact ? "checked" : ""}>
        with_compact <small>(verdict d)</small></label>
    </div>
    <div class="drawer-flag" title="Verdict (e) — exercise force_state_ref jump for Responses-API state-mode">
      <label><input type="checkbox" data-flag="with_force_state_ref" ${row.with_force_state_ref ? "checked" : ""}>
        with_force_state_ref <small>(verdict e)</small></label>
    </div>
    <div class="drawer-flag" title="Verdict (c) bracket-aware — split trial into segments by reset_context">
      <label><input type="checkbox" data-flag="with_reset" ${row.with_reset ? "checked" : ""}>
        with_reset <small>(verdict c segments)</small></label>
    </div>
    <div class="drawer-flag" title="Verdict (i) — exercise mcp_admin mutation between turns; requires mcp=mutable">
      <label><input type="checkbox" data-flag="with_e20_verification" ${row.with_e20_verification ? "checked" : ""}>
        with_e20_verification <small>(verdict i; mcp=mutable required)</small></label>
    </div>
  `;

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
        <h3 style="margin:0;">Plan flags</h3>
        <div id="drawer-flags-status" class="tp-status"></div>
      </div>
      <div class="drawer-flags-row">${flagsRow}</div>
      <div id="drawer-flag-warning-slot">${_renderFlagWarning(row)}</div>
      <p class="plan-note">
        Each flag swaps the default turn plan to a verdict-purposed
        template. If multiple are set, precedence wins:
        <code>with_force_state_ref</code> > <code>with_e20_verification</code> >
        <code>with_reset</code> > <code>with_compact</code>. Toggling a
        flag PATCHes the row immediately; click <code>Reset to default</code>
        below to reload the editor with the new template.
      </p>
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
          title='append {"kind":"user_msg","content":"…"}'>user_msg</button>
        <button type="button" data-tpl="compact_drop_half" class="tp-add-turn-btn"
          title='append {"kind":"compact","strategy":"drop_half"}'>compact (drop_half)</button>
        <button type="button" data-tpl="compact_drop_tool_calls" class="tp-add-turn-btn"
          title='append {"kind":"compact","strategy":"drop_tool_calls"}'>compact (drop_tool_calls)</button>
        <button type="button" data-tpl="compact_summarize" class="tp-add-turn-btn"
          title='append {"kind":"compact","strategy":"summarize"}'>compact (summarize)</button>
        <button type="button" data-tpl="force_state_ref" class="tp-add-turn-btn"
          title='append {"kind":"force_state_ref","lookback":2,"content":"…"}'>force_state_ref (lookback=2)</button>
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

  // ── Wire E37 plan-flag checkboxes ──
  document.querySelectorAll(".drawer-flag input[type=checkbox]").forEach(cb => {
    cb.onchange = async () => {
      const flag = cb.dataset.flag;
      const value = cb.checked;
      const status = document.getElementById("drawer-flags-status");
      try {
        const r = await fetch(`${API_BASE}/matrix/row/${row.row_id}`, {
          method: "PATCH",
          headers: {"Content-Type": "application/json"},
          body: JSON.stringify({[flag]: value}),
        });
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        // Mutate the local row object so subsequent toggles see fresh state
        row[flag] = value;
        status.textContent = `✓ ${flag} = ${value} saved. Click 'Reset to default' to reload editor with the new template.`;
        status.className = "tp-status tp-status-info";
      } catch (e) {
        // Revert UI checkbox on failure so it reflects backend truth
        cb.checked = !value;
        status.textContent = `✗ ${flag}: ${e.message}`;
        status.className = "tp-status tp-status-error";
      }
      // Re-render the multi-flag mutex warning so it reflects the new
      // active-flag set every toggle (regardless of PATCH outcome — local
      // row[flag] was reverted on failure).
      const slot = document.getElementById("drawer-flag-warning-slot");
      if (slot) slot.innerHTML = _renderFlagWarning(row);
      setTimeout(() => { status.textContent = ""; }, 4000);
    };
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
