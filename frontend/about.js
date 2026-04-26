// About modal — two tables side-by-side:
//   1. Library-level native support (static research data — what the
//      underlying SDK supports natively, independent of aiplay)
//   2. Aiplay adapter implementation status (live from /info.frameworks
//      which mirrors harness/validator.py::ADAPTER_CAPABILITIES)
//
// Cells: ✓ native, ⚠ via wrapper / litellm / community plugin,
//        ✗ no support, — N/A.
//
// Library data is hand-curated and accurate as of 2026-04-26. When a
// library adds first-class support for a new API, edit LIBRARY_NATIVE_SUPPORT
// here. The aiplay-adapter table is data-driven so it auto-updates from
// the harness validator.

import { API_BASE } from "/config.js";

const FRAMEWORKS = [
  "langchain",
  "langgraph",
  "crewai",
  "pydantic-ai",
  "autogen",
  "llamaindex",
  "direct-mcp",
];

// Columns for the library-level table. The aiplay-adapter table only
// shows API columns since that's all `validator.py::ADAPTER_CAPABILITIES`
// currently tracks.
const LIBRARY_COLUMNS = [
  {key: "chat",            label: "chat",            title: "OpenAI Chat Completions /v1/chat/completions"},
  {key: "messages",        label: "messages",        title: "Anthropic /v1/messages"},
  {key: "responses",       label: "responses",       title: "OpenAI /v1/responses"},
  {key: "responses+conv",  label: "+conv",           title: "OpenAI Conversations API container"},
  {key: "mcp",             label: "mcp",             title: "Model Context Protocol client (tools/resources)"},
  {key: "streaming",       label: "streaming",       title: "Server-Sent Events streaming responses"},
  {key: "tool_calling",    label: "tool_calling",    title: "Function/tool calling primitive"},
];

// LIBRARY_NATIVE_SUPPORT — source-of-truth for library capabilities
// (independent of whether aiplay's adapter implements them).
//
// Cell values:
//   "yes"  → first-class native support in the library
//   "via"  → supported via a wrapper layer (e.g., crewai uses litellm)
//   "no"   → no support
//   "na"   → not applicable (e.g., LLM APIs for direct-mcp)
const LIBRARY_NATIVE_SUPPORT = {
  langchain: {
    chat:              "yes",
    messages:          "yes",
    responses:         "yes",
    // langchain-openai 1.1.16 has no first-class `conversation` model field
    // (verified against ChatOpenAI.model_fields). aiplay's adapter passes
    // the conversation id via .bind(conversation={"id": ...}) — generic
    // kwargs forwarding to the openai SDK, not a typed library API.
    "responses+conv":  "via",
    mcp:               "yes",
    streaming:         "yes",
    tool_calling:      "yes",
  },
  langgraph: {
    // Sits on langchain — inherits all langchain capabilities (incl. the
    // same kwargs-forwarding pattern for +conv, hence "via" not "yes")
    chat:              "yes",
    messages:          "yes",
    responses:         "yes",
    "responses+conv":  "via",
    mcp:               "yes",
    streaming:         "yes",
    tool_calling:      "yes",
  },
  crewai: {
    // crewai 1.14+ routes openai/anthropic/gemini/etc. to NATIVE provider
    // classes (OpenAICompletion, AnthropicCompletion, GeminiCompletion)
    // wrapping the vendor SDKs directly. litellm is only the fallback for
    // unrecognized model strings.
    chat:              "yes",  // OpenAICompletion via native openai SDK
    messages:          "yes",  // AnthropicCompletion via native anthropic SDK
    responses:         "yes",  // OpenAICompletion has first-class responses.create + previous_response_id
    "responses+conv":  "no",   // no /v1/conversations container support in crewai's responses path
    mcp:               "yes",  // crewai-tools[mcp] adapter
    streaming:         "yes",
    tool_calling:      "yes",
  },
  "pydantic-ai": {
    chat:              "yes",
    messages:          "yes",
    responses:         "yes",  // OpenAIResponsesModel
    "responses+conv":  "no",   // no Conversations API container support
    mcp:               "yes",  // pydantic-ai-mcp
    streaming:         "yes",
    tool_calling:      "yes",
  },
  autogen: {
    // Microsoft AutoGen 0.7+. autogen-ext ships OpenAIChatCompletionClient
    // and AnthropicChatCompletionClient but NO Responses-API client (verified
    // against autogen-ext 0.7.5: exports only OpenAIChatCompletionClient and
    // AzureOpenAIChatCompletionClient). aiplay's autogen adapter exposes
    // Responses by bypassing AssistantAgent and calling the openai SDK
    // directly — see ADAPTER_BYPASS_APIS below.
    chat:              "yes",
    messages:          "yes",
    responses:         "no",   // no native OpenAIResponsesChatCompletionClient in autogen-ext
    "responses+conv":  "no",   // no Responses → no Conversations API container support
    mcp:               "yes",  // autogen-ext mcp tools
    streaming:         "yes",
    tool_calling:      "yes",
  },
  llamaindex: {
    chat:              "yes",
    // Library has native Anthropic support via the separate
    // `llama-index-llms-anthropic` package (versions through 0.11.3).
    // aiplay's adapter just doesn't wire it (Plan B T6 scope decision —
    // see ADAPTER_TBD_ENHANCEMENTS / E5e). Library cell reflects library
    // reality, not adapter scope.
    messages:          "yes",
    responses:         "yes",  // OpenAIResponses
    // Zero `conversation` references in llama-index-llms-openai source.
    // aiplay's adapter handles +conv by direct httpx POST to /v1/responses
    // with the conversation field — the openai client class doesn't model
    // it. (Library: no; aiplay: bypass.)
    "responses+conv":  "no",
    mcp:               "yes",  // llama-index-tools-mcp
    streaming:         "yes",
    tool_calling:      "yes",
  },
  "direct-mcp": {
    // Custom adapter — no LLM client, raw MCP only
    chat:              "na",
    messages:          "na",
    responses:         "na",
    "responses+conv":  "na",
    mcp:               "yes",  // raw mcp Python SDK
    streaming:         "na",
    tool_calling:      "na",
  },
};

