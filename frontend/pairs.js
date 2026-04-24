// E4 — pair diff page. Renders the output of GET /pairs/{row_id} side by
// side (governed left, baseline right) with a summary banner up top.
import { API_BASE } from "/config.js";

const params = new URLSearchParams(location.search);
const rowId = params.get("row_id");
if (!rowId) {
  document.body.innerHTML =
    "<p class=\"error\">Missing <code>?row_id=...</code> query param.</p>";
  throw new Error("missing row_id");
}

document.getElementById("pair-row-id").textContent = rowId;

function escapeHtml(s) {
  return String(s)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function formatMs(v) {
  if (v === null || v === undefined) return "n/a";
  return `${Number(v).toFixed(1)} ms`;
}

function verdictPills(verdicts) {
  if (!verdicts || Object.keys(verdicts).length === 0) {
    return "<span class=\"vp vp-na\" title=\"no verdicts\">—</span>";
  }
  return Object.entries(verdicts)
    .filter(([k]) => !k.startsWith("_"))
    .map(([k, v]) => {
      const cls = (v && v.verdict) || "na";
      const reason = (v && v.reason) || "";
      return `<span class="vp vp-${escapeHtml(cls)}" title="${escapeHtml(reason)}">${escapeHtml(k)}:${escapeHtml(cls)}</span>`;
    })
    .join(" ");
}

function renderSummary(pair) {
  const s = pair.diff_summary;
  const lo = s.latency_overhead_ms || {};
  const meta = document.getElementById("pair-meta");
  meta.innerHTML =
    `<span class="chip"><span class="chip-k">governed</span>${escapeHtml(pair.governed_row_id)}</span>` +
    `<span class="chip"><span class="chip-k">baseline</span>${escapeHtml(pair.baseline_row_id)}</span>`;

  const expected = s.classification?.expected_diffs || [];
  const unexpected = s.classification?.unexpected_diffs || [];

  const html = `
    <div class="pair-summary-grid">
      <div class="card">
        <h3>Audit entries</h3>
        <div class="kv">governed: <strong>${s.audit_entry_count.governed}</strong></div>
        <div class="kv">baseline: <strong>${s.audit_entry_count.baseline}</strong></div>
      </div>
      <div class="card">
        <h3>Turn count</h3>
        <div class="kv">governed: <strong>${s.turn_count.governed}</strong></div>
        <div class="kv">baseline: <strong>${s.turn_count.baseline}</strong></div>
      </div>
      <div class="card">
        <h3>Latency overhead</h3>
        <div class="kv">median: <strong>${formatMs(lo.median)}</strong></div>
        <div class="kv">p95: <strong>${formatMs(lo.p95)}</strong></div>
        <div class="kv small">over ${lo.n_turns ?? 0} turn${(lo.n_turns ?? 0) === 1 ? "" : "s"}</div>
      </div>
      <div class="card">
        <h3>Verdicts</h3>
        <div class="kv">governed: ${verdictPills(s.verdicts.governed)}</div>
        <div class="kv">baseline: ${verdictPills(s.verdicts.baseline)}</div>
        <div class="kv small">↻ refreshed ${escapeHtml(new Date().toLocaleTimeString())}</div>
      </div>
    </div>
    <div class="pair-classification">
      ${expected.length ? `<div class="cls-expected"><h4>✓ Expected</h4><ul>${expected.map(d => `<li>${escapeHtml(d)}</li>`).join("")}</ul></div>` : ""}
      ${unexpected.length ? `<div class="cls-unexpected"><h4>⚠ Unexpected</h4><ul>${unexpected.map(d => `<li>${escapeHtml(d)}</li>`).join("")}</ul></div>` : ""}
    </div>
  `;
  document.getElementById("pair-summary").innerHTML = html;
}

function turnDurationMs(turn) {
  if (!turn || !turn.started_at || !turn.finished_at) return null;
  const s = Date.parse(turn.started_at);
  const f = Date.parse(turn.finished_at);
  if (Number.isNaN(s) || Number.isNaN(f)) return null;
  return f - s;
}

function renderTurn(i, gT, bT) {
  const gDur = turnDurationMs(gT);
  const bDur = turnDurationMs(bT);
  const delta = (gDur != null && bDur != null) ? (gDur - bDur) : null;
  const deltaHtml = delta != null
    ? `<span class="turn-delta ${delta > 0 ? 'pos' : 'neg'}">${delta >= 0 ? '+' : ''}${delta.toFixed(0)} ms</span>`
    : "";

  const kind = gT?.kind || bT?.kind || "?";
  const el = document.createElement("section");
  el.className = "pair-turn";
  el.innerHTML = `
    <h3>Turn ${i} <span class="turn-kind">(${escapeHtml(kind)})</span> ${deltaHtml}</h3>
    <div class="diff-cols">
      <div class="diff-col">
        <h4>Governed (via_agw)</h4>
        <div class="turn-meta">${gDur != null ? `${gDur} ms` : "<em>no timing</em>"}</div>
        <pre class="trial-json">${escapeHtml(JSON.stringify(gT, null, 2) || "null")}</pre>
      </div>
      <div class="diff-col">
        <h4>Baseline (direct)</h4>
        <div class="turn-meta">${bDur != null ? `${bDur} ms` : "<em>no timing</em>"}</div>
        <pre class="trial-json">${escapeHtml(JSON.stringify(bT, null, 2) || "null")}</pre>
      </div>
    </div>
  `;
  return el;
}

function renderTurns(pair) {
  const g = pair.governed?.turns || [];
  const b = pair.baseline?.turns || [];
  const container = document.getElementById("pair-turns");
  container.innerHTML = "";
  const maxN = Math.max(g.length, b.length);
  if (maxN === 0) {
    container.innerHTML = "<p class=\"empty-state\">No turns recorded on either side.</p>";
    return;
  }
  for (let i = 0; i < maxN; i++) {
    container.appendChild(renderTurn(i, g[i], b[i]));
  }
}

async function load() {
  try {
    // First fetch — learn trial_ids so we know which trials to refresh.
    const r1 = await fetch(`${API_BASE}/pairs/${encodeURIComponent(rowId)}`);
    if (!r1.ok) {
      const text = await r1.text();
      document.getElementById("pair-summary").innerHTML =
        `<p class="error">HTTP ${r1.status}: ${escapeHtml(text)}</p>`;
      return;
    }
    const pair1 = await r1.json();

    // Recompute both sides' verdicts so the frozen (h) reflects the now-
    // existing pair. compute_verdicts inside run_trial fires BEFORE the
    // matrix reconciliation that sets last_trial_id, so the first run of
    // a governed trial always records h=na. Best-effort — don't block the
    // render if a recompute fails.
    const gTid = pair1.governed?.trial_id;
    const bTid = pair1.baseline?.trial_id;
    await Promise.all([
      gTid
        ? fetch(`${API_BASE}/trials/${encodeURIComponent(gTid)}/recompute_verdicts`,
                {method: "POST"}).catch(() => {})
        : Promise.resolve(),
      bTid
        ? fetch(`${API_BASE}/trials/${encodeURIComponent(bTid)}/recompute_verdicts`,
                {method: "POST"}).catch(() => {})
        : Promise.resolve(),
    ]);

    // Second fetch — now with fresh frozen verdicts inside governed/baseline.
    const r2 = await fetch(`${API_BASE}/pairs/${encodeURIComponent(rowId)}`);
    if (!r2.ok) {
      // Fall back to the first envelope if the re-fetch fails.
      renderSummary(pair1);
      renderTurns(pair1);
      return;
    }
    const pair = await r2.json();
    renderSummary(pair);
    renderTurns(pair);
  } catch (e) {
    document.getElementById("pair-summary").innerHTML =
      `<p class="error">Load failed: ${escapeHtml(e.message)}</p>`;
  }
}

load();
