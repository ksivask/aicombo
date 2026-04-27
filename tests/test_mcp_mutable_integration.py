"""E22 integration — confirm /_admin/* endpoints traverse AGW correctly.

Skip-marked unless docker stack is up. Validates the load-bearing claim
that `mcp_admin` turns can ACTUALLY mutate the upstream MCP via AGW.

Why this exists separately from `test_mcp_mutable.py`:
- That file unit-tests the Starlette app directly via ASGITransport — no
  AGW in the loop. It proves the admin endpoints work on their own.
- This file proves the WHOLE PATH works: harness → AGW route
  `mcp-mutable-admin` (host: passthrough) → mcp-mutable container.
- Catches regressions where someone reverts the route's `host:` backend
  to `mcp:` (which AGW gates with JSON-RPC + Accept-header checks that
  the plain REST admin POSTs don't satisfy — see agw/config.yaml comment).

How to run:
    docker compose up -d agentgateway mcp-mutable
    AGW_INTEGRATION_TEST=1 \\
      AGW_INTEGRATION_BASE=http://localhost:8080 \\
      pytest tests/test_mcp_mutable_integration.py -v
"""
from __future__ import annotations

import os

import httpx
import pytest


AGW_BASE = os.environ.get("AGW_INTEGRATION_BASE", "http://agentgateway:8080")

AGW_AVAILABLE = pytest.mark.skipif(
    not os.environ.get("AGW_INTEGRATION_TEST"),
    reason="set AGW_INTEGRATION_TEST=1 + docker stack up to run",
)


@AGW_AVAILABLE
def test_admin_state_via_agw_returns_200():
    """E22 #1 verification: GET /mcp/mutable/_admin/state through AGW.

    Confirms the AGW route lands on a backend that byte-passes plain
    REST. With an `mcp:` backend this returns 4xx; with `host:` it
    returns the JSON state object.
    """
    r = httpx.get(f"{AGW_BASE}/mcp/mutable/_admin/state", timeout=5.0)
    assert r.status_code == 200, (
        f"expected 200, got {r.status_code}: {r.text[:200]}"
    )
    body = r.json()
    assert "tools" in body
    assert "version_counter" in body


@AGW_AVAILABLE
def test_admin_set_tools_then_state_reflects_via_agw():
    """E22 mutation: POST set_tools through AGW, GET state confirms.

    End-to-end proves the harness `mcp_admin` turn kind can drive
    mutations the way E20's snapshot-correlation tests need.
    """
    # Reset first so the test is independent of prior state.
    httpx.post(
        f"{AGW_BASE}/mcp/mutable/_admin/reset", json={}, timeout=5.0,
    )
    new_tools = [{
        "name": "test_only_tool",
        "description": "x",
        "inputSchema": {"type": "object", "properties": {}},
    }]
    r = httpx.post(
        f"{AGW_BASE}/mcp/mutable/_admin/set_tools",
        json={"tools": new_tools},
        timeout=5.0,
    )
    assert r.status_code == 200, (
        f"expected 200, got {r.status_code}: {r.text[:200]}"
    )
    state = httpx.get(
        f"{AGW_BASE}/mcp/mutable/_admin/state", timeout=5.0,
    ).json()
    names = [t["name"] for t in state["tools"]]
    assert names == ["test_only_tool"]
    assert state["version_counter"] >= 1
    # Reset to avoid polluting subsequent runs.
    httpx.post(
        f"{AGW_BASE}/mcp/mutable/_admin/reset", json={}, timeout=5.0,
    )
