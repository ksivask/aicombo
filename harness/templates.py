"""Default turn plans by row config."""
from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

_DEFAULTS_PATH = Path(__file__).with_name("defaults.yaml")


def _load_defaults() -> dict[str, Any]:
    with _DEFAULTS_PATH.open() as f:
        return yaml.safe_load(f)


def _subst(s: str, subs: dict[str, str]) -> str:
    """Substitute {placeholder} tokens using subs. Unknown tokens are left intact."""
    if not s or "{" not in s:
        return s
    out = s
    for k, v in subs.items():
        out = out.replace("{" + k + "}", v)
    return out


def default_turn_plan(row: dict[str, Any]) -> dict[str, Any]:
    """Pick a turn plan template based on the row config."""
    data = _load_defaults()
    templates = data["turn_plan_templates"]

    llm = row.get("llm", "NONE")
    mcp = row.get("mcp", "NONE")

    if llm == "NONE":
        plan = templates["direct_mcp"]
        subs_map = data.get("mcp_query_substitutions", {}) or {}
        subs = subs_map.get(mcp, {}) or {}
        return {
            "turns": [
                ({**t, "content": _subst(t.get("content", ""), subs)}
                 if t.get("kind") == "user_msg" else t)
                for t in plan["turns"]
            ]
        }

    if mcp == "NONE":
        return templates["no_mcp_chat"]

    # Plan B T10 — row requests a compact-between-turns plan. This overrides
    # the per-MCP default because verdict (d) wants deterministic positioning
    # of the compact turn regardless of which MCP is bound.
    if row.get("with_compact") and "with_mcp_with_compact" in templates:
        return templates["with_mcp_with_compact"]

    # Active MCP — pick per-MCP template
    key = f"with_mcp_{mcp}"
    if key in templates:
        return templates[key]

    # Fallback — generic mcp query template (shouldn't reach in Plan A)
    return templates.get("with_mcp_weather", templates["no_mcp_chat"])