const CELL_GLYPH = {
  yes:    {glyph: "✓", className: "cell-yes",    title: "native support"},
  via:    {glyph: "⚠", className: "cell-via",    title: "via wrapper / litellm / kwargs-forwarding (not a first-class library field)"},
  bypass: {glyph: "⊘", className: "cell-bypass", title: "bypass — aiplay adapter calls the API directly via SDK, sidestepping the framework's own abstractions (still exercises the AGW route + governance)"},
  tbd:    {glyph: "⌛", className: "cell-tbd",    title: "TBD — deferred enhancement filed in docs/enhancements.md"},
  no:     {glyph: "✗", className: "cell-no",     title: "no support / not implemented"},
  na:     {glyph: "—", className: "cell-na",     title: "not applicable"},
};

// ADAPTER_BYPASS_APIS — which APIs aiplay's adapter implements via a
// bypass path (driving the underlying SDK directly instead of through
// the framework's higher-level abstractions). Source: comments in
// harness/validator.py::ADAPTER_CAPABILITIES (e.g., autogen comment
// reads "AssistantAgent + openai responses bypass").
//
// Why bypass exists: some frameworks either don't natively expose the
// API yet (e.g., responses+conv missing from the framework's client
// classes) OR their native path is too opaque to test the AGW
// governance hooks. Bypass lets aiplay still exercise the AGW route
// for that API+framework combo, even when the framework itself wouldn't.
//
// Note: bypass-capable but NOT-in-supported_apis combos (crewai
// responses/+conv, pydantic-ai +conv, llamaindex messages) are documented
// in validator.py comments OR docs/enhancements.md (the E5c/d/e cluster)
// — they surface as ⌛ TBD via ADAPTER_TBD_ENHANCEMENTS below, not ✗.
const ADAPTER_BYPASS_APIS = {
  autogen:    new Set(["responses", "responses+conv"]),
  llamaindex: new Set(["responses", "responses+conv"]),
};

// ADAPTER_TBD_ENHANCEMENTS — (framework, api) combos that aiplay's adapter
// currently doesn't implement BUT which have a deferred enhancement filed
// in docs/enhancements.md. The adapter table renders these as ⌛ (TBD)
// instead of ✗ (no), so it's clear the gap is scoped-out, not impossible.
//
// Each value is the enhancement id (E5c/d/e) — surfaced in the cell's
// hover tooltip. Update this map (and the cell hover) when an enhancement
// lands and the adapter starts supporting that combo.
const ADAPTER_TBD_ENHANCEMENTS = {
  crewai:        {responses: "E5c", "responses+conv": "E5c"},
  "pydantic-ai": {"responses+conv": "E5d"},
  llamaindex:    {messages: "E5e"},
};

function renderCell(value, extra) {
  const c = CELL_GLYPH[value] || {glyph: "?", className: "cell-na", title: "unknown"};
  // Optional `extra` string is appended to glyph (used for TBD cells to
  // surface the enhancement id, e.g., "⌛ E5e") and merged into the
  // tooltip title.
  const glyph = extra ? `${c.glyph} ${extra}` : c.glyph;
  const title = extra ? `${c.title} (${extra})` : c.title;
  return `<td class="${c.className}" title="${title}">${glyph}</td>`;
}

function renderLibraryTable() {
  const cols = LIBRARY_COLUMNS;
  const head = `<tr><th>framework</th>${cols.map(c => `<th title="${c.title}">${c.label}</th>`).join("")}</tr>`;
  const rows = FRAMEWORKS.map(fw => {
    const caps = LIBRARY_NATIVE_SUPPORT[fw] || {};
    return `<tr><td class="fw-name">${fw}</td>${cols.map(c => renderCell(caps[c.key])).join("")}</tr>`;
  }).join("");
  return `<table class="support-matrix"><thead>${head}</thead><tbody>${rows}</tbody></table>`;
}

