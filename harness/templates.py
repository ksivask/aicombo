"""Default turn plans by row config."""
from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

_DEFAULTS_PATH = Path(__file__).with_name("defaults.yaml")


def _load_defaults() -> dict[str, Any]:
    with _DEFAULTS_PATH.open() as f:
        return yaml.safe_load(f)


def default_turn_plan(row: dict[str, Any]) -> dict[str, Any]:
    """Pick a turn plan template based on the row config."""
    data = _load_defaults()
    templates = data["turn_plan_templates"]

    llm = row.get("llm", "NONE")
    mcp = row.get("mcp", "NONE")

    if llm == "NONE":
        return templates["direct_mcp"]

    if mcp == "NONE":
        return templates["no_mcp_chat"]

    # Active MCP — pick per-MCP template
    key = f"with_mcp_{mcp}"
    if key in templates:
        return templates[key]

    # Fallback — generic mcp query template (shouldn't reach in Plan A)
    return templates.get("with_mcp_weather", templates["no_mcp_chat"])
