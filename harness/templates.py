"""Default turn plans by row config."""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import yaml

_DEFAULTS_PATH = Path(__file__).with_name("defaults.yaml")
_SETTINGS_PATH = Path(os.environ.get("DATA_DIR", "/data")) / "settings.json"

# Generic continuation prompts used to pad a 3-turn template up to the
# user-configured default_turn_count (when > 3). Picked round-robin
# starting at index 0 so a template padded from 3→5 always gets the
# first 2 of these. Kept content-neutral so they make sense regardless
# of which MCP / topic the trial is exercising.
_CONTINUATION_PROMPTS = [
    "Tell me more.",
    "Anything else worth noting?",
    "Can you summarize the key points so far?",
    "What's a related follow-up question I should ask?",
]


def _load_defaults() -> dict[str, Any]:
    with _DEFAULTS_PATH.open() as f:
        return yaml.safe_load(f)


def get_default_turn_count() -> int:
    """Read the user-configured default turn count.

    Persisted at $DATA_DIR/settings.json under key "default_turn_count".
    Falls back to 3 if the file is missing or unreadable. Clamped to
    [1, 20] to prevent runaway plans.
    """
    try:
        with _SETTINGS_PATH.open() as f:
            v = int(json.load(f).get("default_turn_count", 3))
            return max(1, min(20, v))
    except (FileNotFoundError, ValueError, OSError, json.JSONDecodeError):
        return 3


def set_default_turn_count(n: int) -> int:
    """Persist a new default_turn_count. Returns the value actually saved
    (clamped). Reads-modifies-writes settings.json so other settings
    survive."""
    n = max(1, min(20, int(n)))
    try:
        with _SETTINGS_PATH.open() as f:
            cur = json.load(f)
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        cur = {}
    cur["default_turn_count"] = n
    _SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    with _SETTINGS_PATH.open("w") as f:
        json.dump(cur, f, indent=2)
    return n


def _resize_turns(turns: list[dict[str, Any]], target: int) -> list[dict[str, Any]]:
    """Pad or truncate a turn list to the target count.

    Truncate: keep the first N turns (preserves the opening tool-discovery
    + first real query, drops trailing follow-ups).
    Pad: append generic continuation prompts from _CONTINUATION_PROMPTS,
    cycling if the target exceeds template + pool size.
    """
    if len(turns) >= target:
        return turns[:target]
    needed = target - len(turns)
    extras = []
    for i in range(needed):
        extras.append({
            "kind": "user_msg",
            "content": _CONTINUATION_PROMPTS[i % len(_CONTINUATION_PROMPTS)],
        })
    return turns + extras


def _subst(s: str, subs: dict[str, str]) -> str:
    """Substitute {placeholder} tokens using subs. Unknown tokens are left intact."""
    if not s or "{" not in s:
        return s
    out = s
    for k, v in subs.items():
        out = out.replace("{" + k + "}", v)
    return out


def default_turn_plan(row: dict[str, Any]) -> dict[str, Any]:
    """Pick a turn plan template based on the row config.

    All returned plans are pad/truncated to `get_default_turn_count()`
    (Settings modal value). Plans with fixed semantics (compact /
    force_state_ref) are NOT resized — those need exactly the turns
    they declare for the verdict to make sense.
    """
    data = _load_defaults()
    templates = data["turn_plan_templates"]
    target = get_default_turn_count()

    llm = row.get("llm", "NONE")
    mcp = row.get("mcp", "NONE")

    if llm == "NONE":
        plan = templates["direct_mcp"]
        subs_map = data.get("mcp_query_substitutions", {}) or {}
        subs = subs_map.get(mcp, {}) or {}
        substituted = [
            ({**t, "content": _subst(t.get("content", ""), subs)}
             if t.get("kind") == "user_msg" else t)
            for t in plan["turns"]
        ]
        return {"turns": _resize_turns(substituted, target)}

    # Plan B T11 — row requests a force_state_ref plan. This overrides BOTH
    # the per-MCP default and the no-MCP default because verdict (e)'s design
    # doesn't depend on MCP presence — only on Responses-API state-mode
    # chaining. Check for the template BEFORE the mcp=NONE fast-path below
    # so this plan is selected even for MCP=NONE autogen rows.
    # NOT resized — verdict (e) needs the exact turn shape this template
    # provides (specific force_state_ref turn at a known position).
    if (row.get("with_force_state_ref")
            and "with_responses_state_force_ref" in templates):
        return templates["with_responses_state_force_ref"]

    if mcp == "NONE":
        return {"turns": _resize_turns(templates["no_mcp_chat"]["turns"], target)}

    # Plan B T10 — row requests a compact-between-turns plan. This overrides
    # the per-MCP default because verdict (d) wants deterministic positioning
    # of the compact turn regardless of which MCP is bound.
    # NOT resized — verdict (d) needs the compact turn at its known position.
    if row.get("with_compact") and "with_mcp_with_compact" in templates:
        return templates["with_mcp_with_compact"]

    # Active MCP — pick per-MCP template
    key = f"with_mcp_{mcp}"
    plan = templates.get(key) or templates.get("with_mcp_weather", templates["no_mcp_chat"])
    return {"turns": _resize_turns(plan["turns"], target)}
