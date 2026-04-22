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
    """LLM=NONE + MCP=weather → direct tool invocation plan."""
    plan = default_turn_plan({
        "framework": "NONE", "api": "NONE",
        "stream": False, "state": False,
        "llm": "NONE", "mcp": "weather", "routing": "via_agw",
    })
    # For Plan A: single tools/list + tools/call
    assert len(plan["turns"]) == 2
    assert plan["turns"][0]["kind"] == "direct_mcp_tools_list"
    assert plan["turns"][1]["kind"] == "direct_mcp_tools_call"
