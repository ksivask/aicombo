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
    "responses+conv":  "yes",
    mcp:               "yes",
    streaming:         "yes",
    tool_calling:      "yes",
  },
  langgraph: {
    // Sits on langchain — inherits all langchain capabilities
    chat:              "yes",
    messages:          "yes",
    responses:         "yes",
    "responses+conv":  "yes",
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
    messages:          "no",   // no first-class Anthropic Messages-shape
    responses:         "yes",  // OpenAIResponses
    "responses+conv":  "yes",  // Conversations API
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
  via:    {glyph: "⚠", className: "cell-via",    title: "via wrapper / litellm / community plugin"},
  bypass: {glyph: "⊘", className: "cell-bypass", title: "bypass — aiplay adapter calls the API directly via SDK, sidestepping the framework's own abstractions (still exercises the AGW route + governance)"},
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
// Note: bypass-capable but NOT-in-supported_apis combos (crewai +conv,
// pydantic-ai +conv) are documented in validator.py comments but the
// adapter chose not to wire them up. They surface as ✗ here, not ⊘.
const ADAPTER_BYPASS_APIS = {
  autogen:    new Set(["responses", "responses+conv"]),
  llamaindex: new Set(["responses", "responses+conv"]),
};

function renderCell(value) {
  const c = CELL_GLYPH[value] || {glyph: "?", className: "cell-na", title: "unknown"};
  return `<td class="${c.className}" title="${c.title}">${c.glyph}</td>`;
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
    return `<tr><td class="fw-name">${fw}</td>${apiCols.map(c => {
      // direct-mcp has no LLM APIs; show "—" not "✗"
      if (fw === "direct-mcp") return renderCell("na");
      if (!supported.has(c.key)) return renderCell("no");
      // Supported AND in bypass set → ⊘; otherwise → native ✓
      return renderCell(bypassSet.has(c.key) ? "bypass" : "yes");
    }).join("")}</tr>`;
  }).join("");
  return `<table class="support-matrix"><thead>${head}</thead><tbody>${rows}</tbody></table>`;
}

function renderLegend() {
  return `
    <div class="support-legend">
      <span><span class="cell-yes">✓</span> native</span>
      <span><span class="cell-via">⚠</span> via wrapper / litellm / community plugin</span>
      <span><span class="cell-bypass">⊘</span> bypass (adapter calls SDK directly, sidesteps framework)</span>
      <span><span class="cell-no">✗</span> no support / not implemented</span>
      <span><span class="cell-na">—</span> N/A</span>
    </div>
    <p class="modal-note" style="margin-top:6px;">
      <strong>Bypass</strong> means aiplay's adapter exposes the API by
      calling the underlying SDK (e.g., openai-python's Responses client)
      directly, sidestepping the framework's own higher-level abstractions.
      The AGW route + governance still fire; what's bypassed is only the
      framework's wrapping. Used today by <code>autogen</code> and
      <code>llamaindex</code> for OpenAI Responses (per
      <code>validator.py</code> comments). <code>crewai</code> and
      <code>pydantic-ai</code> have bypass <em>documented as possible</em>
      for <code>+conv</code> but not actually wired in — those cells
      show ✗.
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
