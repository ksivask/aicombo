"""Tests for harness/templates.py — default turn plans by config."""
from templates import default_turn_plan


def test_chat_no_mcp_returns_three_text_turns():
    """No MCP → 3 text turns with default joke content."""
    plan = default_turn_plan({
        "framework": "langchain", "api": "chat",
        "stream": False, "state": False,
        "llm": "ollama", "mcp": "NONE", "routing": "via_agw",
    })
    assert len(plan["turns"]) == 3
    for t in plan["turns"]:
        assert t["kind"] == "user_msg"
        assert isinstance(t["content"], str) and len(t["content"]) > 0


def test_chat_with_weather_mcp_includes_weather_queries():
    """MCP=weather → turns reference weather."""
    plan = default_turn_plan({
        "framework": "langchain", "api": "chat",
        "stream": False, "state": False,
        "llm": "ollama", "mcp": "weather", "routing": "via_agw",
    })
    assert len(plan["turns"]) >= 3
    contents = " ".join(t["content"] for t in plan["turns"])
    assert "weather" in contents.lower() or "paris" in contents.lower()


def test_with_force_state_ref_row_selects_force_ref_template():
    """Plan B T11 — with_force_state_ref=true selects the
    `with_responses_state_force_ref` template regardless of MCP setting.
    Plan has a user_msg → user_msg → force_state_ref → user_msg shape.
    """
    plan = default_turn_plan({
        "framework": "autogen", "api": "responses",
        "stream": False, "state": True,
        "llm": "chatgpt", "mcp": "NONE", "routing": "via_agw",
        "with_force_state_ref": True,
    })
    kinds = [t.get("kind") for t in plan["turns"]]
    assert "force_state_ref" in kinds, kinds
    # The force_state_ref turn spec should carry a lookback hint.
    fsr = next(t for t in plan["turns"] if t.get("kind") == "force_state_ref")
    assert fsr.get("lookback") == 2, fsr
    # Should also have enough user_msg turns before and after for the
    # verdict-e window correlation to land.
    assert kinds.count("user_msg") >= 2


def test_with_reset_row_selects_with_reset_template():
    """E21 — with_reset=true selects the bracket-test plan with a
    reset_context turn between two distinct conversations."""
    plan = default_turn_plan({
        "framework": "langchain", "api": "chat",
        "stream": False, "state": False,
        "llm": "ollama", "mcp": "NONE", "routing": "via_agw",
        "with_reset": True,
    })
    kinds = [t.get("kind") for t in plan["turns"]]
    # 5 turns: user_msg / user_msg / reset_context / user_msg / user_msg
    assert kinds == [
        "user_msg", "user_msg", "reset_context",
        "user_msg", "user_msg",
    ], kinds
    # Distinct topics across the boundary so a CID leak would actually
    # surface as semantic discontinuity in the conversation log too.
    pre = " ".join(
        t.get("content", "") for t in plan["turns"][:2]
    ).lower()
    post = " ".join(
        t.get("content", "") for t in plan["turns"][3:]
    ).lower()
    assert pre != post


def test_with_reset_overrides_mcp_default_template():
    """with_reset wins over the per-MCP template — the bracket-test
    semantics are MCP-independent so picking reset takes priority."""
    plan = default_turn_plan({
        "framework": "langchain", "api": "chat",
        "stream": False, "state": False,
        "llm": "ollama", "mcp": "weather", "routing": "via_agw",
        "with_reset": True,
    })
    kinds = [t.get("kind") for t in plan["turns"]]
    assert "reset_context" in kinds, kinds


def test_with_e20_verification_template_selected_when_flag_set():
    """E20 — with_e20_verification=true selects the close-the-loop trial
    template that produces TWO distinct tools/list snapshots in one trial.

    Pinned shape: user_msg → user_msg → mcp_admin → refresh_tools → user_msg.
    The mcp_admin turn mutates the upstream MCP between the two
    user_msg-driven discoveries so verdict (i) tools_list_correlation has
    a real signal (H1 != H2) to measure.
    """
    plan = default_turn_plan({
        "framework": "langchain", "api": "chat",
        "stream": False, "state": False,
        "llm": "ollama", "mcp": "mutable", "routing": "via_agw",
        "with_e20_verification": True,
    })
    kinds = [t.get("kind") for t in plan["turns"]]
    assert kinds == [
        "user_msg", "user_msg", "mcp_admin",
        "refresh_tools", "user_msg",
    ], kinds
    # The mcp_admin turn must target the mutable MCP and carry an op +
    # payload — the runner-side dispatch dies without these.
    admin = next(t for t in plan["turns"] if t.get("kind") == "mcp_admin")
    assert admin.get("mcp") == "mutable"
    assert admin.get("op") == "set_tools"
    assert isinstance(admin.get("payload", {}).get("tools"), list)


def test_none_llm_with_mcp_produces_direct_mcp_plan():
    """LLM=NONE + MCP=weather → user_msg turns driven by the direct-mcp adapter.

    The direct-mcp adapter uses deterministic keyword routing over the
    tools/list result, so it handles normal user_msg turns end to end.
    """
    plan = default_turn_plan({
        "framework": "NONE", "api": "NONE",
        "stream": False, "state": False,
        "llm": "NONE", "mcp": "weather", "routing": "via_agw",
    })
    assert len(plan["turns"]) == 2
    for t in plan["turns"]:
        assert t["kind"] == "user_msg"
        assert isinstance(t["content"], str) and len(t["content"]) > 0
    contents = " ".join(t["content"] for t in plan["turns"]).lower()
    # Substitution must fire — tokens should not be left raw
    assert "{mcp_specific_query}" not in contents
    assert "{mcp_followup_query}" not in contents
    # Weather queries land as Paris/London
    assert "paris" in contents or "london" in contents or "weather" in contents
