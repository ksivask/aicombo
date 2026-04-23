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
