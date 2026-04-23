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


def test_invalid_combo_api_responses_stream_off_state_on_is_valid():
    """Responses with state but no stream is valid."""
    result = validate({
        "framework": "autogen", "api": "responses",
        "stream": False, "state": True,
        "llm": "chatgpt", "mcp": "NONE", "routing": "via_agw",
    })
    assert result["runnable"] is True
    # State is allowed on responses, not forced
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
