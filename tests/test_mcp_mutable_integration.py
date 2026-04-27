"""E22 integration — confirm /_admin/* endpoints are reachable from harness.

Skip-marked unless the docker stack is up. Validates the load-bearing
claim that `mcp_admin` turns can ACTUALLY mutate the upstream MCP.

Why this exists separately from `test_mcp_mutable.py`:
- That file unit-tests the Starlette app directly via ASGITransport — no
  network in the loop. It proves the admin endpoints work on their own.
- This file proves the harness-direct path works: harness container →
  mcp-mutable container's /_admin/* surface over the docker-compose
  default network. Admin endpoints DO NOT go through AGW (test-harness
  concern; AGW is intentionally unaware).

How to run:
    docker compose up -d mcp-mutable
    MCP_INTEGRATION_TEST=1 \\
      MCP_DIRECT_BASE=http://localhost:8000 \\
      pytest tests/test_mcp_mutable_integration.py -v
"""
from __future__ import annotations

import os

import httpx
import pytest


MCP_DIRECT_BASE = os.environ.get("MCP_DIRECT_BASE", "http://mcp-mutable:8000")

MCP_AVAILABLE = pytest.mark.skipif(
    not os.environ.get("MCP_INTEGRATION_TEST"),
    reason="set MCP_INTEGRATION_TEST=1 + docker stack up to run",
)


@MCP_AVAILABLE
def test_admin_state_direct_returns_200():
    """E22 #1 verification: GET /_admin/state directly on mcp-mutable.

    Confirms the harness can reach the admin surface without AGW in the
    path.
    """
    r = httpx.get(f"{MCP_DIRECT_BASE}/_admin/state", timeout=5.0)
    assert r.status_code == 200, (
        f"expected 200, got {r.status_code}: {r.text[:200]}"
    )
    body = r.json()
    assert "tools" in body
    assert "version_counter" in body


@MCP_AVAILABLE
def test_admin_set_tools_then_state_reflects_direct():
    """E22 mutation: POST set_tools directly, GET state confirms.

    End-to-end proves the harness `mcp_admin` turn kind can drive
    mutations the way E20's snapshot-correlation tests need.
    """
    # Reset first so the test is independent of prior state.
    httpx.post(
        f"{MCP_DIRECT_BASE}/_admin/reset", json={}, timeout=5.0,
    )
    new_tools = [{
        "name": "test_only_tool",
        "description": "x",
        "inputSchema": {"type": "object", "properties": {}},
    }]
    r = httpx.post(
        f"{MCP_DIRECT_BASE}/_admin/set_tools",
        json={"tools": new_tools},
        timeout=5.0,
    )
    assert r.status_code == 200, (
        f"expected 200, got {r.status_code}: {r.text[:200]}"
    )
    state = httpx.get(
        f"{MCP_DIRECT_BASE}/_admin/state", timeout=5.0,
    ).json()
    names = [t["name"] for t in state["tools"]]
    assert names == ["test_only_tool"]
    assert state["version_counter"] >= 1
    # Reset to avoid polluting subsequent runs.
    httpx.post(
        f"{MCP_DIRECT_BASE}/_admin/reset", json={}, timeout=5.0,
    )