function renderAdapterTable(infoFrameworks) {
  // Only API columns — that's all ADAPTER_CAPABILITIES tracks today.
  const apiCols = LIBRARY_COLUMNS.filter(c =>
    ["chat", "messages", "responses", "responses+conv"].includes(c.key));
  const head = `<tr><th>framework</th>${apiCols.map(c => `<th title="${c.title}">${c.label}</th>`).join("")}</tr>`;
  const rows = FRAMEWORKS.map(fw => {
    const supported = new Set((infoFrameworks?.[fw]?.supported_apis) || []);
    const bypassSet = ADAPTER_BYPASS_APIS[fw] || new Set();
    const tbdMap = ADAPTER_TBD_ENHANCEMENTS[fw] || {};
    return `<tr><td class="fw-name">${fw}</td>${apiCols.map(c => {
      // direct-mcp has no LLM APIs; show "—" not "✗"
      if (fw === "direct-mcp") return renderCell("na");
      if (supported.has(c.key)) {
        // Supported AND in bypass set → ⊘; otherwise → native ✓
        return renderCell(bypassSet.has(c.key) ? "bypass" : "yes");
      }
      // Not supported but a deferred enhancement exists → ⌛ <id>
      if (tbdMap[c.key]) return renderCell("tbd", tbdMap[c.key]);
      return renderCell("no");
    }).join("")}</tr>`;
  }).join("");
  return `<table class="support-matrix"><thead>${head}</thead><tbody>${rows}</tbody></table>`;
}

function renderLegend() {
  return `
    <div class="support-legend">
      <span><span class="cell-yes">✓</span> native</span>
      <span><span class="cell-via">⚠</span> via wrapper / litellm / kwargs-forwarding</span>
      <span><span class="cell-bypass">⊘</span> bypass (adapter calls SDK directly, sidesteps framework)</span>
      <span><span class="cell-tbd">⌛</span> TBD — deferred enhancement</span>
      <span><span class="cell-no">✗</span> no support / not implemented</span>
      <span><span class="cell-na">—</span> N/A</span>
    </div>
    <p class="modal-note" style="margin-top:6px;">
      <strong>Bypass</strong> means aiplay's adapter exposes the API by
      calling the underlying SDK (e.g., openai-python's Responses client)
      directly, sidestepping the framework's own higher-level abstractions.
      The AGW route + governance still fire; what's bypassed is only the
      framework's wrapping. Used today by <code>autogen</code> and
      <code>llamaindex</code> for OpenAI Responses.
    </p>
    <p class="modal-note">
      <strong>TBD (⌛)</strong> means a Plan B adapter scope-decision left
      this combo out, but a follow-up enhancement is filed in
      <code>docs/enhancements.md</code> (the E5 cluster: E5c crewai
      +responses/+conv, E5d pydantic-ai +conv, E5e llamaindex +messages).
      Each is ~1–2 hours of work; see the doc for the implementation
      shape and dependencies.
    </p>`;
}

async function openAboutModal() {
  let modal = document.getElementById("about-modal");
  if (!modal) {
    modal = document.createElement("div");
    modal.id = "about-modal";
    modal.className = "modal";
    modal.innerHTML = `
      <div class="modal-content modal-content-wide">
        <div class="modal-header">
          <span>ℹ About — framework support matrix</span>
          <button id="about-modal-close">✕</button>
        </div>
        <div class="modal-body"></div>
      </div>
    `;
    document.body.appendChild(modal);
    modal.addEventListener("click", e => { if (e.target === modal) modal.classList.add("hidden"); });
    modal.querySelector("#about-modal-close").addEventListener("click", () => modal.classList.add("hidden"));
  }

  // Fetch live adapter status from /info.frameworks (I-NEW-1).
  let infoFrameworks = null;
  try {
    const info = await fetch(`${API_BASE}/info`).then(r => r.json());
    infoFrameworks = info.frameworks || null;
  } catch (e) {
    console.warn("About: /info fetch failed:", e);
  }

  const body = `
    <h3>Library-level native support</h3>
    <p class="modal-note">
      What each underlying framework SDK supports natively (independent of
      aiplay's adapter implementation). Hand-curated, accurate as of
      2026-04-26 — edit <code>frontend/about.js</code> when a library
      gains new first-class support.
    </p>
    ${renderLibraryTable()}

    <h3>Aiplay adapter implementation status</h3>
    <p class="modal-note">
      What aiplay's adapter currently exposes via the harness. Live from
      <code>/info.frameworks</code> (single source of truth with
      <code>harness/validator.py::ADAPTER_CAPABILITIES</code>).
    </p>
    ${infoFrameworks
      ? renderAdapterTable(infoFrameworks)
      : `<p class="error-note">Could not load /info.frameworks — check that the harness is reachable.</p>`}

    ${renderLegend()}
  `;

  modal.querySelector(".modal-body").innerHTML = body;
  modal.classList.remove("hidden");
}

document.getElementById("btn-about").addEventListener("click", openAboutModal);
