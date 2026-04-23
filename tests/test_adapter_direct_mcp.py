"""Tests for adapters/direct-mcp — deterministic routing logic (offline)."""
import sys
from pathlib import Path

_ADAPTER_DIR = str(Path(__file__).resolve().parent.parent / "adapters" / "direct-mcp")
_LANGCHAIN_DIR = str(Path(__file__).resolve().parent.parent / "adapters" / "langchain")


def _ensure_adapter_on_path():
    """Force this adapter's framework_bridge to win against the langchain one.

    Both adapters have a module named `framework_bridge`; the first one
    imported gets cached in sys.modules. Pop the cached copy + put our
    adapter's dir first on sys.path so the import resolves to ours.
    """
    # Remove langchain adapter path to avoid it shadowing
    while _LANGCHAIN_DIR in sys.path:
        sys.path.remove(_LANGCHAIN_DIR)
    # Put our dir first
    while _ADAPTER_DIR in sys.path:
        sys.path.remove(_ADAPTER_DIR)
    sys.path.insert(0, _ADAPTER_DIR)
    # Evict any cached framework_bridge module
    sys.modules.pop("framework_bridge", None)


def test_route_matches_tool_by_name_keyword():
    _ensure_adapter_on_path()
    from framework_bridge import route
    tools = [
        {"name": "get_weather", "description": "weather",
         "inputSchema": {"properties": {"city": {"type": "string"}}}},
        {"name": "get_news", "description": "news",
         "inputSchema": {"properties": {"topic": {"type": "string"}}}},
    ]
    tool_name, args = route("What's the weather in Paris?", tools)
    assert tool_name == "get_weather"
    assert args.get("city") == "Paris"


def test_route_extracts_integer_arg():
    _ensure_adapter_on_path()
    from framework_bridge import route
    tools = [{"name": "get_top_books", "description": "books",
              "inputSchema": {"properties": {"limit": {"type": "integer"}}}}]
    _, args = route("Show top 5 books", tools)
    assert args.get("limit") == 5


def test_route_skips_ib_underscore_fields():
    """cidgar-injected _ib_cid/_ib_gar fields should be excluded from arg extraction."""
    _ensure_adapter_on_path()
    from framework_bridge import route
    tools = [{"name": "get_weather", "description": "weather",
              "inputSchema": {"properties": {"city": {"type": "string"},
                                              "_ib_cid": {"type": "string"},
                                              "_ib_gar": {"type": "object"}}}}]
    _, args = route("Weather in Tokyo", tools)
    assert "_ib_cid" not in args
    assert "_ib_gar" not in args
    assert args.get("city") == "Tokyo"


def test_route_fallback_to_first_tool():
    _ensure_adapter_on_path()
    from framework_bridge import route
    tools = [{"name": "mystery_tool", "description": "", "inputSchema": {}}]
    tool_name, args = route("random query with no matches", tools)
    assert tool_name == "mystery_tool"


def test_pick_mcp_base_url_via_agw(monkeypatch):
    _ensure_adapter_on_path()
    monkeypatch.setenv("AGW_MCP_WEATHER", "http://agentgateway:8080/mcp/weather")
    from framework_bridge import pick_mcp_base_url
    url = pick_mcp_base_url(routing="via_agw", mcp="weather")
    assert "agentgateway" in url and "weather" in url
