"""Tests for harness/validator.py — validate row configs."""
from validator import validate


def test_chat_completion_forces_state_false():
    """API=chat → state column must be False and disabled."""
    result = validate({
        "framework": "langchain", "api": "chat",
        "stream": False, "state": False,
        "llm": "ollama", "mcp": "NONE", "routing": "via_agw",
    })
    assert "state" in result["disabled_cells"]
    assert result["forced_values"]["state"] is False
    assert result["runnable"] is True


def test_responses_conv_forces_state_true():
    """API=responses+conv → state forced to True."""
    result = validate({
        "framework": "langchain", "api": "responses+conv",
        "stream": False, "state": True,
        "llm": "chatgpt", "mcp": "NONE", "routing": "via_agw",
    })
    assert "state" in result["disabled_cells"]
    assert result["forced_values"]["state"] is True


def test_messages_api_forces_state_false():
    """API=messages → state forced to False."""
    result = validate({
        "framework": "langchain", "api": "messages",
        "stream": False, "state": False,
        "llm": "claude", "mcp": "NONE", "routing": "via_agw",
    })
    assert "state" in result["disabled_cells"]
    assert result["forced_values"]["state"] is False


def test_none_llm_and_none_mcp_is_not_runnable():
    """No LLM + no MCP → row is not runnable."""
    result = validate({
        "framework": "NONE", "api": "NONE",
        "stream": False, "state": False,
        "llm": "NONE", "mcp": "NONE", "routing": "via_agw",
    })
    assert result["runnable"] is False


def test_none_llm_disables_api_stream_state():
    """LLM=NONE → api/stream/state all disabled."""
    result = validate({
        "framework": "NONE", "api": "NONE",
        "stream": False, "state": False,
        "llm": "NONE", "mcp": "weather", "routing": "via_agw",
    })
    for cell in ("api", "stream", "state", "provider"):
        assert cell in result["disabled_cells"]


def test_responses_state_combo_state_editable_but_unrunnable_when_no_adapter():
    """api=responses + state=T + llm=chatgpt: state cell is editable (api+llm
    support state), but the row is unrunnable when the framework has no
    adapter that implements the Responses API.

    As of Plan B T6, ALL five planned framework adapters are built
    (langchain, langgraph, crewai, pydantic-ai, autogen, llamaindex), so
    this test uses a synthetic unknown framework to exercise the same
    'no ADAPTER_CAPABILITIES entry → not runnable' branch of the validator.
    """
    result = validate({
        "framework": "unknown_fw", "api": "responses",
        "stream": False, "state": True,
        "llm": "chatgpt", "mcp": "NONE", "routing": "via_agw",
    })
    # State remains editable (api/llm rule does not disable it)
    assert "state" not in result["disabled_cells"]
    assert "state" not in result["forced_values"]
    # But the row IS unrunnable: unknown_fw has no registered adapter.
    assert result["runnable"] is False
    assert any("adapter" in w.lower() for w in result["warnings"])


def test_autogen_responses_state_is_runnable():
    """autogen (Plan B T5) supports api=responses with state=T via the
    openai SDK bypass (responses.create + previous_response_id chain).
    This test locks that capability in the validator."""
    result = validate({
        "framework": "autogen", "api": "responses",
        "stream": False, "state": True,
        "llm": "chatgpt", "mcp": "NONE", "routing": "via_agw",
    })
    assert result["runnable"] is True
    # State is NOT disabled — chatgpt+responses supports state.
    assert "state" not in result["disabled_cells"]


def test_state_disabled_when_llm_does_not_support_responses_state():
    """api=responses + llm=ollama → state is disabled+forced F + warning issued.
    Only chatgpt implements Responses API state in v1."""
    result = validate({
        "framework": "langchain", "api": "responses",
        "stream": False, "state": True,
        "llm": "ollama", "mcp": "NONE", "routing": "via_agw",
    })
    assert "state" in result["disabled_cells"]
    assert result["forced_values"]["state"] is False
    assert any("ollama" in w for w in result["warnings"])


def test_state_enabled_when_llm_supports_responses_state():
    """api=responses + llm=chatgpt → state remains editable."""
    result = validate({
        "framework": "autogen", "api": "responses",
        "stream": False, "state": True,
        "llm": "chatgpt", "mcp": "NONE", "routing": "via_agw",
    })
    assert "state" not in result["disabled_cells"]
    assert "state" not in result["forced_values"]


def test_api_llm_mismatch_marks_unrunnable():
    """api=responses + llm=ollama is not runnable (ollama doesn't have responses)."""
    result = validate({
        "framework": "langchain", "api": "responses",
        "stream": False, "state": False,
        "llm": "ollama", "mcp": "NONE", "routing": "via_agw",
    })
    assert result["runnable"] is False


def test_missing_provider_key_disables_option():
    """If env shows chatgpt key missing, chatgpt option is in disabled_dropdown_options."""
    available_keys = {"openai": False, "anthropic": True, "google": True}
    result = validate({
        "framework": "langchain", "api": "chat",
        "stream": False, "state": False,
        "llm": "chatgpt", "mcp": "NONE", "routing": "via_agw",
    }, available_keys=available_keys)
    llm_disabled = {o["id"] for o in result.get("disabled_dropdown_options", {}).get("llm", [])}
    assert "chatgpt" in llm_disabled


# ── E19 — multi-MCP per-row schema ──

def test_validator_accepts_str_mcp_legacy_form():
    """Backwards compat: single-string `mcp` continues to validate as
    runnable for the existing single-MCP path."""
    result = validate({
        "framework": "langchain", "api": "chat",
        "stream": False, "state": False,
        "llm": "ollama", "mcp": "weather", "routing": "via_agw",
    })
    assert result["runnable"] is True
    assert not any("multi-MCP" in w for w in result["warnings"])


def test_validator_rejects_list_mcp_for_non_multi_mcp_framework():
    """E19: list-form `mcp` is not runnable until a framework opts into
    MULTI_MCP_FRAMEWORKS (intentionally empty in the first cut)."""
    result = validate({
        "framework": "langchain", "api": "chat",
        "stream": False, "state": False,
        "llm": "ollama", "mcp": ["weather", "fetch"], "routing": "via_agw",
    })
    assert result["runnable"] is False
    assert any("multi-MCP" in w for w in result["warnings"])


def test_validator_warns_on_none_in_mcp_list():
    """A literal 'NONE' inside a multi-MCP list is a user mistake — warn so
    it surfaces in the row tooltip instead of silently passing through."""
    result = validate({
        "framework": "langchain", "api": "chat",
        "stream": False, "state": False,
        "llm": "ollama", "mcp": ["weather", "NONE"], "routing": "via_agw",
    })
    assert any("'NONE'" in w for w in result["warnings"])


# ── E23 — multi-LLM per-row schema ──

def test_validator_accepts_str_llm_legacy_form():
    """Backwards compat: single-string `llm` continues to validate cleanly
    on the existing single-LLM path; no E23 warnings emitted."""
    result = validate({
        "framework": "langchain", "api": "chat",
        "stream": False, "state": False,
        "llm": "ollama", "mcp": "NONE", "routing": "via_agw",
    })
    assert result["runnable"] is True
    assert not any("multi-LLM" in w for w in result["warnings"])


def test_validator_accepts_list_llm_for_combo_framework():
    """combo is the sole opt-in framework today (E24). list-form `llm`
    with API-compatible providers is runnable."""
    result = validate({
        "framework": "combo", "api": "chat",
        "stream": False, "state": False,
        "llm": ["ollama", "chatgpt"], "mcp": "NONE", "routing": "via_agw",
    })
    # combo isn't in ADAPTER_CAPABILITIES so the api-capability rule still
    # marks unrunnable for that reason — but no E23-multi-LLM warning fires.
    assert not any("multi-LLM" in w for w in result["warnings"])


def test_validator_rejects_list_llm_for_non_multi_llm_framework():
    """E23: list-form `llm` on a framework without combo capability is
    explicitly non-runnable with a multi-LLM warning."""
    result = validate({
        "framework": "langchain", "api": "chat",
        "stream": False, "state": False,
        "llm": ["ollama", "chatgpt"], "mcp": "NONE", "routing": "via_agw",
    })
    assert result["runnable"] is False
    assert any("multi-LLM" in w for w in result["warnings"])


def test_validator_rejects_api_incompatible_llm_in_list():
    """Each LLM in the list must be in API_TO_PROVIDERS for the row's api.
    chat doesn't include claude — flagging it keeps the combo adapter from
    silently dispatching to an incompatible provider."""
    result = validate({
        "framework": "combo", "api": "chat",
        "stream": False, "state": False,
        "llm": ["ollama", "claude"], "mcp": "NONE", "routing": "via_agw",
    })
    assert result["runnable"] is False
    assert any("claude" in w and "compatible" in w for w in result["warnings"])


# ── E20 verification — mcp=mutable gate ──

def test_with_e20_verification_requires_mutable_mcp():
    """E20: with_e20_verification=true on a non-mutable MCP is non-runnable.
    The template's mcp_admin turn calls /_admin/set_tools which only exists
    on adapter-mutable; routing through e.g. weather would 404 mid-trial."""
    result = validate({
        "framework": "langchain", "api": "chat",
        "stream": False, "state": False,
        "llm": "ollama", "mcp": "weather", "routing": "via_agw",
        "with_e20_verification": True,
    })
    assert result["runnable"] is False
    assert any("with_e20_verification" in w and "mutable" in w
               for w in result["warnings"])


def test_with_e20_verification_passes_with_mutable_mcp():
    """E20: with_e20_verification=true + mcp=mutable validates as runnable
    so the row can actually run the close-the-loop verification trial."""
    result = validate({
        "framework": "langchain", "api": "chat",
        "stream": False, "state": False,
        "llm": "ollama", "mcp": "mutable", "routing": "via_agw",
        "with_e20_verification": True,
    })
    assert result["runnable"] is True
    assert not any("with_e20_verification" in w for w in result["warnings"])


def test_validator_rejects_model_list_length_mismatch():
    """If `model` is also a list, it must align 1:1 with `llm` so the combo
    adapter can pick model[i] for llm[i]."""
    result = validate({
        "framework": "combo", "api": "chat",
        "stream": False, "state": False,
        "llm": ["ollama", "chatgpt"],
        "model": ["llama3.2"],  # length 1 vs llm length 2
        "mcp": "NONE", "routing": "via_agw",
    })
    assert result["runnable"] is False
    assert any("model list length" in w for w in result["warnings"])
